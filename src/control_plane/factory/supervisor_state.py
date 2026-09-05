#!/usr/bin/env python3
"""Atomic durable state for the factory supervisor loop."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.durable_store import ArtifactIdentityError, DurableObjectStore
from src.control_plane.task_spec import DataClassSerializationMixin

SUPERVISOR_STATE_SCHEMA_VERSION = "howlplane.factory.supervisor_state/v2"
SUPERVISOR_STATE_ID = "factory_supervisor"


class SupervisorState(str, Enum):
    IDLE = "idle"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    WAITING_FOR_WORK = "waiting_for_work"
    WAITING_FOR_AUTHORITY = "waiting_for_authority"
    WAITING_FOR_DEPENDENCY = "waiting_for_dependency"
    BACKOFF_AFTER_FAILURE = "backoff_after_failure"
    DISPATCHING = "dispatching"
    STOPPED = "stopped"


def _plain(value: Any) -> str:
    return str(getattr(value, "value", value))


SUPERVISOR_TRANSITIONS: Dict[str, Set[str]] = {
    _plain(SupervisorState.IDLE): {
        _plain(SupervisorState.WAITING_FOR_PROVIDER),
        _plain(SupervisorState.WAITING_FOR_WORK),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.WAITING_FOR_PROVIDER): {
        _plain(SupervisorState.WAITING_FOR_WORK),
        _plain(SupervisorState.BACKOFF_AFTER_FAILURE),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.WAITING_FOR_WORK): {
        _plain(SupervisorState.WAITING_FOR_PROVIDER),
        _plain(SupervisorState.BACKOFF_AFTER_FAILURE),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.WAITING_FOR_AUTHORITY): {
        _plain(SupervisorState.WAITING_FOR_WORK),
        _plain(SupervisorState.BACKOFF_AFTER_FAILURE),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.WAITING_FOR_DEPENDENCY): {
        _plain(SupervisorState.WAITING_FOR_WORK),
        _plain(SupervisorState.BACKOFF_AFTER_FAILURE),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.BACKOFF_AFTER_FAILURE): {
        _plain(SupervisorState.WAITING_FOR_PROVIDER),
        _plain(SupervisorState.WAITING_FOR_WORK),
        _plain(SupervisorState.IDLE),
        _plain(SupervisorState.DISPATCHING),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.DISPATCHING): {
        _plain(SupervisorState.IDLE),
        _plain(SupervisorState.WAITING_FOR_PROVIDER),
        _plain(SupervisorState.WAITING_FOR_AUTHORITY),
        _plain(SupervisorState.WAITING_FOR_DEPENDENCY),
        _plain(SupervisorState.BACKOFF_AFTER_FAILURE),
        _plain(SupervisorState.STOPPED),
    },
    _plain(SupervisorState.STOPPED): {
        _plain(SupervisorState.IDLE),
    },
}


class InvalidSupervisorStateTransitionError(ValueError):
    """Raised when an illegal supervisor state transition is attempted."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SupervisorStateRecord(DataClassSerializationMixin):
    """Persisted record of the factory supervisor's state."""

    supervisor_id: str = SUPERVISOR_STATE_ID
    schema_version: str = SUPERVISOR_STATE_SCHEMA_VERSION
    created_at: Optional[str] = None
    state: str = _plain(SupervisorState.IDLE)
    last_tick_at: Optional[str] = None
    last_successful_tick_at: Optional[str] = None
    next_wake_at: Optional[str] = None
    current_work_item_id: Optional[str] = None
    last_work_item_id: Optional[str] = None
    current_task_id: Optional[str] = None
    current_dispatch_id: Optional[str] = None
    observations_consumed: int = 0
    merges_count: int = 0
    admission_decisions: List[Dict[str, Any]] = field(default_factory=list)
    provider_wake_conditions: Dict[str, Any] = field(default_factory=dict)
    recent_completed: List[Dict[str, Any]] = field(default_factory=list)
    recent_failed: List[Dict[str, Any]] = field(default_factory=list)
    recent_parked: List[Dict[str, Any]] = field(default_factory=list)
    last_error: Optional[str] = None
    failure_count: int = 0
    stopped_reason: Optional[str] = None
    transition_history: List[Dict[str, Any]] = field(default_factory=list)
    dispatch_history: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        self.state = _plain(self.state)
        if self.state not in SUPERVISOR_TRANSITIONS:
            raise ValueError(f"Unknown supervisor state: {self.state!r}")

    def transition_to(self, new_state: Any, reason: str = "", at: Optional[str] = None) -> None:
        target = _plain(new_state)
        allowed = SUPERVISOR_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            legal = sorted(allowed)
            raise InvalidSupervisorStateTransitionError(
                f"Cannot move supervisor from '{self.state}' to '{target}'. "
                f"Allowed: {legal or 'none (terminal state)'}"
            )
        self.state = target
        self.last_tick_at = at or _utc_now_iso()
        self.transition_history.append(
            {"at": self.last_tick_at, "to_state": target, "reason": reason}
        )

    def record_dispatch(
        self,
        dispatch_id: str,
        work_item_id: str,
        task_id: str,
        now_iso: str,
        origin: Optional[str] = None,
        repository: Optional[str] = None,
    ) -> None:
        self.current_dispatch_id = dispatch_id
        self.current_work_item_id = work_item_id
        self.current_task_id = task_id
        self.last_work_item_id = work_item_id
        self.dispatch_history.append({
            "dispatch_id": dispatch_id,
            "work_item_id": work_item_id,
            "task_id": task_id,
            "at": now_iso,
            "origin": origin,
            "repository": repository,
        })
        self.dispatch_history = self.dispatch_history[-100:]

    def record_completion(self, work_item_id: str, task_id: str, now_iso: str) -> None:
        self.last_successful_tick_at = now_iso
        entry = {"work_item_id": work_item_id, "task_id": task_id, "at": now_iso}
        self.recent_completed.append(entry)
        self.recent_completed = self.recent_completed[-50:]

    def record_failure(self, work_item_id: str, task_id: str, reason: str, now_iso: str) -> None:
        entry = {
            "work_item_id": work_item_id,
            "task_id": task_id,
            "reason": reason,
            "at": now_iso,
        }
        self.recent_failed.append(entry)
        self.recent_failed = self.recent_failed[-50:]
        self.last_error = reason

    def record_park(self, work_item_id: str, task_id: str, reason: str, now_iso: str) -> None:
        """Authority/dependency parks are not failures; record them separately."""
        entry = {
            "work_item_id": work_item_id,
            "task_id": task_id,
            "reason": reason,
            "at": now_iso,
        }
        self.recent_parked.append(entry)
        self.recent_parked = self.recent_parked[-50:]

    def clear_current_dispatch(self) -> None:
        self.current_work_item_id = None
        self.current_task_id = None
        self.current_dispatch_id = None

    def reconcile_on_load(self) -> None:
        """Fail-closed reconciliation after restart."""
        if self.state == _plain(SupervisorState.DISPATCHING):
            retained_item = self.current_work_item_id
            retained_dispatch = self.current_dispatch_id
            retained_task = self.current_task_id
            self.transition_to(
                SupervisorState.BACKOFF_AFTER_FAILURE,
                reason="restart_during_dispatch",
            )
            self.failure_count += 1
            self.last_error = (
                f"Restart during dispatch; retained current_work_item_id={retained_item} "
                f"dispatch_id={retained_dispatch} task_id={retained_task} for reconciliation"
            )
            self.current_work_item_id = retained_item
            self.current_dispatch_id = retained_dispatch
            self.current_task_id = retained_task


class SupervisorStateStore(DurableObjectStore):
    """Atomic store for the singleton supervisor state record."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=SupervisorStateRecord.from_dict,
            dedup_field=None,
            id_attr=None,
        )

    def load(self) -> SupervisorStateRecord:
        if not self.exists(SUPERVISOR_STATE_ID):
            record = SupervisorStateRecord(created_at=_utc_now_iso())
            self.save(record)
            return record
        try:
            data = safe_load_json(self._path(SUPERVISOR_STATE_ID))
        except Exception as exc:
            # Preserve the malformed artifact byte-for-byte for diagnosis.  The
            # in-memory STOPPED record fails closed without destroying evidence.
            return self._malformed_record(f"Failed to read supervisor state: {exc}")

        if data.get("schema_version") != SUPERVISOR_STATE_SCHEMA_VERSION:
            return self._malformed_record(
                f"Invalid schema version: {data.get('schema_version')!r}"
            )

        try:
            record = self._factory(data)
        except Exception as exc:
            return self._malformed_record(f"Failed to load supervisor state: {exc}")

        if record.state not in SUPERVISOR_TRANSITIONS:
            return self._malformed_record(
                f"Unknown supervisor state: {record.state!r}"
            )

        record.reconcile_on_load()
        return record

    @staticmethod
    def _malformed_record(reason: str) -> SupervisorStateRecord:
        return SupervisorStateRecord(
            created_at=_utc_now_iso(),
            state=_plain(SupervisorState.STOPPED),
            stopped_reason="malformed_state",
            last_error=reason,
        )

    def save(self, record: SupervisorStateRecord) -> Path:
        return DurableObjectStore.save(
            self, SUPERVISOR_STATE_ID, record.to_dict()
        )
