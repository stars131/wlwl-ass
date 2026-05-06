"""Kernel — owns workers, signs capability tokens, routes invocations,
moderates the forum bus.

Single-kernel-per-process invariant. Lazy singleton via ``get_kernel()``.

Public API (ADR-0009 §9.1):
  - add_worker / remove_worker / reload_worker
  - silence / unsilence
  - list_workers / inspect_worker
  - dispatch (called by tools) / dispatch_stream (streaming variant)

Concurrency: a single ``_mutate_lock`` serializes structural mutations
(add/remove/reload/silence). Routing reads take a copy-on-write snapshot.

Production hardening (audit-emitted):
  - Deadline enforcement: dispatch refuses if request's deadline has passed.
  - Circuit breaker: per-worker; trips after consecutive failures, recovers
    via half-open trial.
  - Health probe: background thread polls worker.health() every 30s; 3 down
    reports auto-silence with reason="health_probe_failed".
  - Token rotation: tokens close to expiry are reissued before next dispatch.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterator

from launcher.correlation import bind as correlation_bind
from llmcore import captoken
from llmcore.capabilities import CapabilityRegistry, default_registry
from llmcore.errors import (
    ApiError, CircuitBreaker, CircuitConfig, CircuitState,
    Deadline, DeadlineExceeded, ErrorCode, make_error, safe_hook,
)
from llmcore.forum import ForumBus, ForumMessage
from llmcore.metrics import MetricsRegistry
from llmcore.worker import (
    API_VERSION, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, StreamingWorker,
    Worker, WorkerFactory, WorkerMetadata, now_ms,
)

log = logging.getLogger("wlwl_ass.kernel")

DEFAULT_TOKEN_TTL = 3600
GRACE_AFTER_REVOKE_SEC = 30
TOKEN_ROTATE_BEFORE_S = 60          # rotate when < 60s remaining
HEALTH_PROBE_INTERVAL_S = 30.0
HEALTH_DOWN_TO_SILENCE = 3


@dataclass
class _Registered:
    worker: Worker
    metadata: WorkerMetadata
    factory_id: str
    config: dict
    token_subject_nonce: str
    token_wire: str
    token_expires_at: int
    silenced: bool = False
    silence_reason: str = ""
    silence_until: float | None = None
    in_flight: int = 0
    last_health_at: float = 0.0
    last_health: HealthReport | None = None


@dataclass(frozen=True)
class WorkerView:
    name: str
    kind: str
    capabilities: tuple[str, ...]
    silenced: bool
    silence_reason: str
    in_flight: int
    health_state: str
    factory_id: str
    circuit_state: str = "closed"


@dataclass(frozen=True)
class WorkerInspection:
    view: WorkerView
    metadata: WorkerMetadata
    config_redacted: dict
    token_expires_at: int


class RegistrationError(ValueError):
    pass


class Kernel:
    def __init__(
        self, *,
        signing_key: bytes | None = None,
        registry: CapabilityRegistry | None = None,
        forum: ForumBus | None = None,
        kernel_name: str = "primary-kernel",
        circuit_config: CircuitConfig | None = None,
        enable_health_probe: bool = True,
        health_probe_interval_s: float = HEALTH_PROBE_INTERVAL_S,
    ) -> None:
        self.name = kernel_name
        self.signing_key = signing_key or captoken.random_signing_key()
        self.registry = registry or default_registry()
        self.forum = forum or ForumBus(
            audit_spool_path=os.path.join(_temp_dir(), "forum_audit.jsonl")
        )
        self._workers: dict[str, _Registered] = {}
        self._factories: dict[str, WorkerFactory] = {}
        self._snapshot: tuple[_Registered, ...] = ()
        self._mutate_lock = threading.RLock()
        self._supported_api = ("1.x.x",)
        self._breaker = CircuitBreaker(circuit_config or CircuitConfig())
        self._breaker.add_listener(self._on_circuit_transition)
        self._metrics = MetricsRegistry(
            spool_path=os.path.join(_temp_dir(), "metrics.jsonl"),
            flush_interval_s=300.0,
        )
        if enable_health_probe:
            self._metrics.start_flush_loop()
        self._health = HealthMonitor(
            self, interval_s=health_probe_interval_s,
            consecutive_down_to_silence=HEALTH_DOWN_TO_SILENCE,
        )
        if enable_health_probe:
            self._health.start()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def metrics(self) -> MetricsRegistry:
        return self._metrics

    def shutdown(self) -> None:
        """Stop background tasks. Safe to call multiple times. Used in tests."""
        self._health.stop()
        self._metrics.stop_flush_loop()

    # ── factory registration ───────────────────────────────────────────

    def register_factory(self, factory: WorkerFactory) -> None:
        with self._mutate_lock:
            desc = factory.describe()
            if not _api_compatible(desc.api_version, self._supported_api):
                raise RegistrationError(f"factory {desc.factory_id!r} api_version "
                                        f"{desc.api_version!r} unsupported")
            existed = desc.factory_id in self._factories
            self._factories[desc.factory_id] = factory
            self._publish_audit("factory_registered" if not existed else "factory_replaced", {
                "factory_id": desc.factory_id, "api_version": desc.api_version,
                "transport": desc.transport,
                "capabilities_offered": list(desc.capabilities_offered),
            })

    def list_factories(self) -> list[FactoryDescription]:
        with self._mutate_lock:
            return [f.describe() for f in self._factories.values()]

    # ── worker mutation ────────────────────────────────────────────────

    def add_worker(self, config: dict, *, source: str = "api") -> WorkerMetadata:
        factory_id = config.get("factory_id") or _factory_id_for_kind(config.get("kind"))
        if factory_id is None:
            raise RegistrationError("config must specify factory_id or kind")
        with self._mutate_lock:
            factory = self._factories.get(factory_id)
            if factory is None:
                raise RegistrationError(f"unknown factory {factory_id!r}")
            handle = self._make_handle(config["name"])
            try:
                worker = factory.build(dict(config), handle)
            except (ApiError, RegistrationError):
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — third-party factory; shield the kernel
                log.exception("factory %r build failed", factory_id)
                self._publish_audit("worker_register_rejected", {
                    "factory_id": factory_id, "name": config.get("name"),
                    "error": repr(exc), "stage": "factory_build",
                })
                raise RegistrationError(f"factory build failed: {exc!r}") from exc
            metadata = worker.metadata
            if metadata.name in self._workers:
                raise RegistrationError(f"worker {metadata.name!r} already registered")
            for cap in metadata.capabilities:
                if not self.registry.has(cap) and not cap.endswith(".*"):
                    raise RegistrationError(f"capability {cap!r} not declared in registry")
            tok, wire = captoken.mint(
                issuer=self.name, subject=metadata.name,
                capabilities=metadata.capabilities,
                signing_key=self.signing_key, ttl_sec=DEFAULT_TOKEN_TTL,
            )
            entry = _Registered(
                worker=worker, metadata=metadata, factory_id=factory_id,
                config=dict(config), token_subject_nonce=tok.nonce,
                token_wire=wire, token_expires_at=tok.expires_at,
            )
            self._workers[metadata.name] = entry
            self._refresh_snapshot()
            self._metrics.for_worker(metadata.name)
            self._publish_audit("worker_registered", {
                "name": metadata.name, "kind": metadata.kind,
                "capabilities": list(metadata.capabilities),
                "factory_id": factory_id, "source": source,
            })
            self._notify_worker_token(metadata.name, wire)
            return metadata

    def remove_worker(self, name: str, *, drain_timeout_ms: int = 30_000, reason: str = "") -> None:
        with self._mutate_lock:
            entry = self._workers.get(name)
            if entry is None:
                return  # idempotent
            entry.silenced = True
            self._refresh_snapshot()

        # drain outside the lock
        deadline = time.time() + (drain_timeout_ms / 1000.0)
        while time.time() < deadline:
            if self._workers[name].in_flight == 0:
                break
            time.sleep(0.05)

        with self._mutate_lock:
            entry = self._workers.pop(name, None)
            if entry is None:
                return
            self._refresh_snapshot()
            try:
                entry.worker.shutdown(drain_timeout_ms=drain_timeout_ms)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                log.exception("worker shutdown raised; ignoring")
            self._breaker.reset(name)
            self._metrics.drop_worker(name)
            self._publish_audit("worker_removed", {
                "name": name, "drained": entry.in_flight == 0, "reason": reason,
            })

    def reload_worker(self, name: str, *, new_config: dict | None = None) -> WorkerMetadata:
        with self._mutate_lock:
            entry = self._workers.get(name)
            if entry is None:
                raise KeyError(name)
            old_config = dict(entry.config)
            cfg = new_config if new_config is not None else entry.config
            self.remove_worker(name, drain_timeout_ms=10_000, reason="reload")
            try:
                meta = self.add_worker(cfg, source="reload")
            except RegistrationError:
                # rollback: try to re-register with the previous config
                self._publish_audit("worker_reload_failed_rollback", {
                    "name": name, "config": _redact(cfg),
                })
                self.add_worker(old_config, source="reload_rollback")
                raise
            self._publish_audit("worker_reloaded", {
                "name": name, "had_new_config": new_config is not None,
            })
            return meta

    def silence(self, name: str, reason: str = "", *, ttl_sec: int | None = None) -> None:
        with self._mutate_lock:
            entry = self._workers.get(name)
            if entry is None:
                raise KeyError(name)
            entry.silenced = True
            entry.silence_reason = reason
            entry.silence_until = (time.time() + ttl_sec) if ttl_sec else None
            self._publish_audit("worker_silenced", {"name": name, "reason": reason, "ttl_sec": ttl_sec})

    def unsilence(self, name: str) -> None:
        with self._mutate_lock:
            entry = self._workers.get(name)
            if entry is None:
                raise KeyError(name)
            entry.silenced = False
            entry.silence_reason = ""
            entry.silence_until = None
            self._breaker.reset(name)
            self._publish_audit("worker_unsilenced", {"name": name})

    def list_workers(self, *, include_silenced: bool = True) -> list[WorkerView]:
        with self._mutate_lock:
            views = []
            for entry in self._workers.values():
                if not include_silenced and entry.silenced:
                    continue
                health = entry.last_health.state if entry.last_health else "unknown"
                views.append(WorkerView(
                    name=entry.metadata.name, kind=entry.metadata.kind,
                    capabilities=entry.metadata.capabilities,
                    silenced=entry.silenced, silence_reason=entry.silence_reason,
                    in_flight=entry.in_flight, health_state=health,
                    factory_id=entry.factory_id,
                    circuit_state=self._breaker.state(entry.metadata.name),
                ))
            return views

    def inspect_worker(self, name: str) -> WorkerInspection:
        with self._mutate_lock:
            entry = self._workers.get(name)
            if entry is None:
                raise KeyError(name)
            health = entry.last_health.state if entry.last_health else "unknown"
            view = WorkerView(
                name=entry.metadata.name, kind=entry.metadata.kind,
                capabilities=entry.metadata.capabilities,
                silenced=entry.silenced, silence_reason=entry.silence_reason,
                in_flight=entry.in_flight, health_state=health,
                factory_id=entry.factory_id,
                circuit_state=self._breaker.state(entry.metadata.name),
            )
            return WorkerInspection(
                view=view, metadata=entry.metadata,
                config_redacted=_redact(entry.config),
                token_expires_at=entry.token_expires_at,
            )

    # ── dispatch ───────────────────────────────────────────────────────

    def dispatch(self, *, capability: str, payload: dict,
                 deadline_ms: int = 30_000, trace: dict | None = None) -> InvokeResponse:
        """Synchronous dispatch — routes to one worker and returns its response.

        Streaming workers: prefer ``dispatch_stream``. If you call ``dispatch``
        on a streaming-only worker, you receive the final aggregated chunk.

        Hardening:
          * Validates payload against capability schema (PAYLOAD_INVALID).
          * Enforces ``deadline_ms`` — returns DEADLINE_EXCEEDED if no
            candidate finishes before the absolute deadline.
          * Skips workers whose breaker is OPEN (CIRCUIT_OPEN if all candidates
            are open).
          * Records every outcome to per-worker breaker + audit topic.
          * Token rotation if remaining TTL < 60s.
        """
        if deadline_ms <= 0:
            return InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.PAYLOAD_INVALID, f"deadline_ms must be > 0, got {deadline_ms}"))
        deadline = Deadline.from_ms(deadline_ms)
        ok, msg = self.registry.validate_request(capability, payload)
        if not ok:
            return InvokeResponse(call_id="", ok=False,
                                  error=make_error(ErrorCode.PAYLOAD_INVALID, msg,
                                                   capability=capability))
        all_candidates = self._candidates(capability)
        if not all_candidates:
            self._publish_audit("dispatch_no_candidate", {"capability": capability})
            return InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.CAPABILITY_UNAVAILABLE, f"no worker offers {capability!r}",
                capability=capability))
        # filter by circuit
        live = [e for e in all_candidates if self._breaker.allow(e.metadata.name)]
        if not live:
            self._publish_audit("dispatch_all_circuits_open", {
                "capability": capability,
                "candidates": [e.metadata.name for e in all_candidates],
            })
            return InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.CIRCUIT_OPEN, "all candidate workers have open circuits",
                capability=capability))
        last_err: dict | None = None
        for entry in live:
            if deadline.expired():
                self._publish_audit("dispatch_deadline_pre", {
                    "worker": entry.metadata.name, "capability": capability,
                })
                return InvokeResponse(call_id="", ok=False, error=make_error(
                    ErrorCode.DEADLINE_EXCEEDED,
                    f"deadline {deadline_ms}ms exhausted before dispatch",
                    capability=capability))
            self._maybe_rotate_token(entry)
            remaining = max(1, int(deadline.remaining_ms()))
            req = self._build_request(entry, capability, payload, remaining, trace)
            with correlation_bind(call_id=req.call_id, worker_name=entry.metadata.name):
                resp = self._invoke_one(entry, req, deadline)
                self._publish_dispatch_audit(entry.metadata.name, capability, req, resp)
            if resp.ok:
                return resp
            last_err = resp.error
            if not (resp.error or {}).get("retryable"):
                break
            if deadline.expired():
                break
        return InvokeResponse(call_id="", ok=False, error=last_err or make_error(
            ErrorCode.UPSTREAM_FAILURE, "all candidates failed", capability=capability))

    def _invoke_one(self, entry: "_Registered", req: InvokeRequest,
                    deadline: Deadline) -> InvokeResponse:
        entry.in_flight += 1
        t0 = now_ms()
        resp: InvokeResponse
        try:
            resp = entry.worker.invoke(req)
            if not isinstance(resp, InvokeResponse):
                raise TypeError(f"worker {entry.metadata.name!r} returned non-InvokeResponse")
        except ApiError as exc:
            env = dict(exc.envelope)
            env.setdefault("worker", entry.metadata.name)
            resp = InvokeResponse(call_id=req.call_id, ok=False, error=env)
        except (KeyboardInterrupt, SystemExit):
            entry.in_flight = max(0, entry.in_flight - 1)
            raise
        except Exception as exc:  # noqa: BLE001 — third-party worker; shield kernel
            log.exception("worker %r invoke crashed", entry.metadata.name)
            resp = InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCode.UPSTREAM_FAILURE, str(exc), retryable=True,
                worker=entry.metadata.name))
        finally:
            entry.in_flight = max(0, entry.in_flight - 1)
        resp.latency_ms = now_ms() - t0
        # post-hoc deadline check: if upstream took too long but didn't fail,
        # we still surface success — the caller decides whether to use it.
        # Only convert to DEADLINE_EXCEEDED when we got an error AND deadline expired.
        if not resp.ok and deadline.expired():
            resp.error = make_error(
                ErrorCode.DEADLINE_EXCEEDED,
                f"deadline exceeded after worker error ({(resp.error or {}).get('code')})",
                worker=entry.metadata.name,
                upstream_error=resp.error,
            )
        # circuit breaker accounting
        outcome_retryable = (resp.error or {}).get("retryable", False) if not resp.ok else False
        if resp.ok:
            self._breaker.record_success(entry.metadata.name)
        elif outcome_retryable:
            self._breaker.record_failure(entry.metadata.name)
        # non-retryable errors are typically the caller's fault (PAYLOAD_INVALID
        # etc.) — don't trip the breaker on those.
        self._metrics.record_dispatch(
            worker=entry.metadata.name, capability=req.capability, ok=resp.ok,
            latency_ms=resp.latency_ms, cost_usd=resp.cost_usd,
            tokens_in=int(resp.audit_extras.get("tokens_in", 0) or 0),
            tokens_out=int(resp.audit_extras.get("tokens_out", 0) or 0),
            error_code=(resp.error or {}).get("code") if not resp.ok else None,
        )
        return resp

    async def dispatch_stream(
        self, *, capability: str, payload: dict, deadline_ms: int = 30_000,
        trace: dict | None = None,
    ) -> AsyncIterator[InvokeResponse]:
        """Streaming variant. Workers either implement ``invoke_stream`` (sync
        iter / async iter) or fall back to a single-shot ``invoke`` wrapped in
        a stream of one chunk.
        """
        if deadline_ms <= 0:
            yield InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.PAYLOAD_INVALID, f"deadline_ms must be > 0, got {deadline_ms}"))
            return
        deadline = Deadline.from_ms(deadline_ms)
        ok, msg = self.registry.validate_request(capability, payload)
        if not ok:
            yield InvokeResponse(call_id="", ok=False,
                                 error=make_error(ErrorCode.PAYLOAD_INVALID, msg,
                                                  capability=capability))
            return
        all_candidates = self._candidates(capability, prefer_streaming=True)
        if not all_candidates:
            self._publish_audit("dispatch_no_candidate", {"capability": capability,
                                                            "stream": True})
            yield InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.CAPABILITY_UNAVAILABLE, f"no worker offers {capability!r}",
                capability=capability))
            return
        live = [e for e in all_candidates if self._breaker.allow(e.metadata.name)]
        if not live:
            self._publish_audit("dispatch_all_circuits_open", {
                "capability": capability,
                "candidates": [e.metadata.name for e in all_candidates],
                "stream": True,
            })
            yield InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.CIRCUIT_OPEN, "all candidate workers have open circuits",
                capability=capability))
            return
        entry = live[0]
        if deadline.expired():
            yield InvokeResponse(call_id="", ok=False, error=make_error(
                ErrorCode.DEADLINE_EXCEEDED, "deadline exhausted before stream start"))
            return
        self._maybe_rotate_token(entry)
        remaining = max(1, int(deadline.remaining_ms()))
        req = self._build_request(entry, capability, payload, remaining, trace)
        entry.in_flight += 1
        any_chunk_ok = False
        try:
            with correlation_bind(call_id=req.call_id, worker_name=entry.metadata.name):
                async for chunk in self._invoke_stream_normalized(entry.worker, req):
                    if chunk.ok:
                        any_chunk_ok = True
                    yield chunk
        finally:
            entry.in_flight = max(0, entry.in_flight - 1)
        if any_chunk_ok:
            self._breaker.record_success(entry.metadata.name)
        else:
            self._breaker.record_failure(entry.metadata.name)
        self._publish_audit("dispatch_stream_complete", {
            "worker": entry.metadata.name, "capability": capability,
            "call_id": req.call_id, "ok": any_chunk_ok,
        })

    async def _invoke_stream_normalized(self, worker: Worker, req: InvokeRequest) -> AsyncIterator[InvokeResponse]:
        if hasattr(worker, "invoke_stream"):
            try:
                stream = worker.invoke_stream(req)  # type: ignore[attr-defined]
                if hasattr(stream, "__aiter__"):
                    async for chunk in stream:        # type: ignore[union-attr]
                        yield chunk
                    return
                if hasattr(stream, "__iter__"):
                    for chunk in stream:               # type: ignore[union-attr]
                        yield chunk
                        await asyncio.sleep(0)         # cooperative yield
                    return
            except ApiError as exc:
                env = dict(exc.envelope)
                env.setdefault("worker", worker.metadata.name)
                yield InvokeResponse(call_id=req.call_id, ok=False, error=env)
                return
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — third-party worker
                log.exception("worker stream crashed")
                yield InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCode.UPSTREAM_FAILURE, str(exc), retryable=True))
                return
        # fallback: single invoke wrapped as one-chunk stream
        try:
            resp = worker.invoke(req)
        except ApiError as exc:
            env = dict(exc.envelope)
            yield InvokeResponse(call_id=req.call_id, ok=False, error=env)
            return
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 — third-party worker
            log.exception("worker invoke crashed in stream fallback")
            yield InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCode.UPSTREAM_FAILURE, str(exc), retryable=True))
            return
        resp.is_final = True
        yield resp

    # ── helpers ────────────────────────────────────────────────────────

    def _candidates(self, capability: str, *, prefer_streaming: bool = False) -> list[_Registered]:
        results = []
        for entry in self._snapshot:
            if entry.silenced:
                if entry.silence_until and time.time() >= entry.silence_until:
                    entry.silenced = False
                    entry.silence_reason = ""
                    self._publish_audit("worker_unsilenced", {
                        "name": entry.metadata.name, "reason": "ttl_expired",
                    })
                else:
                    continue
            for cap in entry.metadata.capabilities:
                if cap == capability or _glob_match(cap, capability):
                    results.append(entry)
                    break
        if prefer_streaming:
            results.sort(key=lambda e: 0 if e.metadata.streaming else 1)
        return results

    def _maybe_rotate_token(self, entry: _Registered) -> None:
        now = int(time.time())
        if entry.token_expires_at - now > TOKEN_ROTATE_BEFORE_S:
            return
        new_tok, new_wire = captoken.mint(
            issuer=self.name, subject=entry.metadata.name,
            capabilities=entry.metadata.capabilities,
            signing_key=self.signing_key, ttl_sec=DEFAULT_TOKEN_TTL,
        )
        entry.token_subject_nonce = new_tok.nonce
        entry.token_wire = new_wire
        entry.token_expires_at = new_tok.expires_at
        hook = getattr(entry.worker, "on_token_refresh", None)
        if hook is not None:
            with safe_hook(f"on_token_refresh:{entry.metadata.name}",
                           audit_cb=self._publish_audit):
                hook(new_wire)
        self._publish_audit("token_rotated", {
            "worker": entry.metadata.name, "new_expires_at": new_tok.expires_at,
        })

    def _on_circuit_transition(self, key: str, old: str, new: str, ctx: dict) -> None:
        self._publish_audit("circuit_transition", {
            "worker": key, "from": old, "to": new, **ctx,
        })
        try:
            self._metrics.set_circuit_state(key, new)
        except Exception:  # noqa: BLE001 — metrics must never break breaker
            log.exception("metrics state-change update failed")

    def _build_request(
        self, entry: _Registered, capability: str, payload: dict,
        deadline_ms: int, trace: dict | None,
    ) -> InvokeRequest:
        return InvokeRequest(
            call_id=str(uuid.uuid4()),
            capability=capability, payload=dict(payload),
            deadline_ms=deadline_ms, cap_token=entry.token_wire,
            trace_context=dict(trace or {}),
        )

    def _refresh_snapshot(self) -> None:
        self._snapshot = tuple(self._workers.values())

    def _make_handle(self, worker_name: str) -> KernelHandle:
        def _post(topic: str, payload: dict, type: str = "post") -> None:
            try:
                self.forum.publish(topic, payload, author=worker_name, type=type,
                                   cap_token_subject=worker_name)
            except (KeyError, PermissionError) as exc:
                self._publish_audit("forum_write_denied", {
                    "worker": worker_name, "topic": topic, "error": str(exc),
                })

        def _subscribe(topic: str, cb: Callable[[dict], None]) -> None:
            def adapt(msg: ForumMessage) -> None:
                with safe_hook(f"forum_subscriber:{worker_name}",
                               audit_cb=self._publish_audit):
                    cb({"topic": msg.topic, "author": msg.author, "type": msg.type,
                        "payload": msg.payload, "seq": msg.seq, "ts": msg.timestamp})
            try:
                self.forum.subscribe(topic, subscriber=worker_name, callback=adapt)
            except (KeyError, PermissionError) as exc:
                self._publish_audit("forum_subscribe_denied", {
                    "worker": worker_name, "topic": topic, "error": str(exc),
                })
                raise

        def _verify(token_wire: str) -> bool:
            try:
                tok = captoken.verify(token_wire, self.signing_key)
                return tok.subject == worker_name and not tok.expired()
            except (ValueError, KeyError):
                return False
                return False

        return KernelHandle(
            worker_name=worker_name,
            log=logging.getLogger(f"wlwl_ass.worker.{worker_name}"),
            now=time.time, forum_post=_post, forum_subscribe=_subscribe,
            verify_token=_verify, request_chat=self._request_chat,
            capability_decl=self.registry.get,
        )

    async def _request_chat(self, payload: dict) -> dict:
        """Worker → kernel back-call: route a chat completion via another worker.

        Used by inspiration_worker auto-tag, intent classifier, etc. Failure
        returns ``{"ok": false}`` so callers can soft-fail."""
        resp = self.dispatch(capability="chat.completion.v1", payload=payload, deadline_ms=15_000)
        if resp.ok:
            return {"ok": True, "result": resp.result}
        return {"ok": False, "error": resp.error}

    def _publish_audit(self, type: str, payload: dict) -> None:
        try:
            self.forum.publish("audit", payload, author="kernel", type=type)
        except (KeyError, PermissionError):
            log.exception("audit publish denied")
        except Exception:  # noqa: BLE001 — last resort: audit must never crash dispatch
            log.exception("audit publish failed")

    def _publish_dispatch_audit(self, worker_name: str, capability: str,
                                 req: InvokeRequest, resp: InvokeResponse) -> None:
        self._publish_audit("dispatch", {
            "worker": worker_name, "capability": capability,
            "call_id": req.call_id, "ok": resp.ok,
            "latency_ms": resp.latency_ms, "cost_usd": resp.cost_usd,
            "error": resp.error,
        })

    def _notify_worker_token(self, worker_name: str, token_wire: str) -> None:
        entry = self._workers.get(worker_name)
        if entry is None:
            return
        hook = getattr(entry.worker, "on_token_refresh", None)
        if hook is None:
            return
        with safe_hook(f"on_token_refresh:{worker_name}", audit_cb=self._publish_audit):
            hook(token_wire)


# ── Health monitor ──────────────────────────────────────────────────────


class HealthMonitor:
    """Periodically polls every worker's ``health()``. Workers reporting
    ``state == "down"`` ``HEALTH_DOWN_TO_SILENCE`` consecutive times are
    automatically silenced with reason ``"health_probe_failed"``.

    Runs in a daemon thread; safe to start/stop multiple times.
    """

    def __init__(self, kernel: "Kernel", *, interval_s: float,
                 consecutive_down_to_silence: int) -> None:
        self._kernel = kernel
        self._interval_s = interval_s
        self._threshold = consecutive_down_to_silence
        self._down: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kernel-health-probe",
                                         daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    def poll_once(self) -> None:
        """Public hook for tests — runs one iteration synchronously."""
        self._iterate()

    def _loop(self) -> None:
        # First poll happens after a short delay so workers added at startup
        # have a chance to settle (their `__init__` may have just returned).
        if self._stop.wait(0.5):
            return
        while not self._stop.is_set():
            try:
                self._iterate()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:  # noqa: BLE001 — never let probe loop die
                log.exception("health probe iteration crashed")
            if self._stop.wait(self._interval_s):
                return

    def _iterate(self) -> None:
        for entry in self._kernel.list_workers(include_silenced=False):
            name = entry.name
            try:
                # entry is a WorkerView; use _workers map for the actual worker
                reg = self._kernel._workers.get(name)
                if reg is None:
                    continue
                report = reg.worker.health()
                reg.last_health = report
                reg.last_health_at = time.time()
                if not isinstance(report, HealthReport):
                    raise TypeError(f"health() returned {type(report).__name__}, expected HealthReport")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — third-party worker
                log.exception("worker %r health() crashed", name)
                self._kernel._publish_audit("health_probe_error", {
                    "worker": name, "error": repr(exc),
                })
                self._down[name] = self._down.get(name, 0) + 1
            else:
                if report.state == "down":
                    self._down[name] = self._down.get(name, 0) + 1
                    self._kernel._publish_audit("health_down", {
                        "worker": name, "consecutive": self._down[name],
                        "last_error": report.last_error,
                    })
                else:
                    if name in self._down:
                        self._down.pop(name, None)
            if self._down.get(name, 0) >= self._threshold:
                self._down[name] = 0
                try:
                    self._kernel.silence(name, reason="health_probe_failed",
                                          ttl_sec=None)
                except KeyError:
                    pass


# ── module-level helpers ─────────────────────────────────────────────────


def _glob_match(declared: str, requested: str) -> bool:
    if declared == requested:
        return True
    if declared.endswith(".*") and requested.startswith(declared[:-1]):
        return True
    if requested.endswith(".*") and declared.startswith(requested[:-1]):
        return True
    return False


def _api_compatible(claimed: str, supported: tuple[str, ...]) -> bool:
    major = claimed.split(".")[0]
    return any(s.startswith(f"{major}.") for s in supported)


_KIND_TO_FACTORY = {
    "llm": "wlwl_ass.workers.llm",
    "calendar": "wlwl_ass.workers.calendar",
    "inspiration": "wlwl_ass.workers.inspiration",
    "mock_stt": "wlwl_ass.workers.mock_stt",
    "mock_tts": "wlwl_ass.workers.mock_tts",
}


def _factory_id_for_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    return _KIND_TO_FACTORY.get(kind)


def _redact(config: dict) -> dict:
    out = dict(config)
    for k in ("apikey", "api_key", "secret", "token"):
        if k in out:
            out[k] = "***"
    return out


def _temp_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "temp")


# ── Singleton ────────────────────────────────────────────────────────────


_KERNEL: Kernel | None = None
_KERNEL_LOCK = threading.Lock()


def get_kernel() -> Kernel:
    """Lazy singleton kernel. Tests use ``reset_kernel()`` between cases."""
    global _KERNEL
    with _KERNEL_LOCK:
        if _KERNEL is None:
            _KERNEL = Kernel()
            _bootstrap_in_tree_factories(_KERNEL)
        return _KERNEL


def reset_kernel() -> None:
    """Test-only — drops the singleton so the next ``get_kernel()`` rebuilds."""
    global _KERNEL
    with _KERNEL_LOCK:
        if _KERNEL is not None:
            try:
                _KERNEL.shutdown()
            except Exception:  # noqa: BLE001 — best effort
                log.exception("kernel shutdown during reset_kernel raised")
        _KERNEL = None


def _bootstrap_in_tree_factories(kernel: Kernel) -> None:
    """Register the in-tree worker factories from ``llmcore/workers/``.

    Done lazily on first get_kernel() so importing llmcore is cheap.
    """
    from llmcore.workers import iter_builtin_factories
    for factory in iter_builtin_factories():
        try:
            kernel.register_factory(factory)
        except Exception:
            log.exception("failed to register builtin factory %r", factory)
