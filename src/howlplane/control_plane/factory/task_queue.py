"""Finite factory over an explicit queue of human-approved engineering tasks.

Factory v0.1 is a thin supervisor. The queue file is input only: the factory
never writes it, so it can never approve, reorder, or invent work. Each
eligible task becomes one ordinary ``howlplane orchestrate`` session, and that
session alone owns routing, failover, recovery, verification, independent
audit, and progress. The factory selects the next task, launches its session,
records which session it became and how it ended, and continues until nothing
eligible remains. It never commits, merges, or pushes, and it exits once the
queue offers no further eligible task.

Task identity and revision are distinct. The ``id`` names the task; its
fingerprint names the revision, a digest of the execution contract (see
``_fingerprint``). Every outcome is scoped to one revision:

* same id, same fingerprint: the same revision. A finished outcome is not
  rerun without ``--retry``; when its session is still resumable (for example
  ``HANDOFF REQUIRED``), ``--retry`` resumes that session instead of starting
  a parallel one, so orchestration can reconcile any external repair.
* same id, new fingerprint: a new revision, eligible without ``--retry``. A
  still-resumable session of an older revision is retired through
  ``orchestration.supersede`` (kept as ``SUPERSEDED`` history, recorded in
  the ledger) immediately before the new revision launches.

Dependencies are satisfied only by the current revision of the dependency.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from howlplane.control_plane import orchestration
from howlplane.control_plane.atomic_io import safe_load_json

QUEUE_SCHEMA = "howlplane.factory_queue/v1"
LEDGER_SCHEMA = "howlplane.factory_ledger/v1"
QUEUE_STATES = ("PROPOSED", "APPROVED", "HOLD", "CANCELLED")
DONE = frozenset({"COMPLETE", "COMPLETE WITH WARNINGS"})
INTERRUPTED = "INTERRUPTED"
LAUNCH_FAILED = "LAUNCH_FAILED"
SUPERSEDED = orchestration.SUPERSEDED
SUPERSEDE_REASON = "task definition changed"
LEDGER_EVENTS = frozenset({"started", "finished", "superseded"})
# Fields that decide when or whether a task runs, not what its session does.
# Changing them reschedules the same revision instead of creating a new one.
SCHEDULING_FIELDS = frozenset({"status", "priority"})
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
OPTION_FIELDS = ("orchestrator", "strategy", "policy", "failover", "models", "fallbacks")
TASK_FIELDS = frozenset({"id", "goal", "status", "repo", "priority", "depends_on", "constraints", "verify", "agents",
                         "execution_budget", *OPTION_FIELDS})

Launcher = Callable[[argparse.Namespace], int]


class _RaisingParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def _orchestrate_parser() -> argparse.ArgumentParser:
    """The real ``orchestrate`` parser, so queue options obey its exact choices."""
    root = _RaisingParser(add_help=False)
    common = _RaisingParser(add_help=False)
    common.add_argument("--repo")
    orchestration.add_parser(root.add_subparsers(dest="subcommand"), common)
    return root


@dataclass(frozen=True)
class QueueTask:
    id: str
    goal: str
    status: str
    repo: Path
    priority: int
    position: int
    depends_on: tuple[str, ...]
    fingerprint: str
    session_args: argparse.Namespace

    @property
    def revision(self) -> str:
        return self.fingerprint[:12]

    def launch_args(self, quiet: bool, no_progress: bool, heartbeat: float, resume: bool = False) -> argparse.Namespace:
        args = argparse.Namespace(**vars(self.session_args))
        args.quiet, args.no_progress, args.heartbeat = quiet, no_progress, heartbeat
        if resume:
            args.input = "resume"
        return args


def _fingerprint(raw: dict[str, Any], repo: Path) -> str:
    """Digest of the execution contract: every field except ``SCHEDULING_FIELDS``.

    That covers the id, resolved repository, goal, constraints, verification,
    dependency set, agents, budget, and session options. Dependencies are part
    of the contract because they decide the base the work builds on; their
    order is not. Approval status and priority only decide when a task runs.
    """
    contract = {key: value for key, value in raw.items() if key not in SCHEDULING_FIELDS}
    contract["repo"] = str(repo)
    if "depends_on" in contract:
        contract["depends_on"] = sorted(set(contract["depends_on"]))
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return list(value)


def _task(raw: Any, position: int, base: Path, default_repo: Path, parser: argparse.ArgumentParser) -> QueueTask:
    if not isinstance(raw, dict):
        raise ValueError(f"Queue entry {position} is not an object")
    label = f"Queue task {raw.get('id', position)!r}"
    unknown = sorted(set(raw) - TASK_FIELDS)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    task_id, goal, status = raw.get("id"), raw.get("goal"), raw.get("status")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ValueError(f"{label} needs an id matching {TASK_ID.pattern}")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError(f"{label} needs a non-empty goal")
    if goal.strip() in {"resume", "inspect", "discard"}:
        raise ValueError(f"{label} goal collides with an orchestrate session operation")
    if status not in QUEUE_STATES:
        raise ValueError(f"{label} status must be one of {', '.join(QUEUE_STATES)}")
    priority = raw.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError(f"{label} priority must be an integer")
    repo = raw.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo):
        raise ValueError(f"{label} repo must be a path")
    resolved = (base / repo).resolve() if repo else default_repo
    argv = ["orchestrate", f"--repo={resolved}"]
    for name in OPTION_FIELDS:
        if name in raw:
            if not isinstance(raw[name], str):
                raise ValueError(f"{label} {name} must be a string")
            argv.append(f"--{name}={raw[name]}")
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError(f"{label} agents must map agent to AUTO, RESERVED, or UNAVAILABLE")
    for agent, availability in agents.items():
        if agent not in orchestration.AGENTS or not isinstance(availability, str):
            raise ValueError(f"{label} names unknown agent {agent!r}")
        argv.append(f"--{agent.replace('_', '-')}={availability}")
    budget = raw.get("execution_budget")
    if budget is not None:
        if isinstance(budget, (str, int)) and not isinstance(budget, bool):
            argv.append(f"--execution-budget={budget}")
        elif isinstance(budget, list):
            for item in budget:
                if isinstance(item, (str, int)) and not isinstance(item, bool):
                    argv.append(f"--execution-budget={item}")
                else:
                    raise ValueError(f"{label} execution_budget entries must be strings or integers")
        else:
            raise ValueError(f"{label} execution_budget must be a list, string, or integer")
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc
    # Goal, constraints, and verification bypass argv so leading dashes stay literal.
    args.input = goal.strip()
    args.constraint = _strings(raw.get("constraints", []), f"{label} constraints")
    args.verify = _strings(raw["verify"], f"{label} verify") if raw.get("verify") is not None else None
    args.retain_report = True  # the session report is the evidence the ledger points at
    args.separate = False
    args.json = False
    depends_on = tuple(_strings(raw.get("depends_on", []), f"{label} depends_on"))
    return QueueTask(task_id, goal.strip(), status, resolved, priority, position, depends_on,
                     _fingerprint(raw, resolved), args)


def load_queue(path: Path, default_repo: Path) -> list[QueueTask]:
    """Validate the whole queue before anything runs; one bad entry rejects it all."""
    document = safe_load_json(path)
    if not isinstance(document, dict) or document.get("schema") != QUEUE_SCHEMA:
        raise ValueError(f"Queue must be a JSON object with schema {QUEUE_SCHEMA}")
    if not isinstance(document.get("tasks"), list):
        raise ValueError("Queue needs a tasks list")
    base = path.resolve().parent
    if document.get("repo") is not None:
        if not isinstance(document["repo"], str) or not document["repo"]:
            raise ValueError("Queue repo must be a path")
        default_repo = (base / document["repo"]).resolve()
    parser = _orchestrate_parser()
    tasks = [_task(raw, index, base, default_repo, parser) for index, raw in enumerate(document["tasks"])]
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate queue task ids: {', '.join(duplicates)}")
    known = set(ids)
    for task in tasks:
        missing = [dep for dep in task.depends_on if dep not in known]
        if missing:
            raise ValueError(f"Queue task {task.id!r} depends on unknown tasks: {', '.join(missing)}")
    _reject_cycles(tasks)
    return tasks


def _reject_cycles(tasks: list[QueueTask]) -> None:
    edges = {task.id: task.depends_on for task in tasks}
    finished: set[str] = set()

    def visit(task_id: str, trail: tuple[str, ...]) -> None:
        if task_id in trail:
            raise ValueError(f"Queue dependency cycle: {' -> '.join((*trail, task_id))}")
        if task_id in finished:
            return
        for dep in edges[task_id]:
            visit(dep, (*trail, task_id))
        finished.add(task_id)

    for task_id in edges:
        visit(task_id, ())


def ledger_path(queue: Path) -> Path:
    """Outside every repository, so recording a result never dirties a worktree."""
    digest = hashlib.sha256(str(queue.resolve()).encode()).hexdigest()[:16]
    return orchestration.state_root().parent / "factory_queue" / f"{digest}.jsonl"


class Ledger:
    """Append-only record of which session each task revision became and how it ended.

    Events: ``started`` and ``finished`` for a launch or resume of one revision,
    and ``superseded`` when an older revision's session was retired for a newer one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        if path.exists():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Factory ledger {path} line {number} is corrupt: {exc}") from exc
                if not isinstance(record, dict) or record.get("schema") != LEDGER_SCHEMA:
                    raise ValueError(f"Factory ledger {path} line {number} has an unknown schema")
                if (record.get("event") not in LEDGER_EVENTS or not isinstance(record.get("task_id"), str)
                        or not isinstance(record.get("fingerprint"), str) or not isinstance(record.get("at"), str)):
                    raise ValueError(f"Factory ledger {path} line {number} is not a factory ledger record")
                self.records.append(record)

    def append(self, record: dict[str, Any]) -> None:
        record = json.loads(orchestration.redact(json.dumps({"schema": LEDGER_SCHEMA, "at": orchestration.now(), **record})))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.records.append(record)

    def _runs(self, task_id: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["task_id"] == task_id and record["event"] != "superseded"]

    def outcome(self, task: QueueTask) -> dict[str, Any] | None:
        """Latest outcome of this task's current revision, preferring live session evidence over the ledger."""
        runs = [record for record in self._runs(task.id) if record["fingerprint"] == task.fingerprint]
        if not runs:
            return None
        latest = runs[-1]
        if latest["event"] == "started":
            session = _load_session(latest.get("session_id")) or _find_session(task, since=latest["at"])
            status = session["status"] if session else INTERRUPTED
            return {**latest, "event": "finished", "status": status, "session_id": session and session["id"],
                    "live": bool(session)}
        session = _load_session(latest.get("session_id"))
        if session and session.get("status") != latest["status"]:
            return {**latest, "status": session["status"], "live": True}
        return latest

    def completed(self, task: QueueTask) -> bool:
        """The current revision finished successfully; an older revision's success does not count."""
        if any(record["fingerprint"] == task.fingerprint and record.get("status") in DONE
               for record in self._runs(task.id)):
            return True
        return (self.outcome(task) or {}).get("status") in DONE

    def resumable_session(self, task: QueueTask) -> dict[str, Any] | None:
        """The current revision's own session, when orchestration can still resume it."""
        outcome = self.outcome(task)
        session = _load_session(outcome.get("session_id")) if outcome else None
        return session if session and orchestration.is_resumable(session) else None

    def stale_sessions(self, task: QueueTask) -> list[dict[str, Any]]:
        """Resumable sessions this ledger proves belong to an older revision of this task.

        Proof is a ledger record naming both this task id and the session id
        under a different fingerprint, with no record tying that session to
        the current revision. Sessions the ledger cannot attribute are never
        returned, so they keep blocking their repository.
        """
        revisions: dict[str, set[str]] = {}
        for record in self._runs(task.id):
            if isinstance(record.get("session_id"), str):
                revisions.setdefault(record["session_id"], set()).add(record["fingerprint"])
        stale = []
        for session_id, fingerprints in revisions.items():
            if task.fingerprint in fingerprints or len(fingerprints) != 1:
                continue
            session = _load_session(session_id)
            if session and orchestration.is_resumable(session):
                stale.append({"session_id": session_id, "fingerprint": next(iter(fingerprints)),
                              "status": session.get("status"), "repository": session.get("repository")})
        return stale

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another factory run is already working this queue") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _load_session(session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    try:
        path = orchestration.path_for(orchestration.state_root(), session_id)
    except ValueError:
        return None
    return safe_load_json(path) if path.exists() else None


def _sessions(repo: Path) -> list[dict[str, Any]]:
    """Every session for the repository; orchestration records the Git toplevel."""
    root = orchestration.state_root()
    if not root.exists():
        return []
    try:
        repo = Path(orchestration.git(repo, "rev-parse", "--show-toplevel")).resolve()
    except (OSError, ValueError):
        pass
    return orchestration.active_sessions(root, repo, include_terminal=True)


def _find_session(task: QueueTask, since: str) -> dict[str, Any] | None:
    goal = orchestration.redact(task.goal)
    matches = [doc for doc in _sessions(task.repo) if doc.get("goal") == goal and doc.get("created_at", "") >= since]
    return matches[0] if matches else None


@dataclass(frozen=True)
class Dispatch:
    """What the factory will do for one selected task, decided before anything mutates.

    ``action`` is ``start`` (a fresh session) or ``resume`` (the current
    revision's own resumable session). ``supersede`` lists older-revision
    sessions to retire first.
    """
    task: QueueTask
    action: str
    session_id: str | None = None
    supersede: tuple[dict[str, Any], ...] = ()

    def describe(self) -> dict[str, Any]:
        return {"task_id": self.task.id, "revision": self.task.revision, "action": self.action,
                "session_id": self.session_id,
                "supersede": [{"session_id": item["session_id"], "revision": item["fingerprint"][:12],
                               "status": item["status"]} for item in self.supersede]}


def _dispatch(task: QueueTask, ledger: Ledger) -> tuple[Dispatch | None, str | None]:
    """Decide start, resume, or supersede-then-start, or say why the repository blocks it.

    Worktree ownership is orchestration's rule: at most one resumable session
    per repository. Only this task's own current-revision session, or sessions
    the ledger proves are its older revisions, may be in the way; any other
    resumable session blocks, and nothing is retired to make room.
    """
    repo = task.repo
    if not repo.is_dir():
        return None, f"repository {repo} does not exist"
    try:
        root = Path(orchestration.git(repo, "rev-parse", "--show-toplevel")).resolve()
        dirty = orchestration.git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, ValueError) as exc:
        return None, f"repository {repo} is not inspectable: {exc}"
    own = ledger.resumable_session(task)
    stale = ledger.stale_sessions(task)
    ours = {item["session_id"] for item in stale} | ({own["id"]} if own else set())
    foreign = [doc for doc in _sessions(root) if orchestration.is_resumable(doc) and doc["id"] not in ours]
    if foreign:
        return None, (f"repository {root} has an unfinished orchestration session {foreign[0]['id'][:8]} "
                      f"({foreign[0].get('status')}) that is not this task's; "
                      "use howlplane orchestrate resume, inspect, or discard")
    if own:
        # Resume reconciles whatever is in the worktree, including an external repair.
        return Dispatch(task, "resume", own["id"], tuple(stale)), None
    if dirty:
        return None, (f"repository {root} has uncommitted changes; commit or discard the previous result, "
                      "or give the task its own worktree")
    return Dispatch(task, "start", None, tuple(stale)), None


def eligibility(task: QueueTask, ledger: Ledger, retry: set[str], attempted: set[str]) -> str | None:
    """None when the current revision may run now, otherwise the reason it may not."""
    if task.status != "APPROVED":
        return f"status {task.status} is not APPROVED"
    if task.id in attempted:
        return "already attempted in this run"
    if ledger.completed(task):
        return "already complete"
    outcome = ledger.outcome(task)
    if outcome is not None and task.id not in retry:
        hint = f"pass --retry {task.id}"
        if ledger.resumable_session(task):
            hint += f" to resume session {outcome['session_id'][:8]}"
        return f"previous outcome {outcome['status']}; edit the task or {hint}"
    return None


def next_task(tasks: list[QueueTask], ledger: Ledger, retry: set[str],
              attempted: set[str]) -> tuple[Dispatch | None, dict[str, str]]:
    """Deterministic selection: lowest priority value first, then queue order. Never mutates anything."""
    skipped: dict[str, str] = {}
    done = {task.id for task in tasks if ledger.completed(task)}
    for task in sorted(tasks, key=lambda item: (item.priority, item.position)):
        dispatch = None
        reason = eligibility(task, ledger, retry, attempted)
        if reason is None:
            waiting = [dep for dep in task.depends_on if dep not in done]
            if waiting:
                reason = f"waiting on {', '.join(waiting)}"
            else:
                dispatch, reason = _dispatch(task, ledger)
        if dispatch is not None:
            return dispatch, skipped
        skipped[task.id] = reason or "not eligible"
    return None, skipped


class QueueFactory:
    """Select, launch, record, continue. Finite by construction: one dispatch per task per run."""

    def __init__(self, tasks: list[QueueTask], ledger: Ledger, launcher: Launcher | None = None,
                 retry: set[str] | None = None, stream: TextIO | None = None) -> None:
        self.tasks = tasks
        self.ledger = ledger
        self.launcher = launcher or orchestration.command
        self.retry = retry or set()
        self.stream = stream or sys.stderr
        self.run_id = os.urandom(8).hex()

    def _say(self, label: str, message: str) -> None:
        self.stream.write(f"FACTORY {label:<9} {orchestration.redact(message)}\n")
        self.stream.flush()

    def plan(self) -> dict[str, Any]:
        """What a run would do next. Reads only: no session, ledger, or queue changes."""
        choice, skipped = next_task(self.tasks, self.ledger, self.retry, set())
        return {"next": choice.task.id if choice else None, "dispatch": choice.describe() if choice else None,
                "skipped": skipped}

    def run(self, max_tasks: int | None = None, stop_on_failure: bool = False, quiet: bool = False,
            no_progress: bool = False, heartbeat: float = 30.0) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        superseded: list[dict[str, Any]] = []
        attempted: set[str] = set()
        blocked: dict[str, str] = {}
        skipped: dict[str, str] = {}
        stop_reason = "no eligible task remains"
        with self.ledger.exclusive():
            while max_tasks is None or len(results) < max_tasks:
                dispatch, skipped = next_task(self.tasks, self.ledger, self.retry, attempted)
                if dispatch is None:
                    break
                task = dispatch.task
                attempted.add(task.id)
                self._say("SELECT", f"{task.id}: {task.goal}")
                try:
                    superseded.extend(self._supersede(dispatch))
                except ValueError as exc:
                    # Fail closed: the old revision still owns its worktree, so the new one cannot launch.
                    blocked[task.id] = f"could not retire previous revision: {exc}"
                    self._say("BLOCKED", f"{task.id}: {blocked[task.id]}")
                    continue
                if dispatch.action == "resume":
                    self._say("RESUME", f"{task.id} revision {task.revision}; orchestration session {dispatch.session_id}")
                else:
                    self._say("START", f"{task.id} revision {task.revision}")
                args = task.launch_args(quiet, no_progress, heartbeat, resume=dispatch.action == "resume")
                outcome = self._launch(dispatch, args)
                results.append(outcome)
                session = f" (session {outcome['session_id']})" if outcome["session_id"] else ""
                self._say("RESULT", f"{task.id}: {outcome['status']}{session}")
                if outcome["status"] == INTERRUPTED:
                    stop_reason = "interrupted"
                    break
                if stop_on_failure and outcome["status"] not in DONE:
                    stop_reason = f"stopped after {task.id} ended {outcome['status']}"
                    break
            else:
                stop_reason = f"reached --max-tasks {max_tasks}"
        skipped_final = {k: v for k, v in skipped.items() if k not in attempted}
        skipped_final.update(blocked)
        return {"run_id": self.run_id, "results": results, "superseded": superseded, "blocked": blocked,
                "skipped": skipped_final, "stop_reason": stop_reason}

    def _supersede(self, dispatch: Dispatch) -> list[dict[str, Any]]:
        """Retire older revisions through orchestration, then record it; the session keeps its history."""
        task, records = dispatch.task, []
        for stale in dispatch.supersede:
            self._say("SUPERSEDE", f"{task.id} revision {stale['fingerprint'][:12]} -> {task.revision}; "
                                   f"retiring orchestration session {stale['session_id']}")
            orchestration.supersede(orchestration.state_root(), stale["session_id"], SUPERSEDE_REASON,
                                    f"factory task {task.id} revision {task.revision}")
            self.ledger.append({"event": "superseded", "task_id": task.id, "fingerprint": stale["fingerprint"],
                                "replacement_fingerprint": task.fingerprint, "session_id": stale["session_id"],
                                "previous_status": stale["status"], "status": SUPERSEDED,
                                "reason": SUPERSEDE_REASON, "run_id": self.run_id})
            records.append(self.ledger.records[-1])
        return records

    def _launch(self, dispatch: Dispatch, args: argparse.Namespace) -> dict[str, Any]:
        task = dispatch.task
        before = {doc["id"] for doc in _sessions(task.repo)}
        started = {"event": "started", "task_id": task.id, "fingerprint": task.fingerprint, "action": dispatch.action,
                   "run_id": self.run_id, "repo": str(task.repo)}
        if dispatch.session_id:
            started["session_id"] = dispatch.session_id
        self.ledger.append(started)
        error = None
        try:
            exit_code = self.launcher(args)
        except KeyboardInterrupt:
            exit_code = 130
        except Exception as exc:  # the session's own failure is evidence, not a factory crash
            exit_code, error = 1, str(exc)
        if dispatch.session_id:
            session = _load_session(dispatch.session_id)
        else:
            created = [doc for doc in _sessions(task.repo) if doc["id"] not in before]
            session = created[0] if created else None
        if session:
            status = session["status"]
        else:
            status = INTERRUPTED if exit_code == 130 else LAUNCH_FAILED
        record = {"event": "finished", "task_id": task.id, "fingerprint": task.fingerprint, "action": dispatch.action,
                  "run_id": self.run_id, "status": status, "exit_code": exit_code,
                  "session_id": session["id"] if session else None}
        if error:
            record["error"] = error[:500]
        self.ledger.append(record)
        return self.ledger.records[-1]


def _describe(dispatch: dict[str, Any]) -> str:
    lines = []
    for item in dispatch["supersede"]:
        lines.append(f"would supersede previous revision {item['revision']} -> {dispatch['revision']}; "
                     f"would retire orchestration session {item['session_id']} ({item['status']})")
    if dispatch["action"] == "resume":
        lines.append(f"would resume revision {dispatch['revision']} session {dispatch['session_id']}")
    else:
        lines.append(f"would start revision {dispatch['revision']} as a new orchestration session")
    return "\n".join(f"  {dispatch['task_id']}: {line}" for line in lines)


def command(args: argparse.Namespace, launcher: Launcher | None = None) -> int:
    queue = Path(args.queue_file).resolve()
    tasks = load_queue(queue, Path(getattr(args, "repo", None) or os.getcwd()).resolve())
    ledger = Ledger(Path(args.ledger).resolve() if getattr(args, "ledger", None) else ledger_path(queue))
    factory = QueueFactory(tasks, ledger, launcher=launcher, retry=set(getattr(args, "retry", None) or []))
    if args.dry_run:
        plan = factory.plan()
        if args.json:
            print(json.dumps({"queue": str(queue), "ledger": str(ledger.path), **plan}, indent=2))
        else:
            print(f"Queue: {queue}\nLedger: {ledger.path}\nNext task: {plan['next'] or 'none'}")
            if plan["dispatch"]:
                print(_describe(plan["dispatch"]))
            for task_id, reason in plan["skipped"].items():
                print(f"  {task_id}: {reason}")
        return 0
    # In JSON mode the session reports would corrupt stdout, so they go to stderr instead.
    session_output = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
    with session_output:
        summary = factory.run(max_tasks=args.max_tasks, stop_on_failure=args.stop_on_failure,
                              quiet=args.quiet, no_progress=args.no_progress, heartbeat=args.heartbeat)
    if args.json:
        print(json.dumps({"queue": str(queue), "ledger": str(ledger.path), **summary}, indent=2))
    else:
        print("HOWL FACTORY REPORT")
        print(f"Queue: {queue}\nLedger: {ledger.path}\nRun: {summary['run_id']}")
        for record in summary["superseded"]:
            print(f"  {record['task_id']}: revision {record['fingerprint'][:12]} SUPERSEDED by "
                  f"{record['replacement_fingerprint'][:12]} session={record['session_id']}")
        for result in summary["results"]:
            print(f"  {result['task_id']}: {result['status']} session={result['session_id'] or 'none'} "
                  f"revision={result['fingerprint'][:12]}")
        for task_id, reason in summary["skipped"].items():
            print(f"  {task_id}: not run; {reason}")
        print(f"Stopped: {summary['stop_reason']}")
    statuses = [result["status"] for result in summary["results"]]
    if INTERRUPTED in statuses:
        return 130
    return 0 if all(status in DONE for status in statuses) and not summary["blocked"] else 2
