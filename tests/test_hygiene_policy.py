"""
test_hygiene_policy.py

Comprehensive tests for deterministic repository hygiene policy classification,
ceiling governance, config integrity, provider verification, attack defenses,
and evidence metrics.
"""

from pathlib import Path

from howlplane.control_plane.hygiene_policy import (
    HygienePolicyClassifier,
    PolicyChangeType,
)
from howlplane.control_plane.human_boundary import (
    HumanBoundaryGate,
)
from howlplane.control_plane.verification import (
    VerificationPlan,
)
from howlplane.control_plane.task_spec import TaskSpec
from howlplane.control_plane.evidence_ledger import (
    EvidenceEntry,
)
from howlplane.control_plane.metrics import MetricsCalculator


# ============================================================================
# 1. Phase 3: Ceiling Governance Semantics (The 5 Canonical Cases)
# ============================================================================

def test_case_1_ceiling_increase_must_fail_or_require_human():
    """
    CASE 1:
    active clones = 7, ceiling = 7
    agent introduces clone -> active clones = 8
    agent changes ceiling to 8
    RESULT: must not pass automatically (HARD_REJECT / requires human boundary).
    """
    old_ceilings = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 7}}}
    new_ceilings = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 8}}}

    changes = HygienePolicyClassifier.classify_ceilings(old_ceilings, new_ceilings)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.HARD_REJECT
    assert changes[0].target == "ceiling:python_src"
    assert changes[0].old_value == 7
    assert changes[0].new_value == 8

    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.is_hard_rejected is True
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.HARD_REJECT

    # Verify HumanBoundaryGate catches it and recommends HARD REJECT
    task = TaskSpec(task_id="TASK-C1", repository="howlplane", objective="Introduce clone and bump ceiling")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=["bump ceiling"], hygiene_policy=res)
    assert gate_res.requires_human_approval is True
    assert "hygiene_policy_violation" in gate_res.triggered_boundaries
    assert "HARD REJECT" in gate_res.decision_packet.recommended_action


def test_case_2_ceiling_decrease_autonomously_permissible_after_proof():
    """
    CASE 2:
    active clones = 7, ceiling = 7
    agent refactors duplication -> active clones = 6
    SlopsLint requires ceiling = 6
    agent updates ceiling from 7 to 6
    RESULT: autonomously permissible after deterministic proof.
    """
    old_ceilings = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 7}}}
    new_ceilings = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 6}}}

    changes = HygienePolicyClassifier.classify_ceilings(old_ceilings, new_ceilings)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.TIGHTENING
    assert changes[0].old_value == 7
    assert changes[0].new_value == 6

    res = HygienePolicyClassifier.evaluate_changes(changes, verification_passed=True)
    assert res.is_hard_rejected is False
    assert res.requires_human_approval is False
    assert res.verdict == PolicyChangeType.TIGHTENING

    # Verify HumanBoundaryGate allows autonomous completion without human friction
    task = TaskSpec(task_id="TASK-C2", repository="howlplane", objective="Refactor clone and lower ceiling")
    plan = VerificationPlan(task_id="TASK-C2")
    plan.add_step(
        step_id="step-01",
        name="Hygiene verified",
        command=["python3", "-c", "import sys; sys.exit(0)"],
        category="repository_hygiene",
    )
    plan.execute_all()
    assert plan.overall_status == "passed"

    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=["lower ceiling to 6"], verification=plan, hygiene_policy=res)
    assert gate_res.requires_human_approval is False
    assert gate_res.decision_packet is None


def test_case_3_stale_tombstone_deletion_autonomously_permissible_after_proof():
    """
    CASE 3:
    agent refactors a tombstoned duplicate
    tombstone becomes stale
    agent deletes stale tombstone
    RESULT: autonomously permissible after SlopsLint proves tombstone is stale.
    """
    old_tombstones = {
        "T-LEGACY-01": {
            "id": "T-LEGACY-01",
            "status": "accepted",
            "category": "duplication",
            "match": {"scope": "python_src", "fingerprint": "abc1234567890"},
        }
    }
    new_tombstones = {}  # Tombstone deleted

    # Stale tombstone id identified by SlopsLint diagnostics
    stale_ids = {"T-LEGACY-01"}
    changes = HygienePolicyClassifier.classify_tombstones(old_tombstones, new_tombstones, stale_tombstone_ids=stale_ids)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.TIGHTENING
    assert "Deleted stale tombstone" in changes[0].description

    res = HygienePolicyClassifier.evaluate_changes(changes, verification_passed=True)
    assert res.requires_human_approval is False
    assert res.verdict == PolicyChangeType.TIGHTENING

    task = TaskSpec(task_id="TASK-C3", repository="howlplane", objective="Remove stale tombstone")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=["delete stale tombstone"], hygiene_policy=res)
    assert gate_res.requires_human_approval is False


def test_case_4_new_tombstone_requires_human_approval():
    """
    CASE 4:
    agent creates a new tombstone for a current finding
    RESULT: AWAITING_HUMAN (DEBT_ACCEPTANCE).
    """
    old_tombstones = {}
    new_tombstones = {
        "T-NEW-01": {
            "id": "T-NEW-01",
            "status": "accepted",
            "category": "duplication",
            "match": {"scope": "python_src", "fingerprint": "deadbeef12345"},
        }
    }

    changes = HygienePolicyClassifier.classify_tombstones(old_tombstones, new_tombstones)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.DEBT_ACCEPTANCE

    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.DEBT_ACCEPTANCE

    task = TaskSpec(task_id="TASK-C4", repository="howlplane", objective="Add new tombstone")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=["add tombstone"], hygiene_policy=res)
    assert gate_res.requires_human_approval is True
    assert "slop_debt_acceptance" in gate_res.triggered_boundaries


def test_case_5_tombstone_fingerprint_modification_requires_human_approval():
    """
    CASE 5:
    agent edits an existing tombstone fingerprint to consume a new finding
    RESULT: AWAITING_HUMAN (DEBT_ACCEPTANCE).
    """
    old_tombstones = {
        "T-EXISTING": {
            "id": "T-EXISTING",
            "status": "accepted",
            "category": "duplication",
            "match": {"scope": "python_src", "fingerprint": "111111111111"},
        }
    }
    new_tombstones = {
        "T-EXISTING": {
            "id": "T-EXISTING",
            "status": "accepted",
            "category": "duplication",
            "match": {"scope": "python_src", "fingerprint": "222222222222"},
        }
    }

    changes = HygienePolicyClassifier.classify_tombstones(old_tombstones, new_tombstones)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.DEBT_ACCEPTANCE
    assert "Modified fingerprint" in changes[0].description

    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.DEBT_ACCEPTANCE

    task = TaskSpec(task_id="TASK-C5", repository="howlplane", objective="Repoint tombstone fingerprint")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=["edit tombstone"], hygiene_policy=res)
    assert gate_res.requires_human_approval is True
    assert "slop_debt_acceptance" in gate_res.triggered_boundaries


# ============================================================================
# 2. Phase 4: Config Policy Integrity
# ============================================================================

def test_config_min_lines_and_tokens_classification():
    old_cfg = {
        "schema": 1,
        "defaults": {"min_lines": 5, "min_tokens": 40, "mode": "mild"},
        "scopes": {},
    }

    # WEAKENING: Raising min_lines and min_tokens
    weakened_cfg = {
        "schema": 1,
        "defaults": {"min_lines": 10, "min_tokens": 80, "mode": "mild"},
        "scopes": {},
    }
    changes = HygienePolicyClassifier.classify_config(old_cfg, weakened_cfg)
    assert len(changes) == 2
    assert all(c.change_type == PolicyChangeType.WEAKENING for c in changes)

    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.WEAKENING

    # TIGHTENING: Lowering min_lines and min_tokens
    tightened_cfg = {
        "schema": 1,
        "defaults": {"min_lines": 3, "min_tokens": 25, "mode": "mild"},
        "scopes": {},
    }
    t_changes = HygienePolicyClassifier.classify_config(old_cfg, tightened_cfg)
    assert len(t_changes) == 2
    assert all(c.change_type == PolicyChangeType.TIGHTENING for c in t_changes)

    t_res = HygienePolicyClassifier.evaluate_changes(t_changes)
    assert t_res.requires_human_approval is False
    assert t_res.verdict == PolicyChangeType.TIGHTENING


def test_config_global_and_scope_ignore_classification():
    old_cfg = {
        "schema": 1,
        "global_ignore": ["**/venv/**"],
        "scopes": {
            "src": {
                "scan_path": "src",
                "pattern": "**/*.py",
                "ignore": [],
            }
        },
    }

    # Adding ignore pattern is WEAKENING
    weaker_cfg = {
        "schema": 1,
        "global_ignore": ["**/venv/**", "**/src/legacy/**"],
        "scopes": {
            "src": {
                "scan_path": "src",
                "pattern": "**/*.py",
                "ignore": ["**/helper.py"],
            }
        },
    }
    changes = HygienePolicyClassifier.classify_config(old_cfg, weaker_cfg)
    assert len(changes) == 2
    assert all(c.change_type == PolicyChangeType.WEAKENING for c in changes)

    # Removing ignore pattern is TIGHTENING
    tighter_cfg = {
        "schema": 1,
        "global_ignore": [],
        "scopes": {
            "src": {
                "scan_path": "src",
                "pattern": "**/*.py",
                "ignore": [],
            }
        },
    }
    t_changes = HygienePolicyClassifier.classify_config(old_cfg, tighter_cfg)
    assert len(t_changes) == 1
    assert t_changes[0].change_type == PolicyChangeType.TIGHTENING


def test_config_scope_addition_and_deletion():
    old_cfg = {
        "schema": 1,
        "scopes": {
            "src": {"scan_path": "src", "pattern": "**/*.py", "ignore": []}
        },
    }

    # Adding scope is TIGHTENING
    added_cfg = {
        "schema": 1,
        "scopes": {
            "src": {"scan_path": "src", "pattern": "**/*.py", "ignore": []},
            "scripts": {"scan_path": "scripts", "pattern": "**/*.py", "ignore": []},
        },
    }
    a_changes = HygienePolicyClassifier.classify_config(old_cfg, added_cfg)
    assert len(a_changes) == 1
    assert a_changes[0].change_type == PolicyChangeType.TIGHTENING

    # Deleting scope is WEAKENING
    d_changes = HygienePolicyClassifier.classify_config(added_cfg, old_cfg)
    assert len(d_changes) == 1
    assert d_changes[0].change_type == PolicyChangeType.WEAKENING


def test_config_deletion_is_hard_reject():
    old_cfg = {"schema": 1, "scopes": {"src": {}}}
    changes = HygienePolicyClassifier.classify_config(old_cfg, None)
    assert len(changes) == 1
    assert changes[0].change_type == PolicyChangeType.HARD_REJECT

    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.is_hard_rejected is True


# ============================================================================
# 3. Phase 5: Provider / Tool Integrity
# ============================================================================

def test_provider_integrity_verification():
    # 1. Pinned version verification on live system
    ok, msg, meta = HygienePolicyClassifier.verify_provider_integrity(
        executable_name="slopslint",
        pinned_version="0.1.0",
    )
    assert ok is True
    assert meta["status"] == "verified"
    assert meta["version"] == "0.1.0"
    assert len(meta["checksum"]) == 64

    # 2. Incompatible / mismatched pinned version must fail
    mismatch_ok, mismatch_msg, m_meta = HygienePolicyClassifier.verify_provider_integrity(
        executable_name="slopslint",
        pinned_version="9.9.9",
    )
    assert mismatch_ok is False
    assert m_meta["status"] == "version_mismatch"
    assert "version mismatch" in mismatch_msg.lower()

    # 3. Missing binary must fail
    miss_ok, miss_msg, miss_meta = HygienePolicyClassifier.verify_provider_integrity(
        executable_name="nonexistent_hygiene_tool_xyz",
    )
    assert miss_ok is False
    assert miss_meta["status"] == "missing"


def test_verification_plan_executes_with_hygiene_integrity_checks():
    plan = VerificationPlan(task_id="TASK-INTEGRITY-RUN")
    plan.add_step(
        step_id="step-hygiene",
        name="SlopsLint check",
        command=["slopslint", "check", "--classify", "--enforce"],
        category="repository_hygiene",
    )
    # Execute against current repository root
    repo_root = Path(__file__).resolve().parents[1]
    status = plan.execute_all(cwd=str(repo_root))
    assert status == "passed"
    step = plan.steps[0]
    assert step.status == "verified"
    assert step.metadata.get("hygiene_provider", {}).get("status") == "verified"
    assert step.metadata["hygiene_provider"]["version"] == "0.1.0"


# ============================================================================
# 4. Phase 7: Attack / Bypass Scenarios (Attacks A through F)
# ============================================================================

def test_attack_a_duplicate_code_plus_ceiling_increase_blocked():
    """ATTACK A: duplicate code + ceiling increase."""
    old_c = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 7}}}
    new_c = {"schema": 1, "scopes": {"python_src": {"active_clones_ceiling": 8}}}

    changes = HygienePolicyClassifier.classify_ceilings(old_c, new_c)
    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.is_hard_rejected is True

    task = TaskSpec(task_id="ATTACK-A", repository="howlplane", objective="Sneak duplicate via ceiling bump")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=[], hygiene_policy=res)
    assert gate_res.requires_human_approval is True
    assert "hygiene_policy_violation" in gate_res.triggered_boundaries


def test_attack_b_duplicate_code_plus_new_ignore_rule_blocked():
    """ATTACK B: duplicate code + new ignore rule."""
    old_cfg = {"schema": 1, "global_ignore": []}
    new_cfg = {"schema": 1, "global_ignore": ["**/duplicate_module.py"]}

    changes = HygienePolicyClassifier.classify_config(old_cfg, new_cfg)
    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.WEAKENING

    task = TaskSpec(task_id="ATTACK-B", repository="howlplane", objective="Hide duplicate via ignore pattern")
    gate_res = HumanBoundaryGate.evaluate(task, planned_actions=[], hygiene_policy=res)
    assert gate_res.requires_human_approval is True
    assert "hygiene_policy_weakening" in gate_res.triggered_boundaries


def test_attack_c_duplicate_code_plus_narrowed_scope_blocked():
    """ATTACK C: duplicate code + narrowed scope."""
    old_cfg = {"schema": 1, "scopes": {"src": {"scan_path": "src", "pattern": "**/*.py"}}}
    new_cfg = {"schema": 1, "scopes": {"src": {"scan_path": "src/subfolder", "pattern": "**/*.py"}}}

    changes = HygienePolicyClassifier.classify_config(old_cfg, new_cfg)
    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.WEAKENING


def test_attack_d_duplicate_code_plus_higher_min_tokens_blocked():
    """ATTACK D: duplicate code + higher min_tokens."""
    old_cfg = {"schema": 1, "defaults": {"min_tokens": 40}}
    new_cfg = {"schema": 1, "defaults": {"min_tokens": 100}}

    changes = HygienePolicyClassifier.classify_config(old_cfg, new_cfg)
    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.requires_human_approval is True
    assert res.verdict == PolicyChangeType.WEAKENING


def test_attack_e_duplicate_code_plus_deleting_config_blocked():
    """ATTACK E: duplicate code + deleting .slop/config.yml."""
    old_cfg = {"schema": 1, "scopes": {"src": {}}}
    changes = HygienePolicyClassifier.classify_config(old_cfg, None)
    res = HygienePolicyClassifier.evaluate_changes(changes)
    assert res.is_hard_rejected is True
    assert res.requires_human_approval is True


def test_attack_f_fake_slopslint_command_substitution_blocked():
    """ATTACK F: fake / unpinned slopslint executable substitution."""
    # Test that pointing to a nonexistent or fake tool fails closed
    fake_prov = HygienePolicyClassifier.verify_provider_integrity("fake_slopslint_executable")
    assert fake_prov[0] is False


# ============================================================================
# 5. Phase 8: Evidence Ledger Integrity
# ============================================================================

def test_evidence_ledger_hygiene_policy_actions_and_metrics():
    entries = [
        # Task 1: Tightening
        EvidenceEntry(
            task_id="TASK-EVID-01",
            agent_id="claude_code",
            action="hygiene_policy_tightened",
            metadata={"description": "Removed ignore pattern"},
        ),
        EvidenceEntry(
            task_id="TASK-EVID-01",
            agent_id="claude_code",
            action="hygiene_ceiling_lowered",
            metadata={"scope": "python_src", "old": 7, "new": 6},
        ),
        EvidenceEntry(
            task_id="TASK-EVID-01",
            agent_id="claude_code",
            action="hygiene_tombstone_removed",
            metadata={"tombstone_id": "T-STALE-01"},
        ),
        # Task 2: Blocked ceiling raise attempt
        EvidenceEntry(
            task_id="TASK-EVID-02",
            agent_id="agy",
            action="hygiene_ceiling_raise_attempted",
            metadata={"scope": "python_src", "old": 7, "new": 8},
        ),
        EvidenceEntry(
            task_id="TASK-EVID-02",
            agent_id="agy",
            action="hygiene_policy_weakening_requested",
        ),
        EvidenceEntry(
            task_id="TASK-EVID-02",
            agent_id="agy",
            action="hygiene_tombstone_added",
            metadata={"tombstone_id": "T-NEW-01"},
        ),
        EvidenceEntry(
            task_id="TASK-EVID-02",
            agent_id="agy",
            action="hygiene_provider_mismatch",
            metadata={"failure_mode": "hygiene_provider_mismatch"},
        ),
    ]

    summary = MetricsCalculator.calculate(entries)
    assert summary.hygiene_policies_tightened == 1
    assert summary.hygiene_policies_weakened == 1
    assert summary.hygiene_ceilings_lowered == 1
    assert summary.hygiene_ceiling_raise_attempts == 1
    assert summary.hygiene_tombstones_added == 1
    assert summary.hygiene_tombstones_removed == 1
    assert summary.hygiene_provider_mismatches == 1

    md = summary.render_markdown()
    assert "Ceilings lowered (debt reduced):** 1" in md
    assert "Ceiling raise attempts blocked:** 1" in md
    assert "Policy tightenings:** 1, **Weakenings requested:** 1" in md
    assert "Tombstones added (debt accepted):** 1, **Removed (cleaned):** 1" in md
    assert "Provider integrity mismatches:** 1" in md
