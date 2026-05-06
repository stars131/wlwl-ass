"""Mock STT + TTS workers.

These let the voice loop run end-to-end without any real provider keys. Real
MiniMax workers are a follow-up package; they'll declare the same capability
slugs and the orchestrator picks them up unchanged.

mock_stt: returns the text given via config['canned_responses'] — useful for
tests. Without canned responses, returns the SHA-1 prefix of the audio bytes
so each new utterance is deterministically distinguishable.

mock_tts: streams a single chunk per request. The audio "chunk" is a plain
ASCII byte stream (not real Opus) — the server side documents this in
``ready.tts_format`` as ``codec="mock"`` so test clients know.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time
from typing import Iterator

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, StreamingWorker,
    Worker, WorkerFactory, WorkerMetadata, make_error,
)


class MockSTT:
    def __init__(self, *, name: str, canned: list[str] | None = None) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="mock_stt", capabilities=("voice.stt.v1",),
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Mock STT: cycles through canned responses or hashes audio.",
        )
        self._canned = list(canned or [])
        self._idx = 0
        self._lock = threading.Lock()

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready")

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        audio_b64 = req.payload.get("audio", "")
        with self._lock:
            if self._canned:
                text = self._canned[self._idx % len(self._canned)]
                self._idx += 1
            else:
                # deterministic distinguishing prefix per audio
                try:
                    raw = base64.b64decode(audio_b64) if audio_b64 else b""
                except Exception:
                    raw = audio_b64.encode("utf-8") if isinstance(audio_b64, str) else b""
                text = f"<mock-stt:{hashlib.sha1(raw).hexdigest()[:8]}>"
        return InvokeResponse(call_id=req.call_id, ok=True, result={
            "text": text, "confidence": 0.95, "is_final": True,
        })

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        pass


class MockSTTFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.mock_stt", api_version=API_VERSION,
            capabilities_offered=("voice.stt.v1",), transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        return MockSTT(name=config.get("name") or "mock_stt",
                        canned=config.get("canned_responses"))


class MockTTS:
    def __init__(self, *, name: str) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="mock_tts", capabilities=("voice.tts.v1",),
            api_version=API_VERSION, streaming=True,
            owner_plugin="wlwl-ass-builtin",
            description="Mock TTS: streams a single ASCII chunk per request.",
        )

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready")

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        # non-streaming entry point: aggregate one chunk
        text = req.payload.get("text", "")
        chunk = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return InvokeResponse(call_id=req.call_id, ok=True, is_final=True, result={
            "audio_chunk": chunk, "seq": 0, "is_final": True, "text": text,
        })

    def invoke_stream(self, req: InvokeRequest) -> Iterator[InvokeResponse]:
        text = req.payload.get("text", "")
        # Split into ~20-char chunks to simulate streaming.
        chunks = [text[i:i + 20] for i in range(0, len(text), 20)] or [""]
        for i, ch in enumerate(chunks):
            yield InvokeResponse(
                call_id=req.call_id, ok=True,
                is_final=(i == len(chunks) - 1),
                result={
                    "audio_chunk": base64.b64encode(ch.encode("utf-8")).decode("ascii"),
                    "seq": i, "is_final": (i == len(chunks) - 1),
                    "text": ch,
                },
            )
            time.sleep(0.005)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        pass


class MockTTSFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.mock_tts", api_version=API_VERSION,
            capabilities_offered=("voice.tts.v1",), transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        return MockTTS(name=config.get("name") or "mock_tts")
