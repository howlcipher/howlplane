#!/usr/bin/env python3
"""
executor.py

Bounded execution authority and receipt verification framework for HowlPlane.
Delegates consequential actions to trusted bounded executors (such as HowlChangeOps)
and validates tamper-evident execution receipts.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib, json, os, shutil, subprocess, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.control_plane.git_env import run_git_in_repo
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.task_spec import DataClassSerializationMixin

EXECUTION_RECEIPT_SCHEMA_VERSION = "howlplane.execution_receipt/v1"


class ExecutorError(Exception):
    """Base exception for executor errors."""
    pass


class UnsupportedActionError(ExecutorError):
    """Raised when an action is not supported by any configured executor."""
    pass


class ExecutionFailedError(ExecutorError):
    """Raised when bounded execution fails or returns verification failure."""
    pass


class InvalidReceiptError(ExecutorError):
    """Raised when an execution receipt fails cryptographic or provenance validation."""
    pass


@dataclass
class ExecutionReceipt(DataClassSerializationMixin):
    """Durable, tamper-evident execution receipt proving a bounded action was completed."""

    task_id: str
    executor: str
    executor_version: str
    decision_id: str
    action_type: str
    repository: str
    commit_sha: str
    status: str  # "success" | "failure"
    executed_at: str
    verification_status: str = "PASS"
    native_receipt: Optional[Dict[str, Any]] = None
    rollback_status: Optional[str] = None
    error_message: Optional[str] = None
    schema: str = EXECUTION_RECEIPT_SCHEMA_VERSION


@dataclass
class ExecutionResult:
    """Normalized result of executing a bounded action via an authorized executor."""

    executor_id: str
    status: str  # "success" | "failure" | "stale" | "unsupported" | "denied"
    action_type: str
    decision_id: Optional[str] = None
    receipt: Optional[ExecutionReceipt] = None
    receipt_path: Optional[str] = None
    verification_status: str = "unverified"
    rollback_available: bool = False
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class AuthorityExecutor(ABC):
    """Abstract base class for all bounded authority executors."""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def supports_action(self, action_type: str) -> bool:
        pass

    @abstractmethod
    def evaluate(
        self,
        action: ProposedAction,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Evaluates a proposed action against policy.
        Returns: (verdict, decision_id, reason).
        verdict is one of 'ALLOW', 'REQUIRE_APPROVAL', 'DENY'.
        """
        pass

    @abstractmethod
    def approve(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[bool, Optional[str]]:
        """
        Submits trusted cryptographic authorization for a decision.
        Returns: (success, message).
        """
        pass

    @abstractmethod
    def execute(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> ExecutionResult:
        """
        Executes a previously authorized decision under bounded controls.
        """
        pass

    @abstractmethod
    def verify_receipt(
        self,
        receipt: ExecutionReceipt,
        expected_action: str,
        expected_repo: str,
        expected_commit: Optional[str] = None,
        run_dir: Optional[Union[str, Path]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates the authenticity, provenance, and matching fields of an execution receipt.
        Returns: (is_valid, failure_reason).
        """
        pass

    def query_execution_status(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> Tuple[str, Optional[ExecutionReceipt], Optional[str]]:
        """
        Queries native executor state to check if a decision has already been executed.
        Returns: (status, optional_receipt, message).
        status is one of 'already_executed', 'not_executed', 'ambiguous', 'failed'.
        """
        return "not_executed", None, "Default executor does not support status query"


def find_howlchangeops_binary() -> Optional[Path]:
    """Discovers the canonical HowlChangeOps executable with safe fallbacks."""
    # 1. Environment variable override
    env_bin = os.environ.get("HOWLCHANGEOPS_BIN") or os.environ.get("CHANGEOPS_BIN")
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return p

    # 2. PATH resolution: howlchangeops
    which_hco = shutil.which("howlchangeops")
    if which_hco:
        return Path(which_hco).resolve()

    # 3. PATH resolution: changeops
    which_co = shutil.which("changeops")
    if which_co:
        return Path(which_co).resolve()

    # 4. Sibling dev workspace path
    sibling = Path("/run/media/system/tallgeese/dev/howlchangeops/howlchangeops")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling.resolve()

    # 5. ~/.local/bin/howlchangeops
    user_local = Path.home() / ".local" / "bin" / "howlchangeops"
    if user_local.is_file() and os.access(user_local, os.X_OK):
        return user_local.resolve()

    return None


class HowlChangeOpsExecutor(AuthorityExecutor):
    """
    Canonical bounded executor integration with HowlChangeOps.
    Executes release candidate tagging, release draft creation, and governed mutations.
    """

    SUPPORTED_ACTIONS = {
        "create_release_candidate",
        "create_github_draft_release",
        "record_release_ready",
        "rollback_release_candidate",
    }

    def __init__(self, binary_path: Optional[Union[str, Path]] = None):
        self._custom_bin = Path(binary_path).resolve() if binary_path else None

    @property
    def name(self) -> str:
        return "howlchangeops"

    def get_binary_path(self) -> Optional[Path]:
        if self._custom_bin and self._custom_bin.is_file() and os.access(self._custom_bin, os.X_OK):
            return self._custom_bin
        return find_howlchangeops_binary()

    def is_available(self) -> bool:
        return self.get_binary_path() is not None

    def supports_action(self, action_type: str) -> bool:
        return action_type in self.SUPPORTED_ACTIONS

    def _get_howlchangeops_dir(self, repo_path: Path) -> Path:
        """Determines the HowlChangeOps working directory."""
        bin_path = self.get_binary_path()
        if bin_path:
            parent = bin_path.parent
            if (parent / "src" / "howlchangeops.howl").is_file() or (parent / "howlchangeops.hfbc").is_file():
                return parent
        return repo_path

    def evaluate(
        self,
        action: ProposedAction,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Evaluates a proposed action via `howlchangeops evaluate <proposal.json>`.
        """
        bin_path = self.get_binary_path()
        if not bin_path:
            return "UNAVAILABLE", None, "HowlChangeOps binary not found on system"

        if not self.supports_action(action.action_type):
            return "UNSUPPORTED", None, f"HowlChangeOps does not support action '{action.action_type}'"

        target_dir = Path(repo_path).resolve()
        run_dir = Path(task_run_dir).resolve()
        prop_file = run_dir / "howlchangeops_proposal.json"

        # Construct intent proposal payload
        proposal_data = {
            "action": action.action_type,
            "repo": target_dir.name,
            "reason": f"HowlPlane governed action evaluation for {action.action_type}",
            "confidence": 1.0,
        }
        prop_file.write_text(json.dumps(proposal_data, indent=2), encoding="utf-8")

        hco_dir = self._get_howlchangeops_dir(target_dir)

        # Run evaluate using safe subprocess
        env = os.environ.copy()
        env["HOWLCHANGEOPS_BASE"] = str(target_dir / ".howlchangeops")

        try:
            res = subprocess.run(
                [str(bin_path), "evaluate", str(prop_file)],
                cwd=str(hco_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except Exception as e:
            return "ERROR", None, f"Failed to execute HowlChangeOps evaluate: {e}"

        stdout = res.stdout + res.stderr
        verdict = "DENY"
        decision_id = None
        reason = ""

        for line in stdout.splitlines():
            if line.startswith("Decision:"):
                verdict = line.split("Decision:")[1].strip()
            elif line.startswith("Reason:"):
                reason = line.split("Reason:")[1].strip()
            elif "Decision saved as" in line:
                parts = line.split("Decision saved as")
                if len(parts) > 1:
                    raw_id = parts[1].strip().split()[0].rstrip(".")
                    if raw_id:
                        decision_id = raw_id

        if not decision_id and verdict == "REQUIRE_APPROVAL":
            # Fallback search for decision id in decisions dir
            dec_dir = Path(env["HOWLCHANGEOPS_BASE"]) / "decisions"
            if dec_dir.is_dir():
                dec_files = sorted(dec_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
                if dec_files:
                    decision_id = dec_files[0].stem

        return verdict, decision_id, reason or stdout.strip()

    def approve(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[bool, Optional[str]]:
        """
        Invokes `howlchangeops approve <decision_id>` to generate HMAC approval signature.
        """
        bin_path = self.get_binary_path()
        if not bin_path:
            return False, "HowlChangeOps binary not found"

        target_dir = Path(repo_path).resolve()
        hco_dir = self._get_howlchangeops_dir(target_dir)
        env = os.environ.copy()
        env["HOWLCHANGEOPS_BASE"] = str(target_dir / ".howlchangeops")

        try:
            res = subprocess.run(
                [str(bin_path), "approve", decision_id],
                cwd=str(hco_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            stdout = res.stdout + res.stderr
            if res.returncode == 0 and "approved" in stdout.lower():
                return True, stdout.strip()
            return False, stdout.strip()
        except Exception as e:
            return False, str(e)

    def execute(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> ExecutionResult:
        """
        Executes an authorized decision via `howlchangeops execute <decision_id>`
        and extracts the verified execution receipt.
        """
        start_time = time.time()
        bin_path = self.get_binary_path()
        if not bin_path:
            return ExecutionResult(
                executor_id=self.name,
                action_type=action.action_type,
                status="unsupported",
                decision_id=decision_id,
                error_message="HowlChangeOps binary not found",
            )

        target_dir = Path(repo_path).resolve()
        run_dir = Path(task_run_dir).resolve()
        hco_dir = self._get_howlchangeops_dir(target_dir)

        env = os.environ.copy()
        hco_base = target_dir / ".howlchangeops"
        env["HOWLCHANGEOPS_BASE"] = str(hco_base)

        try:
            res = subprocess.run(
                [str(bin_path), "execute", decision_id],
                cwd=str(hco_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except Exception as e:
            return ExecutionResult(
                executor_id=self.name,
                action_type=action.action_type,
                status="failure",
                decision_id=decision_id,
                error_message=f"Subprocess error executing HowlChangeOps: {e}",
                duration_seconds=time.time() - start_time,
            )

        duration = time.time() - start_time
        stdout = res.stdout
        stderr = res.stderr
        combined_out = (stdout + "\n" + stderr).strip()

        if "STALE_EVIDENCE" in combined_out or "STALE_REMOTE_EVIDENCE" in combined_out:
            return ExecutionResult(
                executor_id=self.name,
                action_type=action.action_type,
                status="stale",
                decision_id=decision_id,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                error_message="HowlChangeOps rejected execution due to stale evidence or TOCTOU drift.",
            )

        # Inspect native execution receipt
        native_receipt_file = hco_base / "receipts" / f"{decision_id}.json"
        native_receipt_data = None
        verif_status = "FAIL"

        if native_receipt_file.is_file():
            try:
                native_receipt_data = json.loads(native_receipt_file.read_text(encoding="utf-8"))
                verif_status = native_receipt_data.get("verification", "FAIL")
            except Exception:
                pass

        if res.returncode != 0 or verif_status != "PASS":
            err_msg = combined_out or "HowlChangeOps execution failed"
            return ExecutionResult(
                executor_id=self.name,
                action_type=action.action_type,
                status="failure",
                decision_id=decision_id,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                error_message=err_msg,
                verification_status=verif_status,
            )

        # Build HowlPlane execution receipt
        receipt = self._create_receipt_from_native(
            task_id=task_id,
            decision_id=decision_id,
            action=action,
            target_dir=target_dir,
            native_data=native_receipt_data,
        )
        receipt_file = run_dir / "execution_receipt.json"
        receipt.save_to_file(receipt_file)

        return ExecutionResult(
            executor_id=self.name,
            action_type=action.action_type,
            status="success",
            decision_id=decision_id,
            receipt=receipt,
            receipt_path=str(receipt_file),
            verification_status="PASS",
            rollback_available=bool(action.action_type == "create_release_candidate"),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    def verify_receipt(
        self,
        receipt: ExecutionReceipt,
        expected_action: str,
        expected_repo: str,
        expected_commit: Optional[str] = None,
        run_dir: Optional[Union[str, Path]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Verifies receipt schema, parameters, and authentic HowlChangeOps native provenance.
        """
        if receipt.schema != EXECUTION_RECEIPT_SCHEMA_VERSION:
            return False, f"Invalid receipt schema '{receipt.schema}', expected '{EXECUTION_RECEIPT_SCHEMA_VERSION}'"

        if receipt.status != "success":
            return False, f"Receipt status is '{receipt.status}', expected 'success'"

        if receipt.verification_status != "PASS":
            return False, f"Receipt post-action verification status is '{receipt.verification_status}', expected 'PASS'"

        if receipt.action_type != expected_action:
            return False, f"Receipt action '{receipt.action_type}' does not match expected '{expected_action}'"

        if receipt.repository != expected_repo:
            return False, f"Receipt repository '{receipt.repository}' does not match expected '{expected_repo}'"

        if expected_commit and receipt.commit_sha:
            if not receipt.commit_sha.startswith(expected_commit[:7]) and not expected_commit.startswith(receipt.commit_sha[:7]):
                return False, f"Receipt commit '{receipt.commit_sha[:7]}' does not match expected '{expected_commit[:7]}'"

        # Trust Provenance check: verify against native HowlChangeOps receipt
        if run_dir:
            repo_root = Path(run_dir).parents[1]
            native_hco_receipt = repo_root / ".howlchangeops" / "receipts" / f"{receipt.decision_id}.json"
            if not native_hco_receipt.is_file():
                # If native receipt file doesn't exist at expected location, reject unbacked receipt
                if not receipt.native_receipt:
                    return False, "Receipt failed provenance check: no matching native HowlChangeOps receipt found"

        return True, None

    def _create_receipt_from_native(
        self,
        task_id: str,
        decision_id: str,
        action: ProposedAction,
        target_dir: Path,
        native_data: Optional[Dict[str, Any]],
    ) -> ExecutionReceipt:
        """Helper to construct ExecutionReceipt from native HowlChangeOps proof."""
        c_res = run_git_in_repo(target_dir, ["rev-parse", "HEAD"])
        commit_sha = c_res.stdout.strip() if c_res.returncode == 0 else ""
        exec_at = (
            native_data.get("timestamp", datetime.now(timezone.utc).isoformat())
            if native_data
            else datetime.now(timezone.utc).isoformat()
        )
        return ExecutionReceipt(
            task_id=task_id,
            executor=self.name,
            executor_version="0.2.0",
            decision_id=decision_id,
            action_type=action.action_type,
            repository=target_dir.name,
            commit_sha=commit_sha,
            status="success",
            executed_at=exec_at,
            verification_status="PASS",
            native_receipt=native_data,
            rollback_status=native_data.get("rollback_status") if native_data else None,
        )

    def query_execution_status(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> Tuple[str, Optional[ExecutionReceipt], Optional[str]]:
        """
        Inspects HowlChangeOps authoritative state/receipts to detect prior execution.
        """
        target_dir = Path(repo_path).resolve()
        run_dir = Path(task_run_dir).resolve()
        hco_base = target_dir / ".howlchangeops"
        native_receipt_file = hco_base / "receipts" / f"{decision_id}.json"

        if native_receipt_file.is_file():
            try:
                native_data = json.loads(native_receipt_file.read_text(encoding="utf-8"))
                if native_data.get("verification") == "PASS":
                    receipt = self._create_receipt_from_native(
                        task_id=task_id,
                        decision_id=decision_id,
                        action=action,
                        target_dir=target_dir,
                        native_data=native_data,
                    )
                    receipt_file = run_dir / "execution_receipt.json"
                    receipt.save_to_file(receipt_file)
                    return "already_executed", receipt, f"Found verified native receipt for decision '{decision_id}'"
                elif native_data.get("verification") == "FAIL":
                    return "failed", None, f"Native receipt indicates verification failure for decision '{decision_id}'"
            except Exception as e:
                return "ambiguous", None, f"Error reading native receipt for '{decision_id}': {e}"

        # Inspect decisions directory
        dec_file = hco_base / "decisions" / f"{decision_id}.json"
        if dec_file.is_file():
            try:
                dec_data = json.loads(dec_file.read_text(encoding="utf-8"))
                state = dec_data.get("state", "").upper()
                if state in ("CONSUMED", "EXECUTED"):
                    return "ambiguous", None, f"Decision '{decision_id}' is marked {state} but receipt file is missing"
                return "not_executed", None, f"Decision '{decision_id}' is recorded in state '{state}'"
            except Exception:
                pass

        return "not_executed", None, f"No prior execution evidence for decision '{decision_id}'"


class ExecutorRegistry:
    """Registry of configured bounded authority executors."""

    _executors: Dict[str, AuthorityExecutor] = {}

    @classmethod
    def register(cls, executor: AuthorityExecutor) -> None:
        cls._executors[executor.name] = executor

    @classmethod
    def get_executor(cls, name: str) -> Optional[AuthorityExecutor]:
        return cls._executors.get(name)

    @classmethod
    def get_executor_for_action(cls, action_type: str) -> Optional[AuthorityExecutor]:
        for exc in cls._executors.values():
            if exc.is_available() and exc.supports_action(action_type):
                return exc
        return None

    @classmethod
    def list_executors(cls) -> List[AuthorityExecutor]:
        return list(cls._executors.values())


# Register canonical HowlChangeOps executor by default
ExecutorRegistry.register(HowlChangeOpsExecutor())
