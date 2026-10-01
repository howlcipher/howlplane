"""`howlplane tia record|check`: the Test Impact Assessment evidence gate."""

import argparse
import json
import sys
from pathlib import Path


def _cli():
    from howlplane.control_plane import cli
    return cli


def _ledger(args: argparse.Namespace):
    ledger_file = _cli()._resolve_ledger_file(args)
    return _cli().EvidenceLedger(ledger_file)


def _record(args: argparse.Namespace) -> int:
    from howlplane.control_plane import test_impact
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
        entry = test_impact.record_assessment(_ledger(args), args.task_id, document, agent_id=args.agent_id)
    except (ValueError, OSError) as exc:
        raise OperatorFailure(OperatorError(
            "TIA_INVALID", f"The Test Impact Assessment was not recorded: {exc}",
            "A TIA must be complete and consistent before it counts as evidence.",
            "Fix the fields named above. The schema is schemas/test-impact-assessment.schema.json."))
    if getattr(args, "json", False):
        print(json.dumps({"task_id": args.task_id, "recorded": True, "entry_id": entry.entry_id}, indent=2))
    else:
        print(f"Test Impact Assessment recorded for {args.task_id}.")
    return 0


def _check(args: argparse.Namespace) -> int:
    from howlplane.control_plane import test_impact
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    if args.no_code_change:
        document, reason = None, ""
    else:
        document, reason = test_impact.check_assessment(_ledger(args), args.task_id)
        if document is None:
            raise OperatorFailure(OperatorError(
                "TIA_MISSING", f"Task {args.task_id} cannot ship: {reason}.",
                "A code-changing task needs a recorded Test Impact Assessment before completion.",
                "Record one with `howlplane tia record`, or pass --no-code-change if the task changed no code or tests.",
                f"howlplane tia record --task-id {args.task_id} --file tia.json"))
    if getattr(args, "json", False):
        print(json.dumps({"task_id": args.task_id, "ok": True, "no_code_change": bool(args.no_code_change),
                          "full_regression_required": (document or {}).get("full_regression_required")}, indent=2))
    else:
        print(f"Test Impact Assessment OK for {args.task_id}" + (" (no code change declared)." if args.no_code_change else "."))
    return 0


def cmd_tia(args: argparse.Namespace) -> int:
    return {"record": _record, "check": _check}[args.tia_action](args)
