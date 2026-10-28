#!/usr/bin/env python3
"""Run inference-free component suites; receipts are observations, not approvals."""

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "howl.operational_proof/v1"
GROUPS = {
    "plane-recovery": (
        "howlplane",
        [
            "tests/test_orchestration_handoff_recovery.py",
            "tests/test_interrupted_governance_recovery.py",
        ],
    ),
    "plane-security": (
        "howlplane",
        [
            "tests/test_human_approval_lifecycle.py",
            "tests/test_authority_execution_gap.py",
            "tests/test_evidence_schema_enforcement.py",
        ],
    ),
    "changeops-authority": ("howlchangeops", None),
    "relay": (
        "howlrelay",
        [
            "tests/test_checkpoint_security.py",
            "tests/test_policy.py",
            "tests/test_adapters.py",
        ],
    ),
    "proof": (
        "howlproof",
        [
            "tests/test_plane_bridge.py",
            "tests/test_attestation.py",
            "tests/test_verdict.py",
        ],
    ),
    "provider-core": ("howl-provider-core", ["tests"]),
}


def snapshot(repo):
    """Hash Git identity, names, modes and actual source bytes without exporting them."""
    repo = Path(repo).resolve()
    head = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    )
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo,
    ).split(b"\0")
    digest = hashlib.sha256(head.encode())
    for raw in sorted(set(names) - {b""}):
        path = repo / os.fsdecode(raw)
        digest.update(len(raw).to_bytes(8, "big") + raw)
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
            mode = b"symlink"
        elif path.is_file():
            content = path.read_bytes()
            mode = str(path.stat().st_mode & 0o777).encode()
        elif not path.exists():
            content, mode = b"", b"deleted"
        else:
            raise ValueError(f"unsupported source entry: {path}")
        digest.update(mode + b"\0" + hashlib.sha256(content).digest())
    return {"head": head, "sha256": digest.hexdigest()}


def run_command(command, repo, timeout):
    """Execute an operator-selected deterministic suite with bounded process lifetime."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    before = snapshot(repo)
    environment = os.environ.copy()
    environment.pop("HOWLPLANE_LIVE_PROVIDERS", None)
    environment.update(
        FORBID_LOCAL_INFERENCE="1",
        HOWL_FORBID_LOCAL_INFERENCE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(Path(repo).resolve() / "src"),
    )
    started = time.monotonic()
    timed_out = False
    exit_code = None
    error = None
    background_processes = False
    try:
        with subprocess.Popen(
            command,
            cwd=repo,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                exit_code = process.wait()
            finally:
                # Leader exit does not establish completion of its descendants.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    background_processes = True
                except ProcessLookupError:
                    pass
    except OSError as exc:
        error = type(exc).__name__
    after = snapshot(repo)
    return {
        "command": command,
        "repository": str(Path(repo).resolve()),
        "source": before,
        "source_after": after,
        "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "timed_out": timed_out,
        "error": error,
        "background_processes": background_processes,
        "status": "passed"
        if exit_code == 0
        and not timed_out
        and not error
        and not background_processes
        and before == after
        else "failed",
    }


def check_receipt(path):
    """Check recorded success and source freshness, not independent authenticity."""
    receipt = json.loads(Path(path).read_text())
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != SCHEMA
        or not isinstance(receipt.get("runs"), list)
        or not receipt["runs"]
    ):
        return False
    for run in receipt["runs"]:
        if (
            not isinstance(run, dict)
            or run.get("status") != "passed"
            or type(run.get("exit_code")) is not int
            or run.get("exit_code") != 0
            or run.get("timed_out")
            or run.get("error")
            or run.get("background_processes")
            or run.get("source") != run.get("source_after")
            or Path(path).resolve().is_relative_to(Path(run["repository"]).resolve())
            or (Path(path).parent.resolve() / Path(path).name).is_relative_to(
                Path(run["repository"]).resolve()
            )
            or snapshot(run["repository"]) != run["source"]
        ):
            return False
    return receipt.get("status") == "passed"


def write_receipt(path, receipt):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="directory containing component checkouts"
    )
    parser.add_argument(
        "--plane", type=Path, help="override Plane checkout, e.g. worktree"
    )
    parser.add_argument("--group", choices=sorted(GROUPS), action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    if args.check:
        try:
            valid = check_receipt(args.check.absolute())
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            valid = False
        print(
            "fresh verified observations"
            if valid
            else "failed, invalid or stale observations"
        )
        return 0 if valid else 1
    if (
        not args.root
        or not args.output
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        parser.error("--root, --output and a finite positive --timeout are required")
    runs = []
    groups = args.group or sorted(GROUPS)
    for group in groups:
        component = GROUPS[group][0]
        repo = (
            args.plane
            if component == "howlplane" and args.plane
            else args.root / component
        )
        if args.output.resolve().is_relative_to(repo.resolve()) or (
            args.output.parent.resolve() / args.output.name
        ).is_relative_to(repo.resolve()):
            parser.error("--output must be outside every measured repository")
    for group in groups:
        component, tests = GROUPS[group]
        repo = (
            args.plane
            if component == "howlplane" and args.plane
            else args.root / component
        )
        command = (
            ["go", "test", "-count=1", "./internal/authority"]
            if tests is None
            else [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *tests,
            ]
        )
        try:
            run = run_command(command, repo, args.timeout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run = {
                "status": "failed",
                "command": command,
                "repository": str(repo),
                "error": type(exc).__name__,
                "exit_code": None,
                "timed_out": False,
            }
        run["group"] = group
        runs.append(run)
    status = "passed" if all(run["status"] == "passed" for run in runs) else "failed"
    write_receipt(
        args.output,
        {
            "schema": SCHEMA,
            "status": status,
            "kind": "deterministic_component_suites",
            "runs": runs,
            "limitations": "No live inference, deployment or protected approval; "
            "receipt is not authenticated independently.",
        },
    )
    print(f"{status}: {len(runs)} groups; {args.output}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
