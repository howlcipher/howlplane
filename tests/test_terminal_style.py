"""Terminal styling: color is optional reinforcement; meaning is always in the words."""

import io
import json

import pytest

from howlplane.control_plane.presentation.operator import derive_operator_status, render_operator_text
from howlplane.control_plane.presentation.style import Style, format_duration, resolve_style, strip_ansi

pytestmark = pytest.mark.unit


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


def test_pipe_is_plain_ascii():
    style = resolve_style(io.StringIO(), env={})
    assert not style.color and not style.unicode and not style.interactive
    assert style.paint("error", "x") == "x" and style.dot == "-"


def test_tty_gets_color_and_unicode():
    style = resolve_style(Tty(), env={"TERM": "xterm"})
    assert style.color and style.unicode
    assert "\x1b[" in style.paint("ok", "fine")


def test_no_color_convention_is_respected():
    assert not resolve_style(Tty(), env={"NO_COLOR": "1", "TERM": "xterm"}).color


def test_explicit_color_override():
    assert resolve_style(io.StringIO(), mode="always", env={}).color
    assert not resolve_style(Tty(), mode="never", env={}).color


def test_dumb_terminal_and_ci_disable_richness():
    assert not resolve_style(Tty(), env={"TERM": "dumb"}).color
    assert not resolve_style(Tty(), env={"CI": "true", "TERM": "xterm"}).interactive


def test_styled_text_equals_plain_text_after_stripping():
    status = {"state": "backoff_after_failure", "project": "p", "recent_completed": [], "recent_failed": [],
              "last_error": "boom"}
    op = derive_operator_status(status)
    plain = "\n".join(render_operator_text(op, status, Style(color=False)))
    colored = "\n".join(render_operator_text(op, status, Style(color=True)))
    assert "\x1b" in colored and strip_ansi(colored) == plain


def test_json_never_contains_ansi():
    op = derive_operator_status({"state": "idle"})
    assert "\x1b" not in json.dumps(op.to_dict())


def test_table_narrow_width_keeps_every_cell_readable():
    style = Style(width=40)
    lines = style.table(["Component", "Status", "Detail"],
                        [["Git", "OK", "x" * 100], ["Codex", "READY", "ok"]])
    assert lines[1].startswith("Git") and lines[2].startswith("Codex")
    assert lines[1].endswith("...")


def test_format_duration_never_invents():
    assert format_duration(None) is None and format_duration(-5) is None
    assert format_duration(374) == "06m 14s" and format_duration(9) == "9s" and format_duration(7500) == "2h 05m"
