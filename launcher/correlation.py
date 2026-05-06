"""Correlation context — contextvars threading request-scoped IDs through
sync + async call stacks. Filter injects them into every log record so
``%(call_id)s`` / ``%(session_id)s`` are always available.

Usage:

    from launcher.correlation import bind, current

    with bind(call_id=req.call_id, session_id=session_id):
        log.info("handling")           # log line carries both ids automatically

The ``bind`` context manager properly resets on exit — including across
``await`` points within the `with` block. asyncio Tasks created inside the
block inherit the bound values; tasks outside don't.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator

# Each is a separate ContextVar so callers can bind subsets.
_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_id", default=None)
_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_WORKER_NAME: contextvars.ContextVar[str | None] = contextvars.ContextVar("worker_name", default=None)
_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def current() -> dict[str, str | None]:
    """Snapshot of the currently-bound IDs. None when nothing is set."""
    return {
        "call_id": _CALL_ID.get(),
        "session_id": _SESSION_ID.get(),
        "worker_name": _WORKER_NAME.get(),
        "trace_id": _TRACE_ID.get(),
    }


@contextlib.contextmanager
def bind(*, call_id: str | None = None, session_id: str | None = None,
         worker_name: str | None = None, trace_id: str | None = None) -> Iterator[None]:
    tokens: list[contextvars.Token] = []
    if call_id is not None:
        tokens.append(_CALL_ID.set(call_id))
    if session_id is not None:
        tokens.append(_SESSION_ID.set(session_id))
    if worker_name is not None:
        tokens.append(_WORKER_NAME.set(worker_name))
    if trace_id is not None:
        tokens.append(_TRACE_ID.set(trace_id))
    try:
        yield
    finally:
        for t in reversed(tokens):
            try:
                t.var.reset(t)
            except (LookupError, ValueError):
                pass  # token already reset (re-entrant misuse) — drop quietly


class CorrelationFilter(logging.Filter):
    """Inject correlation IDs as record attributes so formatters can use
    ``%(call_id)s`` etc. Missing IDs become empty strings (formatters need
    a value, not None)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _CALL_ID.get() or "-"
        record.session_id = _SESSION_ID.get() or "-"
        record.worker_name = _WORKER_NAME.get() or "-"
        record.trace_id = _TRACE_ID.get() or "-"
        return True
