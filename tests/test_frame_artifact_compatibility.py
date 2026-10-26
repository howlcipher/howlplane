"""Real Frame artifact execution; published v0.1.0 is a separate required CI job.

The normal suite uses its provisioned real Frame pin. No simulated compiler or
fallback evaluator may satisfy these integration assertions.
"""

import json
from pathlib import Path
import subprocess

import pytest

from howlplane.control_plane import component_bin
from howlplane.control_plane.howldream_runner import HowlDreamRunner
from howlplane.control_plane.howlframe_runner import find_howlframe_binary

ROOT = Path(__file__).parents[1]
ARTIFACT = ROOT / "integrations/howlframe/candidate_evaluator.hfbc"


def real_frame():
    binary = find_howlframe_binary()
    assert binary is not None, "Provision a real Frame; dedicated CI supplies published v0.1.0"
    return str(binary)


def candidate(status="LOCALLY_VERIFIED", executable=False):
    return {"candidate_id": "synthetic-candidate", "status": status,
            "authority": {"type": "ADVISORY", "executable": executable}}


def test_committed_evaluator_targets_published_hfbc1():
    content = ARTIFACT.read_bytes()
    assert content[:4] == b"HFBC"
    assert int.from_bytes(content[4:8], "little") == 1


@pytest.mark.parametrize("status", ["LOCALLY_VERIFIED", "REJECTED", "UNRESOLVED", "GENERATED"])
@pytest.mark.parametrize("executable", [False, True])
def test_actual_vm_assessment_matches_fallback_without_using_it(tmp_path, monkeypatch, status, executable):
    binary = real_frame()
    runner = HowlDreamRunner()
    payload = candidate(status, executable)
    expected = runner._fallback_evaluate_candidate(payload)

    def forbidden_fallback(*args, **kwargs):
        pytest.fail("A VM failure or artifact mismatch must not count as real execution")

    monkeypatch.setattr(component_bin, "resolve", lambda name: binary if name == "howlframe" else None)
    monkeypatch.setattr(runner, "_fallback_evaluate_candidate", forbidden_fallback)
    actual = runner.evaluate_candidate_with_howlframe(payload, tmp_dir=tmp_path)
    for key in ("candidate_id", "disposition", "confidence", "authority", "limitations"):
        assert actual[key] == expected[key]
    assert actual["schema_version"] == "howl.assessment/v1"
    assert actual["authority"] == {"type": "ADVISORY", "executable": False}


def test_actual_vm_preserves_filesystem_grant_and_finite_instruction_limit(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate()), encoding="utf-8")
    binary = real_frame()
    denied = subprocess.run([binary, "-run-bc", str(ARTIFACT), str(path)],
                            capture_output=True, text=True, timeout=15)
    assert denied.returncode == 3
    assert "Failed to read candidate file" in denied.stderr
    limited = subprocess.run([binary, "-run-bc", "-allow-caps", "filesystem",
                              "-max-instructions", "1", str(ARTIFACT), str(path)],
                             capture_output=True, text=True, timeout=15)
    assert limited.returncode == 1
    assert "LIMIT_EXCEEDED" in limited.stdout + limited.stderr


@pytest.mark.parametrize("schema_version,status", [
    ("howlplane.project_context/v1", "PASS"),
    ("howlplane.project_context/v999", "FAIL"),
])
def test_actual_vm_project_context_audit(tmp_path, schema_version, status):
    artifact = ROOT / "integrations/howlframe/project_context_audit.hfbc"
    assert int.from_bytes(artifact.read_bytes()[4:8], "little") == 1
    payload = {"schema": schema_version, "project_name": "synthetic",
               "project_types": ["python"], "has_agents_md": True, "has_manifest": False,
               "verification": {"test_count": 1, "build_count": 1, "lint_count": 1, "hygiene_count": 1},
               "hygiene_status": "configured_and_passed", "declared_capabilities": []}
    result = subprocess.run([real_frame(), "-run-bc", str(artifact), json.dumps(payload)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["schema"] == "howlplane.project_context_audit/v1"
    assert data["status"] == status


def test_artifact_provenance_matches_committed_bytes():
    import hashlib

    provenance = json.loads((ROOT / "integrations/howlframe/ARTIFACT_PROVENANCE.json").read_text())
    assert provenance["producer_release"] == "v0.1.0"
    for name, expected in provenance["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
