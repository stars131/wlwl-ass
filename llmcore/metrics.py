"""Per-worker metrics — counters, latency histogram, gauges + JSONL spool.

Stays in-process (thread-safe locking, no external metrics backend). Periodic
snapshots roll to ``temp/metrics.jsonl`` so the CLI / future GUI can read
them without touching the live registry.

The kernel's ``dispatch`` and ``dispatch_stream`` paths call into
``MetricsRegistry`` automatically via ``record_dispatch()``. Workers may emit
custom counters too (cost, tokens) via the same registry.
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger("wlwl_ass.metrics")


# ── Histogram ────────────────────────────────────────────────────────────


_DEFAULT_BUCKETS_MS = (
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 30_000, 60_000,
)


class Histogram:
    """Bucketed cumulative + sample reservoir.

    Buckets are upper-bound milliseconds. We keep both bucket counts (for
    fast P50/P90/P99 estimation) and a small reservoir sample (for the
    occasional ad-hoc analysis).
    """

    __slots__ = ("_buckets", "_counts", "_sum", "_n", "_lock")

    def __init__(self, buckets: tuple[int, ...] = _DEFAULT_BUCKETS_MS) -> None:
        self._buckets = buckets
        self._counts = [0] * (len(buckets) + 1)  # +1 for overflow
        self._sum = 0.0
        self._n = 0
        self._lock = threading.RLock()

    def observe(self, ms: float) -> None:
        idx = bisect.bisect_left(self._buckets, ms)
        with self._lock:
            self._counts[idx] += 1
            self._sum += ms
            self._n += 1

    def percentile(self, q: float) -> float:
        """Approximate q-th percentile. Returns 0.0 if no observations."""
        with self._lock:
            n = self._n
            if n == 0:
                return 0.0
            target = max(1, int(n * q))
            running = 0
            for i, c in enumerate(self._counts):
                running += c
                if running >= target:
                    return float(self._buckets[i] if i < len(self._buckets) else self._buckets[-1])
            return float(self._buckets[-1])

    def stats(self) -> dict[str, float]:
        with self._lock:
            n = self._n
            return {
                "count": n,
                "avg_ms": (self._sum / n) if n else 0.0,
                "p50_ms": self.percentile(0.50),
                "p90_ms": self.percentile(0.90),
                "p99_ms": self.percentile(0.99),
            }


# ── Counters / gauges ────────────────────────────────────────────────────


class Counter:
    __slots__ = ("_v", "_lock")
    def __init__(self) -> None:
        self._v = 0.0
        self._lock = threading.Lock()
    def add(self, n: float = 1.0) -> None:
        with self._lock:
            self._v += n
    def value(self) -> float:
        with self._lock:
            return self._v


class Gauge:
    __slots__ = ("_v", "_lock")
    def __init__(self) -> None:
        self._v = 0.0
        self._lock = threading.Lock()
    def set(self, v: float) -> None:
        with self._lock:
            self._v = v
    def add(self, n: float) -> None:
        with self._lock:
            self._v += n
    def value(self) -> float:
        with self._lock:
            return self._v


# ── Registry ────────────────────────────────────────────────────────────


@dataclass
class WorkerMetrics:
    name: str
    invocations_ok: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    invocations_err: dict[str, dict[str, Counter]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Counter))
    )
    latency: dict[str, Histogram] = field(default_factory=lambda: defaultdict(Histogram))
    cost_usd: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    tokens_in: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    tokens_out: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    in_flight: Gauge = field(default_factory=Gauge)
    last_state_change: float = 0.0
    circuit_state: str = "closed"

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "worker": self.name,
            "in_flight": self.in_flight.value(),
            "circuit_state": self.circuit_state,
            "by_capability": {},
        }
        caps = set(self.invocations_ok) | set(self.invocations_err) | set(self.latency) \
            | set(self.cost_usd) | set(self.tokens_in) | set(self.tokens_out)
        for cap in caps:
            ok = self.invocations_ok[cap].value()
            err_by_code = {code: c.value() for code, c in self.invocations_err[cap].items()}
            err_total = sum(err_by_code.values())
            total = ok + err_total
            stats = self.latency[cap].stats()
            out["by_capability"][cap] = {
                "ok": ok, "err_total": err_total,
                "err_by_code": err_by_code,
                "success_rate": (ok / total) if total else 0.0,
                "latency": stats,
                "cost_usd_total": self.cost_usd[cap].value(),
                "tokens_in_total": self.tokens_in[cap].value(),
                "tokens_out_total": self.tokens_out[cap].value(),
            }
        return out


class MetricsRegistry:
    """Thread-safe registry. One instance per kernel. Optionally periodically
    flushes snapshots to ``temp/metrics.jsonl`` (one line per flush).
    """

    def __init__(self, *, spool_path: str | None = None,
                 flush_interval_s: float = 300.0) -> None:
        self._workers: dict[str, WorkerMetrics] = {}
        self._lock = threading.RLock()
        self._spool_path = spool_path
        self._flush_interval_s = flush_interval_s
        self._flush_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def for_worker(self, name: str) -> WorkerMetrics:
        with self._lock:
            wm = self._workers.get(name)
            if wm is None:
                wm = WorkerMetrics(name=name)
                self._workers[name] = wm
            return wm

    def drop_worker(self, name: str) -> None:
        with self._lock:
            self._workers.pop(name, None)

    def record_dispatch(
        self, *, worker: str, capability: str, ok: bool,
        latency_ms: float, cost_usd: float = 0.0,
        tokens_in: int = 0, tokens_out: int = 0,
        error_code: str | None = None,
    ) -> None:
        wm = self.for_worker(worker)
        wm.latency[capability].observe(latency_ms)
        if cost_usd:
            wm.cost_usd[capability].add(cost_usd)
        if tokens_in:
            wm.tokens_in[capability].add(tokens_in)
        if tokens_out:
            wm.tokens_out[capability].add(tokens_out)
        if ok:
            wm.invocations_ok[capability].add(1)
        else:
            code = error_code or "unknown"
            wm.invocations_err[capability][code].add(1)

    def set_circuit_state(self, worker: str, state: str) -> None:
        wm = self.for_worker(worker)
        wm.circuit_state = state
        wm.last_state_change = time.time()

    def increment_in_flight(self, worker: str, delta: float = 1.0) -> None:
        self.for_worker(worker).in_flight.add(delta)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [w.snapshot() for w in self._workers.values()]

    def render_text(self) -> str:
        out = []
        for w in self.snapshot():
            out.append(f"worker={w['worker']} in_flight={int(w['in_flight'])} circuit={w['circuit_state']}")
            for cap, stats in w["by_capability"].items():
                line = (
                    f"  cap={cap} ok={int(stats['ok'])} err={int(stats['err_total'])} "
                    f"success={stats['success_rate']:.2%} "
                    f"p50={int(stats['latency']['p50_ms'])}ms "
                    f"p90={int(stats['latency']['p90_ms'])}ms "
                    f"p99={int(stats['latency']['p99_ms'])}ms"
                )
                if stats["cost_usd_total"]:
                    line += f" cost=${stats['cost_usd_total']:.4f}"
                if stats["err_by_code"]:
                    line += f"  errors={stats['err_by_code']}"
                out.append(line)
        return "\n".join(out) if out else "(no metrics)"

    # ── periodic flush ──

    def start_flush_loop(self) -> None:
        if not self._spool_path:
            return
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, name="kernel-metrics-flush",
                                                daemon=True)
        self._flush_thread.start()

    def stop_flush_loop(self) -> None:
        self._stop.set()
        t = self._flush_thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._flush_thread = None

    def flush_now(self) -> None:
        if not self._spool_path:
            return
        try:
            os.makedirs(os.path.dirname(self._spool_path), exist_ok=True)
            with open(self._spool_path, "a", encoding="utf-8") as f:
                payload = {
                    "ts": time.time(),
                    "workers": self.snapshot(),
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("metrics flush failed")

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._flush_interval_s):
                break
            self.flush_now()


# ── Reading the spool back ──────────────────────────────────────────────


def read_spool(path: str, *, since_seconds: float | None = None) -> Iterator[dict]:
    if not os.path.isfile(path):
        return
    cutoff = (time.time() - since_seconds) if since_seconds else None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and rec.get("ts", 0) < cutoff:
                continue
            yield rec
