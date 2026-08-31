#!/usr/bin/env python3
"""Deterministic factory supervisor tick/run loop."""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        provider_retry_interval_seconds: float = DEFAULT_PROVIDER_RETRY_INTERVAL_SECONDS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        state_dir: Optional[Union[str, Path]] = None,
        lock: Optional[Any] = None,
    ):
        self.state_store = state_store
        self.work_item_store = work_item_store
        self.repo_proposal_store = repo_proposal_store
        self.capability_registry = CapabilityRegistry(capability_store)
        self.dispatcher = dispatcher
        self.discovery = discovery
        self.provider_pool = provider_pool
        self.policy = policy or FactoryPolicy()
        self._clock = clock
        self._sleep = sleep
        self.tick_interval_seconds = tick_interval_seconds
        self.provider_retry_interval_seconds = provider_retry_interval_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._state_dir = Path(state_dir) if state_dir else None
        self._lock = lock
        self._state_record = self.state_store.load()
        if self._state_record.created_at is None:
            self._state_record.created_at = clock().isoformat()

    @property
    def state_record(self) -> SupervisorStateRecord:
        return self._state_record

    def _now(self) -> datetime:
        return self._clock()

    def _now_iso(self) -> str:
        return self._clock().isoformat()

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
            "state": self._state_record.state,
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
            "admission_decisions_count": len(self._state_record.admission_decisions),
            "proposals_awaiting_authority": [
                p.proposal_id for p in self.repo_proposal_store.list_awaiting_authority()
            ],
        }

    def stop(self, reason: str = "operator_stop") -> None:
        self._reload_state()
        if self._state_record.state != SupervisorState.STOPPED:
            self._state_record.transition_to(SupervisorState.STOPPED, reason=reason, at=self._now_iso())
            self._state_record.stopped_reason = reason
            self.state_store.save(self._state_record)

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
            return
        self._state_record.admission_decisions.append(decision)
        self._state_record.admission_decisions = self._state_record.admission_decisions[-1000:]
        self.work_item_store.admit_evidence(
            origin=origin,
            repository=repository,
            title=evidence.get("title", ""),
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
        """Return the soonest (retry_after, resource_id) from provider inventory."""
        soonest: Optional[datetime] = None
        soonest_id: Optional[str] = None
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
        dispatch_id = f"D-{item.work_item_id}-{self._state_record.observations_consumed}"
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
        self._state_record.transition_to(SupervisorState.DISPATCHING, reason="item_selected", at=now_iso)
        self._persist()
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
            item.retry_after = (
                dispatch_result.retry_after
                or self._state_record.provider_wake_conditions.get("retry_after")
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

        work_items = self.work_item_store.list_all()
        selection = select(work_items, self._state_record.dispatch_history, self.policy, now=now)

        if selection.item is not None:
            dispatch_result = self._dispatch(selection.item, now_iso)
            if dispatch_result is None:
                # Missing item: retain evidence and stop/park.
                self._state_record.transition_to(
                    SupervisorState.STOPPED,
                    reason="missing_item_at_dispatch",
                    at=now_iso,
                )
                self._state_record.stopped_reason = "missing_item"
                self._persist()
                return TickResult(
                    state=self._state_record.state,
                    selected_work_item_id=selection.item.work_item_id,
                    next_wake_at=next_wake,
                    reason="missing_item_at_dispatch",
                )

            next_state, reason = self._apply_dispatch_result(selection.item, dispatch_result, now_iso)
            self._state_record.clear_current_dispatch()
            try:
                self._state_record.transition_to(next_state, reason=reason, at=now_iso)
            except InvalidSupervisorStateTransitionError:
                self._state_record.failure_count += 1
                self._state_record.transition_to(
                    SupervisorState.BACKOFF_AFTER_FAILURE, reason="dispatch_state_rejected", at=now_iso
                )
            next_wake = self._ensure_next_wake_persisted(now)
            return TickResult(
                state=self._state_record.state,
                selected_work_item_id=selection.item.work_item_id,
                next_wake_at=next_wake,
                reason=reason,
            )

        if not self._provider_has_capacity():
            next_state = SupervisorState.WAITING_FOR_PROVIDER
            reason = "no_provider_capacity"
        elif selection.reason == "all_candidates_capped":
            next_state = SupervisorState.WAITING_FOR_WORK
            reason = "all_candidates_capped"
        else:
            next_state = SupervisorState.WAITING_FOR_WORK
            reason = "no_dispatchable_work"

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
        )

    def _run_loop(self, until: Optional[datetime] = None) -> None:
        while True:
            self._reload_state()
            if self._state_record.state == SupervisorState.STOPPED:
                break
            result = self.tick()
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

    def run(self, until: Optional[datetime] = None) -> None:
        # The run loop holds a single-supervisor lock for the state directory.
        lock = self._configured_lock()
        if lock is not None:
            from src.control_plane.locking import LockError

            try:
                with lock:
                    self._run_loop(until)
            except LockError as exc:
                now_iso = self._now_iso()
                self._state_record.transition_to(
                    SupervisorState.STOPPED, reason="lock_contention", at=now_iso
                )
                self._state_record.stopped_reason = "lock_contention"
                self._state_record.last_error = str(exc)
                self._persist()
            return
        self._run_loop(until)
