"""Governance command handlers (approve, reject, resume, cancel, unlock).

Moved out of ``cli.py`` (backlog #78). ``cli.py`` re-exports every name and these
handlers look shared helpers up on the ``cli`` module at call time, so existing
imports and test monkeypatches of ``cli.<name>`` keep working unchanged.
"""

import argparse


def _cli():
    from howlplane.control_plane import cli
    return cli


def _owner_decision_spec(args: argparse.Namespace, decision: str, flag: str):
    """(decide, error class, titles) for the ``--work-item`` or ``--proposal`` decision."""
    reason = getattr(args, "reason", None)
    if flag == "work-item":
        from howlplane.control_plane.factory.work_item_decision import WorkItemDecisionError, decide_work_item
        try:
            target = str(_cli()._resolve_repo(args))
        except Exception:
            target = None
        return (lambda ledger: decide_work_item(args.state_dir, args.work_item, decision, target_dir=target,
                                                reason=reason, ledger=ledger),
                WorkItemDecisionError,
                ("WORK ITEM REQUEUED", "WORK ITEM REJECTED",
                 "The Factory will pick it up again. Governed checks and authority limits still apply."))
    from howlplane.control_plane.factory.proposal_decision import ProposalDecisionError, decide_proposal
    return (lambda ledger: decide_proposal(args.state_dir, args.proposal, decision, reason=reason, ledger=ledger),
            ProposalDecisionError,
            ("PROPOSAL ACCEPTED", "PROPOSAL REJECTED",
             "Recorded as accepted. No repository was created; a later governed bootstrap step acts on it."))


def _run_owner_decision(args: argparse.Namespace, decision: str, flag: str) -> int:
    """Shared body of the ``--work-item`` and ``--proposal`` decisions (one authority path)."""
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    from howlplane.control_plane.factory.owner_decision import require_state_dir
    require_state_dir(args, f"--{flag}", flag.replace("-", " "))
    ledger_file = _cli()._resolve_ledger_file(args)
    ledger = _cli().EvidenceLedger(ledger_file) if ledger_file else None
    decide, error_cls, (approved_title, rejected_title, approved_note) = _owner_decision_spec(args, decision, flag)
    try:
        result = decide(ledger)
    except error_cls as exc:
        raise OperatorFailure(OperatorError(exc.code, str(exc), exc.why, exc.next_action, exc.command))
    if getattr(args, "json", False):
        import json
        print(json.dumps(result, indent=2))
        return 0
    item_id = result.get("work_item_id") or result["proposal_id"]
    print(f"{approved_title if decision == 'approved' else rejected_title}: "
          f"{item_id} ({result['from_state']} -> {result['state']})")
    if decision == "approved":
        print(approved_note)
    if result.get("reason"):
        print(f"Reason: {result['reason']}")
    return 0


def _handle_work_item_decision(args: argparse.Namespace, decision: str) -> int:
    return _run_owner_decision(args, decision, "work-item")


def _handle_proposal_decision(args: argparse.Namespace, decision: str) -> int:
    return _run_owner_decision(args, decision, "proposal")


def _handle_decision(args: argparse.Namespace, decision: str) -> int:
    from howlplane.control_plane.presentation.errors import OperatorError, OperatorFailure
    targets = [getattr(args, name, None) for name in ("task_id", "work_item", "proposal")]
    if sum(1 for target in targets if target) != 1:
        raise OperatorFailure(OperatorError(
            "DECISION_TARGET_REQUIRED", "Give exactly one of TASK_ID, --work-item or --proposal.",
            "A decision applies to one governed task, one parked Factory work item, or one repository proposal.",
            "List what is waiting and copy the exact command.", "howlplane factory status"))
    if getattr(args, "work_item", None):
        return _cli()._handle_work_item_decision(args, decision)
    if getattr(args, "proposal", None):
        return _cli()._handle_proposal_decision(args, decision)
    target_repo = str(_cli()._resolve_repo(args))
    ledger_file = _cli()._resolve_ledger_file(args)
    ledger = _cli().EvidenceLedger(ledger_file) if ledger_file else None
    fn = _cli().HumanLifecycleManager.approve if decision == "approved" else _cli().HumanLifecycleManager.reject
    record = fn(
        target_repo=target_repo,
        task_id=args.task_id,
        reason=getattr(args, "reason", None),
        operator_source="cli",
        ledger=ledger,
    )
    if getattr(args, "json", False):
        print(record.to_json())
    else:
        title = "TASK AUTHORIZED" if decision == "approved" else "TASK REJECTED"
        print("=" * 60)
        print(f"HOWLPLANE — {title}: {record.task_id}")
        print("=" * 60)
        print(f"Task:               {record.task_id}")
        print(f"Decision:           {decision.upper()}")
        print(f"Timestamp:          {record.timestamp}")
        if record.reason:
            print(f"Reason:             {record.reason}")
        if decision == "approved":
            print("")
            print("Next Action:")
            print(f"  howlplane resume {record.task_id}")
        else:
            print("Terminal state:     FAILED (Rejected)")
        print("=" * 60)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _cli()._handle_decision(args, "approved")


def cmd_reject(args: argparse.Namespace) -> int:
    return _cli()._handle_decision(args, "rejected")


def cmd_resume(args: argparse.Namespace) -> int:
    target_repo = str(_cli()._resolve_repo(args))
    ledger_file = _cli()._resolve_ledger_file(args)
    ledger = _cli().EvidenceLedger(ledger_file) if ledger_file else None
    res = _cli().HumanLifecycleManager.resume(
        target_repo=target_repo,
        task_id=args.task_id,
        ledger=ledger,
    )
    if getattr(args, "json", False):
        import json
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(f"Task '{res.task_id}' RESUMED. Final state: {res.final_state.upper()} (Exit {res.exit_code}).")
    return res.exit_code


def cmd_cancel(args: argparse.Namespace) -> int:
    target_repo = str(_cli()._resolve_repo(args))
    ledger_file = _cli()._resolve_ledger_file(args)
    ledger = _cli().EvidenceLedger(ledger_file) if ledger_file else None
    res = _cli().HumanLifecycleManager.cancel(
        target_repo=target_repo,
        task_id=args.task_id,
        reason=getattr(args, "reason", None),
        ledger=ledger,
    )
    if getattr(args, "json", False):
        import json
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(f"Task '{res.task_id}' CANCELLED safely. Code changes preserved in working tree.")
    return res.exit_code


def _lock_candidates(repo_dir, task_id):
    """Every lock that could be holding this task back, in reclaim order.

    `ai status` reports the repository lock, so `ai unlock` has to be able to
    act on it. Reclaiming only `.task.lock` meant the command truthfully said
    "nothing to reclaim" while `.git/howlplane.lock` kept the task unrecoverable
    (HOWLFRAM-SLOPFIX-06).
    """
    from howlplane.control_plane.locking import get_repo_lock_path, get_task_lock_path

    return [
        ("task run", get_task_lock_path(repo_dir, task_id)),
        ("repository", get_repo_lock_path(repo_dir)),
    ]


def _lock_relevance(owner: dict, task_id: str):
    """Reports whether a lock belongs to this task, and why not when it does not.

    Deliberately narrow: this is a reclaim path, not a general lock remover. A
    lock written for another task, or for an operation that is not a task's
    repository mutation, is never this command's business.
    """
    if owner.get("task_id") != task_id:
        return False, (
            f"held for task '{owner.get('task_id')}', not '{task_id}'"
        )
    if owner.get("lock_type") not in ("task_run", "repository_mutation"):
        return False, (
            f"lock type '{owner.get('lock_type')}' is not a task-owned lock"
        )
    return True, ""


def cmd_unlock(args: argparse.Namespace) -> int:
    """Reclaims the locks holding a task back, when their owners are gone.

    The one explicit, audited takeover path. `ai resume` deliberately keeps its
    fail-closed behavior, so nothing ever steals a lock implicitly; a person
    asks for this, and the reclamation is recorded (HOWLFRAM-SLOPFIX-05).
    Both the task-run lock and the repository lock are inspected, so what this
    command acts on matches what `ai status` reports (HOWLFRAM-SLOPFIX-06).
    """
    from howlplane.control_plane.locking import (
        LockError,
        classify_lock_owner,
        reclaim_lock,
    )
    from howlplane.control_plane.atomic_io import safe_load_json

    target_repo = str(_cli()._resolve_repo(args))
    ledger_file = _cli()._resolve_ledger_file(args)
    ledger = _cli().EvidenceLedger(ledger_file) if ledger_file else None
    as_json = bool(getattr(args, "json", False))

    def audit(action, result, artifact=None, metadata=None):
        if ledger is None:
            return
        ledger.append_entry(
            _cli().EvidenceEntry(
                task_id=args.task_id,
                agent_id="human_operator",
                action=action,
                command=f"ai unlock {args.task_id}",
                result=result,
                artifact=artifact,
                repository=str(target_repo),
                metadata=metadata or {},
            )
        )

    audit("unlock_requested", "REQUESTED")

    inspected = []
    reclaimed = []
    refusals = []

    for label, lock_path in _cli()._lock_candidates(target_repo, args.task_id):
        if not lock_path.exists():
            continue
        try:
            owner = safe_load_json(lock_path)
        except Exception as err:
            refusals.append(f"{label} lock at '{lock_path}' is unreadable: {err}")
            continue

        relevant, why_not = _cli()._lock_relevance(owner, args.task_id)
        state, reason = classify_lock_owner(
            owner.get("pid", -1),
            owner.get("hostname", ""),
            owner.get("process_create_time", 0.0),
        )
        inspected.append(
            {
                "scope": label,
                "path": str(lock_path),
                "owner_state": state.value,
                "reason": reason,
                "relevant": relevant,
                "pid": owner.get("pid"),
                "hostname": owner.get("hostname"),
                "operation": owner.get("operation"),
                "task_id": owner.get("task_id"),
            }
        )

        if not as_json:
            print(f"{label.capitalize()} lock: {lock_path}")
            print(
                f"  Owner: pid {owner.get('pid')} @ {owner.get('hostname')} "
                f"({owner.get('command')}) -- {state.value}"
            )
            print(f"  {reason}")

        if not relevant:
            msg = f"Refusing to reclaim {label} lock: {why_not}."
            refusals.append(msg)
            if not as_json:
                print(f"  {msg}")
            audit(
                "unlock_refused",
                "NOT_THIS_TASK",
                artifact=str(lock_path),
                metadata={"scope": label, "reason": why_not},
            )
            continue

        try:
            record = reclaim_lock(lock_path)
        except LockError as err:
            refusals.append(str(err))
            if not as_json:
                print(f"  ERROR: {err}")
            audit(
                "unlock_refused",
                state.value,
                artifact=str(lock_path),
                metadata={"scope": label, "reason": str(err)},
            )
            continue

        payload = record.to_dict()
        payload["scope"] = label
        reclaimed.append(payload)
        # Keeps the established ledger vocabulary rather than introducing a
        # second name for the same event.
        audit(
            "stale_lock_reclaimed",
            record.owner_state,
            artifact=record.lock_path,
            metadata=payload,
        )
        if not as_json:
            print(f"  Reclaimed {record.owner_state} {label} lock.")

    if as_json:
        import json
        print(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "inspected": inspected,
                    "reclaimed": reclaimed,
                    "refused": refusals,
                },
                indent=2,
            )
        )

    if not inspected:
        if not as_json:
            print(f"No locks held for '{args.task_id}'. Nothing to reclaim.")
        audit("unlock_requested", "NO_OP")
        return 0

    if reclaimed:
        if not as_json:
            print(f"You can now run: howlplane resume {args.task_id}")
        return 0

    return 1 if refusals else 0
