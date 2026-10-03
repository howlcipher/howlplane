"""`howlplane creative`: native Dream -> Writer -> Create orchestration.

Plane runs each component through its own supported CLI (``python -m
howldream.cli``, ``python -m howlwriter``, ``python -m howlcreate.cli``) and
passes only the artifacts those components define:

    Dream   howl.candidate/v1            (explore, export, or cluster external)
    Writer  howlwriter.request/v1 -> howlwriter.copy_package/v1
    Create  howl.development_result/v1 -> sandbox artifacts + manifest

No stage reshapes another component's output. Plane's own contribution is
orchestration: it persists every stage's output in an explicit run
directory, records per-stage state so ``creative resume`` continues from
the first incomplete stage, reports failures by stage with the partial
outputs kept, and writes ``contribution-audit.json`` naming who generated,
transformed, selected, designed and materialized each artifact.

Plane never imports the components, never runs git, and never writes outside
the run directory and the sandbox the operator names (which Create guards).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from howlplane.control_plane.atomic_io import atomic_write_json

STATE_SCHEMA = "howlplane.creative_run/v1"
AUDIT_SCHEMA = "howlplane.creative_contribution_audit/v1"
STAGES = ("preflight", "dream", "writer_request", "writer_write", "create_develop",
          "materialize", "audit")
STAGE_TIMEOUT_SECONDS = 900
STDERR_TAIL = 1200


class StageFailed(RuntimeError):
    def __init__(self, stage: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.detail = detail or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class CreativeRun:
    """One persisted Dream -> Writer -> Create run."""

    def __init__(self, run_dir: Path, inputs: Optional[Dict[str, Any]] = None,
                 python: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        self.run_dir = run_dir.expanduser().resolve()
        self.state_path = self.run_dir / "creative-run.json"
        self.python = python or sys.executable
        if self.state_path.exists():
            self.state = _load(self.state_path)
            if inputs and inputs != self.state["inputs"]:
                raise ValueError("run directory already holds a run with different inputs; use resume")
        else:
            if inputs is None:
                raise ValueError(f"no creative run found in {self.run_dir}")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.state = {
                "schema": STATE_SCHEMA,
                "run_id": "crt-" + hashlib.sha256(
                    (str(self.run_dir) + _now()).encode()).hexdigest()[:12],
                "created_at": _now(),
                "inputs": inputs,
                "stages": {},
                "status": "PENDING",
            }
            self._save()
        base_env = dict(os.environ if env is None else env)
        # Every child component inherits the restrictive inference policy.
        base_env["HOWL_FORBID_LOCAL_INFERENCE"] = "1"
        self.env = base_env

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def path(self, *parts: str) -> Path:
        target = self.run_dir.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _stage_done(self, stage: str) -> bool:
        record = self.state["stages"].get(stage)
        if not record or record.get("status") != "COMPLETED":
            return False
        return all(Path(o["path"]).exists() and _sha256(Path(o["path"])) == o["sha256"]
                   for o in record.get("outputs", []))

    def _complete(self, stage: str, outputs: List[Path], **extra: Any) -> None:
        record = self.state["stages"].setdefault(stage, {})
        record.update(status="COMPLETED", finished_at=_now(),
                      outputs=[{"path": str(p), "sha256": _sha256(p)} for p in outputs], **extra)
        self._save()

    # -- component invocation ---------------------------------------------
    def _component(self, stage: str, module: str, args: List[str], *,
                   stdout_to: Optional[Path] = None) -> subprocess.CompletedProcess:
        argv = [self.python, "-m", module, *args]
        record = self.state["stages"].setdefault(stage, {})
        record.setdefault("commands", []).append([Path(self.python).name, "-m", module, *args])
        self._save()
        try:
            result = subprocess.run(argv, capture_output=True, text=True, env=self.env,
                                    cwd=self.run_dir, timeout=STAGE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise StageFailed(stage, f"{module} timed out after {STAGE_TIMEOUT_SECONDS}s") from None
        if result.returncode != 0:
            raise StageFailed(stage, f"{module} exited {result.returncode}", {
                "exit_code": result.returncode, "stderr_tail": result.stderr[-STDERR_TAIL:]})
        if stdout_to is not None:
            stdout_to.write_text(result.stdout, encoding="utf-8")
        return result

    # -- stages ------------------------------------------------------------
    def stage_preflight(self) -> None:
        from howlplane.control_plane import creative_doctor

        inputs = self.state["inputs"]
        config = Path(inputs["command_config"]) if inputs.get("command_config") else None
        report = creative_doctor.summarize(creative_doctor.run_checks(
            command_config=config, workspace=self.run_dir, env=self.env))
        out = self.path("preflight", "doctor.json")
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        blocking = [c for c in report["checks"] if c["status"] == creative_doctor.FAIL
                    and not c["id"].startswith("cli.")]
        if blocking:
            raise StageFailed("preflight", "creative doctor reported FAIL", {
                "failures": [f"{c['label']}: {c['detail']}" for c in blocking]})
        self._complete("preflight", [out], doctor_status=report["status"])

    def _select_unit(self, discovery: Dict[str, Any]) -> str:
        wanted = self.state["inputs"].get("candidate_id")
        if wanted:
            return wanted
        if discovery.get("ranking"):
            return discovery["ranking"][0]["representative"]
        if discovery.get("units"):
            return discovery["units"][0]["id"]
        raise StageFailed("dream", "Dream produced no idea units to select")

    def stage_dream(self) -> None:
        inputs = self.state["inputs"]
        candidate = self.path("dream", "candidate.json")
        source = inputs["dream_source"]
        kind = source["kind"]
        detail: Dict[str, Any] = {"source_kind": kind}
        if kind == "candidate":
            shutil.copyfile(source["path"], candidate)
            detail["operation"] = "PROVIDED"
        elif kind == "run":
            run_dir = Path(source["path"])
            unit = inputs.get("candidate_id") or self._select_unit(_load(run_dir / "discovery.json"))
            self._component("dream", "howldream.cli", ["export", str(run_dir), "--candidate-id", unit],
                            stdout_to=candidate)
            detail["selected"] = unit
        elif kind == "external":
            discovery = self.path("dream", "external-discovery.json")
            self._component("dream", "howldream.cli", [
                "cluster", "--external", source["path"], "--rank", "objective_fit",
                "--out", str(discovery)])
            unit = self._select_unit(_load(discovery))
            self._component("dream", "howldream.cli", ["export", str(discovery), "--candidate-id", unit],
                            stdout_to=candidate)
            detail["selected"] = unit
        elif kind == "explore":
            request = self.path("dream", "exploration-request.json")
            request.write_text(json.dumps({
                "request_id": self.state["run_id"],
                "objective": source["objective"],
                "originating_component": "howlplane",
                "ranking_criteria": ["objective_fit", "novelty"],
                "constraints": source.get("constraints", []),
                "budget": {"max_candidates": source.get("max_candidates", 3), "max_calls": 6,
                           "provider_allowlist": ["command"], "local_only": False,
                           "forbid_local_inference": True},
            }, indent=2), encoding="utf-8")
            runs = self.path("dream", "runs", ".keep").parent
            result = self._component("dream", "howldream.cli", [
                "explore", str(request), "--output", str(runs),
                "--command-config", inputs["command_config"], "--allow-remote"])
            exploration = json.loads(result.stdout)
            run_dir = runs / exploration["exploration_id"]
            self.path("dream", "exploration-result.json").write_text(result.stdout, encoding="utf-8")
            unit = self._select_unit(_load(run_dir / "discovery.json"))
            self._component("dream", "howldream.cli", ["export", str(run_dir), "--candidate-id", unit],
                            stdout_to=candidate)
            detail.update(selected=unit, dream_run=str(run_dir))
        else:
            raise StageFailed("dream", f"unknown Dream source {kind!r}")
        value = _load(candidate)
        detail["dream_candidate_id"] = value["candidate_id"]
        self._complete("dream", [candidate], **detail)

    def stage_writer_request(self) -> None:
        out = self.path("writer", "request.json")
        self._component("writer_request", "howlwriter", [
            "native", "request", "--from-dream", str(self.path("dream", "candidate.json")),
            "--spec", self.state["inputs"]["copy_spec"], "--out", str(out)])
        self._complete("writer_request", [out], writer_request_id=_load(out)["request_id"])

    def stage_writer_write(self) -> None:
        out = self.path("writer", "copy-package.json")
        failure = self.path("writer", "failure.json")
        try:
            self._component("writer_write", "howlwriter", [
                "native", "write", "--request", str(self.path("writer", "request.json")),
                "--command-config", self.state["inputs"]["command_config"],
                "--out", str(out), "--failure-out", str(failure)])
        except StageFailed as error:
            if failure.exists():
                error.detail["writer_failure"] = _load(failure).get("failure")
            raise
        package = _load(out)
        self._complete("writer_write", [out], writer_proposal_id=package["writer_proposal_id"],
                       factual_status=package["factual_status"])

    def stage_create_develop(self) -> None:
        inputs = self.state["inputs"]
        out = self.path("create", "development.json")
        mode = inputs.get("create_mode", "develop")
        args = [mode, "--from-writer", str(self.path("writer", "copy-package.json")),
                "--from-dream", str(self.path("dream", "candidate.json")), "--output", str(out)]
        if mode == "develop":
            args += ["--provider", "command", "--command-config", inputs["command_config"]]
        self._component("create_develop", "howlcreate.cli", args)
        development = _load(out)
        self._complete("create_develop", [out], create_development_id=development["development_id"],
                       mode=mode)

    def stage_materialize(self) -> None:
        sandbox = self.state["inputs"].get("sandbox")
        if not sandbox:
            self._complete("materialize", [], skipped=True)
            return
        self._component("materialize", "howlcreate.cli", [
            "materialize", "--input", str(self.path("create", "development.json")),
            "--output-dir", sandbox])
        manifest = Path(sandbox).expanduser().resolve() / "create-artifact-manifest.json"
        self._complete("materialize", [manifest], sandbox=str(manifest.parent),
                       materialization_id=_load(manifest)["materialization_id"])

    def stage_audit(self) -> None:
        out = self.path("contribution-audit.json")
        out.write_text(json.dumps(contribution_audit(self), indent=2), encoding="utf-8")
        self._complete("audit", [out])

    # -- driver ------------------------------------------------------------
    def execute(self) -> Dict[str, Any]:
        self.state["status"] = "RUNNING"
        self._save()
        for stage in STAGES:
            if self._stage_done(stage):
                continue
            if stage == "preflight" and self.state["inputs"].get("skip_doctor"):
                self._complete("preflight", [], skipped=True)
                continue
            self.state["stages"][stage] = {"status": "RUNNING", "started_at": _now()}
            self._save()
            try:
                getattr(self, f"stage_{stage}")()
            except StageFailed as error:
                self.state["stages"][stage].update(status="FAILED", finished_at=_now(),
                                                   error=str(error), **error.detail)
                self.state.update(status="FAILED", failed_stage=stage)
                self._save()
                return self.state
            except (OSError, ValueError, KeyError) as error:
                self.state["stages"][stage].update(status="FAILED", finished_at=_now(),
                                                   error=f"{type(error).__name__}: {error}")
                self.state.update(status="FAILED", failed_stage=stage)
                self._save()
                return self.state
        self.state.update(status="COMPLETED", failed_stage=None, completed_at=_now())
        self._save()
        return self.state


def contribution_audit(run: CreativeRun) -> Dict[str, Any]:
    """Who generated, transformed, selected, designed, materialized and orchestrated."""
    candidate = _load(run.path("dream", "candidate.json"))
    package = _load(run.path("writer", "copy-package.json"))
    development = _load(run.path("create", "development.json"))
    materialize = run.state["stages"].get("materialize", {})
    manifest = None
    if materialize.get("sandbox"):
        manifest = _load(Path(materialize["sandbox"]) / "create-artifact-manifest.json")
    participation = (candidate.get("provenance") or {}).get("participation") or []
    entries: List[Dict[str, Any]] = []
    for record in participation:
        entries.append({
            "component": record.get("transforming_component", "howldream"),
            "operation": record["operation"],
            "subject": candidate["candidate_id"],
            "origin_component": record.get("origin_component"),
            "origin_model": record.get("origin_model"),
            "evidence": "dream/candidate.json#provenance.participation",
        })
    if not participation:
        entries.append({"component": "howldream", "operation": "UNRECORDED",
                        "subject": candidate["candidate_id"],
                        "evidence": "candidate predates participation provenance"})
    contribution = package["contribution"]
    entries.append({
        "component": "howlwriter", "operation": contribution["operation"],
        "subject": package["writer_proposal_id"],
        "inference_occurred": contribution.get("inference_occurred"),
        "model": package["execution"].get("model"),
        "proposals_from_model": contribution.get("proposals_from_model"),
        "proposals_operator_edited": contribution.get("proposals_operator_edited", 0),
        "factual_status": package["factual_status"],
        "evidence": "writer/copy-package.json#contribution",
    })
    design = (development.get("provenance") or {}).get("contribution") or {}
    entries.append({
        "component": "howlcreate", "operation": design.get("operation", "DESIGNED"),
        "subject": development["development_id"],
        "inference_occurred": design.get("inference_occurred"),
        "evidence": "create/development.json#provenance.contribution",
    })
    if manifest:
        entries.append({
            "component": "howlcreate", "operation": manifest["contribution"]["operation"],
            "subject": manifest["materialization_id"],
            "artifacts": [a["path"] for a in manifest["artifacts"]],
            "evidence": "create-artifact-manifest.json",
        })
    entries.append({"component": "howlplane", "operation": "ORCHESTRATED",
                    "subject": run.state["run_id"], "evidence": "creative-run.json"})
    lineage = {
        "dream_candidate_id": candidate["candidate_id"],
        "dream_run_id": candidate.get("source_run_id"),
        "writer_request_id": package["request_id"],
        "writer_proposal_id": package["writer_proposal_id"],
        "create_development_id": development["development_id"],
        "create_materialization_id": manifest["materialization_id"] if manifest else None,
        "artifacts": [{"artifact_id": a["artifact_id"], "path": a["path"]}
                      for a in (manifest or {}).get("artifacts", [])],
    }
    checks = {
        "writer_source_matches_dream": package["source_idea_id"] == candidate["candidate_id"],
        "create_lineage_matches_writer": development["lineage"].get("writer_proposal_id")
        == package["writer_proposal_id"],
        "manifest_matches_create": manifest is None
        or manifest["create_run_id"] == development["development_id"],
        "manifest_names_writer": manifest is None
        or package["writer_proposal_id"] in manifest["source_writer_ids"],
    }
    return {"schema": AUDIT_SCHEMA, "run_id": run.state["run_id"], "lineage": lineage,
            "lineage_checks": checks, "lineage_intact": all(checks.values()),
            "contributions": entries,
            "note": "Credit is read from each component's own provenance, never inferred "
                    "from where a file lives."}


def _inputs_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    sources = [s for s in ("dream_candidate", "dream_run", "external_ideas", "objective")
               if getattr(args, s, None)]
    if len(sources) != 1:
        raise ValueError("choose exactly one Dream source: --dream-candidate, --dream-run, "
                         "--external-ideas or --objective")
    kind = {"dream_candidate": "candidate", "dream_run": "run", "external_ideas": "external",
            "objective": "explore"}[sources[0]]
    if kind == "explore":
        source: Dict[str, Any] = {"kind": kind, "objective": args.objective,
                                  "constraints": list(args.constraint or [])}
    else:
        source = {"kind": kind, "path": str(Path(getattr(args, sources[0])).expanduser().resolve())}
    if not args.command_config:
        raise ValueError("--command-config is required: Writer makes one bounded remote call")
    return {
        "dream_source": source,
        "candidate_id": args.candidate_id,
        "copy_spec": str(Path(args.copy_spec).expanduser().resolve()),
        "command_config": str(Path(args.command_config).expanduser().resolve()),
        "create_mode": args.create_mode,
        "sandbox": str(Path(args.sandbox).expanduser().resolve()) if args.sandbox else None,
        "skip_doctor": bool(args.skip_doctor),
    }


def _report(state: Dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(state, indent=2))
    else:
        print(f"Creative run {state['run_id']}: {state['status']}")
        for stage in STAGES:
            record = state["stages"].get(stage)
            if record:
                note = " (skipped)" if record.get("skipped") else ""
                print(f"  {stage:<15} {record.get('status')}{note}")
                if record.get("status") == "FAILED":
                    print(f"    {record.get('error')}")
                    for line in record.get("failures", []):
                        print(f"    - {line}")
    return 0 if state["status"] == "COMPLETED" else 1


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "creative_action", None)
    try:
        if action == "run":
            run = CreativeRun(Path(args.run_dir), _inputs_from_args(args))
        elif action == "resume":
            run = CreativeRun(Path(args.run_dir))
        elif action == "status":
            return _report(CreativeRun(Path(args.run_dir)).state, args.json)
        else:
            print("usage: howlplane creative {run,resume,status} ...", file=sys.stderr)
            return 2
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return _report(run.execute(), args.json)


def add_parser(subparsers: Any, common_parser: Any) -> None:
    parser = subparsers.add_parser(
        "creative", parents=[common_parser],
        help="Run the native HowlDream -> HowlWriter -> HowlCreate pipeline")
    actions = parser.add_subparsers(dest="creative_action")
    run = actions.add_parser("run", help="Start a creative run in an explicit run directory")
    run.add_argument("--run-dir", required=True, help="Where every stage output is persisted")
    run.add_argument("--dream-candidate", help="An exported howl.candidate/v1")
    run.add_argument("--dream-run", help="A HowlDream run directory to select from")
    run.add_argument("--external-ideas", help="Externally authored ideas for Dream to cluster")
    run.add_argument("--objective", help="Let Dream explore this objective with the remote provider")
    run.add_argument("--constraint", action="append", help="Hard constraint for Dream exploration")
    run.add_argument("--candidate-id", help="Dream unit or candidate to select (default: top ranked)")
    run.add_argument("--copy-spec", required=True, help="Writer copy spec: items, audience, evidence")
    run.add_argument("--command-config", help="Reviewed remote provider profile (provider-core)")
    run.add_argument("--create-mode", choices=["develop", "scaffold"], default="develop")
    run.add_argument("--sandbox", help="Explicit directory for Create materialization")
    run.add_argument("--skip-doctor", action="store_true", help="Skip the creative preflight")
    run.add_argument("--json", action="store_true")
    for name in ("resume", "status"):
        sub = actions.add_parser(name, help=f"{name.title()} a creative run")
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--json", action="store_true")
