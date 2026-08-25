#!/usr/bin/env python3
"""
Run a review topology experiment on a historical repair candidate diff.

Usage:
    python run_review_topology.py \
        --worktree /run/media/system/tallgeese/dev/howlplane/.worktrees/fix35-claude-001 \
        --base-sha dd66019fea14f1daca5fe18cc6adf27e9d377574 \
        --provider claude_code \
        --topology correctness_only \
        --out-file review_topology_correctness.json

Topologies:
    correctness_only   - single correctness-reviewer
    correctness_regression - correctness-reviewer + regression-reviewer
"""

import argparse
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ReviewTopologyRun:
    run_id: str
    fixture_id: str
    provider: str
    topology: str
    base_sha: str
    worktree: Path
    roles: List[str]
    findings: List[Dict[str, Any]] = field(default_factory=list)
    raw_outputs: List[str] = field(default_factory=list)
    provider_commands: List[str] = field(default_factory=list)
    provider_exit_codes: List[int] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fixture_id": self.fixture_id,
            "provider": self.provider,
            "topology": self.topology,
            "base_sha": self.base_sha,
            "worktree": str(self.worktree),
            "roles": self.roles,
            "findings": self.findings,
            "raw_outputs": self.raw_outputs,
            "provider_commands": self.provider_commands,
            "provider_exit_codes": self.provider_exit_codes,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


TOPOLOGIES = {
    "correctness_only": ["correctness-reviewer"],
    "correctness_regression": ["correctness-reviewer", "regression-reviewer"],
    "correctness_security": ["correctness-reviewer", "security-reviewer"],
}


def role_prompt(role: str, diff: str, fixture_id: str) -> str:
    return f"""You are a {role} reviewing a proposed code change in an isolated historical worktree.

Task context:
- Fixture: {fixture_id}
- This is a historical engineering replay; the diff under review is a proposed fix for a real defect.
- Do not read future commits, PR descriptions, or known historical patches.

Review the diff below and identify any findings that match your specialty. For each finding, provide:
- id: short unique identifier
- severity: blocker, high, medium, low, or informational
- category: correctness, regression, security, architecture, simplicity, or test
- location: file and approximate line(s)
- claim: concise problem statement
- evidence: why the diff supports the claim
- suggested_fix: how to address it

If you find no issues, return an empty findings list.

DIFF:
```diff
{diff[:12000]}
```

Respond ONLY with a JSON object in this exact shape (no markdown fences, no extra prose):
{{"findings": [{{"id": "...", "severity": "...", "category": "...", "location": "...", "claim": "...", "evidence": "...", "suggested_fix": "..."}}]}}
"""


def invoke_claude_review(prompt: str) -> Dict[str, Any]:
    cmd = ["claude", "-p", prompt]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return {
        "command": " ".join(shlex.quote(c) for c in cmd),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }


def invoke_codex_review(prompt: str, worktree: Path) -> Dict[str, Any]:
    cmd = [
        "codex", "exec",
        "-C", str(worktree),
        "--approve-for-me",
        "--ephemeral",
        prompt,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {
        "command": " ".join(shlex.quote(c) for c in cmd),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }


def parse_findings(raw: str) -> List[Dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    # Strip markdown fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "findings" in data:
            return data.get("findings", [])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--fixture-id", default="FIX-35")
    parser.add_argument("--provider", default="claude_code", choices=["claude_code", "codex"])
    parser.add_argument("--topology", required=True, choices=list(TOPOLOGIES.keys()))
    parser.add_argument("--source-paths", nargs="*", default=None, help="Paths to include in the reviewed diff")
    parser.add_argument("--out-file", required=True)
    args = parser.parse_args()

    worktree = Path(args.worktree)
    diff_cmd = ["git", "diff", args.base_sha]
    if args.source_paths:
        diff_cmd.extend(["--", *args.source_paths])
    diff_proc = subprocess.run(
        diff_cmd,
        cwd=str(worktree), capture_output=True, text=True, check=True,
    )
    diff = diff_proc.stdout

    run = ReviewTopologyRun(
        run_id=f"{args.fixture_id}-{args.topology}-{args.provider}-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        fixture_id=args.fixture_id,
        provider=args.provider,
        topology=args.topology,
        base_sha=args.base_sha,
        worktree=worktree,
        roles=TOPOLOGIES[args.topology],
    )

    for role in TOPOLOGIES[args.topology]:
        prompt = role_prompt(role, diff, args.fixture_id)
        if args.provider == "claude_code":
            result = invoke_claude_review(prompt)
        else:
            result = invoke_codex_review(prompt, worktree)
        run.provider_commands.append(result["command"])
        run.raw_outputs.append(result["stdout"] + "\n" + result["stderr"])
        run.provider_exit_codes.append(result["exit_code"])
        findings = parse_findings(result["stdout"])
        for f in findings:
            f["reviewer_role"] = role
        run.findings.extend(findings)

    run.completed_at = datetime.now(timezone.utc).isoformat()
    with open(args.out_file, "w", encoding="utf-8") as fh:
        json.dump(run.to_dict(), fh, indent=2)
    print(f"Wrote {args.out_file}")
    print(f"Findings: {len(run.findings)} ({args.topology})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
