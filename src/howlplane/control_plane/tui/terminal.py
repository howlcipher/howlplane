"""Terminal ownership for `howlplane tui`: raw keys, resize, painting, restore.

HowlFrame has no terminal access (TUI-00), so the host owns it. Two
implementations share one small interface:

* `RawTerminal` for an interactive TTY: cbreak input without echo or signal
  keys, a line editor, an alternate screen, SIGWINCH redraws, and a terminal
  restore on every exit path;
* `LineTerminal` for pipes, scripts and `TERM=dumb`: one line per input and a
  plain repaint, so the same session is scriptable and testable.

Colors come only from `presentation/style.py` (ADR 0007); stripping them
leaves the same words.
"""

from __future__ import annotations

import codecs
import os
import select
import shutil
import signal
import sys
from dataclasses import dataclass
from typing import List, Optional, TextIO

from howlplane.control_plane.presentation.style import Style, resolve_style

ESC = "\x1b"


@dataclass(frozen=True)
class InputEvent:
    kind: str  # "line", "interrupt", "eof" or "redraw" (resize or line edit)
    text: str = ""


def _kind_for(line: str) -> Optional[str]:
    if line.startswith("FAILED"):
        return "error"
    if line.startswith("! "):
        return "warn"
    if line.startswith(("Outcome:", "Verification")):
        return "ok"
    if line.startswith(("Next:", "Command:")):
        return "command"
    return None


def wrap_line(line: str, width: int) -> List[str]:
    """Hard-wraps one view line to ``width`` columns; continuation lines are indented."""
    if len(line) <= width:
        return [line]
    pieces, rest, indent = [line[:width]], line[width:], "  "
    while rest:
        room = max(1, width - len(indent))
        pieces.append(indent + rest[:room])
        rest = rest[room:]
    return pieces


def styled(style: Style, line: str) -> str:
    kind = _kind_for(line)
    return style.paint(kind, line) if kind else line


class Terminal:
    """Shared interface; subclasses implement input and painting."""

    style: Style

    def __enter__(self) -> "Terminal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def read(self, timeout: Optional[float]) -> Optional[InputEvent]:
        raise NotImplementedError

    def paint(self, header: str, view: List[str], prompt: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class LineTerminal(Terminal):
    """Line mode: no raw input; repaints only when the view changes."""

    def __init__(self, stdin: TextIO = None, stdout: TextIO = None, style: Optional[Style] = None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.style = style or resolve_style(self.stdout)
        self._last: Optional[List[str]] = None

    def read(self, timeout: Optional[float]) -> Optional[InputEvent]:
        try:
            ready, _, _ = select.select([self.stdin], [], [], timeout)
        except (ValueError, OSError, TypeError):
            ready = [self.stdin]  # not selectable (for example a StringIO): read directly
        if not ready:
            return None
        line = self.stdin.readline()
        if line == "":
            return InputEvent("eof")
        return InputEvent("line", line.rstrip("\r\n"))

    def paint(self, header: str, view: List[str], prompt: str) -> None:
        if view != self._last:
            self.stdout.write("\n".join([header] + [styled(self.style, line) for line in view]) + "\n")
            self._last = list(view)
        self.stdout.write(prompt)
        self.stdout.flush()


class RawTerminal(Terminal):
    """Interactive TTY: cbreak input, alternate screen, resize, guaranteed restore."""

    def __init__(self, fd_in: int = None, stdout: TextIO = None, style: Optional[Style] = None):
        import termios  # POSIX only; LineTerminal covers everything else
        self._termios = termios
        self.fd = sys.stdin.fileno() if fd_in is None else fd_in
        self.stdout = stdout if stdout is not None else sys.stdout
        self.style = style or resolve_style(self.stdout)
        self.buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending: List[str] = []
        self._resized = False
        self._saved = termios.tcgetattr(self.fd)
        self._old_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        self._old_term = signal.signal(signal.SIGTERM, self._on_term)
        self._old_hup = signal.signal(signal.SIGHUP, self._on_term)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
        self._closed = False
        self.stdout.write(f"{ESC}[?1049h")
        self.stdout.flush()

    def _on_winch(self, signum, frame) -> None:
        self._resized = True

    def _on_term(self, signum, frame) -> None:
        self.close()
        raise SystemExit(128 + signum)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self._saved)
        finally:
            self.stdout.write(f"{ESC}[?1049l")
            self.stdout.flush()
            signal.signal(signal.SIGWINCH, self._old_winch)
            signal.signal(signal.SIGTERM, self._old_term)
            signal.signal(signal.SIGHUP, self._old_hup)

    def read(self, timeout: Optional[float]) -> Optional[InputEvent]:
        while True:
            if self._resized:
                self._resized = False
                return InputEvent("redraw")
            if self._pending:
                event = self._key(self._pending.pop(0))
                if event:
                    return event
                continue
            try:
                ready, _, _ = select.select([self.fd], [], [], timeout)
            except InterruptedError:
                continue  # SIGWINCH interrupted the wait
            if not ready:
                return None
            data = os.read(self.fd, 1024)
            if not data:
                return InputEvent("eof")
            self._pending.extend(self._decoder.decode(data))

    def _key(self, ch: str) -> Optional[InputEvent]:
        if ch in ("\r", "\n"):
            line, self.buffer = self.buffer, ""
            return InputEvent("line", line)
        if ch == "\x03":
            self.buffer = ""
            return InputEvent("interrupt")
        if ch == "\x04":
            return InputEvent("eof") if not self.buffer else None
        if ch in ("\x7f", "\x08"):
            self.buffer = self.buffer[:-1]
            return InputEvent("redraw")  # repaint the edited line
        if ch == "\x15":  # Ctrl-U clears the line
            self.buffer = ""
            return InputEvent("redraw")
        if ch == ESC:
            self._drop_escape_sequence()
            return None
        if ch.isprintable():
            self.buffer += ch
            return InputEvent("redraw")
        return None

    def _drop_escape_sequence(self) -> None:
        """Arrow and function keys are not commands; discard ESC [ ... final."""
        if self._pending and self._pending[0] in "[O":
            self._pending.pop(0)
            while self._pending:
                if "\x40" <= self._pending.pop(0) <= "\x7e":
                    break

    def paint(self, header: str, view: List[str], prompt: str) -> None:
        size = shutil.get_terminal_size((80, 24))
        width, height = max(20, size.columns), max(5, size.lines)
        # Wrap rather than cut: a path or command on a long line is often the
        # one thing the operator needs (the decision packet, the approve command).
        body = [piece for line in [header] + view for piece in wrap_line(line, width)]
        body = body[-(height - 1):]  # keep the newest lines when the screen is short
        out = [f"{ESC}[H{ESC}[2J"]
        for line in body:
            out.append(styled(self.style, line) + "\r\n")
        tail = (prompt + self.buffer)[-(width - 1):]
        out.append(f"{ESC}[{height};1H{ESC}[2K" + self.style.paint("title", tail))
        self.stdout.write("".join(out))
        self.stdout.flush()


def open_terminal(stdin: TextIO = None, stdout: TextIO = None) -> Terminal:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        interactive = stdin.isatty() and stdout.isatty() and os.environ.get("TERM", "") != "dumb"
    except (AttributeError, ValueError):
        interactive = False
    if interactive:
        try:
            return RawTerminal(stdin.fileno(), stdout)
        except (ImportError, OSError):
            pass
    return LineTerminal(stdin, stdout)
