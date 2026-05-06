"""LLMWorker — wraps an existing ``llmcore.BaseSession`` subclass as a kernel worker.

Capability offered: ``chat.completion.v1`` (and ``chat.classify_tags.v1`` /
``chat.intent_classify.v1`` if you opt them in by listing them in the config).

Streaming is supported for ``chat.completion.v1`` via the underlying session's
generator-mode ``ask`` (BaseSession line 77).
"""
from __future__ import annotations

import json
import logging
from typing import Iterator

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)


DEFAULT_CAPS = ("chat.completion.v1",)
log = logging.getLogger("wlwl_ass.workers.llm")


class LLMWorker:
    def __init__(self, *, name: str, session, capabilities: tuple[str, ...]) -> None:
        self._session = session
        self._meta = WorkerMetadata(
            name=name, kind="llm", capabilities=capabilities,
            api_version=API_VERSION, streaming=True,
            owner_plugin="wlwl-ass-builtin",
            description=f"LLM session worker ({type(session).__name__}).",
        )
        self._in_flight = 0

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready", in_flight=self._in_flight)

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        self._in_flight += 1
        try:
            text = self._do_completion(req.payload)
            return InvokeResponse(call_id=req.call_id, ok=True, result={"text": text})
        except Exception as exc:
            log.exception("LLM invoke failed")
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE, str(exc), retryable=True))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def invoke_stream(self, req: InvokeRequest) -> Iterator[InvokeResponse]:
        self._in_flight += 1
        try:
            for chunk in self._do_completion_stream(req.payload):
                yield InvokeResponse(call_id=req.call_id, ok=True,
                                     is_final=False, result={"text_chunk": chunk})
            yield InvokeResponse(call_id=req.call_id, ok=True, is_final=True, result={"text_chunk": ""})
        except Exception as exc:
            yield InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE, str(exc), retryable=True))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        # BaseSession has no explicit close; clear state to drop refs.
        self._session = None

    # ── helpers ──

    def _do_completion(self, p: dict) -> str:
        if "system" in p and p["system"]:
            self._session.system = p["system"]
        prompt = p["prompt"]
        out = self._session.ask(prompt, stream=False)
        return out if isinstance(out, str) else "".join(out)

    def _do_completion_stream(self, p: dict) -> Iterator[str]:
        if "system" in p and p["system"]:
            self._session.system = p["system"]
        prompt = p["prompt"]
        gen = self._session.ask(prompt, stream=True)
        for chunk in gen:
            yield chunk


class LLMFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.llm", api_version=API_VERSION,
            capabilities_offered=DEFAULT_CAPS, transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        # Lazy-import so this worker is opt-in (won't fail import without keys).
        session = config.get("session")  # caller can pre-build a session
        if session is None:
            session = _construct_session_from_config(config)
        caps = tuple(config.get("capabilities") or DEFAULT_CAPS)
        return LLMWorker(name=config["name"], session=session, capabilities=caps)


def _construct_session_from_config(config: dict):
    """Build a session from a launcher_api_configs.json-style entry."""
    import llmcore
    kind = config.get("kind", "native_oai")
    if kind == "native_oai":
        return llmcore.NativeOAISession(config)
    if kind == "native_claude":
        return llmcore.NativeClaudeSession(config)
    if kind == "oai" or kind == "llm":
        return llmcore.LLMSession(config)
    if kind == "claude":
        return llmcore.ClaudeSession(config)
    raise ValueError(f"unsupported session kind {kind!r}")
