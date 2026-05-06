"""Tests for the production-hardening additions:
  * centralized logging + correlation ID propagation
  * standard error envelope + retry + circuit breaker + deadline
  * kernel: deadline enforcement, circuit-skip routing, health probe,
    token rotation, audit completeness
  * metrics registry: counters / histogram / per-worker isolation
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── A. logging + correlation ────────────────────────────────────────────


def test_correlation_filter_injects_into_records(tmp_path, monkeypatch):
    from launcher import logging_config as lc
    from launcher.correlation import bind

    log_dir = str(tmp_path / "logs")
    lc.setup_logging(level="DEBUG", log_dir=log_dir, force=True)
    log_file = next(p for p in os.listdir(log_dir) if p.endswith(".log"))
    log = logging.getLogger("wlwl_ass.test.correlation")

    with bind(call_id="c-77", session_id="s-9"):
        log.info("with binding")
    log.info("no binding")
    # flush
    for h in logging.getLogger().handlers:
        h.flush()

    text = (tmp_path / "logs" / log_file).read_text(encoding="utf-8")
    assert "call=c-77" in text and "session=s-9" in text
    # the no-binding line should have placeholders
    assert any("call=- session=-" in line for line in text.splitlines() if "no binding" in line)


@pytest.mark.asyncio
async def test_correlation_propagates_through_asyncio_tasks():
    from launcher.correlation import bind, current

    async def child():
        return current()["call_id"]

    async def parent():
        with bind(call_id="parent-call"):
            return await asyncio.create_task(child())

    assert await parent() == "parent-call"


def test_setup_logging_idempotent(tmp_path):
    from launcher import logging_config as lc
    log_dir = str(tmp_path / "logs")
    lc.setup_logging(log_dir=log_dir, force=True)
    handlers_before = list(logging.getLogger().handlers)
    lc.setup_logging(log_dir=log_dir)  # no force → no-op
    handlers_after = list(logging.getLogger().handlers)
    assert handlers_before == handlers_after


# ── B. errors module ────────────────────────────────────────────────────


def test_make_error_default_retryable():
    from llmcore.errors import ErrorCode, make_error
    e = make_error(ErrorCode.RATE_LIMITED, "slow down")
    assert e["retryable"] is True
    assert "hint" in e
    e2 = make_error(ErrorCode.PAYLOAD_INVALID, "bad shape")
    assert e2["retryable"] is False


def test_retry_succeeds_on_transient():
    from llmcore.errors import ApiError, ErrorCode, RetryPolicy, with_retry
    attempts = [0]
    def flaky():
        attempts[0] += 1
        if attempts[0] < 3:
            raise ApiError(ErrorCode.UPSTREAM_FAILURE, "transient")
        return 42
    out = with_retry(flaky, policy=RetryPolicy(max_attempts=4, base_delay_s=0.001))
    assert out == 42 and attempts[0] == 3


def test_retry_bubbles_non_retryable():
    from llmcore.errors import ApiError, ErrorCode, with_retry
    def bad():
        raise ApiError(ErrorCode.PAYLOAD_INVALID, "no")
    with pytest.raises(ApiError) as ei:
        with_retry(bad)
    assert ei.value.envelope["code"] == ErrorCode.PAYLOAD_INVALID


def test_deadline_check_raises():
    from llmcore.errors import Deadline, DeadlineExceeded
    d = Deadline.from_ms(20)
    time.sleep(0.03)
    assert d.expired()
    with pytest.raises(DeadlineExceeded):
        d.check()


def test_circuit_breaker_lifecycle():
    from llmcore.errors import CircuitBreaker, CircuitConfig, CircuitState
    br = CircuitBreaker(CircuitConfig(failure_threshold=2, cooldown_s=0.05))
    assert br.allow("w")
    br.record_failure("w"); br.record_failure("w")
    assert not br.allow("w")
    assert br.state("w") == CircuitState.OPEN
    time.sleep(0.06)
    assert br.allow("w")
    assert br.state("w") == CircuitState.HALF_OPEN
    br.record_success("w")
    assert br.state("w") == CircuitState.CLOSED


def test_safe_hook_audits_failure():
    from llmcore.errors import safe_hook
    audit = []
    with safe_hook("test", audit_cb=lambda t, p: audit.append((t, p))):
        raise RuntimeError("oops")
    assert audit and audit[0][0] == "hook_failed"


# ── C. kernel hardening ─────────────────────────────────────────────────


@pytest.fixture
def kernel(tmp_path):
    """Fresh kernel with health probe DISABLED so tests have full control."""
    from llmcore.kernel import Kernel, reset_kernel
    from llmcore.errors import CircuitConfig
    reset_kernel()
    k = Kernel(
        circuit_config=CircuitConfig(failure_threshold=2, cooldown_s=0.05),
        enable_health_probe=False,
    )
    # bootstrap factories manually since we constructed Kernel directly
    from llmcore.workers import iter_builtin_factories
    for f in iter_builtin_factories():
        k.register_factory(f)
    yield k, tmp_path
    # Explicit cleanup: remove every worker so DB connections close before
    # pytest's tmp_path cleanup tries to delete the SQLite files on Windows.
    for view in list(k.list_workers()):
        try:
            k.remove_worker(view.name, drain_timeout_ms=500, reason="test_teardown")
        except Exception:
            pass
    k.shutdown()


def test_kernel_deadline_pre_dispatch(kernel):
    k, td = kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    r = k.dispatch(
        capability="calendar.query_events.v1", payload={}, deadline_ms=0,
    )
    assert not r.ok
    assert r.error["code"] == "payload_invalid"  # deadline_ms must be > 0


def test_kernel_circuit_open_after_failures(kernel):
    """Force a worker to fail twice → breaker opens → next dispatch rejected fast."""
    k, _ = kernel
    from llmcore.worker import (
        WorkerMetadata, HealthReport, InvokeRequest, InvokeResponse,
        FactoryDescription, KernelHandle, Worker, WorkerFactory, API_VERSION,
    )
    from llmcore.errors import ErrorCode, make_error
    from llmcore.capabilities import register_capability
    register_capability(id="test.always_fail.v1", description="always fails for tests",
                        request_schema={"type": "object"})

    class FailingWorker:
        def __init__(self, name):
            self._meta = WorkerMetadata(name=name, kind="failing",
                                         capabilities=("test.always_fail.v1",),
                                         api_version=API_VERSION)
        @property
        def metadata(self): return self._meta
        def health(self): return HealthReport(state="ready")
        def invoke(self, req: InvokeRequest):
            return InvokeResponse(call_id=req.call_id, ok=False,
                                   error=make_error(ErrorCode.UPSTREAM_FAILURE, "boom",
                                                    retryable=True))
        def shutdown(self, *, drain_timeout_ms=5000): pass

    class FailingFactory:
        def describe(self):
            return FactoryDescription(
                factory_id="test.failing_worker", api_version=API_VERSION,
                capabilities_offered=("test.always_fail.v1",), transport="in_process",
            )
        def build(self, config: dict, kernel: KernelHandle) -> Worker:
            return FailingWorker(name=config["name"])

    k.register_factory(FailingFactory())
    k.add_worker({"name": "boom1", "kind": "failing",
                  "factory_id": "test.failing_worker"}, source="test")
    k.dispatch(capability="test.always_fail.v1", payload={})
    k.dispatch(capability="test.always_fail.v1", payload={})
    # Now circuit should be OPEN (failure_threshold=2)
    r = k.dispatch(capability="test.always_fail.v1", payload={})
    assert not r.ok
    assert r.error["code"] == "circuit_open"

    # Verify audit captured the transition
    audit = k.forum.tail("audit", reader="kernel", n=50)
    transitions = [m for m in audit if m.type == "circuit_transition"]
    assert any(t.payload["to"] == "open" for t in transitions)


def test_kernel_token_rotation_fires_hook(kernel):
    """Set a tiny token TTL via direct mutation to exercise the rotation path."""
    k, td = kernel
    k.add_worker({"name": "cal2", "kind": "calendar", "db_path": str(td / "cal2.db")})
    entry = k._workers["cal2"]
    refresh_called = []
    # attach hook
    entry.worker.on_token_refresh = lambda new_wire: refresh_called.append(new_wire)
    # Force rotation by setting expires_at to "now"
    entry.token_expires_at = int(time.time()) + 5  # < TOKEN_ROTATE_BEFORE_S of 60
    k.dispatch(capability="calendar.query_events.v1", payload={})
    assert refresh_called, "on_token_refresh hook should fire when TTL < threshold"

    # audit captured
    audit = k.forum.tail("audit", reader="kernel", n=50)
    rotated = [m for m in audit if m.type == "token_rotated"]
    assert any(m.payload["worker"] == "cal2" for m in rotated)


def test_kernel_health_probe_silences_after_consecutive_down(kernel):
    """A worker reporting 'down' three times gets auto-silenced."""
    k, _ = kernel
    from llmcore.worker import (
        WorkerMetadata, HealthReport, InvokeRequest, InvokeResponse,
        FactoryDescription, KernelHandle, Worker, API_VERSION,
    )
    from llmcore.capabilities import register_capability
    register_capability(id="test.healthcheck.v1", description="for health probe test")

    class FlakyWorker:
        def __init__(self, name):
            self._meta = WorkerMetadata(name=name, kind="flaky",
                                         capabilities=("test.healthcheck.v1",),
                                         api_version=API_VERSION)
        @property
        def metadata(self): return self._meta
        def health(self): return HealthReport(state="down", last_error="upstream offline")
        def invoke(self, req): return InvokeResponse(call_id=req.call_id, ok=True, result={})
        def shutdown(self, *, drain_timeout_ms=5000): pass

    class FlakyFactory:
        def describe(self):
            return FactoryDescription(factory_id="test.flaky", api_version=API_VERSION,
                                       capabilities_offered=("test.healthcheck.v1",),
                                       transport="in_process")
        def build(self, config, kernel): return FlakyWorker(config["name"])

    k.register_factory(FlakyFactory())
    k.add_worker({"name": "flaky", "kind": "flaky",
                  "factory_id": "test.flaky"}, source="test")

    # Manually drive 3 health probe iterations
    for _ in range(3):
        k._health.poll_once()
    # Worker should now be silenced
    assert k._workers["flaky"].silenced
    audit = k.forum.tail("audit", reader="kernel", n=80)
    assert any(m.type == "health_down" for m in audit)
    assert any(m.type == "worker_silenced" for m in audit)


def test_kernel_audits_factory_register(kernel):
    """Registering a factory should emit a factory_registered event."""
    k, _ = kernel
    audit = k.forum.tail("audit", reader="kernel", n=80)
    factories_audited = [m for m in audit if m.type == "factory_registered"]
    assert len(factories_audited) >= 4   # 4 in-tree factories at minimum


def test_kernel_audits_reload_with_rollback(kernel):
    """A failed reload (bad config) should rollback + audit."""
    k, td = kernel
    k.add_worker({"name": "cal3", "kind": "calendar", "db_path": str(td / "cal3.db")})
    # Try reload with a bogus config that will fail (missing factory_id mapping)
    with pytest.raises(Exception):
        k.reload_worker("cal3", new_config={"name": "cal3", "kind": "nonexistent_kind"})
    # rollback should have re-registered
    assert "cal3" in k._workers


# ── D. metrics ──────────────────────────────────────────────────────────


def test_metrics_records_dispatch_outcomes(kernel):
    k, td = kernel
    k.add_worker({"name": "cal4", "kind": "calendar", "db_path": str(td / "cal4.db")})
    for _ in range(3):
        k.dispatch(capability="calendar.query_events.v1", payload={})
    snap = k.metrics.snapshot()
    cal_snap = next(w for w in snap if w["worker"] == "cal4")
    assert "calendar.query_events.v1" in cal_snap["by_capability"]
    s = cal_snap["by_capability"]["calendar.query_events.v1"]
    assert s["ok"] == 3
    assert s["err_total"] == 0
    assert s["latency"]["count"] == 3


def test_histogram_percentiles():
    from llmcore.metrics import Histogram
    h = Histogram(buckets=(10, 50, 100, 500))
    for ms in (5, 8, 30, 80, 200):
        h.observe(ms)
    s = h.stats()
    assert s["count"] == 5
    assert s["p50_ms"] >= 10  # median ≈ 30, falls into bucket 50
    assert s["p99_ms"] >= 100


def test_metrics_per_worker_isolation(kernel):
    k, td = kernel
    k.add_worker({"name": "cA", "kind": "calendar", "db_path": str(td / "cA.db")})
    k.add_worker({"name": "cB", "kind": "calendar", "db_path": str(td / "cB.db")})
    k.dispatch(capability="calendar.query_events.v1", payload={})  # routes to cA (first)
    snap = k.metrics.snapshot()
    by_name = {s["worker"]: s for s in snap}
    a = by_name["cA"]["by_capability"].get("calendar.query_events.v1", {}).get("ok", 0)
    b = by_name["cB"]["by_capability"].get("calendar.query_events.v1", {}).get("ok", 0)
    assert a + b == 1
