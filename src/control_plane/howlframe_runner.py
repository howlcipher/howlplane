#!/usr/bin/env python3
"""
howlframe_runner.py

HowlFrame project context audit runner and dogfooding integration adapter.
Executes the fixed HowlFrame audit program in capability-bounded shadow mode,
evaluates invariants, detects disagreements against ProjectAdapter facts,
and records structured evidence to the durable ledger.
"""

from dataclasses import dataclass, field, asdict
import json, os, shutil, subprocess, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger, sanitize_value
from src.control_plane.project_adapter import ProjectContext
from src.control_plane.task_spec import DataClassSerializationMixin

PROJECT_CONTEXT_SCHEMA_VERSION = "howlplane.project_context/v1"
PROJECT_CONTEXT_AUDIT_SCHEMA_VERSION = "howlplane.project_context_audit/v1"
DEFAULT_INSTRUCTION_BUDGET = 100000
MAX_INPUT_PAYLOAD_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 10.0

HOWLFRAME_STATUS_NOT_COMPUTED = "NOT_COMPUTED"

VALID_COMPARISON_STATUSES = {
    "MATCH",
    "MISMATCH",
    "HOWLFRAME_FAILURE",
    "HOWLFRAME_UNAVAILABLE",
    "INVALID_OUTPUT",
    "BUDGET_EXCEEDED",
    "TIMEOUT",
    HOWLFRAME_STATUS_NOT_COMPUTED,
}


@dataclass
class AuditRunResult(DataClassSerializationMixin):
    """Structured execution outcome of a HowlFrame project context audit."""

    status: str
    audit_status: Optional[str] = None
    findings: List[str] = field(default_factory=list)
    observed: Dict[str, Any] = field(default_factory=dict)
    comparison_notes: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    instruction_budget: int = DEFAULT_INSTRUCTION_BUDGET
    howlframe_bin: Optional[str] = None
    howlframe_version: Optional[str] = None
    error_message: Optional[str] = None
    raw_output: Optional[str] = None
    input_payload: Dict[str, Any] = field(default_factory=dict)
    context_schema_version: str = PROJECT_CONTEXT_SCHEMA_VERSION
    audit_schema_version: str = PROJECT_CONTEXT_AUDIT_SCHEMA_VERSION


def get_dogfood_mode() -> str:
    """Returns the configured HowlFrame dogfood mode: 'off' | 'shadow'."""
    env_val = os.environ.get("HOWLPLANE_HOWLFRAME_DOGFOOD") or os.environ.get("HOWLFRAME_DOGFOOD")
    if env_val:
        return "shadow" if env_val.strip().lower() in ("shadow", "on", "true", "1") else "off"

    cfg_file = Path.home() / ".config" / "howlplane" / "config.toml"
    if cfg_file.is_file():
        try:
            import tomllib
            with open(cfg_file, "rb") as f:
                data = tomllib.load(f)
            df = data.get("dogfood", {}).get("howlframe", "off")
            if str(df).lower() in ("shadow", "on", "true", "1"):
                return "shadow"
        except Exception:
            pass
    return "off"


def find_howlframe_binary() -> Optional[str]:
    """Discovers the howlframe binary executable path via env or PATH."""
    env_bin = os.environ.get("HOWLFRAME_BIN")
    if env_bin:
        p = Path(env_bin).expanduser().resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None

    which_bin = shutil.which("howlframe")
    if which_bin:
        p = Path(which_bin).resolve()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def get_howlframe_version(howlframe_bin: str) -> Optional[str]:
    """Inspects the version of the howlframe binary."""
    try:
        res = subprocess.run(
            [howlframe_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


def normalize_project_context(context: ProjectContext) -> Dict[str, Any]:
    """Constructs the minimal versioned howlplane.project_context/v1 payload."""
    payload = {
        "schema": PROJECT_CONTEXT_SCHEMA_VERSION,
        "project_name": str(context.name),
        "project_types": [str(t) for t in context.project_types],
        "has_agents_md": bool(context.has_agents_md),
        "has_manifest": bool(context.has_manifest),
        "verification": {
            "test_count": len(context.test_commands),
            "build_count": len(context.build_commands),
            "lint_count": len(context.lint_commands),
            "hygiene_count": len(context.hygiene_commands),
        },
        "hygiene_status": str(context.hygiene_status),
        "declared_capabilities": [str(c) for c in context.capabilities],
    }
    return sanitize_value(payload)


class HowlFrameAuditRunner:
    """Runner for capability-bounded HowlFrame project context audit program."""

    @classmethod
    def resolve_artifact_path(cls) -> Path:
        repo_root = Path(__file__).resolve().parents[2]
        bytecode_path = repo_root / "integrations" / "howlframe" / "project_context_audit.hfbc"
        if not bytecode_path.is_file():
            source_path = repo_root / "integrations" / "howlframe" / "project_context_audit.howl"
            if source_path.is_file():
                h_bin = find_howlframe_binary()
                if h_bin:
                    bytecode_path.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        [h_bin, "build", str(source_path), "-o", str(bytecode_path)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
        return bytecode_path

    @classmethod
    def compare_facts(
        cls,
        context: ProjectContext,
        audit_res: Dict[str, Any],
    ) -> Tuple[str, List[str]]:
        notes: List[str] = []
        observed = audit_res.get("observed", {})
        audit_status = audit_res.get("status")

        field_comparisons = [
            ("project_name", context.name, observed.get("project_name")),
            ("has_agents_md", context.has_agents_md, observed.get("has_agents_md")),
            ("has_manifest", context.has_manifest, observed.get("has_manifest")),
            ("hygiene_status", context.hygiene_status, observed.get("hygiene_status")),
            (
                "verification_surfaces",
                len(context.test_commands) + len(context.build_commands) + len(context.lint_commands) + len(context.hygiene_commands),
                observed.get("verification_surfaces"),
            ),
            ("capabilities_count", len(context.capabilities), observed.get("capabilities_count")),
        ]

        for fname, expected, actual in field_comparisons:
            if expected != actual:
                notes.append(f"{fname} mismatch: context={expected!r} vs observed={actual!r}")

        obs_types = observed.get("project_types")
        if isinstance(obs_types, list):
            if sorted(obs_types) != sorted(context.project_types):
                notes.append(f"project_types mismatch: context={context.project_types} vs observed={obs_types}")
        else:
            notes.append(f"project_types invalid in observed: {obs_types}")

        if notes:
            return "MISMATCH", notes
        if audit_status == "FAIL":
            notes.append("HowlFrame audit returned FAIL status on valid context data")
            return "MISMATCH", notes
        return "MATCH", notes

    @classmethod
    def _create_result(
        cls,
        status: str,
        start_time: float,
        instruction_budget: int,
        howlframe_bin: Optional[str] = None,
        howlframe_version: Optional[str] = None,
        error_message: Optional[str] = None,
        raw_output: Optional[str] = None,
        input_payload: Optional[Dict[str, Any]] = None,
        audit_status: Optional[str] = None,
        findings: Optional[List[str]] = None,
        observed: Optional[Dict[str, Any]] = None,
        comparison_notes: Optional[List[str]] = None,
    ) -> AuditRunResult:
        return AuditRunResult(
            status=status,
            audit_status=audit_status,
            findings=findings or [],
            observed=observed or {},
            comparison_notes=comparison_notes or [],
            duration_seconds=round(time.time() - start_time, 4),
            instruction_budget=instruction_budget,
            howlframe_bin=howlframe_bin,
            howlframe_version=howlframe_version,
            error_message=error_message,
            raw_output=raw_output,
            input_payload=input_payload or {},
        )

    @classmethod
    def run_audit(
        cls,
        context: ProjectContext,
        max_instructions: int = DEFAULT_INSTRUCTION_BUDGET,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        record_evidence: bool = True,
        ledger: Optional[EvidenceLedger] = None,
        task_id: Optional[str] = None,
        dogfood_mode: Optional[str] = None,
    ) -> AuditRunResult:
        start_time = time.time()
        mode = dogfood_mode or get_dogfood_mode()
        h_bin = find_howlframe_binary()
        h_ver = get_howlframe_version(h_bin) if h_bin else None
        artifact_path = cls.resolve_artifact_path()

        normalized_input = normalize_project_context(context)
        input_json = json.dumps(normalized_input)
        if len(input_json.encode("utf-8")) > MAX_INPUT_PAYLOAD_BYTES:
            res = cls._create_result(
                status="HOWLFRAME_FAILURE",
                start_time=start_time,
                instruction_budget=max_instructions,
                howlframe_bin=h_bin,
                howlframe_version=h_ver,
                error_message=f"Normalized input payload exceeds size limit ({len(input_json)} bytes > {MAX_INPUT_PAYLOAD_BYTES})",
                input_payload=normalized_input,
            )
            if record_evidence:
                cls._record_evidence(res, context, task_id, ledger, mode)
            return res

        if not h_bin:
            res = cls._create_result(
                status="HOWLFRAME_UNAVAILABLE",
                start_time=start_time,
                instruction_budget=max_instructions,
                error_message="howlframe executable not found on PATH or via HOWLFRAME_BIN",
                input_payload=normalized_input,
            )
            if record_evidence:
                cls._record_evidence(res, context, task_id, ledger, mode)
            return res

        if not artifact_path.is_file():
            res = cls._create_result(
                status="HOWLFRAME_FAILURE",
                start_time=start_time,
                instruction_budget=max_instructions,
                howlframe_bin=h_bin,
                howlframe_version=h_ver,
                error_message=f"HowlFrame audit artifact not found at {artifact_path}",
                input_payload=normalized_input,
            )
            if record_evidence:
                cls._record_evidence(res, context, task_id, ledger, mode)
            return res

        cmd = [
            h_bin,
            "run",
            "--max-instructions",
            str(max_instructions),
            str(artifact_path),
            input_json,
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()
            raw_output = f"{stdout}\n{stderr}".strip()

            if "LIMIT_EXCEEDED" in raw_output:
                res = cls._create_result(
                    status="BUDGET_EXCEEDED",
                    start_time=start_time,
                    instruction_budget=max_instructions,
                    howlframe_bin=h_bin,
                    howlframe_version=h_ver,
                    error_message="HowlFrame instruction budget exceeded",
                    raw_output=raw_output,
                    input_payload=normalized_input,
                )
            elif "CAPABILITY_DENIED" in raw_output:
                res = cls._create_result(
                    status="HOWLFRAME_FAILURE",
                    start_time=start_time,
                    instruction_budget=max_instructions,
                    howlframe_bin=h_bin,
                    howlframe_version=h_ver,
                    error_message="HowlFrame capability denied",
                    raw_output=raw_output,
                    input_payload=normalized_input,
                )
            else:
                audit_data = None
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{") and "howlplane.project_context_audit" in line:
                        try:
                            audit_data = json.loads(line)
                            break
                        except Exception:
                            pass
                if not audit_data:
                    try:
                        audit_data = json.loads(stdout)
                    except Exception:
                        pass

                if not audit_data or not isinstance(audit_data, dict):
                    res = cls._create_result(
                        status="INVALID_OUTPUT" if proc.returncode == 0 else "HOWLFRAME_FAILURE",
                        start_time=start_time,
                        instruction_budget=max_instructions,
                        howlframe_bin=h_bin,
                        howlframe_version=h_ver,
                        error_message=stderr or "Unable to parse structured JSON from HowlFrame output",
                        raw_output=raw_output,
                        input_payload=normalized_input,
                    )
                elif (
                    audit_data.get("schema") != PROJECT_CONTEXT_AUDIT_SCHEMA_VERSION
                    or audit_data.get("status") not in ("PASS", "WARN", "FAIL")
                ):
                    res = cls._create_result(
                        status="INVALID_OUTPUT",
                        start_time=start_time,
                        instruction_budget=max_instructions,
                        howlframe_bin=h_bin,
                        howlframe_version=h_ver,
                        error_message="Output does not conform to howlplane.project_context_audit/v1 schema",
                        raw_output=raw_output,
                        input_payload=normalized_input,
                    )
                else:
                    comp_status, notes = cls.compare_facts(context, audit_data)
                    res = cls._create_result(
                        status=comp_status,
                        start_time=start_time,
                        instruction_budget=max_instructions,
                        howlframe_bin=h_bin,
                        howlframe_version=h_ver,
                        raw_output=stdout,
                        input_payload=normalized_input,
                        audit_status=audit_data.get("status"),
                        findings=audit_data.get("findings", []),
                        observed=audit_data.get("observed", {}),
                        comparison_notes=notes,
                    )

        except subprocess.TimeoutExpired:
            res = cls._create_result(
                status="TIMEOUT",
                start_time=start_time,
                instruction_budget=max_instructions,
                howlframe_bin=h_bin,
                howlframe_version=h_ver,
                error_message=f"HowlFrame audit execution timed out after {timeout}s",
                input_payload=normalized_input,
            )
        except Exception as exc:
            res = cls._create_result(
                status="HOWLFRAME_FAILURE",
                start_time=start_time,
                instruction_budget=max_instructions,
                howlframe_bin=h_bin,
                howlframe_version=h_ver,
                error_message=f"Subprocess execution error: {exc}",
                input_payload=normalized_input,
            )

        if record_evidence:
            cls._record_evidence(res, context, task_id, ledger, mode)
        return res

    @classmethod
    def _record_evidence(
        cls,
        result: AuditRunResult,
        context: ProjectContext,
        task_id: Optional[str],
        ledger: Optional[EvidenceLedger],
        mode: str,
    ) -> None:
        try:
            target_ledger = ledger or EvidenceLedger()
            entry = EvidenceEntry(
                task_id=task_id or f"AUDIT-{context.name.upper()}",
                agent_id="howlframe",
                action="howlframe_project_context_audit",
                repository=context.name,
                result=result.status,
                artifact="integrations/howlframe/project_context_audit.hfbc",
                command=f"howlframe run --max-instructions {result.instruction_budget} project_context_audit.hfbc",
                metadata={
                    "dogfood_mode": mode,
                    "comparison_status": result.status,
                    "audit_status": result.audit_status,
                    "findings": result.findings,
                    "comparison_notes": result.comparison_notes,
                    "duration_seconds": result.duration_seconds,
                    "instruction_budget": result.instruction_budget,
                    "howlframe_bin": result.howlframe_bin,
                    "howlframe_version": result.howlframe_version,
                    "context_schema": result.context_schema_version,
                    "audit_schema": result.audit_schema_version,
                    "error_message": result.error_message,
                },
            )
            target_ledger.append_entry(entry)
        except Exception:
            pass
