"""The one governed consumer of an accepted repository proposal (#82).

``approve --proposal`` only records an owner decision. This module is the single
path that may act on it, and it is deliberately narrow:

* the proposal must be ``accepted`` and the contract it carries must hash to the
  fingerprint the owner approved, both on the proposal and in the append-only
  evidence ledger. A contract changed after approval is refused;
* v1 scaffolds a LOCAL repository only (directory, README, mission file,
  minimal source, baseline test, ``git init``). It never creates a remote,
  touches credentials, publishes, or adds external dependencies; a contract that
  asks for any of that is refused as needing owner authority;
* the new repository is not trusted because files were written: the scaffold's
  own deterministic test must pass before the run completes and before the
  capability is registered as verified;
* execution state lives in a separate ``bootstrap_runs`` record so it is never
  confused with the owner's decision, and a completed run cannot run twice.
"""

import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from howlplane.control_plane.durable_store import DurableObjectStore
from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from howlplane.control_plane.factory import owner_decision as od
from howlplane.control_plane.factory.proposal_decision import EVIDENCE_ACTION as DECISION_ACTION
from howlplane.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityStore,
    ProposalState,
    RepoProposal,
    RepoProposalStore,
    VerificationStatus,
    contract_fingerprint,
)
from howlplane.control_plane.git_env import run_git_in_repo
from howlplane.control_plane.task_spec import DataClassSerializationMixin

BOOTSTRAP_RUN_SCHEMA_VERSION = "howlplane.factory.bootstrap_run/v1"
MARKER_FILE = ".howlplane-bootstrap.json"
AGENT_ID = "factory_bootstrap"

STARTED, COMPLETED, FAILED = "bootstrap_started", "bootstrap_completed", "bootstrap_failed"

SUPPORTED_LANGUAGES = ("python", "go")
_PLAN_KEYS = {
    "capability_id", "consumer_repositories", "clear_purpose", "bounded_maintenance",
    "deterministic_verification", "language",
}
# A contract that asks for any of these needs authority this path does not have.
_AUTHORITY_KEYS = {
    "create_remote", "remote", "remote_url", "visibility", "organization", "org", "credentials",
    "secrets", "publish", "package_publishing", "external_dependencies", "paid_resources",
    "branch_protection", "messaging",
}
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_VERIFY_TIMEOUT_SECONDS = 120

Verifier = Callable[[Path, str], Dict[str, Any]]


class BootstrapError(od.OwnerDecisionError):
    """The bootstrap cannot proceed; ``code`` is a stable operator error code."""


@dataclass
class BootstrapRun(DataClassSerializationMixin):
    """Durable execution record for one proposal, separate from the proposal's own state."""

    proposal_id: str
    execution_id: str
    state: str
    contract_sha256: str
    repository_name: str
    target_path: str
    decision_entry_id: str = ""
    started_at: str = ""
    finished_at: Optional[str] = None
    verification: Dict[str, Any] = field(default_factory=dict)
    capability_id: Optional[str] = None
    error: Optional[str] = None
    schema_version: str = BOOTSTRAP_RUN_SCHEMA_VERSION


class BootstrapRunStore(DurableObjectStore):
    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(base_dir, factory=BootstrapRun.from_dict, dedup_field=None, id_attr="proposal_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_evidence(ledger: EvidenceLedger, proposal_id: str, result: str, metadata: Dict[str, Any], *,
                     action: str) -> None:
    ledger.append_entry(EvidenceEntry(
        task_id=proposal_id, agent_id=AGENT_ID, action=action, result=result,
        metadata={"authority": "accepted_proposal_local_scaffold_only", **metadata}))


def _refuse(ledger: Optional[EvidenceLedger], proposal_id: str, code: str, message: str, why: str,
            next_action: str, command: Optional[str] = None, **meta: Any) -> BootstrapError:
    """Build the error, recording that the bootstrap was refused (never raises itself)."""
    if ledger is not None:
        _record_evidence(ledger, proposal_id, code, {"reason": message, **meta}, action="bootstrap_refused")
    return BootstrapError(code, message, why, next_action, command)


# ---- contract validation ---------------------------------------------------

def validate_contract(proposal: RepoProposal) -> List[str]:
    """Reasons the contract is malformed (empty when it is bootstrappable)."""
    problems: List[str] = []
    if not _REPO_NAME.fullmatch(proposal.repository_name or ""):
        problems.append("repository_name must be 1-63 characters of letters, digits, '-' or '_'")
    plan = proposal.bootstrap_plan
    if not isinstance(plan, dict):
        return problems + ["bootstrap_plan must be an object"]
    if plan.get("language") not in SUPPORTED_LANGUAGES:
        problems.append(f"bootstrap_plan.language must be one of {list(SUPPORTED_LANGUAGES)}")
    if not plan.get("capability_id"):
        problems.append("bootstrap_plan.capability_id is required")
    if plan.get("deterministic_verification") is not True:
        problems.append("bootstrap_plan.deterministic_verification must be true")
    if not isinstance(plan.get("consumer_repositories", []), list):
        problems.append("bootstrap_plan.consumer_repositories must be a list")
    unknown = sorted(set(plan) - _PLAN_KEYS - _AUTHORITY_KEYS)
    if unknown:
        problems.append(f"bootstrap_plan has unknown keys {unknown}")
    return problems


def _authority_requests(proposal: RepoProposal) -> List[str]:
    plan = proposal.bootstrap_plan if isinstance(proposal.bootstrap_plan, dict) else {}
    return sorted(k for k in plan if k in _AUTHORITY_KEYS and plan.get(k))


# ---- approved-decision evidence --------------------------------------------

def _approved_decision(ledger: EvidenceLedger, proposal_id: str) -> Optional[EvidenceEntry]:
    decisions = [
        e for e in ledger.get_task_entries(proposal_id)
        if e.action == DECISION_ACTION and e.human_decision in (od.APPROVED, od.REJECTED)
    ]
    return decisions[-1] if decisions and decisions[-1].human_decision == od.APPROVED else None


# ---- scaffolding -----------------------------------------------------------

def _scaffold_files(proposal: RepoProposal, fingerprint: str) -> Dict[str, str]:
    name, plan = proposal.repository_name, proposal.bootstrap_plan
    mission = (
        f"# Bootstrap contract: {name}\n\n"
        f"- Proposal: `{proposal.proposal_id}`\n"
        f"- Contract fingerprint (sha256): `{fingerprint}`\n"
        f"- Capability: `{plan.get('capability_id')}`\n"
        f"- Consumers: {', '.join(plan.get('consumer_repositories') or []) or 'none recorded'}\n"
        f"- Language: {plan['language']}\n\n"
        "Generated by the HowlPlane governed bootstrap. Local scaffold only; no remote was created.\n"
    )
    files = {"README.md": f"# {name}\n\nBootstrapped from proposal `{proposal.proposal_id}`.\n",
             "BOOTSTRAP.md": mission, ".gitignore": "__pycache__/\n*.pyc\n"}
    if plan["language"] == "python":
        pkg = name.replace("-", "_")
        files.update({
            f"{pkg}/__init__.py": '"""Bootstrapped package."""\n\n\ndef ready() -> bool:\n    return True\n',
            "tests/__init__.py": "",
            "tests/test_baseline.py": (
                f"import unittest\n\nimport {pkg}\n\n\nclass Baseline(unittest.TestCase):\n"
                f"    def test_ready(self):\n        self.assertTrue({pkg}.ready())\n\n\n"
                "if __name__ == '__main__':\n    unittest.main()\n"),
        })
    else:
        files.update({
            "go.mod": f"module {name}\n\ngo 1.21\n",
            "ready.go": "package main\n\nfunc ready() bool { return true }\n\nfunc main() {}\n",
            "ready_test.go": 'package main\n\nimport "testing"\n\nfunc TestReady(t *testing.T) {\n\tif !ready() {\n\t\tt.Fatal("not ready")\n\t}\n}\n',
        })
    return files


def _write_scaffold(target: Path, files: Dict[str, str]) -> None:
    for rel, content in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _git_init(target: Path) -> None:
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=HowlPlane", "-c", "user.email=howlplane@localhost",
                  "commit", "-q", "-m", "Bootstrap from accepted repository proposal"]):
        res = run_git_in_repo(target, args)
        if res.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {res.stderr.strip()[:300]}")


def default_verifier(target: Path, language: str) -> Dict[str, Any]:
    """Run the scaffold's own baseline test. Never raises; failure is data."""
    command = ([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
               if language == "python" else ["go", "test", "./..."])
    try:
        res = subprocess.run(command, cwd=target, capture_output=True, text=True, timeout=_VERIFY_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "command": command, "exit_code": None, "output_tail": str(exc)[:500]}
    return {"status": "passed" if res.returncode == 0 else "failed", "command": command,
            "exit_code": res.returncode, "output_tail": (res.stdout + res.stderr)[-500:]}


# ---- the consumer ----------------------------------------------------------

def bootstrap_proposal(
    state_dir: Union[str, Path],
    proposal_id: str,
    *,
    ledger: Optional[EvidenceLedger],
    target_root: Optional[Union[str, Path]] = None,
    retry: bool = False,
    verifier: Verifier = default_verifier,
) -> Dict[str, Any]:
    """Consume one accepted, unchanged proposal by scaffolding and verifying a local repository."""
    state = Path(state_dir).resolve()
    proposals = RepoProposalStore(state / "repo_proposals")
    runs = BootstrapRunStore(state / "bootstrap_runs")
    cmd = f"howlplane factory bootstrap --proposal {proposal_id} --state-dir {state}"

    if not proposals.exists(proposal_id):
        raise od.not_found_error(BootstrapError, "PROPOSAL_NOT_FOUND", "repository proposal", proposal_id, state)
    try:
        proposal: RepoProposal = proposals.load(proposal_id)
    except Exception as exc:
        raise BootstrapError(
            "PROPOSAL_MALFORMED", f"Repository proposal '{proposal_id}' cannot be read: {exc}",
            "The stored record is damaged, so nothing was created.",
            "Inspect the proposal file in the Factory state directory.", "howlplane factory status --verbose")

    if proposal.state != ProposalState.ACCEPTED.value:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_PROPOSAL_NOT_ACCEPTED",
            f"Repository proposal '{proposal_id}' is '{proposal.state}', not accepted.",
            "Only a proposal the owner approved can be bootstrapped.",
            "Ask the owner to decide it, then bootstrap.", "howlplane factory status", state=proposal.state)

    existing: Optional[BootstrapRun] = runs.load(proposal_id) if runs.exists(proposal_id) else None
    if existing and existing.state == COMPLETED:
        raise BootstrapError(
            "BOOTSTRAP_ALREADY_COMPLETED", f"Proposal '{proposal_id}' was already bootstrapped at {existing.target_path}.",
            "A completed bootstrap is never executed twice.", "Use the repository that was created.")

    problems = validate_contract(proposal)
    if problems:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_CONTRACT_MALFORMED", f"The bootstrap contract is malformed: {'; '.join(problems)}.",
            "A contract this path cannot understand is not executed.",
            "Reject this proposal and have a corrected one proposed.", "howlplane factory status", problems=problems)
    requested = _authority_requests(proposal)
    if requested:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_AUTHORITY_REQUIRED",
            f"The contract requests {requested}, which acceptance does not authorize.",
            "This path only creates a local scaffold. Remote creation, credentials, publishing and "
            "external dependencies need their own owner authority.",
            "Nothing was created. Handle those steps through their own governed path.", "howlplane factory status",
            requested=requested)

    fingerprint = contract_fingerprint(proposal)
    decision = _approved_decision(ledger, proposal_id) if ledger is not None else None
    approved = decision.metadata.get("bootstrap_contract_sha256") if decision else None
    if decision is None or not approved or not proposal.approved_contract_sha256:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_DECISION_EVIDENCE_MISSING",
            "There is no recorded owner approval for this contract's fingerprint.",
            "The fingerprint the owner approved must be in the evidence ledger before anything is created.",
            "Re-approve the proposal with the same ledger.", f"howlplane approve --proposal {proposal_id} --state-dir {state}")
    if fingerprint != approved or fingerprint != proposal.approved_contract_sha256:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_CONTRACT_CHANGED",
            "The bootstrap contract changed after the owner approved it.",
            "What would be created is not what was approved.",
            "Nothing was created. Reject this proposal and have the changed contract proposed and decided again.",
            f"howlplane reject --proposal {proposal_id} --state-dir {state}",
            approved_sha256=approved, current_sha256=fingerprint)

    root = Path(target_root).resolve() if target_root else state / "bootstrapped_repositories"
    target = root / proposal.repository_name
    marker = target / MARKER_FILE
    resuming = False
    if existing and existing.state == STARTED and target.is_dir() and not target.is_symlink():
        try:
            resuming = json.loads(marker.read_text(encoding="utf-8")).get("contract_sha256") == fingerprint
        except (OSError, ValueError):
            resuming = False
    if existing and existing.state == FAILED and not retry:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_PREVIOUSLY_FAILED", f"A previous bootstrap failed: {existing.error}.",
            "A failed bootstrap is retried only on purpose.",
            f"Remove {existing.target_path}, then retry.", f"{cmd} --retry")
    if target.is_symlink() or (target.exists() and not resuming) or target.parent != root:
        raise _refuse(
            ledger, proposal_id, "BOOTSTRAP_TARGET_CONFLICT", f"The target {target} already exists or is unsafe.",
            "A bootstrap never overwrites or adopts an existing path.",
            "Choose another --target-root or remove the leftover directory.", cmd, target=str(target))
    root.mkdir(parents=True, exist_ok=True)

    run = BootstrapRun(
        proposal_id=proposal_id, execution_id=existing.execution_id if resuming and existing else f"BOOT-{uuid.uuid4().hex[:12]}",
        state=STARTED, contract_sha256=fingerprint, repository_name=proposal.repository_name,
        target_path=str(target), decision_entry_id=decision.entry_id, started_at=_now(),
        capability_id=proposal.bootstrap_plan.get("capability_id"))
    runs.save_object(run)
    context = {"execution_id": run.execution_id, "contract_sha256": fingerprint,
               "repository_name": proposal.repository_name, "decision_entry_id": decision.entry_id,
               "target_path": str(target), "resumed": resuming}
    _record_evidence(ledger, proposal_id, "started", context, action="bootstrap_started")

    verification: Dict[str, Any] = {}
    try:
        target.mkdir(exist_ok=resuming)
        _write_scaffold(target, _scaffold_files(proposal, fingerprint))
        marker.write_text(json.dumps({"contract_sha256": fingerprint, "execution_id": run.execution_id}), encoding="utf-8")
        if not (target / ".git").exists():
            _git_init(target)
        verification = verifier(target, proposal.bootstrap_plan["language"])
        error = None if verification.get("status") == "passed" else f"verification {verification.get('status')}"
    except Exception as exc:  # failure is recorded truthfully, never reported as success
        error = f"{type(exc).__name__}: {exc}"

    run.finished_at, run.verification, run.error = _now(), verification, error
    if error:
        run.state = FAILED
        runs.save_object(run)
        _record_evidence(ledger, proposal_id, "failed", {**context, "error": error, "verification": verification},
                         action="bootstrap_failed")
        return {"proposal_id": proposal_id, "state": FAILED, "execution_id": run.execution_id, "error": error,
                "repository_name": proposal.repository_name, "target_path": str(target), "verification": verification}

    plan = proposal.bootstrap_plan
    registry = CapabilityRegistry(CapabilityStore(state / "capabilities"))
    record = registry.find(plan["capability_id"]) or CapabilityRecord(
        capability_id=plan["capability_id"], name=proposal.repository_name)
    record.provided_by = sorted(set(record.provided_by) | {proposal.repository_name})
    record.required_by = sorted(set(record.required_by) | set(plan.get("consumer_repositories") or []))
    record.evidence_fingerprints = sorted(set(record.evidence_fingerprints) | set(proposal.evidence_fingerprints))
    record.verification_status = VerificationStatus.VERIFIED.value
    record.verified_by = f"{AGENT_ID}:{run.execution_id}"
    record.last_verified_at = _now()
    registry.register(record)

    run.state = COMPLETED
    runs.save_object(run)
    _record_evidence(ledger, proposal_id, "completed", {
        **context, "verification": verification, "capability_id": record.capability_id,
        "capability_verification_status": record.verification_status}, action="bootstrap_completed")
    return {"proposal_id": proposal_id, "state": COMPLETED, "execution_id": run.execution_id,
            "repository_name": proposal.repository_name, "target_path": str(target),
            "capability_id": record.capability_id, "verification": verification}


def bootstrap_command(proposal_id: str, state_dir: str) -> str:
    """The exact command that consumes an accepted proposal."""
    import shlex
    return f"howlplane factory bootstrap --proposal {shlex.quote(proposal_id)} --state-dir {shlex.quote(str(state_dir))}"


__all__ = ["bootstrap_proposal", "bootstrap_command", "validate_contract", "BootstrapError", "BootstrapRun",
           "BootstrapRunStore", "default_verifier", "STARTED", "COMPLETED", "FAILED"]
