#!/usr/bin/env python3
"""Deterministic factory supervisor tick/run loop."""

import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("howlplane.factory.supervisor")

from src.control_plane.atomic_io import atomic_write_json, atomic_write_text, safe_load_json
from src.control_plane.factory.dispatcher import DispatchOutcome, MarathonDispatcherAdapter
from src.control_plane.factory.portfolio import FactoryPolicy, select
from src.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityStore,
    NeedDisposition,
    RepoProposalStore,
    resolve_need_disposition,
)
from src.control_plane.factory.supervisor_state import (
    InvalidSupervisorStateTransitionError,
    SupervisorState,
    SupervisorStateRecord,
    SupervisorStateStore,
)
from src.control_plane.factory.work_item import WorkItem, WorkItemState, WorkItemStore, WorkItemOrigin
from src.control_plane.factory.work_item import work_item_fingerprint


DEFAULT_TICK_INTERVAL_SECONDS = 10.0
DEFAULT_PROVIDER_RETRY_INTERVAL_SECONDS = 30.0
DEFAULT_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0


@dataclass
class TickResult:
    state: str
    selected_work_item_id: Optional[str] = None
    next_wake_at: Optional[datetime] = None
    reason: str = ""
    alert: Optional[str] = None


class FactorySupervisor:
    """Deterministic, dependency-injected factory supervisor."""

    def __init__(
        self,
        state_store: SupervisorStateStore,
        work_item_store: WorkItemStore,
        repo_proposal_store: RepoProposalStore,
        capability_store: CapabilityStore,
        dispatcher: MarathonDispatcherAdapter,
        discovery: Callable[[], List[Dict[str, Any]]],
        provider_pool: Any,
        policy: Optional[FactoryPolicy] = None,
        product_repo: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        provider_retry_interval_seconds: float = DEFAULT_PROVIDER_RETRY_INTERVAL_SECONDS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        state_dir: Optional[Union[str, Path]] = None,
        lock: Optional[Any] = None,
        max_work_items: Optional[int] = None,
    ):
        self.state_store = state_store
        self.work_item_store = work_item_store
        self.repo_proposal_store = repo_proposal_store
        self.capability_registry = CapabilityRegistry(capability_store)
        self.dispatcher = dispatcher
        self.discovery = discovery
        self.provider_pool = provider_pool
        self._clock = clock
        self._sleep = sleep
        self.tick_interval_seconds = tick_interval_seconds
        self.provider_retry_interval_seconds = provider_retry_interval_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._state_dir = Path(state_dir) if state_dir else None
        self._lock = lock
        self._stop_requested: Optional[str] = None
        self.instance_id: str = self._resolve_instance_id()
        self._state_record = self.state_store.load()

        # Dynamically parameterize policy product_repository
        def _resolve_repo_slug(val: Optional[Union[str, Path]]) -> Optional[str]:
            if not val:
                return None
            s = str(val).strip()
            if not s:
                return None
            if "/" in s and not Path(s).exists():
                return s
            try:
                from src.control_plane.git_integration import detect_repo_slug
                slug = detect_repo_slug(s)
                if slug:
                    return slug
            except Exception:
                pass
            return s

        target_product = None
        if product_repo:
            target_product = _resolve_repo_slug(product_repo)
        elif self._state_record.target_repository:
            target_product = _resolve_repo_slug(self._state_record.target_repository)
        elif policy is not None and policy.product_repository != "howlcipher/howlframe":
            target_product = policy.product_repository

        if target_product:
            base_policy = policy or FactoryPolicy()
            self.policy = replace(base_policy, product_repository=target_product)
        else:
            self.policy = policy or FactoryPolicy()

        self._consecutive_capped_ticks: int = getattr(self._state_record, "consecutive_capped_ticks", 0)

        # Apply bounded execution policy from constructor (CLI/service args).
        if max_work_items is not None:
            self._state_record.run_mode = "bounded"
            self._state_record.max_work_items = max_work_items
            if self._state_record.bounded_run_started_at is None:
                self._state_record.bounded_run_started_at = clock().isoformat()
            self._persist()
        if self._state_record.created_at is None:
            self._state_record.created_at = clock().isoformat()

    def _resolve_instance_id(self) -> str:
        """Resolve or persist a unique instance identifier scoped to the state directory."""
        candidates = []
        if self._state_dir is not None:
            candidates.append(Path(self._state_dir) / "instance_id")
            candidates.append(Path(self._state_dir) / "supervisor" / "instance_id")
        candidates.append(Path(self.state_store.base_dir) / "instance_id")

        for p in candidates:
            if p.is_file():
                try:
                    val = p.read_text(encoding="utf-8").strip()
                    if val:
                        return val
                except OSError:
                    pass

        target_file = candidates[0]
        new_id = uuid.uuid4().hex[:12]
        try:
            atomic_write_text(target_file, new_id + "\n")
        except Exception:
            pass
        return new_id

    @property
    def state_record(self) -> SupervisorStateRecord:
        return self._state_record

    def _now(self) -> datetime:
        return self._clock()

    def _now_iso(self) -> str:
        return self._clock().isoformat()

    def _bounded_limit_reached(self) -> bool:
        """True when the bounded execution budget is exhausted."""
        if self._state_record.run_mode != "bounded":
            return False
        if self._state_record.max_work_items is None:
            return False
        return self._state_record.work_items_dispatched >= self._state_record.max_work_items

    def _collect_provider_attempts(self, wid: str) -> List[Dict[str, Any]]:
        """Extract durable provider attempt history for a work item from task run evidence."""
        attempts: List[Dict[str, Any]] = []
        target_repo = self._state_record.target_repository
        run_dirs = []
        if target_repo:
            target_path = Path(target_repo)
            run_dirs.append(target_path / ".task_runs" / wid)
            run_dirs.append(target_path / ".task_runs" / f"FACTORY-{wid}")
        if self._state_dir:
            run_dirs.append(self._state_dir / ".task_runs" / wid)
            run_dirs.append(self._state_dir / ".task_runs" / f"FACTORY-{wid}")
            run_dirs.append(self._state_dir / "work_items" / wid / ".task_runs")

        run_dir = None
        for cand in run_dirs:
            if cand.is_dir():
                run_dir = cand
                break

        if run_dir is not None and run_dir.is_dir():
            # 1. Implementation attempts
            impl_dir = run_dir / "implementation" / "attempts"
            if not impl_dir.is_dir():
                impl_dir = run_dir / "attempts"
            if impl_dir.is_dir():
                for p in sorted(impl_dir.iterdir()):
                    if not p.is_dir():
                        continue
                    rec_file = p / "attempt_record.json"
                    res_file = p / "result.json"
                    rec = safe_load_json(rec_file) if rec_file.is_file() else {}
                    res = safe_load_json(res_file) if res_file.is_file() else {}
                    if not rec and not res:
                        continue
                    resource_id = rec.get("resource_id") or res.get("agent_id")
                    if not resource_id and "-" in p.name:
                        parts = p.name.split("-", 1)
                        if parts[1]:
                            resource_id = parts[1]
                    resource_id = resource_id or "unknown"
                    duration = res.get("duration_seconds") if res.get("duration_seconds") is not None else rec.get("duration_seconds")
                    ended_at = res.get("timestamp") or rec.get("timestamp") or res.get("ended_at") or rec.get("ended_at")
                    started_at = res.get("started_at") or rec.get("started_at")
                    if not started_at and ended_at and duration is not None:
                        try:
                            end_dt = datetime.fromisoformat(ended_at)
                            started_at = (end_dt - timedelta(seconds=duration)).isoformat()
                        except Exception:
                            started_at = None

                    outcome = None
                    if rec.get("success") or res.get("success"):
                        outcome = "implementation succeeded"
                    elif rec.get("failure_class"):
                        outcome = rec["failure_class"]
                    elif res.get("error_message"):
                        outcome = "failed"
                    else:
                        outcome = "failed"

                    capacity_after = rec.get("capacity_after", {}).get(resource_id)
                    reason = res.get("error_message") or rec.get("failure_class") or res.get("stderr")

                    att: Dict[str, Any] = {
                        "work_item_id": wid,
                        "resource_id": resource_id,
                        "role": "implementation",
                    }
                    if started_at:
                        att["started_at"] = started_at
                    if ended_at:
                        att["ended_at"] = ended_at
                    if duration is not None:
                        att["duration_seconds"] = duration
                    if outcome:
                        att["outcome"] = outcome
                    if capacity_after:
                        att["provider_state_after"] = capacity_after
                    if reason:
                        att["reason"] = reason
                    attempts.append(att)

            # 2. Review attempts
            rev_dir = run_dir / "reviews"
            if rev_dir.is_dir():
                for p in sorted(rev_dir.iterdir()):
                    if p.is_dir():
                        res_file = p / "result.json"
                        if res_file.is_file():
                            r_data = safe_load_json(res_file)
                            failover = r_data.get("failover") or {}
                            failover_attempts = failover.get("attempts")
                            if failover_attempts:
                                for fa in failover_attempts:
                                    res_id = fa.get("resource_id") or fa.get("provider") or "unknown"
                                    fo_outcome = fa.get("outcome") or fa.get("failure_class") or "failed"
                                    fo_reason = fa.get("failure_class") or fa.get("error_message")
                                    att = {
                                        "work_item_id": wid,
                                        "resource_id": res_id,
                                        "role": "review",
                                        "outcome": fo_outcome,
                                    }
                                    if fa.get("started_at"):
                                        att["started_at"] = fa["started_at"]
                                    if fa.get("ended_at"):
                                        att["ended_at"] = fa["ended_at"]
                                    elif fa.get("checked_at"):
                                        att["ended_at"] = fa["checked_at"]
                                    if fa.get("duration_seconds") is not None:
                                        att["duration_seconds"] = fa.get("duration_seconds")
                                    if fo_reason:
                                        att["reason"] = fo_reason
                                    attempts.append(att)
                            else:
                                res_id = r_data.get("resource_id") or r_data.get("assigned_resource_id") or "unknown"
                                is_indep = r_data.get("independence", {}).get("independent", False)
                                is_success = r_data.get("process", {}).get("success", False) or r_data.get("status") in ("completed", "findings_detected")
                                r_outcome = "independent review" if (is_indep and is_success) else (r_data.get("disposition") or r_data.get("status") or "completed")
                                att = {
                                    "work_item_id": wid,
                                    "resource_id": res_id,
                                    "role": "review",
                                    "outcome": r_outcome,
                                }
                                if r_data.get("completed_at"):
                                    att["ended_at"] = r_data["completed_at"]
                                    if r_data.get("duration_seconds") is not None:
                                        try:
                                            c_dt = datetime.fromisoformat(r_data["completed_at"])
                                            att["started_at"] = (c_dt - timedelta(seconds=r_data["duration_seconds"])).isoformat()
                                        except Exception:
                                            pass
                                if r_data.get("duration_seconds") is not None:
                                    att["duration_seconds"] = r_data["duration_seconds"]
                                attempts.append(att)

            # 3. Remediation attempts
            rem_dir = run_dir / "remediation"
            if rem_dir.is_dir():
                for p in sorted(rem_dir.iterdir()):
                    if not p.is_dir():
                        continue
                    res_file = p / "result.json"
                    fail_file = p / "failure.json"
                    res = safe_load_json(res_file) if res_file.is_file() else {}
                    fail = safe_load_json(fail_file) if fail_file.is_file() else {}
                    if not res and not fail:
                        continue
                    res_id = fail.get("resource_id") or res.get("agent_id") or "unknown"
                    ended_at = res.get("timestamp") or res.get("ended_at") or fail.get("timestamp") or fail.get("ended_at")
                    started_at = res.get("started_at") or fail.get("started_at")
                    duration = res.get("duration_seconds") if res.get("duration_seconds") is not None else fail.get("duration_seconds")
                    if not started_at and ended_at and duration is not None:
                        try:
                            e_dt = datetime.fromisoformat(ended_at)
                            started_at = (e_dt - timedelta(seconds=duration)).isoformat()
                        except Exception:
                            pass
                    if res.get("success"):
                        rem_outcome = "remediation succeeded"
                        rem_reason = None
                    else:
                        rem_outcome = fail.get("failure_class") or fail.get("provider_failure_class") or "remediation failed"
                        rem_reason = res.get("error_message") or fail.get("failure_class")
                    att = {
                        "work_item_id": wid,
                        "resource_id": res_id,
                        "role": "remediation",
                        "outcome": rem_outcome,
                    }
                    if started_at:
                        att["started_at"] = started_at
                    if ended_at:
                        att["ended_at"] = ended_at
                    if duration is not None:
                        att["duration_seconds"] = duration
                    if rem_reason:
                        att["reason"] = rem_reason
                    attempts.append(att)

            # 4. Verification
            verif_file = run_dir / "verification_result.json"
            if not verif_file.is_file():
                verif_file = run_dir / "verification_plan.json"
            if verif_file.is_file():
                v_data = safe_load_json(verif_file)
                v_status = v_data.get("status") or v_data.get("overall_status")
                if v_status:
                    att = {
                        "work_item_id": wid,
                        "role": "verification",
                        "outcome": "passed" if str(v_status).lower() in ("passed", "success", "completed") else str(v_status).lower(),
                    }
                    if v_data.get("started_at"):
                        att["started_at"] = v_data["started_at"]
                    ended_at = v_data.get("completed_at") or v_data.get("ended_at") or v_data.get("timestamp")
                    if ended_at:
                        att["ended_at"] = ended_at
                    if v_data.get("duration_seconds") is not None:
                        att["duration_seconds"] = v_data["duration_seconds"]
                    attempts.append(att)

            def _attempt_sort_key(att: Dict[str, Any]) -> str:
                return str(att.get("started_at") or att.get("ended_at") or "9999-99-99")

            attempts.sort(key=_attempt_sort_key)

        # Fallback if no disk evidence was found: check work item blocked reason
        if not attempts:
            try:
                item = self.work_item_store.load(wid)
                reason = item.admission_blocked_reason or ""
                if "tried " in reason:
                    prov_list = reason.split("tried", 1)[1].strip()
                    providers = [pr.strip().rstrip(".") for pr in prov_list.split(",") if pr.strip()]
                    for pr in providers:
                        attempts.append({
                            "work_item_id": wid,
                            "resource_id": pr,
                            "role": "implementation",
                            "outcome": "unavailable/exhausted",
                            "reason": reason,
                        })
            except Exception:
                pass

        return attempts

    def _bounded_stop(self, reason: str, now_iso: str) -> None:
        """Complete a bounded run: persist reason, write report, transition to STOPPED."""
        self._state_record.bounded_run_stop_reason = reason
        # Ensure completion timestamp is at least as late as any recorded provider activity
        for wid in self._state_record.bounded_dispatched_ids:
            for att in self._collect_provider_attempts(wid):
                att_end = att.get("ended_at")
                if att_end and att_end > now_iso:
                    now_iso = att_end
        self._state_record.bounded_run_completed_at = now_iso
        self._state_record.clear_current_dispatch()
        self._state_record.transition_to(
            SupervisorState.STOPPED, reason=reason, at=now_iso
        )
        self._state_record.stopped_reason = reason
        self._persist()
        # Proactively mark process.json as stopped if it exists
        if self._state_dir is not None:
            proc_file = self._state_dir / "campaign" / "process.json"
            if proc_file.is_file():
                try:
                    proc_data = safe_load_json(proc_file)
                    if proc_data and proc_data.get("status") != "stopped":
                        proc_data["status"] = "stopped"
                        atomic_write_json(proc_file, proc_data)
                except Exception:
                    pass
        self._write_bounded_report()

    @staticmethod
    def _bounded_stop_reason_for(
        dispatch_result: DispatchOutcome, item: WorkItem
    ) -> str:
        """Derive a normalized stop reason from the dispatch outcome."""
        if dispatch_result.success:
            return "bounded_work_item_completed"
        if dispatch_result.requires_authority:
            return "bounded_work_item_authority_blocked"
        if dispatch_result.provider_unavailable:
            return "bounded_no_eligible_provider"
        if dispatch_result.next_work_item_state == WorkItemState.BLOCKED:
            return "bounded_work_item_blocked"
        if dispatch_result.next_work_item_state == WorkItemState.DEFERRED:
            return "bounded_work_item_deferred"
        if dispatch_result.next_work_item_state == WorkItemState.FAILED:
            return "bounded_work_item_failed"
        return "max_work_items_reached"

    def _write_bounded_report(self) -> None:
        """Write deterministic JSON and Markdown summaries when a bounded run completes."""
        if self._state_dir is None:
            return
        import json as json_mod

        reports_dir = self._state_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        started = self._state_record.bounded_run_started_at or ""
        completed = self._state_record.bounded_run_completed_at or ""
        ts = completed.replace(":", "").replace("+", "").replace("-", "")[:15] or "unknown"

        duration_seconds = None
        if started and completed:
            try:
                s_dt = datetime.fromisoformat(started)
                c_dt = datetime.fromisoformat(completed)
                duration_seconds = max(0.0, (c_dt - s_dt).total_seconds())
            except Exception:
                duration_seconds = None

        # Gather provider attempt chain from dispatch history.
        provider_chain = []
        for entry in self._state_record.dispatch_history:
            if entry.get("work_item_id") in self._state_record.bounded_dispatched_ids:
                provider_chain.append(entry)

        # Collect detailed provider attempts across all bounded items
        all_provider_attempts: List[Dict[str, Any]] = []
        for wid in self._state_record.bounded_dispatched_ids:
            all_provider_attempts.extend(self._collect_provider_attempts(wid))

        report_data = {
            "schema": "howlplane.factory.bounded_run_report/v1",
            "run_mode": self._state_record.run_mode,
            "max_work_items": self._state_record.max_work_items,
            "work_items_dispatched": self._state_record.work_items_dispatched,
            "bounded_dispatched_ids": list(self._state_record.bounded_dispatched_ids),
            "bounded_run_started_at": started,
            "bounded_run_completed_at": completed,
            "duration_seconds": duration_seconds,
            "bounded_run_stop_reason": self._state_record.bounded_run_stop_reason,
            "target_repository": self._state_record.target_repository,
            "target_mode": self._state_record.target_mode,
            "objective": self._state_record.objective,
            "supervisor_id": self._state_record.supervisor_id,
            "instance_id": self.instance_id,
            "failure_count": self._state_record.failure_count,
            "merges_count": self._state_record.merges_count,
            "provider_attempts": all_provider_attempts,
            "provider_dispatch_chain": provider_chain,
            "recent_completed": list(self._state_record.recent_completed),
            "recent_failed": list(self._state_record.recent_failed),
            "recent_parked": list(self._state_record.recent_parked),
            "stopped_reason": self._state_record.stopped_reason,
        }

        # Enrich with per-item final state from work item store.
        work_item_states = {}
        for wid in self._state_record.bounded_dispatched_ids:
            try:
                item = self.work_item_store.load(wid)
                work_item_states[wid] = {
                    "state": item.state,
                    "origin": item.origin,
                    "title": item.title,
                    "attempts": item.attempts,
                    "admission_blocked_reason": item.admission_blocked_reason,
                }
            except Exception:
                work_item_states[wid] = {"state": "unknown", "error": "load_failed"}
        report_data["work_item_final_states"] = work_item_states

        json_path = reports_dir / f"bounded_run_{ts}.json"
        md_path = reports_dir / f"bounded_run_{ts}.md"

        try:
            atomic_write_text(json_path, json_mod.dumps(report_data, indent=2, default=str) + "\n")
        except Exception:
            pass

        # Deterministic Markdown summary.
        lines = [
            "# HowlPlane Factory Bounded Run Report",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Run mode | {self._state_record.run_mode} |",
            f"| Max work items | {self._state_record.max_work_items} |",
            f"| Work items dispatched | {self._state_record.work_items_dispatched} |",
            f"| Started | {started} |",
            f"| Completed | {completed} |",
        ]
        if duration_seconds is not None:
            from src.control_plane.progress import format_elapsed
            lines.append(f"| Duration | {format_elapsed(duration_seconds)} |")
        lines.extend([
            f"| Stop reason | {self._state_record.bounded_run_stop_reason} |",
            f"| Target repository | {self._state_record.target_repository} |",
            f"| Merges | {self._state_record.merges_count} |",
            f"| Failures | {self._state_record.failure_count} |",
            "",
            "## Dispatched Work Items",
            "",
        ])
        for wid in self._state_record.bounded_dispatched_ids:
            ws = work_item_states.get(wid, {})
            lines.append(f"### {wid}")
            lines.append("")
            lines.append(f"- **State:** {ws.get('state', 'unknown')}")
            lines.append(f"- **Origin:** {ws.get('origin', 'unknown')}")
            lines.append(f"- **Title:** {ws.get('title', 'unknown')}")
            lines.append(f"- **Attempts:** {ws.get('attempts', 'unknown')}")
            if ws.get("admission_blocked_reason"):
                lines.append(f"- **Blocked reason:** {ws['admission_blocked_reason']}")
            lines.append("")

        if all_provider_attempts:
            lines.append("## Provider Attempt Chain")
            lines.append("")
            for wid in self._state_record.bounded_dispatched_ids:
                item_attempts = [a for a in all_provider_attempts if a.get("work_item_id") == wid]
                lines.append(f"### {wid}")
                lines.append("")
                if not item_attempts:
                    lines.append("- No provider attempts recorded.")
                    lines.append("")
                else:
                    for idx, att in enumerate(item_attempts, 1):
                        name = att.get("resource_id") or att.get("role") or "unknown"
                        lines.append(f"{idx}. {name}")
                        if att.get("role"):
                            lines.append(f"   - role: {att['role']}")
                        if att.get("started_at"):
                            lines.append(f"   - started: {att['started_at']}")
                        if att.get("ended_at"):
                            lines.append(f"   - ended: {att['ended_at']}")
                        if att.get("outcome"):
                            lines.append(f"   - outcome: {att['outcome']}")
                        if att.get("provider_state_after"):
                            lines.append(f"   - provider state after attempt: {att['provider_state_after']}")
                        if att.get("reason"):
                            lines.append(f"   - failure/retry reason: {att['reason']}")
                        lines.append("")
                ws = work_item_states.get(wid, {})
                final_outcome = ws.get("admission_blocked_reason") or ws.get("state")
                if final_outcome:
                    lines.append("Final work-item outcome:")
                    lines.append(f"{final_outcome}")
                    lines.append("")

        if provider_chain:
            lines.append("## Provider Dispatch Chain")
            lines.append("")
            for entry in provider_chain:
                lines.append(f"- Dispatch `{entry.get('dispatch_id')}` at {entry.get('at')}")
            lines.append("")

        try:
            atomic_write_text(md_path, "\n".join(lines) + "\n")
        except Exception:
            pass


    def _reload_state(self) -> None:
        """Reload persisted state so external stop commands are visible."""
        self._state_record = self.state_store.load()

    def _provider_inventory(self) -> List[Dict[str, Any]]:
        try:
            return self.provider_pool.inventory()
        except Exception:
            return []

    def status(self) -> Dict[str, Any]:
        return {
            "supervisor_id": self._state_record.supervisor_id,
            "instance_id": self.instance_id,
            "state": self._state_record.state,
            "objective": self._state_record.objective,
            "target_mode": self._state_record.target_mode,
            "target_repository": self._state_record.target_repository,
            "workspace_file": self._state_record.workspace_file,
            "created_at": self._state_record.created_at,
            "last_tick_at": self._state_record.last_tick_at,
            "last_successful_tick_at": self._state_record.last_successful_tick_at,
            "next_wake_at": self._state_record.next_wake_at,
            "current_work_item_id": self._state_record.current_work_item_id,
            "last_work_item_id": self._state_record.last_work_item_id,
            "current_task_id": self._state_record.current_task_id,
            "current_dispatch_id": self._state_record.current_dispatch_id,
            "observations_consumed": self._state_record.observations_consumed,
            "merges_count": self._state_record.merges_count,
            "failure_count": self._state_record.failure_count,
            "last_error": self._state_record.last_error,
            "stopped_reason": self._state_record.stopped_reason,
            "provider_wake_conditions": self._state_record.provider_wake_conditions,
            "provider_inventory": self._provider_inventory(),
            "recent_completed": self._state_record.recent_completed,
            "recent_failed": self._state_record.recent_failed,
            "recent_parked": self._state_record.recent_parked,
            "dispatch_history_count": len(self._state_record.dispatch_history),
            "transition_history_count": len(self._state_record.transition_history),
            "run_mode": self._state_record.run_mode,
            "max_work_items": self._state_record.max_work_items,
            "work_items_dispatched": self._state_record.work_items_dispatched,
            "bounded_run_started_at": self._state_record.bounded_run_started_at,
            "bounded_run_completed_at": self._state_record.bounded_run_completed_at,
            "bounded_run_stop_reason": self._state_record.bounded_run_stop_reason,
            "bounded_dispatched_ids": list(self._state_record.bounded_dispatched_ids),
            "admission_decisions_count": len(self._state_record.admission_decisions),
            "proposals_awaiting_authority": [
                p.proposal_id for p in self.repo_proposal_store.list_awaiting_authority()
            ],
            "consecutive_capped_ticks": getattr(self._state_record, "consecutive_capped_ticks", 0),
            "alerts": list(getattr(self._state_record, "alerts", [])),
        }

    def stop(self, reason: str = "operator_stop") -> None:
        self._reload_state()
        if self._state_record.state != SupervisorState.STOPPED:
            self._state_record.transition_to(SupervisorState.STOPPED, reason=reason, at=self._now_iso())
            self._state_record.stopped_reason = reason
            self.state_store.save(self._state_record)

    def request_stop(self, reason: str = "operator_stop") -> None:
        """Defer signal-driven state writes until the current tick settles."""
        self._stop_requested = reason

    def resume(self) -> None:
        self._reload_state()
        if self._state_record.state == SupervisorState.STOPPED:
            self._state_record.transition_to(SupervisorState.IDLE, reason="operator_resume", at=self._now_iso())
            self._state_record.stopped_reason = None
            self.state_store.save(self._state_record)

    def _persist(self) -> None:
        self.state_store.save(self._state_record)

    @staticmethod
    def _decision_key(decision: Dict[str, Any]) -> Tuple:
        return (
            decision.get("origin"),
            decision.get("repository"),
            decision.get("capability_id"),
            tuple(sorted(decision.get("identity_keys") or [])),
            tuple(sorted(decision.get("evidence_fingerprints") or [])),
            decision.get("disposition"),
        )

    def _ingest_discovered(self) -> None:
        for evidence in self.discovery():
            self._state_record.observations_consumed += 1
            if evidence.get("capability_need"):
                self._handle_capability_need(evidence)
            else:
                self._handle_work_evidence(evidence)

    def _handle_work_evidence(self, evidence: Dict[str, Any]) -> None:
        origin = evidence.get("origin", "inferred_need")
        repository = evidence.get("repository", "")
        identity_keys = evidence.get("identity_keys", [])
        trusted = evidence.get("trusted_provenance", False)
        is_ambiguous = evidence.get("is_ambiguous", False)

        # Owner direction must come from a trusted provenance; otherwise it is
        # reviewed like any other speculative origin.
        if origin == WorkItemOrigin.OWNER_DIRECTION and not trusted:
            is_ambiguous = True

        decision = {
            "at": self._now_iso(),
            "origin": origin,
            "repository": repository,
            "title": evidence.get("title"),
            "identity_keys": list(identity_keys),
            "action": "admit",
            "is_ambiguous": is_ambiguous,
            "trusted_provenance": trusted,
            "evidence_fingerprints": sorted(
                evidence.get("evidence_fingerprints", [])
            ),
        }
        if not self._is_new_decision(decision):
            # Older discovery omitted backlog detail. Fill that missing context
            # without reopening dispositions or changing a running task's scope.
            if origin == WorkItemOrigin.EXISTING_BACKLOG and evidence.get("description"):
                item = self.work_item_store.find_by_fingerprint(
                    work_item_fingerprint(origin, repository, identity_keys)
                )
                if (item is not None and not item.description and not item.is_terminal
                        and item.state not in (WorkItemState.IN_PROGRESS, WorkItemState.VERIFYING)):
                    item.description = evidence["description"]
                    item.updated_at = self._now_iso()
                    self.work_item_store.save_object(item)
            return
        self._state_record.admission_decisions.append(decision)
        self._state_record.admission_decisions = self._state_record.admission_decisions[-1000:]
        self.work_item_store.admit_evidence(
            origin=origin,
            repository=repository,
            title=evidence.get("title", ""),
            description=evidence.get("description", ""),
            identity_keys=identity_keys,
            evidence_refs=evidence.get("evidence_refs", []),
            evidence_fingerprints=evidence.get("evidence_fingerprints", []),
            is_ambiguous=is_ambiguous,
            trusted_provenance=trusted,
            source_file_rank=evidence.get("source_file_rank", 0),
            source_rank=evidence.get("source_rank", 0),
            kind=evidence.get("kind", "improvement"),
        )

    def _handle_capability_need(self, evidence: Dict[str, Any]) -> None:
        need = evidence.get("capability_need", {})
        evidence_fingerprints = evidence.get("evidence_fingerprints", [])
        disposition = resolve_need_disposition(
            self.capability_registry,
            need,
            evidence_fingerprints,
        )
        disp_value = getattr(disposition, "value", str(disposition))
        decision = {
            "at": self._now_iso(),
            "capability_id": need.get("capability_id"),
            "disposition": disp_value,
            "repository": evidence.get("repository"),
            "evidence_fingerprints": sorted(evidence_fingerprints),
        }
        if not self._is_new_decision(decision):
            return
        self._state_record.admission_decisions.append(decision)
        self._state_record.admission_decisions = self._state_record.admission_decisions[-1000:]

        if disposition == NeedDisposition.USE_EXISTING_CAPABILITY:
            record = self.capability_registry.find(need.get("capability_id"))
            if record is not None:
                repo = evidence.get("repository")
                if repo and repo not in record.required_by:
                    record.required_by = sorted(set(record.required_by) | {repo})
                merged_fps = sorted(set(record.evidence_fingerprints) | set(evidence_fingerprints))
                if merged_fps != record.evidence_fingerprints:
                    record.evidence_fingerprints = merged_fps
                self.capability_registry.register(record)
            return

        if disposition in {
            NeedDisposition.IMPROVE_EXISTING_REPOSITORY,
            NeedDisposition.BUILD_REUSABLE_CAPABILITY,
            NeedDisposition.LOCAL_PROJECT_FIX,
        }:
            return

        if disposition == NeedDisposition.PROPOSE_NEW_REPOSITORY and evidence_fingerprints:
            proposal_id = f"PROP-{need.get('capability_id', 'unknown')}"
            self.repo_proposal_store.propose(
                proposal_id=proposal_id,
                repository_name=need.get("proposed_repository", ""),
                disposition=disp_value,
                rationale=disp_value,
                evidence_fingerprints=evidence_fingerprints,
                bootstrap_plan={
                    "capability_id": need.get("capability_id"),
                    "consumer_repositories": need.get("consumer_repositories", []),
                    "clear_purpose": need.get("clear_purpose", False),
                    "bounded_maintenance": need.get("bounded_maintenance", False),
                    "deterministic_verification": need.get("deterministic_verification", False),
                },
            )
            return

        # NEEDS_HUMAN_DECISION and anything else is parked implicitly by not creating a proposal.

    def _is_new_decision(self, decision: Dict[str, Any]) -> bool:
        key = self._decision_key(decision)
        for existing in self._state_record.admission_decisions:
            if self._decision_key(existing) == key:
                return False
        return True

    def _next_provider_retry_after(self) -> Optional[Tuple[datetime, Optional[str]]]:
        """Return the soonest (retry_after, resource_id) from provider inventory in the future."""
        soonest: Optional[datetime] = None
        soonest_id: Optional[str] = None
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        try:
            rows = self.provider_pool.inventory()
        except Exception:
            return None
        for row in rows:
            retry_after = row.get("retry_after")
            if not retry_after:
                continue
            try:
                when = datetime.fromisoformat(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if when <= now:
                continue
            if soonest is None or when < soonest:
                soonest = when
                soonest_id = row.get("identity", {}).get("resource_id")
        if soonest is None:
            return None
        return soonest, soonest_id

    def _provider_retry_after_dt(self) -> Optional[datetime]:
        """Return the soonest retry_after from provider inventory, if any."""
        result = self._next_provider_retry_after()
        return result[0] if result else None

    def _provider_retry_after(self) -> Optional[datetime]:
        result = self._next_provider_retry_after()
        if result is None:
            if self._state_record.provider_wake_conditions:
                self._state_record.provider_wake_conditions = {}
            return None
        soonest, soonest_id = result
        self._state_record.provider_wake_conditions = {
            "resource_id": soonest_id,
            "retry_after": soonest.isoformat(),
            "reason": "provider_retry_after",
        }
        return soonest

    def _compute_next_wake(self, state: str, now: datetime) -> datetime:
        if state == SupervisorState.WAITING_FOR_PROVIDER:
            retry_after = self._provider_retry_after()
            if retry_after is not None and retry_after > now:
                return retry_after
            return now + timedelta(seconds=self.provider_retry_interval_seconds)
        if state == SupervisorState.BACKOFF_AFTER_FAILURE:
            backoff = min(
                self.backoff_base_seconds * (2 ** self._state_record.failure_count),
                self.max_backoff_seconds,
            )
            return now + timedelta(seconds=backoff)
        return now + timedelta(seconds=self.tick_interval_seconds)

    def _provider_has_capacity(self) -> bool:
        try:
            return self.provider_pool.has_available_providers()
        except Exception:
            return False

    def _dispatch(self, item: WorkItem, now_iso: str) -> Optional[DispatchOutcome]:
        """Persist IN_PROGRESS, dispatch, and persist the resulting terminal/park state."""
        # Reload the item from the store in case state changed between selection
        # and dispatch; if it disappeared, retain evidence and stop/park.
        try:
            fresh_item = self.work_item_store.load(item.work_item_id)
        except Exception:
            self._state_record.last_error = (
                f"Selected work item {item.work_item_id} disappeared before dispatch"
            )
            return None

        item = fresh_item
        dispatch_id = f"D-{item.work_item_id}-{self.instance_id}-{self._state_record.observations_consumed}"
        task_id = f"FACTORY-{item.work_item_id}"
        item.transition_to(WorkItemState.IN_PROGRESS, reason="dispatched")
        item.task_ids = sorted(set(item.task_ids) | {task_id})
        item.attempts += 1
        self.work_item_store.save_object(item)
        self._state_record.record_dispatch(
            dispatch_id,
            item.work_item_id,
            task_id,
            now_iso,
            origin=item.origin,
            repository=item.repository,
        )
        # Bounded execution: count distinct work items committed to dispatch.
        if self._state_record.run_mode == "bounded":
            self._state_record.work_items_dispatched += 1
            if item.work_item_id not in self._state_record.bounded_dispatched_ids:
                self._state_record.bounded_dispatched_ids.append(item.work_item_id)
        self._state_record.transition_to(SupervisorState.DISPATCHING, reason="item_selected", at=now_iso)
        self._persist()
        try:
            dispatch_result = self.dispatcher.dispatch(
                item, dispatch_id=dispatch_id, task_id=task_id, run_mode=self._state_record.run_mode
            )
        except TypeError:
            dispatch_result = self.dispatcher.dispatch(
                item, dispatch_id=dispatch_id, task_id=task_id
            )
        if (
            dispatch_result.success
            and dispatch_result.git_record
            and dispatch_result.git_record.get("merged")
        ):
            self._state_record.merges_count += 1
            self._persist()
        item.transition_to(dispatch_result.next_work_item_state, reason=dispatch_result.reason)
        item.blocked_by = item.blocked_by or []
        if dispatch_result.blocker:
            item.admission_blocked_reason = dispatch_result.reason
            if dispatch_result.next_work_item_state == WorkItemState.BLOCKED:
                blocker_id = dispatch_result.blocker
                if blocker_id not in item.blocked_by:
                    item.blocked_by.append(blocker_id)
        if dispatch_result.next_work_item_state == WorkItemState.DEFERRED:
            wake_retry = self._state_record.provider_wake_conditions.get("retry_after")
            if wake_retry:
                try:
                    wake_dt = datetime.fromisoformat(wake_retry)
                    if wake_dt.tzinfo is None:
                        wake_dt = wake_dt.replace(tzinfo=timezone.utc)
                    now_dt = self._now()
                    if now_dt.tzinfo is None:
                        now_dt = now_dt.replace(tzinfo=timezone.utc)
                    if wake_dt <= now_dt:
                        wake_retry = None
                except (ValueError, TypeError):
                    wake_retry = None

            item.retry_after = (
                dispatch_result.retry_after
                or wake_retry
                or (self._provider_retry_after_dt().isoformat() if self._provider_retry_after_dt() else None)
            )
        self.work_item_store.save_object(item)
        return dispatch_result

    def _apply_dispatch_result(
        self,
        item: WorkItem,
        dispatch_result: DispatchOutcome,
        now_iso: str,
    ) -> Tuple[str, str]:
        """Translate a dispatch outcome into the next supervisor state and durable audit record."""
        if dispatch_result.success:
            self._state_record.record_completion(item.work_item_id, dispatch_result.task_id or "", now_iso)
            return SupervisorState.IDLE, dispatch_result.reason
        if dispatch_result.requires_authority:
            self._state_record.record_park(item.work_item_id, dispatch_result.task_id or "", dispatch_result.reason, now_iso)
            return SupervisorState.WAITING_FOR_AUTHORITY, dispatch_result.reason
        if dispatch_result.provider_unavailable:
            self._state_record.record_park(item.work_item_id, dispatch_result.task_id or "", dispatch_result.reason, now_iso)
            return SupervisorState.WAITING_FOR_PROVIDER, dispatch_result.reason
        if dispatch_result.next_work_item_state == WorkItemState.BLOCKED:
            self._state_record.record_park(item.work_item_id, dispatch_result.task_id or "", dispatch_result.reason, now_iso)
            return SupervisorState.WAITING_FOR_DEPENDENCY, dispatch_result.reason
        self._state_record.record_failure(item.work_item_id, dispatch_result.task_id or "", dispatch_result.reason, now_iso)
        self._state_record.failure_count += 1
        return SupervisorState.BACKOFF_AFTER_FAILURE, dispatch_result.reason

    def _park_reconciled_item(self) -> None:
        item_id = self._state_record.current_work_item_id
        if item_id is None:
            return
        try:
            item = self.work_item_store.load(item_id)
        except Exception:
            return
        if item.state == WorkItemState.IN_PROGRESS:
            item.transition_to(WorkItemState.AWAITING_OWNER, reason="restart_during_dispatch")
            item.admission_blocked_reason = "restart_during_dispatch_reconciliation"
            self.work_item_store.save_object(item)

    def _reconcile_in_progress_items(self, now_iso: str) -> None:
        """Park any IN_PROGRESS items that were orphaned by a crash, even if the
        supervisor state was not left in DISPATCHING.
        """
        for item in self.work_item_store.list_all():
            if item.state != WorkItemState.IN_PROGRESS:
                continue
            try:
                item.transition_to(WorkItemState.AWAITING_OWNER, reason="orphan_in_progress_reconciled")
                item.admission_blocked_reason = "orphan_in_progress_reconciled"
                self.work_item_store.save_object(item)
            except Exception:
                pass

    def _requeue_deferred_items(self, now: datetime) -> None:
        """Requeue due deferred work only after provider capacity is observed."""
        capacity_observed = self._provider_has_capacity()
        for item in self.work_item_store.list_all():
            if item.state != WorkItemState.DEFERRED:
                continue
            retry_after = item.retry_after
            if retry_after is None:
                # Older records without retry metadata receive a durable fallback
                # wake, but are not immediately made dispatchable.
                item.retry_after = (
                    now + timedelta(seconds=self.provider_retry_interval_seconds)
                ).isoformat()
                self.work_item_store.save_object(item)
                continue
            try:
                when = datetime.fromisoformat(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if when <= now and capacity_observed:
                item.transition_to(WorkItemState.READY, reason="retry_after_due")
                item.retry_after = None
                self.work_item_store.save_object(item)

    def _unblock_resolved_dependencies(self) -> None:
        """Remove shipped dependency ids and requeue items with no blockers."""
        items = {item.work_item_id: item for item in self.work_item_store.list_all()}
        for item in items.values():
            if item.state != WorkItemState.BLOCKED:
                continue
            unresolved = [
                blocker_id
                for blocker_id in item.blocked_by
                if blocker_id not in items
                or items[blocker_id].state != WorkItemState.SHIPPED
            ]
            if unresolved == item.blocked_by:
                continue
            item.blocked_by = unresolved
            if not unresolved:
                item.admission_blocked_reason = None
                item.blocker_class = None
                item.transition_to(
                    WorkItemState.READY,
                    reason="dependencies_resolved",
                )
            self.work_item_store.save_object(item)

    def _ensure_next_wake_persisted(self, now: datetime, state: Optional[str] = None) -> datetime:
        state = state or self._state_record.state
        next_wake = self._compute_next_wake(state, now)
        self._state_record.next_wake_at = next_wake.isoformat()
        self._persist()
        return next_wake

    def tick(self) -> TickResult:
        self._reload_state()
        now = self._now()
        now_iso = self._now_iso()
        state = self._state_record.state
        if state == SupervisorState.STOPPED:
            return TickResult(state=state, reason="stopped")

        if self._bounded_limit_reached():
            reason = self._state_record.bounded_run_stop_reason or "max_work_items_reached"
            if self._state_record.state != SupervisorState.STOPPED:
                self._bounded_stop(reason, now_iso)
            return TickResult(state=self._state_record.state, reason=reason)

        # Persist the next wake time *before* doing any work.  If the process
        # crashes mid-tick, the next supervisor knows when it was due.
        next_wake = self._ensure_next_wake_persisted(now, state)

        if state == SupervisorState.BACKOFF_AFTER_FAILURE and self._state_record.current_work_item_id:
            self._park_reconciled_item()
            self._state_record.clear_current_dispatch()

        # Reconcile orphan IN_PROGRESS items even if the persisted state was not
        # DISPATCHING, then only requeue DEFERRED work whose retry_after is due.
        self._reconcile_in_progress_items(now_iso)
        self._requeue_deferred_items(now)
        self._unblock_resolved_dependencies()

        self._ingest_discovered()

        if self._stop_requested:
            self._persist()
            self.stop(self._stop_requested)
            return TickResult(state=self._state_record.state, reason=self._stop_requested)

        work_items = self.work_item_store.list_all()
        selection = select(work_items, self._state_record.dispatch_history, self.policy, now=now)

        if selection.item is not None:
            self._consecutive_capped_ticks = 0
            self._state_record.consecutive_capped_ticks = 0
            dispatch_result = self._dispatch(selection.item, now_iso)
            if dispatch_result is None:
                # Missing item: retain evidence and stop/park.
                stop_at = self._now_iso()
                self._state_record.transition_to(
                    SupervisorState.STOPPED,
                    reason="missing_item_at_dispatch",
                    at=stop_at,
                )
                self._state_record.stopped_reason = "missing_item"
                self._persist()
                return TickResult(
                    state=self._state_record.state,
                    selected_work_item_id=selection.item.work_item_id,
                    next_wake_at=next_wake,
                    reason="missing_item_at_dispatch",
                )

            after_dispatch_now = self._now()
            after_dispatch_iso = self._now_iso()

            next_state, reason = self._apply_dispatch_result(selection.item, dispatch_result, after_dispatch_iso)
            self._state_record.clear_current_dispatch()

            # Bounded execution check: stop instead of continuing if the
            # work-item budget is exhausted. The current item's lifecycle is
            # already finished (evidence persisted, work-item state updated)
            # so this is a safe point to stop.
            if self._bounded_limit_reached():
                stop_reason = self._bounded_stop_reason_for(
                    dispatch_result, selection.item
                )
                self._bounded_stop(stop_reason, after_dispatch_iso)
                return TickResult(
                    state=self._state_record.state,
                    selected_work_item_id=selection.item.work_item_id,
                    next_wake_at=None,
                    reason=stop_reason,
                )

            try:
                self._state_record.transition_to(next_state, reason=reason, at=after_dispatch_iso)
            except InvalidSupervisorStateTransitionError:
                self._state_record.failure_count += 1
                self._state_record.transition_to(
                    SupervisorState.BACKOFF_AFTER_FAILURE, reason="dispatch_state_rejected", at=after_dispatch_iso
                )
            next_wake = self._ensure_next_wake_persisted(after_dispatch_now)
            return TickResult(
                state=self._state_record.state,
                selected_work_item_id=selection.item.work_item_id,
                next_wake_at=next_wake,
                reason=reason,
            )

        capped_alert: Optional[str] = None
        if not self._provider_has_capacity():
            next_state = SupervisorState.WAITING_FOR_PROVIDER
            reason = "no_provider_capacity"
            self._consecutive_capped_ticks = 0
            self._state_record.consecutive_capped_ticks = 0
        elif selection.no_valuable_work:
            next_state = SupervisorState.WAITING_FOR_WORK
            reason = "NO_VALUABLE_WORK"
            if selection.reason == "all_candidates_capped" and len(selection.withheld) > 0:
                self._consecutive_capped_ticks += 1
                self._state_record.consecutive_capped_ticks = self._consecutive_capped_ticks
                if self._consecutive_capped_ticks > 3:
                    reasons = sorted(set(w.get("reason", "") for w in selection.withheld))
                    capped_alert = (
                        f"CAP_DEADLOCK_ALERT: Supervisor entered WAITING_FOR_WORK with "
                        f"{len(selection.withheld)} ready candidate(s) capped for "
                        f"{self._consecutive_capped_ticks} consecutive ticks. "
                        f"Reasons: {reasons}"
                    )
                    logger.warning(capped_alert)
                    self._state_record.record_alert(
                        alert_type="capped_candidates_deadlock",
                        message=capped_alert,
                        now_iso=now_iso,
                        details={
                            "consecutive_ticks": self._consecutive_capped_ticks,
                            "withheld_count": len(selection.withheld),
                            "reasons": reasons,
                        },
                    )
            else:
                self._consecutive_capped_ticks = 0
                self._state_record.consecutive_capped_ticks = 0
        else:
            next_state = SupervisorState.WAITING_FOR_WORK
            reason = "no_dispatchable_work"
            self._consecutive_capped_ticks = 0
            self._state_record.consecutive_capped_ticks = 0

        if self._state_record.state != next_state:
            try:
                self._state_record.transition_to(next_state, reason=reason, at=now_iso)
            except InvalidSupervisorStateTransitionError:
                self._state_record.transition_to(
                    SupervisorState.BACKOFF_AFTER_FAILURE, reason="reset_after_error", at=now_iso
                )
        next_wake = self._ensure_next_wake_persisted(now)
        self._persist()
        return TickResult(
            state=self._state_record.state,
            next_wake_at=next_wake,
            reason=reason,
            alert=capped_alert,
        )

    def _run_loop(self, until: Optional[datetime] = None) -> None:
        while True:
            self._reload_state()
            if self._state_record.state == SupervisorState.STOPPED:
                break
            if self._bounded_limit_reached():
                self._bounded_stop(
                    self._state_record.bounded_run_stop_reason or "max_work_items_reached",
                    self._now_iso(),
                )
                break
            if self._stop_requested:
                self.stop(self._stop_requested)
                break
            result = self.tick()
            self._state_record.last_successful_tick_at = self._now_iso()
            self._persist()
            if self._stop_requested:
                self.stop(self._stop_requested)
                break
            if self._state_record.state == SupervisorState.STOPPED:
                break
            now = self._now()
            if until is not None and now >= until:
                self.stop(reason="until_deadline")
                break
            next_wake = result.next_wake_at or (now + timedelta(seconds=self.tick_interval_seconds))
            sleep_seconds = (next_wake - now).total_seconds()
            if sleep_seconds > 0:
                self._sleep(sleep_seconds)

    def _configured_lock(self) -> Optional[Any]:
        if self._lock is None and self._state_dir is not None:
            from src.control_plane.locking import SupervisorLock

            self._lock = SupervisorLock(self._state_dir)
        return self._lock

    def run_once(self) -> TickResult:
        """Execute one tick under the same mutual-exclusion lock as the loop."""
        lock = self._configured_lock()
        if lock is None:
            return self.tick()
        with lock:
            return self.tick()

    def run(self, until: Optional[datetime] = None, resume_stopped: bool = False) -> None:
        # The run loop holds a single-supervisor lock for the state directory.
        lock = self._configured_lock()
        if lock is not None:
            from src.control_plane.locking import LockError

            try:
                with lock:
                    if resume_stopped:
                        self.resume()
                    self._run_loop(until)
            except LockError:
                # This instance never became the supervisor.  Its cached state
                # may predate the lock holder's current tick, so persisting a
                # contention result could overwrite the active supervisor.
                return
            return
        if resume_stopped:
            self.resume()
        self._run_loop(until)
