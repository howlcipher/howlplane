"""Content-free optional HowlLoop observations for orchestration stages."""

import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from importlib import import_module
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

_dispatch: ContextVar[tuple[str, str] | None] = ContextVar("howlloop_dispatch", default=None)


def observe_operation(operation: str, session_id: str | None = None):
    if not os.environ.get("HOWLLOOP_STORE"):
        return nullcontext()
    try:
        producer = import_module("howlloop.producer")
    except ImportError:
        return nullcontext()

    identifiers = {}
    dispatch = _dispatch.get()
    if dispatch is not None:
        identifiers.update(run_id=dispatch[0], task_id=dispatch[1])
    if session_id:
        try:
            session = str(UUID(session_id))
        except (TypeError, ValueError, AttributeError):
            # Older or custom callers may lack a stable session UUID.
            pass
        else:
            identifiers["session_id"] = session
            identifiers.setdefault("run_id", session)
    return producer.observe("plane", operation, **identifiers)


@contextmanager
def observe_dispatch(operation: str, task_fingerprint: str):
    if not os.environ.get("HOWLLOOP_STORE"):
        yield None
        return
    token = _dispatch.set((str(uuid4()), str(uuid5(NAMESPACE_URL, task_fingerprint))))
    try:
        with observe_operation(operation) as observation:
            yield observation
    finally:
        _dispatch.reset(token)
