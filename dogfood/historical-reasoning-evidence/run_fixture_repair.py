#!/usr/bin/env python3
"""
Run a historical repair experiment against an isolated git worktree.

Usage:
    python run_fixture_repair.py \
        --fixture FIX-39 \
        --provider claude_code \
        --worktree-base /tmp/fix39-001 \
        --out-dir dogfood/historical-reasoning-evidence/runs

The script:
  1. creates a fresh worktree from the fixture's historical base SHA,
  2. applies only the future test patch (not the production fix),
  3. runs the deterministic verifier and records the failing output,
  4. invokes the requested provider with a bounded repair prompt,
  5. re-runs the verifier and records the final result,
  6. writes a trajectory summary without leaking the historical fix.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "dogfood" / "historical-reasoning-evidence" / "fixture_catalog.yaml"
PATCH_DIR = ROOT / "dogfood" / "historical-reasoning-evidence"
VENV_PYTHON = Path("/run/media/system/tallgeese/dev/.ci_verify_venv/bin/python3")


@dataclass
class RepairRun:
    run_id: str
    fixture_id: str
    provider: str
    role: str
    base_sha: str
    worktree: Path
    test_patch: Path
    verifier: List[str]
    initial_status: str = "pending"
    initial_output: str = ""
    final_status: str = "pending"
    final_output: str = ""
    provider_command: str = ""
    provider_stdout: str = ""
    provider_stderr: str = ""
    provider_exit_code: int = -1
    provider_duration_seconds: float = 0.0
    observed_model_id: Optional[str] = None
    cost_if_available: Optional[float] = None
    files_changed: List[str] = field(default_factory=list)
    context_docs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fixture_id": self.fixture_id,
            "provider": self.provider,
            "role": self.role,
            "base_sha": self.base_sha,
            "worktree": str(self.worktree),
            "test_patch": str(self.test_patch),
            "verifier": self.verifier,
            "initial_status": self.initial_status,
            "final_status": self.final_status,
            "provider_command": self.provider_command,
            "provider_exit_code": self.provider_exit_code,
            "provider_duration_seconds": self.provider_duration_seconds,
            "observed_model_id": self.observed_model_id,
            "cost_if_available": self.cost_if_available,
            "files_changed": self.files_changed,
            "context_docs": self.context_docs,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


def load_catalog() -> Dict[str, Any]:
    with open(CATALOG, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def git(args: List[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check)


def run_pytest(worktree: Path, test_ids: List[str]) -> subprocess.CompletedProcess:
    cmd = [str(VENV_PYTHON), "-m", "pytest", "-x", "-v", *test_ids]
    return subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True)


def create_worktree(base_sha: str, worktree: Path) -> None:
    if worktree.exists():
        git(["worktree", "remove", "--force", str(worktree)], cwd=ROOT, check=False)
    git(["worktree", "add", "--detach", str(worktree), base_sha], cwd=ROOT)


def _read_context_docs(context_docs: List[Path], worktree: Path, base_sha: str) -> str:
    parts = []
    for doc in context_docs:
        # Resolve relative to worktree when a path is relative; absolute paths
        # (e.g. files in the main campaign repo) are used as-is.
        target = worktree / doc if not doc.is_absolute() else doc
        if target.exists():
            content = target.read_text(encoding="utf-8")
        else:
            # Fallback: read from the historical base commit via git show.
            result = git(["show", f"{base_sha}:{doc}"], cwd=worktree, check=False)
            content = result.stdout if result.returncode == 0 else f"[context doc {doc} not found]"
        parts.append(f"--- {doc} ---\n{content[:6000]}\n")
    return "\n".join(parts)


def build_prompt(
    fixture: Dict[str, Any],
    initial_failure: str,
    role: str = "implementation",
    plan_artifact: Optional[str] = None,
    context_docs: Optional[List[Path]] = None,
    worktree: Optional[Path] = None,
    base_sha: Optional[str] = None,
) -> str:
    allowed = "\n".join(f"  - {p}" for p in fixture["allowed_paths"])
    context_block = ""
    if context_docs and worktree and base_sha:
        context_block = "\nAdditional pre-fix architecture context (does not contain the fix):\n\n" + _read_context_docs(context_docs, worktree, base_sha)
    prompt = f"""You are a senior software engineer working in an isolated historical worktree.

TASK: Fix a real historical HowlPlane defect.

Defect summary:
{fixture['known_defect_summary']}

Allowed files you may edit (do not modify anything else):
{allowed}

Deterministic verifier that must pass after your fix:
{', '.join(fixture['deterministic_verifier'])}

Current verifier output (failing):
```
{initial_failure[:4000]}
```{context_block}

Constraints:
- Do not add new dependencies.
- Do not change unrelated files or tests.
- Keep the diff minimal and idiomatic to the existing codebase.
- Run the verifier yourself when done.
- Do not read future commits, PR descriptions, or the historical fix patch.
"""
    if role == "plan":
        prompt += """

Your role is PLANNER. Produce a concrete implementation plan that another
agent can execute. Output only the plan: what files to change, what exact
code edits to make, and why. Do not implement the changes yourself.
"""
    elif role == "implementation" and plan_artifact:
        prompt += f"""

Your role is IMPLEMENTER. A planner has already produced the following plan.
Implement it exactly, then run the verifier.

PLAN:
{plan_artifact}
"""
    else:
        prompt += """

Your role is SOLO IMPLEMENTER. Analyze the failure, implement the fix, and
run the verifier.
"""
    return prompt


def _extract_claude_model_and_cost(stdout: str) -> tuple:
    observed_model_id = None
    cost_usd = None
    if stdout.strip().startswith("{"):
        try:
            data = json.loads(stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            try:
                data = json.loads(stdout.strip())
            except json.JSONDecodeError:
                data = None
        if data:
            model_usage = data.get("modelUsage") or {}
            candidates = [
                (key, info)
                for key, info in model_usage.items()
                if isinstance(info, dict) and info.get("costUSD")
            ]
            # Prefer the model that produced the final response (higher output
            # tokens) rather than a fast tool-routing model.
            candidates.sort(
                key=lambda item: item[1].get("outputTokens", 0),
                reverse=True,
            )
            if candidates:
                observed_model_id = candidates[0][1].get("canonicalModel") or candidates[0][0]
                cost_usd = candidates[0][1].get("costUSD")
            if cost_usd is None and "total_cost_usd" in data:
                cost_usd = data.get("total_cost_usd")
    return observed_model_id, cost_usd


def _extract_codex_model_and_cost(stdout: str) -> tuple:
    observed_model_id = None
    cost_usd = None
    for line in stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            if usage.get("output_tokens") is not None:
                # Codex does not expose exact model here; record family if known.
                observed_model_id = event.get("model") or "codex-gpt-5-family"
    return observed_model_id, cost_usd


def invoke_provider(provider: str, prompt: str, worktree: Path, role: str = "implementation") -> Dict[str, Any]:
    if provider == "claude_code":
        # Non-interactive print mode with explicit tool allowance; JSON output
        # exposes canonical model identity and cost metadata.
        cmd = [
            "claude", "-p", prompt,
            "--allowedTools", "Bash,Edit,Read",
            "--output-format", "json",
        ]
    elif provider == "codex":
        # --approve-for-me routes approvals through automatic review using the
        # workspace-write sandbox; it is mutually exclusive with --sandbox.
        # --json emits JSONL events that may include usage/model metadata.
        cmd = [
            "codex", "exec",
            "-C", str(worktree),
            "--approve-for-me",
            "--ephemeral",
            "--json",
            prompt,
        ]
    elif provider == "agy":
        cmd = ["agy", "-p", prompt, "--mode", "accept-edits"]
    elif provider == "local_ollama":
        return invoke_ollama(prompt, worktree, role)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    start = time.time()
    completed = subprocess.run(
        cmd,
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=900,
    )
    elapsed = round(time.time() - start, 3)
    return {
        "command": " ".join(shlex.quote(c) for c in cmd),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "duration_seconds": elapsed,
    }


def invoke_ollama(prompt: str, worktree: Path, role: str) -> Dict[str, Any]:
    import urllib.request
    payload = json.dumps({
        "model": "qwen2.5-coder:7b-instruct",
        "prompt": prompt,
        "stream": False,
        "keep_alive": "0",
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 - fixed local Ollama endpoint
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = round(time.time() - start, 3)
        return {
            "command": "ollama_local:qwen2.5-coder:7b-instruct",
            "stdout": data.get("response", ""),
            "stderr": "",
            "exit_code": 0,
            "duration_seconds": elapsed,
        }
    except Exception as exc:
        elapsed = round(time.time() - start, 3)
        return {
            "command": "ollama_local:qwen2.5-coder:7b-instruct",
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "duration_seconds": elapsed,
        }


def capture_files_changed(worktree: Path, base_sha: str) -> List[str]:
    """Return paths modified relative to the historical base SHA."""
    result = git(["diff", "--name-only", base_sha], cwd=worktree, check=False)
    return sorted([p for p in result.stdout.strip().split("\n") if p])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--role", default="implementation", choices=["implementation", "plan"])
    parser.add_argument("--plan-artifact", default=None)
    parser.add_argument("--context-docs", nargs="*", default=None)
    parser.add_argument("--worktree-base", required=True)
    parser.add_argument("--out-dir", default=str(ROOT / "dogfood" / "historical-reasoning-evidence" / "runs"))
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    catalog = load_catalog()
    fixture = catalog["fixtures"][args.fixture]
    fixture["fixture_id"] = args.fixture
    base_sha = fixture["historical_base_sha"]
    test_patch = PATCH_DIR / f"{args.fixture.lower().replace('-', '')}_test_only.patch"
    context_docs = [Path(d) for d in (args.context_docs or [])]

    run_id = args.run_id or f"{args.fixture}-{args.provider}-{args.role}-{hashlib.sha256(os.urandom(16)).hexdigest()[:8]}"
    worktree = Path(args.worktree_base)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run = RepairRun(
        run_id=run_id,
        fixture_id=args.fixture,
        provider=args.provider,
        role=args.role,
        base_sha=base_sha,
        worktree=worktree,
        test_patch=test_patch,
        verifier=fixture["deterministic_verifier"],
        context_docs=[str(d) for d in context_docs],
    )

    print(f"[{run_id}] Creating worktree at {worktree} from {base_sha}")
    create_worktree(base_sha, worktree)

    print(f"[{run_id}] Applying test patch {test_patch}")
    subprocess.run(["git", "apply", str(test_patch)], cwd=str(worktree), check=True)

    print(f"[{run_id}] Running initial verifier")
    initial = run_pytest(worktree, fixture["deterministic_verifier"])
    run.initial_status = "passed" if initial.returncode == 0 else "failed"
    run.initial_output = initial.stdout + "\n" + initial.stderr

    if run.initial_status == "passed":
        print(f"[{run_id}] WARNING: verifier already passed at base; aborting")
        run.final_status = "no_repair_needed"
        run.completed_at = datetime.now(timezone.utc).isoformat()
        _write_run(run, out_dir)
        return 0

    prompt = build_prompt(
        fixture,
        run.initial_output,
        args.role,
        args.plan_artifact,
        context_docs=context_docs,
        worktree=worktree,
        base_sha=base_sha,
    )
    print(f"[{run_id}] Invoking provider {args.provider} as {args.role}")
    result = invoke_provider(args.provider, prompt, worktree, args.role)
    run.provider_command = result["command"]
    run.provider_stdout = result["stdout"]
    run.provider_stderr = result["stderr"]
    run.provider_exit_code = result["exit_code"]
    run.provider_duration_seconds = result["duration_seconds"]
    if args.provider == "claude_code":
        model, cost = _extract_claude_model_and_cost(result["stdout"])
        run.observed_model_id = model
        run.cost_if_available = cost
    elif args.provider == "codex":
        model, cost = _extract_codex_model_and_cost(result["stdout"])
        run.observed_model_id = model
        run.cost_if_available = cost

    print(f"[{run_id}] Running final verifier")
    final = run_pytest(worktree, fixture["deterministic_verifier"])
    run.final_status = "passed" if final.returncode == 0 else "failed"
    run.final_output = final.stdout + "\n" + final.stderr
    run.files_changed = capture_files_changed(worktree, base_sha)
    run.completed_at = datetime.now(timezone.utc).isoformat()

    _write_run(run, out_dir)
    print(f"[{run_id}] Done: initial={run.initial_status} final={run.final_status}")
    return 0 if run.final_status == "passed" else 1


def _write_run(run: RepairRun, out_dir: Path) -> None:
    path = out_dir / f"{run.run_id}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(run.to_dict(), fh, indent=2)
    print(f"[{run.run_id}] Wrote {path}")


if __name__ == "__main__":
    sys.exit(main())
