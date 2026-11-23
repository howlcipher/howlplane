"""Terminal ownership for `howlplane tui`: line mode, raw keys and restore in a real pty."""

import io
import os
import pty
import signal
import sys
import termios
import textwrap

import pytest

from howlplane.control_plane.presentation.style import Style, strip_ansi
from howlplane.control_plane.tui.terminal import LineTerminal, open_terminal, styled, wrap_line
from tests._tui_support import read_until

pytestmark = pytest.mark.unit


def test_line_terminal_reads_lines_and_eof():
    term = LineTerminal(io.StringIO("first\r\nsecond\n"), io.StringIO(), Style())
    assert [term.read(0).text, term.read(0).text, term.read(0).kind] == ["first", "second", "eof"]


def test_line_terminal_repaints_only_changed_views():
    out = io.StringIO()
    term = LineTerminal(io.StringIO(""), out, Style())
    term.paint("H", ["a"], "> ")
    term.paint("H", ["a"], "> ")
    term.paint("H", ["b"], "> ")
    assert out.getvalue() == "H\na\n> > H\nb\n> "


def test_color_only_decorates_words():
    colored = styled(Style(color=True), "FAILED: x")
    assert colored != "FAILED: x" and strip_ansi(colored) == "FAILED: x"
    assert styled(Style(color=False), "FAILED: x") == "FAILED: x"


def test_long_lines_wrap_instead_of_being_cut():
    line = "Detail:    Read /a/very/long/path/to/.task_runs/TUI-1/decision_packet.md first."
    pieces = wrap_line(line, 30)
    assert all(len(piece) <= 30 for piece in pieces)
    assert pieces[0] + "".join(piece[2:] for piece in pieces[1:]) == line
    assert wrap_line("short", 30) == ["short"]


def test_pipes_get_line_mode():
    assert isinstance(open_terminal(io.StringIO(""), io.StringIO()), LineTerminal)


CHILD = textwrap.dedent("""
    import sys, termios
    from howlplane.control_plane.tui.terminal import RawTerminal
    before = termios.tcgetattr(0)
    term = RawTerminal(0, sys.stdout)
    print("RAWMODE", flush=True)  # keys typed before this would meet the kernel's line discipline
    events = []
    try:
        while True:
            e = term.read(5)
            if e is None or e.kind == "eof":
                break
            if e.kind != "redraw":
                events.append((e.kind, e.text))
            if e.kind == "line" and e.text == "wait":
                print("READY", flush=True)
                import time; time.sleep(30)
    finally:
        term.close()
    print("EVENTS", events, flush=True)
    print("RESTORED", termios.tcgetattr(0) == before, flush=True)
""")


def spawn(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, str(script)])
    return pid, fd


def test_raw_keys_line_editing_and_restore(tmp_path):
    pid, fd = spawn(tmp_path)
    started = read_until(fd, b"RAWMODE")
    assert b"RAWMODE" in started and b"\x1b[?1049h" in started, "raw mode enters the alternate screen"
    # Typed text, a backspace, an arrow key (ignored), Ctrl-C, then Ctrl-D on an empty line.
    os.write(fd, "fix itx\x7f\x1b[A\r".encode() + "héllo\r".encode() + b"\x03\x04")
    out = read_until(fd, b"RESTORED True")
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "EVENTS [('line', 'fix it'), ('line', 'héllo'), ('interrupt', '')]" in text
    assert "RESTORED True" in text
    assert "\x1b[?1049l" in text


def test_sigterm_restores_the_terminal(tmp_path):
    pid, fd = spawn(tmp_path)
    assert b"RAWMODE" in read_until(fd, b"RAWMODE")
    os.write(fd, b"wait\r")
    assert b"READY" in read_until(fd, b"READY")
    os.kill(pid, signal.SIGTERM)
    out = read_until(fd, b"\x1b[?1049l")
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 128 + signal.SIGTERM
    assert b"\x1b[?1049l" in out
    assert termios.tcgetattr(fd)[3] & termios.ICANON, "canonical mode is back after SIGTERM"
