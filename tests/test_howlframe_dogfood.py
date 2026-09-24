"""Dogfooding integration tests and adversarial verification for HowlFrame project context audit."""

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
from jsonschema import validate

from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.howlframe_runner import (
    HowlFrameAuditRunner,
    find_howlframe_binary,
    normalize_project_context,
    PROJECT_CONTEXT_SCHEMA_VERSION,
)
from howlplane.control_plane.project_adapter import ProjectAdapter, ProjectContext
from howlplane.control_plane.router import TaskRouter
from howlplane.control_plane.task_spec import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "schemas" / "project-context-contract.schema.json"
AUDIT_SCHEMA_PATH = REPO_ROOT / "schemas" / "project-context-audit.schema.json"
HOWL_FILE = REPO_ROOT / "integrations" / "howlframe" / "project_context_audit.howl"
HFBC_FILE = REPO_ROOT / "integrations" / "howlframe" / "project_context_audit.hfbc"
DEV_ROOT = Path(
    os.environ.get("HOWL_DEV_WORKSPACE")
    or (REPO_ROOT.parent if (REPO_ROOT.parent / "howlframe").is_dir() else "/run/media/system/tallgeese/dev")
)


def _read_schema(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _make_mock_bin(tmp_path: Path, code: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True); f = tmp_path / "mock_bin"
    f.write_text(code, encoding="utf-8")
    f.chmod(0o755)
    return str(f)


def test_schema_and_artifacts():
    assert HOWL_FILE.is_file() and HFBC_FILE.is_file()
    ctx = ProjectAdapter.discover(REPO_ROOT)
    data = normalize_project_context(ctx)
    validate(instance=data, schema=_read_schema(CONTRACT_PATH))
    assert data["schema"] == PROJECT_CONTEXT_SCHEMA_VERSION


@pytest.mark.parametrize(
    "name, types, has_agents",
    [
        ("howlplane", ["go", "python", "howlframe"], True),
        ("howlframe", ["go"], True),
        ("howlnotes", ["howlframe"], False),
        ("howlchangeops", ["go"], False),
        ("DevOps-Learn-by-Doing", ["python"], False),
    ],
)
def test_dogfood_matrix(name: str, types: list, has_agents: bool):
    h_bin = find_howlframe_binary()
    assert h_bin is not None, "Real howlframe binary is required for integration tests; not found on PATH or via HOWLFRAME_BIN"

    dir_path = DEV_ROOT / name
    if not dir_path.is_dir():
        pytest.skip(f"Repo missing: {dir_path}")

    ctx = ProjectAdapter.discover(dir_path)
    for t in types:
        assert t in ctx.project_types
    assert ctx.has_agents_md == has_agents

    res = HowlFrameAuditRunner.run_audit(ctx, record_evidence=False)
    assert res.status == "MATCH"
    assert res.audit_status in ("PASS", "WARN")

    validate(
        instance={
            "schema": res.audit_schema_version,
            "status": res.audit_status,
            "findings": res.findings,
            "observed": res.observed,
        },
        schema=_read_schema(AUDIT_SCHEMA_PATH),
    )
    if not has_agents:
        assert "missing AGENTS.md" in res.findings


@pytest.mark.parametrize(
    "payload_str, expected_msg",
    [
        ("{invalid_json", "malformed json input"),
        (json.dumps({"schema": "howlplane.project_context/v999", "project_name": "x", "project_types": ["go"], "has_agents_md": True, "has_manifest": False, "verification": {"test_count": 1, "build_count": 1, "lint_count": 1, "hygiene_count": 1}, "hygiene_status": "configured_and_passed", "declared_capabilities": []}), "unsupported schema version"),
        (json.dumps({"schema": "howlplane.project_context/v1", "project_types": ["go"], "has_agents_md": True, "has_manifest": False, "verification": {"test_count": 1, "build_count": 1, "lint_count": 1, "hygiene_count": 1}, "hygiene_status": "configured_and_passed", "declared_capabilities": []}), "missing project_name"),
        (json.dumps({"schema": "howlplane.project_context/v1", "project_name": "x", "project_types": ["go"], "has_agents_md": True, "has_manifest": False, "verification": {"test_count": 1, "build_count": 1, "lint_count": 1, "hygiene_count": 1}, "hygiene_status": "bogus_status", "declared_capabilities": []}), "invalid hygiene_status"),
        (json.dumps({"schema": "howlplane.project_context/v1", "project_name": "x", "project_types": ["go"], "has_agents_md": True, "has_manifest": False, "verification": {"test_count": -1, "build_count": 1, "lint_count": 1, "hygiene_count": 1}, "hygiene_status": "configured_and_passed", "declared_capabilities": []}), "negative test_count"),
    ],
)
def test_falsifications_payload(payload_str, expected_msg):
    h_bin = find_howlframe_binary()
    assert h_bin is not None, "Real howlframe binary is required for integration tests; not found on PATH or via HOWLFRAME_BIN"
    proc = subprocess.run([h_bin, "run", "--max-instructions", "100000", str(HFBC_FILE), payload_str], capture_output=True, text=True, check=False)
    out = json.loads(proc.stdout.strip())
    assert out["status"] == "FAIL" and expected_msg in out["findings"]


@pytest.mark.parametrize(
    "script, expected_outcome",
    [
        ("#!/bin/sh\nexit 127\n", "HOWLFRAME_FAILURE"),
        ("#!/bin/sh\necho 'Not JSON'\nexit 0\n", "INVALID_OUTPUT"),
        ("#!/bin/sh\necho '{\"bad\":1}'\nexit 0\n", "INVALID_OUTPUT"),
    ],
)
def test_falsifications_subproc(tmp_path, script, expected_outcome):
    mock_bin = _make_mock_bin(tmp_path, script)
    ctx = ProjectAdapter.discover(REPO_ROOT)
    with patch("howlplane.control_plane.howlframe_runner.find_howlframe_binary", return_value=mock_bin):
        res = HowlFrameAuditRunner.run_audit(ctx, record_evidence=False)
        assert res.status == expected_outcome


def test_falsifications_bounds_and_lifecycle(tmp_path):
    ctx = ProjectAdapter.discover(REPO_ROOT)
    h_bin = find_howlframe_binary()
    assert h_bin is not None, "Real howlframe binary is required for integration tests; not found on PATH or via HOWLFRAME_BIN"

    # 1. Unbounded size
    huge_ctx = ProjectContext(project_root=str(tmp_path), name="huge", capabilities=["c" * 1000 for _ in range(100)])
    assert HowlFrameAuditRunner.run_audit(huge_ctx, record_evidence=False).status == "HOWLFRAME_FAILURE"

    # 2. Missing binary
    with patch("howlplane.control_plane.howlframe_runner.find_howlframe_binary", return_value=None):
        assert HowlFrameAuditRunner.run_audit(ctx, record_evidence=False).status == "HOWLFRAME_UNAVAILABLE"

    # 3. Timeout
    sleep_bin = _make_mock_bin(tmp_path / "t1", "#!/bin/sh\nsleep 5\n")
    with patch("howlplane.control_plane.howlframe_runner.find_howlframe_binary", return_value=sleep_bin):
        assert HowlFrameAuditRunner.run_audit(ctx, timeout=0.1, record_evidence=False).status == "TIMEOUT"

    # 4. Budget limit
    assert HowlFrameAuditRunner.run_audit(ctx, max_instructions=5, record_evidence=False).status == "BUDGET_EXCEEDED"

    # 5. Fixed artifact path
    assert HowlFrameAuditRunner.resolve_artifact_path() == HFBC_FILE

    # 6. Source integrity check
    assert subprocess.run([h_bin, "check", str(HOWL_FILE)], capture_output=True, check=False).returncode == 0

    # 7. Redaction
    sec_ctx = ProjectContext(project_root=str(tmp_path), name="sec", capabilities=["key=sk-999999999999999999999999999999"])
    raw_json = json.dumps(normalize_project_context(sec_ctx))
    assert "sk-999999999999999999999999999999" not in raw_json and "[REDACTED" in raw_json

    # 8. Shadow failure isolation
    fail_bin = _make_mock_bin(tmp_path / "t2", "#!/bin/sh\nexit 1\n")
    spec = TaskSpec(task_id="T-ISO", repository=ctx.name, objective="Verify isolation", acceptance_criteria=["OK"])
    router = TaskRouter()
    r1 = router.route(spec)
    with patch("howlplane.control_plane.howlframe_runner.find_howlframe_binary", return_value=fail_bin):
        assert HowlFrameAuditRunner.run_audit(ctx, record_evidence=False, dogfood_mode="shadow").status == "HOWLFRAME_FAILURE"
        r2 = router.route(spec)
        assert r1.selected_agent_id == r2.selected_agent_id

    # 9. Ledger write
    ldg = EvidenceLedger(str(tmp_path / "ldg.jsonl"))
    assert HowlFrameAuditRunner.run_audit(ctx, record_evidence=True, ledger=ldg, task_id="T-EV", dogfood_mode="shadow").status == "MATCH"
    assert len(ldg.list_all_entries()) == 1


def test_bytecode_compilation_deterministic_reproducibility(tmp_path: Path):
    """Proves that compiling project_context_audit.howl reproduces the exact committed .hfbc artifact."""
    h_bin = find_howlframe_binary()
    assert h_bin is not None, "Real howlframe binary is required for integration tests; not found on PATH or via HOWLFRAME_BIN"

    out_hfbc = tmp_path / "compiled_audit.hfbc"
    proc = subprocess.run(
        [h_bin, "build", str(HOWL_FILE), "-o", str(out_hfbc)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Compilation failed: {proc.stderr}"
    assert out_hfbc.is_file(), "Compiled .hfbc missing"

    import hashlib
    committed_hash = hashlib.sha256(HFBC_FILE.read_bytes()).hexdigest()
    compiled_hash = hashlib.sha256(out_hfbc.read_bytes()).hexdigest()
    assert compiled_hash == committed_hash, f"Bytecode drift detected: {committed_hash} != {compiled_hash}"
