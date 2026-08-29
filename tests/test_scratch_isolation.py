"""
test_scratch_isolation.py

Verifies that provider scratch workspaces are strictly isolated outside the
target repository to prevent contaminating deterministic repository verification gates
(HOWLFRAM-SLOPFIX-07S).

Tests:
1. Scratch workspace is externalized outside target_repo.
2. Debris in scratch (nested source trees, build caches, git repos) does not
   appear in target repository git status or verification scans.
3. Durable scratch manifest records location, attempt, provider, and cleanup status.
4. Disposable caches are pruned at task completion while preserving provenance and patches.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional
import pytest

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    SCRATCH_MANIFEST_SCHEMA_VERSION,
)
from src.control_plane.task_spec import TaskSpec
from tests.test_provider_failover import (
    _FakeBackendResolver,
    _edit_feature_to_true,
    _init_test_repo,
    _run_failover_task,
)


def test_scratch_is_located_outside_target_repository(tmp_path: Path):
    """Provider scratch workspace must live outside the target repository root."""
    repo = _init_test_repo(tmp_path / "target_repo")
    scratch_root = tmp_path / "external_scratch"

    def write_heavy_scratch(task, cwd: Path, _prompt) -> None:
        _edit_feature_to_true(task, cwd, _prompt)

    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": write_heavy_scratch},
    })

    res = _run_failover_task(
        repo,
        resolver,
        scratch_root=scratch_root,
        max_attempts=1,
    )
    assert res.final_state == "complete"

    run_dir = Path(res.run_dir)
    # The target repo must NOT contain a provider_scratch directory
    assert not (run_dir / "provider_scratch").exists()
    assert not (repo / ".task_runs" / res.task_id / "provider_scratch").exists()

    # The scratch manifest must exist in run_dir evidence
    manifest_path = run_dir / "scratch_manifest.json"
    assert manifest_path.is_file()
    manifest = safe_load_json(manifest_path)
    assert manifest["schema"] == SCRATCH_MANIFEST_SCHEMA_VERSION
    assert manifest["task_id"] == res.task_id

    attempt_entry = manifest["attempts"]["01-resource_a"]
    assert attempt_entry["attempt"] == 1
    assert attempt_entry["resource_id"] == "resource_a"

    scratch_path = Path(attempt_entry["scratch_path"])
    # Scratch path is inside external_scratch, NOT inside target_repo
    assert scratch_root in scratch_path.parents
    assert repo not in scratch_path.parents
    assert scratch_path.is_dir()


def test_provider_scratch_debris_does_not_contaminate_target_repo(tmp_path: Path):
    """Heavy debris (source trees, caches, patches) created by a provider in scratch
    must never be visible to target repo git status or verification scans."""
    repo = _init_test_repo(tmp_path / "target_repo")
    scratch_root = tmp_path / "external_scratch"

    def create_scratch_debris(task, cwd: Path, prompt: str) -> None:
        # Provider discovers scratch directory from prompt
        # and creates source clones, caches, and patches there
        orch_scratch = scratch_root / repo.name / task.task_id / "01-resource_a"
        orch_scratch.mkdir(parents=True, exist_ok=True)

        # 1. Nested Go clone tree
        go_clone = orch_scratch / "clone-isolation" / "head" / "internal" / "vm"
        go_clone.mkdir(parents=True, exist_ok=True)
        (go_clone / "vm.go").write_text("package vm\nfunc Cloned() {}\n", encoding="utf-8")

        # 2. Build cache
        go_cache = orch_scratch / "go-cache" / "build"
        go_cache.mkdir(parents=True, exist_ok=True)
        (go_cache / "cache.bin").write_bytes(b"\x00" * 1024)

        # 3. Patch file
        (orch_scratch / "feature.patch").write_text("diff --git a/...\n", encoding="utf-8")

        # Legitimate edit in live tree
        _edit_feature_to_true(task, cwd, prompt)

    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": create_scratch_debris},
    })

    res = _run_failover_task(
        repo,
        resolver,
        scratch_root=scratch_root,
        max_attempts=1,
    )
    assert res.final_state == "complete"

    # Verify target repo git status does NOT see any clone-isolation or go-cache
    import subprocess
    git_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # The working tree was committed or has only task_runs
    assert "clone-isolation" not in git_status
    assert "go-cache" not in git_status
    assert "vm.go" not in git_status

    # Scratch manifest records the attempt
    manifest = safe_load_json(Path(res.run_dir) / "scratch_manifest.json")
    attempt_entry = manifest["attempts"]["01-resource_a"]
    scratch_path = Path(attempt_entry["scratch_path"])

    # Feature patch is preserved
    assert (scratch_path / "feature.patch").is_file()

    # Disposable go-cache was pruned at task completion
    assert not (scratch_path / "go-cache").exists()
    assert attempt_entry["status"] == "cleaned"


def test_stray_evidence_root_artifacts_are_relocated_externally(tmp_path: Path):
    """When a provider drops files directly in .task_runs/<task>/ evidence root,
    _sweep_provider_scratch moves them to external scratch, not into target repo."""
    repo = _init_test_repo(tmp_path / "target_repo")
    scratch_root = tmp_path / "external_scratch"

    def drop_stray_files(task, cwd: Path, prompt: str) -> None:
        run_dir = Path(cwd) / ".task_runs" / task.task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "rogue-test.patch").write_text("rogue patch\n", encoding="utf-8")
        _edit_feature_to_true(task, cwd, prompt)

    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": drop_stray_files},
    })

    res = _run_failover_task(
        repo,
        resolver,
        scratch_root=scratch_root,
        max_attempts=1,
    )
    assert res.final_state == "complete"
    run_dir = Path(res.run_dir)

    # Stray file removed from evidence root
    assert not (run_dir / "rogue-test.patch").exists()

    # Scratch is NOT inside run_dir
    assert not (run_dir / "provider_scratch").exists()

    # Manifest records swept artifact
    manifest = safe_load_json(run_dir / "scratch_manifest.json")
    attempt_entry = manifest["attempts"]["01-resource_a"]
    scratch_path = Path(attempt_entry["scratch_path"])
    assert scratch_root in scratch_path.parents

    relocated = scratch_path / "rogue-test.patch"
    assert relocated.is_file()
    assert relocated.read_text(encoding="utf-8") == "rogue patch\n"

    provenance = safe_load_json(scratch_path / "_provenance.json")
    assert provenance["origin"] == "provider_scratch"
    assert "rogue-test.patch" in provenance["files"]
