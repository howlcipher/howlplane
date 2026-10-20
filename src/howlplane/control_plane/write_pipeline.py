"""`howlplane write`: run HowlWriter on a prompt as a governed, resumable tool call.

Plane hands one writing request to HowlWriter's own CLI (``howlwriter``) and
keeps every stage's output in an explicit run directory, as ``creative`` does:

    preflight  ``howlwriter --version``; the nested-session guard
    write      ``writer`` (compose), ``project draft``, or ``humanize``
    check      ``howlwriter lint --json`` on the output (deterministic, no model)
    audit      ``write-audit.json``: who generated what, with which model

The operation is chosen from the inputs, never guessed by a model:
``--operation`` if given; otherwise ``project-draft`` with ``--project``,
``humanize`` with ``--input``, and ``compose`` for a bare prompt.

HowlWriter's exit codes keep their meaning (``docs/agents.md`` in HowlWriter):
3 is a draft that is saved but not ready, 4 an owner-only decision nobody could
be asked for, 2 a missing choice or a remote model the project has not been
authorized for. Plane records them as NOT_READY, AWAITING_OWNER and
NEEDS_INPUT, exits with the same code, and ``write resume`` retries the stage
once the owner has acted. Plane never passes ``--owner-confirmed`` or
``--allow-remote``: those decisions are the owner's to make at a terminal.

HowlWriter runs with stdin closed, so it can never wait on a question nobody
sees, and with no inherited ``HOWLWRITER_*PROVIDER`` choice, so the run record
names every model used. The prompt is passed so it can never be read as an
option. Plane never imports HowlWriter and never writes outside the run
directory; HowlWriter keeps project drafts in its own private project store.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from howlplane.control_plane.staged_run import StageFailed, StagedRun, load as _load, sha256 as _sha256

STATE_SCHEMA = "howlplane.write_run/v1"
AUDIT_SCHEMA = "howlplane.write_audit/v1"
STAGES = ("preflight", "write", "check", "audit")
OPERATIONS = ("compose", "project-draft", "humanize")
PROVIDERS = ("claude", "agy", "codex", "local")
DEFAULT_TIMEOUT_SECONDS = 3600
STDERR_TAIL = 1200

# HowlWriter's documented exit codes, and what a run in that state is waiting for.
EXIT_NEEDS_INPUT, EXIT_NOT_READY, EXIT_OWNER_REQUIRED = 2, 3, 4
BLOCKED_STATUS = {EXIT_NEEDS_INPUT: "NEEDS_INPUT", EXIT_OWNER_REQUIRED: "AWAITING_OWNER"}
RUN_EXIT = {"COMPLETED": 0, "FAILED": 1, "NEEDS_INPUT": EXIT_NEEDS_INPUT, "NOT_READY": EXIT_NOT_READY,
            "AWAITING_OWNER": EXIT_OWNER_REQUIRED}

#: Decisions only the owner makes; Plane must never put them on HowlWriter's command line.
OWNER_ONLY_FLAGS = ("--owner-confirmed", "--allow-remote")
#: Choices HowlWriter would otherwise read from the environment, unrecorded.
INHERITED_CHOICES = ("HOWLWRITER_PROVIDER", "HOWLWRITER_REVIEW_PROVIDER")


class WriteRun(StagedRun):
    """One persisted prompt -> HowlWriter run."""

    KIND = "write"
    STATE_FILE = "write-run.json"
    STATE_SCHEMA = STATE_SCHEMA
    RUN_ID_PREFIX = "wrt"
    STAGES = STAGES

    def _child_env(self, env: Dict[str, str]) -> Dict[str, str]:
        return {k: v for k, v in env.items() if k not in INHERITED_CHOICES}

    def _final_status(self) -> str:
        return "COMPLETED" if self.state["stages"]["write"].get("ready", True) else "NOT_READY"

    # -- HowlWriter invocation ---------------------------------------------
    def _howlwriter(self, stage: str, args: List[str], *,
                    accept: tuple = (0,)) -> subprocess.CompletedProcess:
        # Only options count: after `--` an argument is text (the prompt), whatever it says.
        options = args[:args.index("--")] if "--" in args else args
        leaked = [flag for flag in OWNER_ONLY_FLAGS
                  if any(a == flag or a.startswith(flag + "=") for a in options)]
        if leaked:  # constructed here, never from input; a regression must not reach HowlWriter
            raise StageFailed(stage, f"refusing to relay an owner-only decision ({', '.join(leaked)})")
        executable = shutil.which("howlwriter", path=self.env.get("PATH"))
        if executable is None:
            raise StageFailed(stage, "howlwriter is not on PATH; install it with `howl install howlwriter`")
        record = self.state["stages"].setdefault(stage, {})
        record.setdefault("commands", []).append(["howlwriter", *args])
        record["executable"] = executable
        self._save()
        timeout = self.state["inputs"]["timeout_seconds"]
        try:
            result = subprocess.run([executable, *args], capture_output=True, text=True, env=self.env,
                                    stdin=subprocess.DEVNULL, cwd=self.run_dir, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise StageFailed(stage, f"howlwriter timed out after {timeout}s") from None
        except OSError as error:
            raise StageFailed(stage, f"howlwriter could not start: {error}") from None
        record["exit_code"] = result.returncode
        if result.returncode not in accept:
            detail = {"exit_code": result.returncode, "stderr_tail": result.stderr[-STDERR_TAIL:]}
            status = BLOCKED_STATUS.get(result.returncode, "FAILED")
            raise StageFailed(stage, f"howlwriter exited {result.returncode}", detail, status=status)
        return result

    def _model_flags(self) -> List[str]:
        inputs = self.state["inputs"]
        flags = ["--provider", inputs["provider"]]
        if inputs.get("review_provider"):
            flags += ["--review-provider", inputs["review_provider"]]
        if inputs.get("local_model"):
            flags += ["--local-model", inputs["local_model"]]
        return flags

    # -- stages ------------------------------------------------------------
    def stage_preflight(self) -> None:
        inputs = self.state["inputs"]
        # Inside a Claude Code session, `--provider claude` starts a second session on the
        # same account; HowlWriter's model step cannot call back into Plane (it runs with no
        # tools in an empty directory), so this guards quota, not recursion.
        nested = [p for p in (inputs["provider"], inputs.get("review_provider")) if p == "claude"]
        if nested and self.env.get("CLAUDECODE") == "1" and not inputs.get("allow_nested"):
            raise StageFailed("preflight", "this is already a Claude Code session; --provider claude would start a "
                              "second one on the same account. Choose another model, or pass --allow-nested",
                              status="NEEDS_INPUT")
        result = self._howlwriter("preflight", ["--version"])
        out = self.path("preflight", "howlwriter-version.txt")
        out.write_text(result.stdout, encoding="utf-8")
        self._complete("preflight", [out], howlwriter_version=result.stdout.strip())

    def stage_write(self) -> None:
        inputs = self.state["inputs"]
        operation = inputs["operation"]
        out = self.path("write", "output.md")
        if operation == "compose":
            args = [*self._model_flags(), "writer", "--out", str(out)]
            if inputs.get("target_words"):
                args += ["--target-words", str(inputs["target_words"])]
            # After `--` the prompt is the positional argument, whatever it starts with.
            args += ["--", inputs["prompt"]]
        elif operation == "project-draft":
            args = [*self._model_flags(), "project", "draft", f"--instructions={inputs['prompt']}",
                    "--json", "--fail-if-blocked", "--out", str(out)]
            for key in ("mode", "voice"):
                if inputs.get(key):
                    args.append(f"--{key}={inputs[key]}")
            args += ["--", inputs["project"]]
        elif operation == "humanize":
            args = [*self._model_flags(), "humanize", "--out", str(out)]
            if inputs.get("voice"):
                args.append(f"--voice={inputs['voice']}")
            args += ["--", inputs["input"]]
        else:
            raise StageFailed("write", f"unknown operation {operation!r}")
        result = self._howlwriter("write", args, accept=(0, EXIT_NOT_READY))
        outputs = [out]
        extra: Dict[str, Any] = {"operation": operation}
        if operation == "project-draft":
            report = self.path("write", "result.json")
            report.write_text(result.stdout, encoding="utf-8")
            outputs.append(report)
            validation = ((_load(report).get("provenance") or {}).get("validation") or {})
            extra.update(verdict=validation.get("verdict"), issue_counts=validation.get("issue_counts"),
                         draft_path=_load(report).get("path"))
        extra["ready"] = result.returncode == 0
        if not out.exists():
            raise StageFailed("write", "howlwriter reported success but wrote no output")
        self._complete("write", outputs, **extra)

    def stage_check(self) -> None:
        result = self._howlwriter("check", ["lint", str(self.path("write", "output.md")), "--json"])
        out = self.path("check", "lint.json")
        out.write_text(result.stdout, encoding="utf-8")
        findings = _load(out)
        count = len(findings) if isinstance(findings, list) else len(findings.get("findings", []))
        self._complete("check", [out], lint_findings=count)

    def stage_audit(self) -> None:
        out = self.path("write-audit.json")
        out.write_text(json.dumps(write_audit(self), indent=2), encoding="utf-8")
        self._complete("audit", [out])


def write_audit(run: WriteRun) -> Dict[str, Any]:
    """Who generated or transformed the text, with which model, and who orchestrated."""
    inputs = run.state["inputs"]
    write = run.state["stages"]["write"]
    operation = {"compose": "GENERATED", "project-draft": "GENERATED_FROM_EVIDENCE",
                 "humanize": "TRANSFORMED"}[inputs["operation"]]
    output = run.path("write", "output.md")
    entries = [
        {"component": "howlwriter", "operation": operation, "subject": "write/output.md",
         "sha256": _sha256(output), "provider": inputs["provider"],
         "review_provider": inputs.get("review_provider"), "local_model": inputs.get("local_model"),
         "verdict": write.get("verdict"), "ready": write.get("ready"),
         "evidence": "write/result.json#provenance" if inputs["operation"] == "project-draft"
         else "write-run.json#stages.write"},
        {"component": "howlwriter", "operation": "CHECKED", "subject": "check/lint.json",
         "inference_occurred": False, "evidence": "check/lint.json"},
        {"component": "howlplane", "operation": "ORCHESTRATED", "subject": run.state["run_id"],
         "inference_occurred": False, "evidence": "write-run.json"},
    ]
    return {"schema": AUDIT_SCHEMA, "run_id": run.state["run_id"], "operation": inputs["operation"],
            "contributions": entries, "owner_decisions_relayed": [],
            "note": "Plane passed no owner-only decision to HowlWriter; any owner decision in this "
                    "run was made by the owner at a terminal."}


def _operation(args: argparse.Namespace) -> str:
    if args.operation:
        return args.operation
    if args.project:
        return "project-draft"
    if args.input:
        return "humanize"
    return "compose"


#: Options that belong to one operation: (attribute, flag, operation it applies to).
_OPERATION_ONLY = (("project", "--project", "project-draft"), ("input", "--input", "humanize"),
                   ("mode", "--mode", "project-draft"), ("target_words", "--target-words", "compose"))


def _check_combination(args: argparse.Namespace, operation: str) -> None:
    if operation in ("compose", "project-draft") and not (args.prompt or "").strip():
        raise ValueError(f"{operation} needs --prompt")
    if operation == "humanize" and args.prompt:
        raise ValueError("humanize rewrites --input and takes no instructions; drop --prompt")
    for attribute, flag, owner in _OPERATION_ONLY:
        if getattr(args, attribute) and operation != owner:
            raise ValueError(f"{flag} applies only to {owner}, not {operation}")
    if operation == "project-draft" and not args.project:
        raise ValueError("project-draft needs --project (a HowlWriter project id or folder)")
    if operation == "humanize" and not args.input:
        raise ValueError("humanize needs --input FILE")
    if args.local_model and "local" not in (args.provider, args.review_provider):
        raise ValueError("--local-model applies only when --provider or --review-provider is local")


def _inputs_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.provider:
        raise ValueError("--provider is required: choose claude, agy, codex or local "
                         "(local keeps everything on this machine)")
    operation = _operation(args)
    _check_combination(args, operation)
    source = None
    if args.input:
        path = Path(args.input).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"--input {args.input} is not a file")
        source = str(path)
    return {
        "operation": operation,
        "prompt": args.prompt,
        "provider": args.provider,
        "review_provider": args.review_provider,
        "local_model": args.local_model,
        "project": args.project,
        "input": source,
        "voice": args.voice,
        "mode": args.mode,
        "target_words": args.target_words,
        "timeout_seconds": args.timeout,
        "allow_nested": bool(args.allow_nested),
    }


_NEXT_STEP = {
    "AWAITING_OWNER": "HowlWriter needs a decision only the owner can make. The owner runs the command "
                      "HowlWriter names above at a terminal, then:",
    "NEEDS_INPUT": "HowlWriter needs a choice or authorization (above). Resolve it, then:",
}


def _report(state: Dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(state, indent=2))
        return RUN_EXIT.get(state["status"], 1)
    print(f"Write run {state['run_id']} ({state['inputs']['operation']}): {state['status']}")
    for stage in STAGES:
        record = state["stages"].get(stage)
        if record:
            print(f"  {stage:<10} {record.get('status')}")
            if record.get("status") in ("FAILED", "BLOCKED"):
                print(f"    {record.get('error')}")
                for line in (record.get("stderr_tail") or "").strip().splitlines()[-6:]:
                    print(f"    | {line}")
    write = state["stages"].get("write") or {}
    if state["status"] in ("COMPLETED", "NOT_READY"):
        print(f"Output: {Path(state['stages']['write']['outputs'][0]['path'])}")
        if write.get("verdict"):
            print(f"HowlWriter verdict: {write['verdict']}")
        if state["status"] == "NOT_READY":
            print("The draft is saved but HowlWriter's validation did not pass; review write/result.json.")
    if state["status"] in _NEXT_STEP:
        print(_NEXT_STEP[state["status"]])
        print(f"  howlplane write resume --run-dir {state.get('run_dir', '<run-dir>')}")
    return RUN_EXIT.get(state["status"], 1)


def command(args: argparse.Namespace) -> int:
    action = getattr(args, "write_action", None)
    try:
        if action == "run":
            run = WriteRun(Path(args.run_dir), _inputs_from_args(args))
        elif action == "resume":
            run = WriteRun(Path(args.run_dir))
        elif action == "status":
            run = WriteRun(Path(args.run_dir))
            return _report({**run.state, "run_dir": str(run.run_dir)}, args.json)
        else:
            print("usage: howlplane write {run,resume,status} ...", file=sys.stderr)
            return 2
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return _report({**run.execute(), "run_dir": str(run.run_dir)}, args.json)


def add_parser(subparsers: Any, common_parser: Any) -> None:
    parser = subparsers.add_parser(
        "write", parents=[common_parser],
        help="Run HowlWriter on a writing prompt as a governed, resumable run")
    actions = parser.add_subparsers(dest="write_action")
    run = actions.add_parser("run", help="Start a write run in an explicit run directory")
    run.add_argument("--run-dir", required=True, help="Where every stage output is persisted")
    run.add_argument("--prompt", help="What to write (compose) or the instructions for a project draft")
    run.add_argument("--provider", choices=PROVIDERS,
                     help="Model HowlWriter uses (required; local keeps everything on this machine)")
    run.add_argument("--review-provider", choices=PROVIDERS, help="Model for HowlWriter's independent reviews")
    run.add_argument("--local-model", help="Ollama model for local, e.g. qwen2.5:7b-instruct")
    run.add_argument("--operation", choices=OPERATIONS,
                     help="Default: project-draft with --project, humanize with --input, else compose")
    run.add_argument("--project", help="HowlWriter project id or folder (project-draft)")
    run.add_argument("--input", help="File to rewrite (humanize)")
    run.add_argument("--voice", help="HowlWriter voice to write in")
    run.add_argument("--mode", help="HowlWriter mode for project-draft (default: academic)")
    run.add_argument("--target-words", type=int, help="Target length for compose")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                     help=f"Seconds each HowlWriter step may run (default {DEFAULT_TIMEOUT_SECONDS})")
    run.add_argument("--allow-nested", action="store_true",
                     help="Allow --provider claude inside a Claude Code session (a second session, same quota)")
    run.add_argument("--json", action="store_true")
    for name in ("resume", "status"):
        sub = actions.add_parser(name, help=f"{name.title()} a write run")
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--json", action="store_true")
