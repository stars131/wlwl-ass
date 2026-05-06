"""Error envelope, retry/backoff, deadline guards, circuit breaker.

Design principles:
  * **Boundary-only validation.** Workers trust kernel-validated input.
  * **Structured error envelope.** Every wire-bound failure is a dict with
    ``code`` / ``message`` / ``retryable`` / optional ``hint`` / ``docs``.
  * **Narrow catches.** Specific exception types where we know what we're
    catching; ``Exception`` only at hook boundaries (subscriber callbacks,
    plugin lifecycle hooks) where author bugs must not crash the kernel.
  * **No silent swallowing.** Every catch either re-raises, returns a typed
    error, or publishes an audit event.
"""
from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

log = logging.getLogger("wlwl_ass.errors")
T = TypeVar("T")


# ── Standard error codes ────────────────────────────────────────────────


class ErrorCode:
    # request-shape problems
    PAYLOAD_INVALID = "payload_invalid"
    CAPABILITY_REQUIRED = "capability_required"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    NOT_IMPLEMENTED = "not_implemented"
    NOT_FOUND = "not_found"
    CONFIG_INVALID = "config_invalid"

    # auth / authorization
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    TOKEN_INVALID = "token_invalid"
    TOKEN_EXPIRED = "token_expired"

    # runtime / dispatch
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    UPSTREAM_FAILURE = "upstream_failure"
    STORAGE_FAILED = "storage_failed"
    INTERNAL = "internal"

    # back-pressure / overflow
    QUEUE_FULL = "queue_full"
    AUDIT_OVERFLOW = "audit_overflow"


_HINT_BY_CODE: dict[str, str] = {
    ErrorCode.PAYLOAD_INVALID:        "check the request payload against the capability schema",
    ErrorCode.CAPABILITY_UNAVAILABLE: "ensure a worker offering that capability is registered + not silenced",
    ErrorCode.AUTH_FAILED:            "verify the bearer token / signing key matches the kernel",
    ErrorCode.TOKEN_EXPIRED:          "token is past TTL; the kernel should mint a new one",
    ErrorCode.DEADLINE_EXCEEDED:      "increase deadline_ms or reduce upstream latency",
    ErrorCode.RATE_LIMITED:           "back off then retry; check provider quotas",
    ErrorCode.CIRCUIT_OPEN:           "worker keeps failing; will auto-recover after cooldown",
    ErrorCode.UPSTREAM_FAILURE:       "transient — retried automatically; persistent = check provider",
    ErrorCode.STORAGE_FAILED:         "DB or disk is full / corrupted / locked",
}

_RETRYABLE_CODES = frozenset({
    ErrorCode.RATE_LIMITED, ErrorCode.UPSTREAM_FAILURE,
    ErrorCode.DEADLINE_EXCEEDED, ErrorCode.STORAGE_FAILED,
})


def make_error(code: str, message: str, *, retryable: bool | None = None,
               hint: str | None = None, **extras: Any) -> dict:
    """Construct the standard error envelope.

    ``retryable`` defaults to ``True`` for codes in the retryable set.
    ``hint`` defaults to a per-code suggestion.
    Extra fields (``call_id``, ``worker``, etc.) are merged in.
    """
    if retryable is None:
        retryable = code in _RETRYABLE_CODES
    if hint is None:
        hint = _HINT_BY_CODE.get(code)
    out: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if hint:
        out["hint"] = hint
    if extras:
        out.update(extras)
    return out


class ApiError(Exception):
    """Raise inside a worker / handler when you have a structured error to bubble.

    The kernel converts this to an InvokeResponse error envelope. Plain
    ``Exception`` becomes ``UPSTREAM_FAILURE retryable=True``; raise this
    when you have something more specific.
    """
    def __init__(self, code: str, message: str, *, retryable: bool | None = None,
                 hint: str | None = None, **extras: Any):
        super().__init__(message)
        self.envelope = make_error(code, message, retryable=retryable, hint=hint, **extras)


# ── Deadline guard ──────────────────────────────────────────────────────


class DeadlineExceeded(ApiError):
    def __init__(self, message: str = "deadline exceeded", **extras: Any):
        super().__init__(ErrorCode.DEADLINE_EXCEEDED, message, retryable=True, **extras)


@dataclass
class Deadline:
    """Absolute deadline as a monotonic timestamp.

    Construct via ``Deadline.from_ms(deadline_ms)`` at the boundary; pass the
    Deadline object inwards. Workers can call ``deadline.remaining_ms()``
    when delegating to a slower upstream.
    """
    expires_at: float          # time.monotonic()-relative

    @classmethod
    def from_ms(cls, deadline_ms: int | float) -> "Deadline":
        return cls(expires_at=time.monotonic() + (deadline_ms / 1000.0))

    @classmethod
    def never(cls) -> "Deadline":
        return cls(expires_at=float("inf"))

    def remaining_ms(self) -> float:
        rem = (self.expires_at - time.monotonic()) * 1000.0
        return max(0.0, rem)

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded("deadline reached before completion")


# ── Retry / backoff ─────────────────────────────────────────────────────


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 5.0
    multiplier: float = 2.0
    jitter: float = 0.25                # ±25% jitter
    retry_codes: frozenset[str] = field(default_factory=lambda: _RETRYABLE_CODES)
    retry_exception_types: tuple[type[Exception], ...] = (ConnectionError, TimeoutError)


def with_retry(
    fn: Callable[[], T], *, policy: RetryPolicy | None = None,
    deadline: Deadline | None = None, on_error: Callable[[int, Exception], None] | None = None,
) -> T:
    """Run ``fn`` with retries on retryable errors. Stops on:

      - success
      - non-retryable ApiError (re-raised)
      - max_attempts reached
      - deadline expired (raises DeadlineExceeded)
    """
    p = policy or RetryPolicy()
    last_exc: Exception | None = None
    for attempt in range(1, p.max_attempts + 1):
        if deadline and deadline.expired():
            raise DeadlineExceeded(f"deadline expired before attempt {attempt}")
        try:
            return fn()
        except ApiError as e:
            last_exc = e
            if e.envelope.get("code") not in p.retry_codes:
                raise
            if on_error:
                on_error(attempt, e)
        except p.retry_exception_types as e:
            last_exc = e
            if on_error:
                on_error(attempt, e)
        if attempt == p.max_attempts:
            break
        delay = min(p.max_delay_s, p.base_delay_s * (p.multiplier ** (attempt - 1)))
        delay = delay * (1 + random.uniform(-p.jitter, p.jitter))
        if deadline:
            remaining_s = deadline.remaining_ms() / 1000.0
            if delay >= remaining_s:
                raise DeadlineExceeded("retry delay would overshoot deadline")
        time.sleep(max(0.0, delay))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry exhausted attempts without an exception")


# ── Circuit breaker ─────────────────────────────────────────────────────


class CircuitState:
    CLOSED = "closed"      # normal traffic
    OPEN = "open"          # all calls rejected immediately
    HALF_OPEN = "half_open"  # one trial call permitted


@dataclass
class CircuitConfig:
    failure_threshold: int = 5      # consecutive failures to trip
    cooldown_s: float = 30.0        # OPEN → HALF_OPEN after this
    success_to_close: int = 1       # successes in HALF_OPEN to fully close
    failure_window_s: float = 60.0  # consecutive only counts within this window


@dataclass
class _CircuitState:
    state: str = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    half_open_successes: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Per-key circuit breaker. Thread-safe."""

    def __init__(self, config: CircuitConfig | None = None) -> None:
        self.config = config or CircuitConfig()
        self._state: dict[str, _CircuitState] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, str, str, dict], None]] = []

    def add_listener(self, cb: Callable[[str, str, str, dict], None]) -> None:
        """Listener gets ``(key, old_state, new_state, ctx)`` on every transition."""
        self._listeners.append(cb)

    def allow(self, key: str) -> bool:
        with self._lock:
            s = self._state.setdefault(key, _CircuitState())
            if s.state == CircuitState.CLOSED:
                return True
            if s.state == CircuitState.OPEN:
                if (time.monotonic() - s.opened_at) >= self.config.cooldown_s:
                    self._transition(key, s, CircuitState.HALF_OPEN, reason="cooldown_elapsed")
                    return True
                return False
            # HALF_OPEN: permit
            return True

    def record_success(self, key: str) -> None:
        with self._lock:
            s = self._state.setdefault(key, _CircuitState())
            s.consecutive_failures = 0
            if s.state == CircuitState.HALF_OPEN:
                s.half_open_successes += 1
                if s.half_open_successes >= self.config.success_to_close:
                    self._transition(key, s, CircuitState.CLOSED, reason="recovered")

    def record_failure(self, key: str) -> None:
        with self._lock:
            s = self._state.setdefault(key, _CircuitState())
            now = time.monotonic()
            if (now - s.last_failure_ts) > self.config.failure_window_s:
                s.consecutive_failures = 0
            s.last_failure_ts = now
            s.consecutive_failures += 1
            if s.state == CircuitState.HALF_OPEN:
                self._transition(key, s, CircuitState.OPEN, reason="half_open_trial_failed")
                return
            if s.consecutive_failures >= self.config.failure_threshold:
                self._transition(key, s, CircuitState.OPEN, reason="threshold_reached")

    def state(self, key: str) -> str:
        with self._lock:
            return self._state.setdefault(key, _CircuitState()).state

    def reset(self, key: str) -> None:
        with self._lock:
            old = self._state.get(key)
            if old is None:
                return
            old_state = old.state
            self._state[key] = _CircuitState()
            self._notify(key, old_state, CircuitState.CLOSED, {"reason": "manual_reset"})

    def _transition(self, key: str, s: _CircuitState, new_state: str, *, reason: str) -> None:
        old = s.state
        if old == new_state:
            return
        s.state = new_state
        if new_state == CircuitState.OPEN:
            s.opened_at = time.monotonic()
            s.half_open_successes = 0
        elif new_state == CircuitState.CLOSED:
            s.consecutive_failures = 0
            s.half_open_successes = 0
        self._notify(key, old, new_state, {"reason": reason,
                                           "consecutive_failures": s.consecutive_failures})

    def _notify(self, key: str, old: str, new: str, ctx: dict) -> None:
        for cb in self._listeners:
            try:
                cb(key, old, new, ctx)
            except Exception:  # noqa: BLE001 — listener bug must not break breaker
                log.exception("circuit listener crashed")


# ── Hook safety ──────────────────────────────────────────────────────────


@contextlib.contextmanager
def safe_hook(name: str, *, audit_cb: Callable[[str, dict], None] | None = None):
    """Wrap a third-party hook (subscriber callback, plugin lifecycle method)
    that we don't trust to be exception-safe.

    Logs at ERROR level + emits ``hook_failed`` audit event. Does NOT
    suppress :class:`KeyboardInterrupt` / :class:`SystemExit`.
    """
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.exception("hook %r failed", name)
        if audit_cb:
            try:
                audit_cb("hook_failed", {"hook": name, "error": repr(exc)})
            except Exception:  # noqa: BLE001
                pass
