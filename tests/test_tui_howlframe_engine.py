"""`howlplane tui`: the pure HowlFrame terminal_app (ADR 0010).

The launcher only resolves paths and writes the exact grant profile; these
tests pin that authority, check the committed artifact against its source,
and drive the real program in line mode (stdin is a pipe) against the real
`howlplane` CLI with a stand-in agent, so no provider is contacted.
"""

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.tui import howlframe_engine
from tests._tui_support import fake_agent_sandbox, scratch_repo

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
TUI_DIR = ROOT / "integrations" / "howlframe" / "tui"


@pytest.fixture(scope="module")
def howlframe():
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    return hf


# -- launcher authority ---------------------------------------------------------------

def test_grant_profile_is_exact_and_holds_no_coarse_authority(tmp_path):
    entry, repo, runs, logs = "/s/bin/entry", "/r", "/r/.task_runs", "/s/logs"
    grants = howlframe_engine.grant_profile(entry, repo, runs, logs)
    assert grants[0] == f"process:exec_inherit_env={entry}"
    assert {"process", "filesystem", "network", "environment", "database"}.isdisjoint(grants)
    execs = [g for g in grants if g.startswith("process:exec=")]
    assert all(g.startswith(f"process:exec={entry} ") for g in execs), "only the HowlPlane entry can run"
    subcommands = {g.split()[1] for g in execs}
    assert subcommands == {"providers", "route", "work", "cancel"}
    # Free text only after `--`, so an objective can never become a flag.
    for g in execs:
        if " -- " in g:
            assert g.split(" -- ")[1] in ("**", "*")
    assert f"filesystem:read={runs}" in grants and f"filesystem:write={logs}" in grants
    assert len(grants) == 9


def test_launch_argv_carries_limits_and_a_plain_config(tmp_path, howlframe):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = TuiSettings(max_instructions=60_000, progress_poll_seconds=0.5, cancel_grace_seconds=3)
    argv = howlframe_engine.launch_argv(howlframe, repo, settings, tmp_path / "state", session_id="s1")
    assert argv[:2] == [howlframe, "run"]
    assert argv[argv.index("--max-event-instructions") + 1] == "60000"
    assert argv[argv.index("--max-event-memory-bytes") + 1] == str(settings.max_memory_bytes)
    assert argv[argv.index("--max-state-bytes") + 1] == str(settings.max_step_bytes)
    assert argv[argv.index("--color") + 1] == "auto", "the runtime decides colour unless the operator chose"
    assert argv[-2] == str(howlframe_engine.app_artifact())
    config = json.loads(argv[-1])
    assert config["repo"] == os.path.realpath(repo) and config["session_id"] == "s1"
    assert config["poll_ms"] == 500 and config["cancel_grace_ms"] == 3000
    assert config["cancel_reason"] == howlframe_engine.CANCEL_REASON
    entry = Path(config["cli"][0])
    assert entry.read_text().startswith(f"#!{sys.executable}\n")
    assert stat.S_IMODE(entry.stat().st_mode) == 0o700 and stat.S_IMODE(entry.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state" / "logs").stat().st_mode) == 0o700


def test_paths_a_grant_cannot_carry_are_refused(tmp_path, howlframe):
    spaced = tmp_path / "my repo"
    spaced.mkdir()
    with pytest.raises(howlframe_engine.LaunchError, match="whitespace or a comma"):
        howlframe_engine.launch_argv(howlframe, spaced, TuiSettings(), tmp_path / "state")


def test_colour_policy_becomes_the_runtime_flag(tmp_path, howlframe):
    assert howlframe_engine.color_mode(None, {}) == "auto"
    assert howlframe_engine.color_mode("never", {"HOWLPLANE_COLOR": "always"}) == "never", "--color wins"
    assert howlframe_engine.color_mode(None, {"HOWLPLANE_COLOR": "ALWAYS"}) == "always"
    assert howlframe_engine.color_mode("sometimes", {}) == "auto"
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = howlframe_engine.launch_argv(howlframe, repo, TuiSettings(), tmp_path / "state", color="never")
    assert argv[argv.index("--color") + 1] == "never"
    with pytest.raises(howlframe_engine.LaunchError, match="--color must be one of"):
        howlframe_engine.launch_argv(howlframe, repo, TuiSettings(), tmp_path / "state", color="sometimes")


def fake_runtime(tmp_path, descriptor):
    """A stand-in howlframe that only answers `interop --json`."""
    fake = tmp_path / "old-howlframe"
    reply = f"cat <<'EOF'\n{json.dumps(descriptor)}\nEOF" if descriptor else "exit 1"
    fake.write_text(f"#!/bin/sh\n{reply}\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


@pytest.mark.parametrize("descriptor,shown", [
    ({"schema": "howl.interop/v1", "version": "0.3.0",
      "commands": [{"path": ["run"], "flags": ["--allow-caps", "--max-event-instructions", "--max-event-memory-bytes", "--max-state-bytes"]}]},
     "HowlFrame 0.3.0"),
    (None, "HowlFrame without an interop descriptor"),
])
def test_a_runtime_without_the_needed_flags_is_refused_before_anything_starts(tmp_path, descriptor, shown):
    repo = tmp_path / "repo"
    repo.mkdir()
    old = fake_runtime(tmp_path, descriptor)
    with pytest.raises(howlframe_engine.LaunchError, match="too old for howlplane tui") as refused:
        howlframe_engine.launch_argv(old, repo, TuiSettings(), tmp_path / "state")
    assert shown in str(refused.value) and "--color" in str(refused.value)
    assert "HOWLFRAME_BIN" in refused.value.next_action
    assert not (tmp_path / "state").exists(), "nothing is prepared for a runtime that cannot run the TUI"


def test_provenance_matches_committed_bytes():
    provenance = json.loads((TUI_DIR / "ARTIFACT_PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["producer_source_revision"] == "e64256e5059df32bef82ef42f9814750f3a06fad"
    assert set(provenance["files"]) == {f"integrations/howlframe/tui/{n}" for n in ("tui_core.howl", "tui_app.howl", "tui_app.hfbc")}
    for name, digest in provenance["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name


def test_missing_runtime_is_an_actionable_error(monkeypatch, tmp_path):
    from howlplane.control_plane.presentation.errors import OperatorFailure
    from howlplane.control_plane import tui
    monkeypatch.setattr("howlplane.control_plane.howlframe_runner.find_howlframe_binary", lambda: None)
    repo = scratch_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    args = type("Args", (), {"repo": str(repo), "control_plane_dir": None})()
    with pytest.raises(OperatorFailure) as failure:
        tui.command(args)
    assert failure.value.error.code == "HOWLFRAME_UNAVAILABLE"


def test_committed_app_is_what_the_source_builds(tmp_path, howlframe):
    """Fails when tui_app.howl or tui_core.howl changed without scripts/build_tui_app.sh."""
    shutil.copy(TUI_DIR / "tui_app.howl", tmp_path)
    shutil.copy(TUI_DIR / "tui_core.howl", tmp_path)
    out = tmp_path / "rebuilt.hfbc"
    built = subprocess.run([howlframe, "build", str(tmp_path / "tui_app.howl"), "-o", str(out)],
                           capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    assert out.read_bytes() == (TUI_DIR / "tui_app.hfbc").read_bytes()
    packaged = ROOT / "src" / "howlplane" / "control_plane" / "data" / "integrations" / "howlframe" / "tui" / "tui_app.hfbc"
    assert packaged.read_bytes() == out.read_bytes()


# -- the program, in line mode --------------------------------------------------------------

class Session:
    def __init__(self, box, howlframe, argv=None):
        tmp = box["tmp"]
        root = tmp / "control-plane"
        root.mkdir(exist_ok=True)
        (root / "AGENTS.md").write_text("# test control plane root\n", encoding="utf-8")
        self.state = tmp / "state"
        env = {"HOME": str(tmp), "PATH": f"{box['bindir']}:/usr/bin:/bin",
               "HOWLPLANE_HOME": str(root), "HOWLPLANE_LOCAL_CONFIG": str(box["config"]),
               "HOWLPLANE_CAPACITY_PATH": str(tmp / "cap.json"), "HOWLPLANE_DIAGNOSTICS_DIR": str(tmp / "diag"),
               "HOWLFRAME_BIN": howlframe, "XDG_STATE_HOME": str(self.state),
               "PYTHONPATH": os.pathsep.join(sys.path)}
        self.env = env
        self.proc = subprocess.Popen(
            argv or [sys.executable, "-m", "howlplane.control_plane", "tui"],
            cwd=box["repo"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.out = b""

    def send(self, line):
        self.proc.stdin.write(line.encode() + b"\n")
        self.proc.stdin.flush()

    def wait_for(self, text, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.proc.stdout.read()
            if chunk:
                self.out += chunk
            if text.encode() in self.out:
                return self.out.decode(errors="replace")
            if self.proc.poll() is not None and not chunk:
                break
            time.sleep(0.05)
        raise AssertionError(f"{text!r} not shown:\n{self.out.decode(errors='replace')}")

    def finish(self):
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=30)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()

    def log_codes(self):
        log = self.state / "howlplane" / "tui" / "logs" / "tui_events.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()], log


@pytest.fixture
def session(tmp_path, howlframe):
    box = fake_agent_sandbox(tmp_path)
    s = Session(box, howlframe)
    s.box = box
    yield s
    if s.proc.poll() is None:
        s.proc.kill()


def test_preview_never_runs_and_a_run_needs_typed_confirmation(session):
    session.wait_for("codex  READY  usable")
    session.send("Add a /health endpoint")
    session.wait_for("Route:     codex for implementation")
    session.send("/run")
    session.wait_for("Run with codex? [y/N]")
    session.send("n")
    session.wait_for("! Run not started.")
    session.send("/quit")
    assert session.finish() == 0
    assert not session.box["marker"].exists(), "preview and a declined run never launch the agent"
    assert not (session.box["repo"] / ".task_runs").exists()


def test_commands_explain_themselves(session):
    session.wait_for("codex  READY  usable")
    session.send("/agent nosuch")
    session.wait_for("! Provider nosuch is not currently usable; selection unchanged.")
    session.send("/frobnicate")
    session.wait_for("! Unknown command /frobnicate. Type /help.")
    session.send("/run")
    session.wait_for("! Type an objective first.")
    session.send("/cancel")
    session.wait_for("! Nothing is running.")
    assert session.finish() == 0  # end of input leaves


def test_a_missing_agent_blocks_the_route(session):
    session.wait_for("codex  READY  usable")
    session.box["agent"].unlink()
    session.send("/refresh")
    session.wait_for("codex became unavailable: MISSING_EXECUTABLE")
    session.send("Add a /health endpoint")
    session.wait_for("Route:     BLOCKED")
    session.send("/run")
    session.wait_for("! The route is BLOCKED; nothing can run.")
    session.send("/quit")
    assert session.finish() == 0


def test_session_log_is_private_versioned_and_sequenced(session):
    session.wait_for("codex  READY  usable")
    session.send("/quit")
    assert session.finish() == 0
    events, log = session.log_codes()
    assert {e["schema"] for e in events} == {"howlplane.tui_event/v1"}
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert len({e["session_id"] for e in events}) == 1
    assert events[0]["code"] == "TUI_STARTED" and events[-1]["code"] == "TUI_STOPPED"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700


# -- the wrapper-free path ----------------------------------------------------------------

def test_print_command_is_the_exact_launch_argv(tmp_path, howlframe):
    repo = scratch_repo(tmp_path / "repo")
    line = howlframe_engine.print_command(howlframe, repo, TuiSettings(), tmp_path / "state")
    argv = shlex.split(line)
    expected = howlframe_engine.launch_argv(howlframe, repo, TuiSettings(), tmp_path / "state", reusable=True)
    assert argv == expected
    config = json.loads(argv[-1])
    assert "session_id" not in config, "a saved command must not reuse one session id"
    assert argv[argv.index("--allow-caps") + 1].split(",") == howlframe_engine.grant_profile(
        config["cli"][0], config["repo"], config["task_runs"], os.path.dirname(config["log_path"]))


def test_printed_command_runs_the_tui_on_howlframe_alone(tmp_path, howlframe):
    """`howlplane tui --print-command`, then that line run directly: no Python wrapper in the session."""
    box = fake_agent_sandbox(tmp_path)
    probe = Session(box, howlframe, argv=[sys.executable, "-m", "howlplane.control_plane", "tui", "--print-command"])
    printed = probe.proc.communicate(timeout=60)[0].decode()
    assert probe.proc.returncode == 0, printed
    assert printed.count("\n") == 1, "exactly one line, so it can be saved as a script"
    argv = shlex.split(printed)
    assert argv[0] == howlframe and argv[1] == "run"
    assert argv[argv.index("--color") + 1] == "auto"

    sessions = []
    for _ in range(2):
        s = Session(box, howlframe, argv=argv)
        s.wait_for("codex  READY  usable")
        s.send("Add a /health endpoint")
        s.wait_for("Route:     codex for implementation")
        s.send("/quit")
        assert s.finish() == 0
        sessions.append(s)
    assert not box["marker"].exists(), "the preview never launches the agent"
    events, _ = sessions[-1].log_codes()
    ids = {e["session_id"] for e in events}
    assert len(ids) == 2, "each run of a saved command is its own session"
    assert all(len(i) == 16 for i in ids)


def test_the_global_color_flag_reaches_the_runtime(tmp_path, howlframe):
    box = fake_agent_sandbox(tmp_path)
    probe = Session(box, howlframe, argv=[sys.executable, "-m", "howlplane.control_plane", "--color", "never", "tui", "--print-command"])
    printed = probe.proc.communicate(timeout=60)[0].decode()
    assert probe.proc.returncode == 0, printed
    argv = shlex.split(printed)
    assert argv[argv.index("--color") + 1] == "never"


# -- the build, in HowlFrame alone --------------------------------------------------------

PROVENANCE_PROGRAM = ROOT / "scripts" / "tui_provenance.howl"


def run_provenance(howlframe, tui, pkg, grants):
    return subprocess.run([howlframe, "run", "--allow-caps", ",".join(grants), str(PROVENANCE_PROGRAM), str(tui), str(pkg)],
                          capture_output=True, text=True, timeout=60)


def test_provenance_program_reproduces_the_committed_record(tmp_path, howlframe):
    tui, pkg = tmp_path / "tui", tmp_path / "pkg"
    shutil.copytree(TUI_DIR, tui)
    pkg.mkdir()
    done = run_provenance(howlframe, tui, pkg, [f"filesystem:read={tui}", f"filesystem:write={tui}", f"filesystem:write={pkg}"])
    assert done.returncode == 0, done.stdout + done.stderr
    rebuilt = json.loads((tui / "ARTIFACT_PROVENANCE.json").read_text(encoding="utf-8"))
    committed = json.loads((TUI_DIR / "ARTIFACT_PROVENANCE.json").read_text(encoding="utf-8"))
    assert rebuilt == committed, "the committed record is what the HowlFrame program writes"
    for name, digest in rebuilt["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    assert (pkg / "tui_app.hfbc").read_bytes() == (TUI_DIR / "tui_app.hfbc").read_bytes()
    assert (tui / "ARTIFACT_PROVENANCE.json").read_text() == (TUI_DIR / "ARTIFACT_PROVENANCE.json").read_text()


def test_provenance_program_holds_only_its_grants(tmp_path, howlframe):
    tui, pkg = tmp_path / "tui", tmp_path / "pkg"
    shutil.copytree(TUI_DIR, tui)
    pkg.mkdir()
    before = (tui / "ARTIFACT_PROVENANCE.json").read_bytes()
    denied = run_provenance(howlframe, tui, pkg, [f"filesystem:read={tui}", f"filesystem:write={pkg}"])
    assert denied.returncode != 0 and "CAPABILITY_DENIED" in denied.stderr, denied.stderr
    assert (tui / "ARTIFACT_PROVENANCE.json").read_bytes() == before
