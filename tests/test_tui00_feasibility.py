"""TUI-00 feasibility probes: observed HowlFrame behavior for a HowlPlane TUI.

These run the real HowlFrame toolchain against probes in
``integrations/howlframe/tui00`` and contract fixtures captured from HowlPlane.
No AI provider is contacted: the execution boundary is a fake that answers with
fixtures, and the one real-HowlPlane probe only runs ``providers --json`` with a
fake, never-invoked agent executable on PATH. Every subprocess has a timeout.

The ``xfail(strict=True)`` tests are regression-test proposals for HowlFrame
defects HF-D1..HF-D4 (documentation/journals/2026-11-22_tui00_feasibility_gate.md).
They assert the correct behavior, so they flip to XPASS (and fail) once fixed.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = REPO_ROOT / "integrations" / "howlframe" / "tui00"
FIXTURES = PROBE_DIR / "fixtures"
STEP_LIMITS = ["--max-instructions", "20000", "--max-memory-bytes", "8388608", "--deadline", "5s"]
TIMEOUT = 30


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _canonical(name):
    """Absolute, symlink-free path of a host program, as HowlFrame exec grants require."""
    found = shutil.which(name)
    return os.path.realpath(found) if found else None


@pytest.fixture(scope="module")
def hf():
    found = find_howlframe_binary()
    assert found, "Real howlframe binary is required for TUI-00 probes; set HOWLFRAME_BIN or put it on PATH"
    return found


@pytest.fixture(scope="module")
def work(tmp_path_factory, hf):
    """A private copy of the probes with freshly built bytecode; nothing is written to the checkout."""
    root = tmp_path_factory.mktemp("tui00")
    shutil.copytree(PROBE_DIR, root / "tui00")
    probes = root / "tui00"
    for name in ("tui_reducer", "exec_boundary_probe"):
        built = subprocess.run([hf, "build", str(probes / f"{name}.howl"), "-o", str(probes / f"{name}.hfbc")],
                               capture_output=True, text=True, timeout=TIMEOUT)
        assert built.returncode == 0, built.stdout + built.stderr
    return probes


def _run(hf, bytecode, *args, caps=None, stdin=None, limits=STEP_LIMITS):
    cmd = [hf, "run", *limits]
    if caps:
        cmd += ["--allow-caps", caps]
    cmd += [str(bytecode), *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=TIMEOUT, env={})


def _new_state():
    return {"objective": None, "explicit_resource": None, "rows": [], "available": [], "notices": []}


def _step(hf, work, state, event, max_chars=4000):
    step = {"schema": "howlplane.tui_step/v0", "state": state, "event": event,
            "config": {"max_objective_chars": max_chars}}
    proc = _run(hf, work / "tui_reducer.hfbc", stdin=json.dumps(step))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


# 1. Dynamic prompt ---------------------------------------------------------

def test_dynamic_objective_is_trimmed_and_proposed_as_route_intent(hf, work):
    out = _step(hf, work, _new_state(), {"type": "objective_submitted", "text": "  Add a /health endpoint  "})
    assert out["state"]["objective"] == "Add a /health endpoint"
    assert out["intents"] == [{"kind": "route_preview", "objective": "Add a /health endpoint", "resource_id": None}]


@pytest.mark.parametrize("text,code", [("   ", "OBJECTIVE_EMPTY"), ("x" * 41, "OBJECTIVE_TOO_LONG")])
def test_invalid_objective_fails_before_any_intent(hf, work, text, code):
    out = _step(hf, work, _new_state(), {"type": "objective_submitted", "text": text}, max_chars=40)
    assert out["intents"] == []
    assert out["state"]["failure"]["code"] == code
    assert out["state"]["failure"]["retry"] == "SAFE_TO_RETRY"


def test_malformed_step_is_a_structured_refusal(hf, work):
    proc = _run(hf, work / "tui_reducer.hfbc", stdin="{not json")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["code"] == "MALFORMED_STEP"


# 2. Structured inventory ---------------------------------------------------

def test_reads_real_inventory_contract(hf, work):
    out = _step(hf, work, _new_state(), {"type": "inventory", "document": _fixture("ai_resources_codex_ready")})
    rows = {row["resource_id"]: row for row in out["state"]["rows"]}
    configured = {r["identity"]["resource_id"] for r in _fixture("ai_resources_codex_ready")["resources"] if r["configured"]}
    assert set(rows) == configured
    assert out["state"]["available"] == [rid for rid in rows if rows[rid]["usable"]]


def test_unknown_inventory_version_fails_closed(hf, work):
    doc = dict(_fixture("ai_resources_codex_ready"), schema="howlplane.ai_resources/v2")
    out = _step(hf, work, _new_state(), {"type": "inventory", "document": doc})
    assert out["state"]["failure"]["code"] == "UNSUPPORTED_INVENTORY_SCHEMA"
    assert out["state"]["rows"] == []


# 3. Provider becomes unavailable ---------------------------------------------

def test_provider_loss_clears_explicit_choice(hf, work):
    state = _step(hf, work, _new_state(), {"type": "inventory", "document": _fixture("ai_resources_codex_ready")})["state"]
    usable = state["available"]
    assert usable, "fixture must contain a usable resource"
    state = _step(hf, work, state, {"type": "provider_selected", "resource_id": usable[0]})["state"]
    assert state["explicit_resource"] == usable[0]
    out = _step(hf, work, state, {"type": "inventory", "document": _fixture("ai_resources_codex_missing")})
    assert out["state"]["explicit_resource"] is None
    assert any("became unavailable" in note for note in out["state"]["notices"])


def test_selecting_an_unusable_provider_is_refused(hf, work):
    state = _step(hf, work, _new_state(), {"type": "inventory", "document": _fixture("ai_resources_codex_missing")})["state"]
    out = _step(hf, work, state, {"type": "provider_selected", "resource_id": "codex"})
    assert out["state"]["explicit_resource"] is None


# 4. Routing preview without AI ---------------------------------------------

def test_route_preview_flags_selection_unconfirmed_by_inventory(hf, work):
    state = _step(hf, work, _new_state(), {"type": "inventory", "document": _fixture("ai_resources_codex_missing")})["state"]
    out = _step(hf, work, state, {"type": "route_preview", "document": _fixture("route_selected_unprobed")})
    assert out["state"]["route"]["status"] == "SELECTED"
    assert out["state"]["route"]["readiness_confirmed"] is False


def test_blocked_route_lists_exclusions(hf, work):
    out = _step(hf, work, _new_state(), {"type": "route_preview", "document": _fixture("route_blocked_local_only")})
    assert out["state"]["route"]["reason"] == "NO_ELIGIBLE_AI_RESOURCE"
    assert out["state"]["route"]["excluded"]


# 5. Structured failure ---------------------------------------------------------

def test_howlplane_error_is_rendered_without_guessing_retry_safety(hf, work):
    out = _step(hf, work, _new_state(), {"type": "failure", "document": _fixture("error_not_a_repo")})
    failure = out["state"]["failure"]
    assert failure["code"] == "NOT_A_GIT_REPOSITORY" and failure["next"]
    assert failure["retry"] == "UNKNOWN"
    assert any(line.startswith("Next: ") for line in out["view"])


def test_unrecognized_failure_document_is_explicit(hf, work):
    out = _step(hf, work, _new_state(), {"type": "failure", "document": {"oops": 1}})
    assert out["state"]["failure"]["code"] == "MALFORMED_FAILURE"


def test_work_result_reports_verification_status_verbatim(hf, work):
    out = _step(hf, work, _new_state(), {"type": "work_result", "document": _fixture("work_result_failed")})
    assert out["state"]["outcome"] == {"final_state": "failed", "exit_code": 1, "next": "FAILED",
                                       "verification_status": "none"}


# 6. Fake execution boundary --------------------------------------------------

@pytest.fixture
def boundary(tmp_path):
    shell = _canonical("dash") or _canonical("sh")
    assert shell, "a POSIX shell is required for the fake boundary"
    shutil.copy(PROBE_DIR / "fake_howlplane.sh", tmp_path / "fake_howlplane.sh")
    shutil.copytree(FIXTURES, tmp_path / "fixtures")
    script = str(tmp_path / "fake_howlplane.sh")
    grants = ",".join(f"process:exec={shell} {script} {shape}" for shape in (
        "providers --json", "route * --json"))

    def call(hf, work, scenario, *args, limits=STEP_LIMITS, extra_caps=""):
        (tmp_path / "scenario").write_text(scenario + "\n", encoding="utf-8")
        caps = grants + ("," + extra_caps if extra_caps else "")
        return _run(hf, work / "exec_boundary_probe.hfbc", shell, script, *args, caps=caps, limits=limits)

    call.log = tmp_path / "invocations.log"
    call.script = script
    return call


def test_boundary_success_and_verbatim_argv(hf, work, boundary):
    objective = 'add /health; $(touch pwned) "quoted"'
    proc = boundary(hf, work, "ok", "route", objective)
    out = json.loads(proc.stdout)
    assert out["ok"] is True and out["schema"] == "howlplane.resource_selection/v1"
    assert boundary.log.read_text(encoding="utf-8") == f"[route][{objective}][--json]\n"
    assert not (Path(boundary.script).parent / "pwned").exists()


def test_boundary_blocked_exit_code_keeps_document(hf, work, boundary):
    out = json.loads(boundary(hf, work, "provider_down", "route", "x").stdout)
    assert out["ok"] is True and out["exit_code"] == 3
    assert out["document"]["status"] == "BLOCKED"


def test_boundary_howlplane_error_on_stderr(hf, work, boundary):
    out = json.loads(boundary(hf, work, "fail", "providers").stdout)
    assert out["ok"] is False and out["failure"]["code"] == "NOT_A_GIT_REPOSITORY"


@pytest.mark.parametrize("scenario", ["malformed", "stderr_noise"])
def test_boundary_unparseable_output_is_never_success(hf, work, boundary, scenario):
    """stderr_noise documents gap HF-G3: exec merges stdout and stderr into one buffer."""
    out = json.loads(boundary(hf, work, scenario, "providers").stdout)
    assert out["ok"] is False and out["failure"]["code"] == "MALFORMED_OUTPUT"


@pytest.mark.parametrize("args", [("route", "--execute"), ("work", "do it"), ("route", "x", "codex")])
def test_ungranted_argv_is_denied_before_the_boundary_runs(hf, work, boundary, args):
    out = json.loads(boundary(hf, work, "ok", *args).stdout)
    assert out["failure"]["code"] == "CAPABILITY_DENIED"
    assert not boundary.log.exists()


# 7. Resource bounds --------------------------------------------------------------

def test_instruction_budget_fails_closed(hf, work):
    step = {"schema": "howlplane.tui_step/v0", "state": _new_state(),
            "event": {"type": "inventory", "document": _fixture("ai_resources_codex_ready")},
            "config": {"max_objective_chars": 4000}}
    proc = _run(hf, work / "tui_reducer.hfbc", stdin=json.dumps(step), limits=["--max-instructions", "50"])
    assert proc.returncode == 1 and json.loads(proc.stderr)["code"] == "LIMIT_EXCEEDED"
    assert proc.stdout == ""


def test_hung_boundary_is_killed_at_deadline(hf, work, boundary):
    start = time.monotonic()
    proc = boundary(hf, work, "hang", "providers", limits=["--deadline", "2s"])
    assert time.monotonic() - start < 10
    assert json.loads(proc.stderr)["message"] == "wall-clock deadline exceeded"
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", boundary.script], capture_output=True, text=True, timeout=TIMEOUT)
    assert left.stdout.strip() == ""


def test_output_flood_is_bounded(hf, work, boundary):
    proc = boundary(hf, work, "flood", "providers", limits=["--deadline", "10s", "--max-exec-output-bytes", "65536"])
    out = json.loads(proc.stdout)
    assert out["failure"]["code"] == "LIMIT_EXCEEDED"


# Real HowlPlane through HowlFrame exec ---------------------------------------------

@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="bin/howlplane needs VIRTUAL_ENV to find its interpreter without PATH")
def test_exec_child_cannot_observe_operator_path(hf, work, tmp_path):
    """Finding HP-F1: provider readiness differs when HowlPlane is an exec child.

    The fake agent executable only exists on the operator's PATH. HowlFrame exec
    children start with an empty environment and refuse process:env=PATH, so
    HowlPlane's shutil.which readiness probe cannot see it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "invoked"
    fake = bindir / "codex"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n", encoding="utf-8")
    fake.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('[ai_resources]\noperating_mode = "connected"\n'
                      '[ai_resources.providers.codex]\nenabled = true\n', encoding="utf-8")
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "VIRTUAL_ENV": sys.prefix,
           "HOWLPLANE_HOME": str(REPO_ROOT), "HOWLPLANE_LOCAL_CONFIG": str(config),
           "HOWLPLANE_CAPACITY_PATH": str(tmp_path / "capacity.json")}
    bash = _canonical("bash")
    launcher = str(REPO_ROOT / "bin" / "howlplane")

    def readiness(text):
        doc = json.loads(text)
        return {r["identity"]["resource_id"]: r["readiness"] for r in doc["resources"]}["codex"]

    direct = subprocess.run([bash, launcher, "providers", "--json"], capture_output=True, text=True,
                            timeout=120, env=env)
    assert direct.returncode == 0, direct.stderr
    caps = ",".join([f"process:exec={bash} {launcher} providers --json"]
                    + [f"process:env={name}" for name in env if name not in ("HOME", "PATH")])
    probe = tmp_path / "real.howl"
    probe.write_text('(cli_app (print (bytes_to_string (exec (cli_args 0) (list (cli_args 1) "providers" "--json")))))\n',
                     encoding="utf-8")
    assert subprocess.run([hf, "build", str(probe), "-o", str(tmp_path / "real.hfbc")],
                          capture_output=True, timeout=TIMEOUT).returncode == 0
    via_frame = subprocess.run([hf, "run", "--deadline", "100s", "--allow-caps", caps, str(tmp_path / "real.hfbc"),
                                bash, launcher], capture_output=True, text=True, timeout=120, env=env)
    assert via_frame.returncode == 0, via_frame.stderr
    assert readiness(direct.stdout) == "READY"
    assert readiness(via_frame.stdout) == "MISSING_EXECUTABLE"
    assert not marker.exists(), "readiness must never launch the agent executable"


# HowlFrame defect regression proposals -------------------------------------------------

def _reproducer(hf, tmp_path, name, *run_args, stdin=None):
    out = tmp_path / f"{name}.hfbc"
    built = subprocess.run([hf, "build", str(PROBE_DIR / "reproducers" / f"{name}.howl"), "-o", str(out)],
                           capture_output=True, text=True, timeout=TIMEOUT)
    assert built.returncode == 0, built.stdout + built.stderr
    return subprocess.run([hf, "run", *run_args, str(out)], input=stdin, capture_output=True,
                          timeout=TIMEOUT, env={})


@pytest.mark.xfail(strict=True, reason="HF-D1: spawn bodies get a fresh instruction budget")
def test_hf_d1_spawn_work_is_bounded_by_the_run_budget(hf, tmp_path):
    proc = _reproducer(hf, tmp_path, "hf_d1_spawn_instruction_budget", "--max-instructions", "1500",
                       "--allow-caps", "process")
    assert proc.stdout.count(b"child finished loop") < 5


@pytest.mark.xfail(strict=True, reason="HF-D2: concurrent map writes crash the Go runtime")
def test_hf_d2_spawn_shared_dict_has_structured_outcome(hf, tmp_path):
    proc = _reproducer(hf, tmp_path, "hf_d2_spawn_shared_map_race", "--max-instructions", "10000000",
                       "--max-memory-bytes", "1000000000", "--allow-caps", "process")
    assert b"fatal error" not in proc.stderr


@pytest.mark.xfail(strict=True, reason="HF-D3: read_line is not charged to the memory budget")
def test_hf_d3_read_line_respects_memory_budget(hf, tmp_path):
    proc = _reproducer(hf, tmp_path, "hf_d3_read_line_unbounded", "--max-memory-bytes", "1048576",
                       stdin=b"a" * (8 * 1024 * 1024) + b"\n")
    assert proc.returncode != 0 and b"LIMIT_EXCEEDED" in proc.stderr


@pytest.mark.xfail(strict=True, reason="HF-D4: parse_json does not charge the decoded tree")
def test_hf_d4_parse_json_respects_memory_budget(hf, tmp_path):
    payload = ("[" + ",".join(['{"a":[]}'] * 500000) + "]").encode()
    proc = _reproducer(hf, tmp_path, "hf_d4_parse_json_uncharged", "--max-memory-bytes", "8388608",
                       "--max-instructions", "1000", stdin=payload)
    assert proc.returncode != 0 and b"LIMIT_EXCEEDED" in proc.stderr
