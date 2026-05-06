"""Worker Protocol + supporting dataclasses + KernelHandle.

This is the **stable contract** plugin authors target. Implementation details
of the kernel may change; these types may not without a major-version bump.
See ADR-0009 §3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Protocol, runtime_checkable

API_VERSION = "1.0.0"


# ── Dataclasses (the wire types) ─────────────────────────────────────────

@dataclass(frozen=True)
class WorkerMetadata:
    name: str
    kind: str                                     # "llm" | "mcp" | "subprocess" | <plugin-defined>
    capabilities: tuple[str, ...]
    api_version: str = API_VERSION
    config_schema_id: str | None = None
    owner_plugin: str | None = None
    description: str = ""
    streaming: bool = False                       # implements invoke_stream?


@dataclass(frozen=True)
class InvokeRequest:
    call_id: str
    capability: str
    payload: dict
    deadline_ms: int = 30_000
    cap_token: str = ""
    trace_context: dict = field(default_factory=dict)


@dataclass
class InvokeResponse:
    call_id: str
    ok: bool
    result: dict | None = None
    error: dict | None = None                     # {"code", "message", "retryable"}
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    audit_extras: dict = field(default_factory=dict)
    is_final: bool = True                          # streaming chunks set False


@dataclass(frozen=True)
class HealthReport:
    state: str                                    # "ready" | "degraded" | "down"
    in_flight: int = 0
    queue_depth: int = 0
    last_error: str | None = None
    version: str = "0.0.0"


# ── KernelHandle: the narrow proxy workers see ──────────────────────────

@dataclass
class KernelHandle:
    """The deliberately-restricted view of the kernel that workers receive.

    Workers don't get the full kernel — they get this. Future sandboxed
    transports replace this with a JSON-RPC stub having the same shape.
    """
    worker_name: str
    log: logging.Logger
    now: Callable[[], float]                                       # time.time-like
    forum_post: Callable[[str, dict, str], None]                   # (topic, payload, type)
    forum_subscribe: Callable[[str, Callable[[dict], None]], None] # (topic, callback)
    verify_token: Callable[[str], bool]
    request_chat: Callable[[dict], Awaitable[dict]] | None = None  # opt-in: route a chat call back via kernel
    capability_decl: Callable[[str], Any] | None = None            # CapabilityRegistry.get


# ── Worker Protocols ────────────────────────────────────────────────────

@runtime_checkable
class Worker(Protocol):
    @property
    def metadata(self) -> WorkerMetadata: ...
    def health(self) -> HealthReport: ...
    def invoke(self, req: InvokeRequest) -> InvokeResponse: ...
    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None: ...


@runtime_checkable
class StreamingWorker(Worker, Protocol):
    """Workers that produce a stream of responses (TTS, large LLM completions).

    Either a sync iterator or an async iterator is acceptable; the kernel
    router probes `__aiter__` first then falls back to `__iter__`.
    """
    def invoke_stream(self, req: InvokeRequest) -> Iterator[InvokeResponse] | AsyncIterator[InvokeResponse]: ...


# ── Factory ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FactoryDescription:
    factory_id: str
    api_version: str = API_VERSION
    config_schema_id: str | None = None
    capabilities_offered: tuple[str, ...] = ()
    transport: str = "in_process"                  # "in_process" | "subprocess"


class WorkerFactory(Protocol):
    def describe(self) -> FactoryDescription: ...
    def build(self, config: dict, kernel: KernelHandle) -> Worker: ...


# ── Standard error codes ────────────────────────────────────────────────

class ErrorCodes:
    CAPABILITY_REQUIRED = "capability_required"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_INVALID = "token_invalid"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PAYLOAD_INVALID = "payload_invalid"
    UPSTREAM_FAILURE = "upstream_failure"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL = "internal"


def make_error(code: str, message: str, *, retryable: bool = False, **extras: Any) -> dict:
    e = {"code": code, "message": message, "retryable": retryable}
    if extras:
        e.update(extras)
    return e


def now_ms() -> float:
    return time.time() * 1000.0
