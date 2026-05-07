"""Tests for the local Whisper STT fallback worker.

Three layers:

1. Unit tests with mocked ``faster_whisper`` — verify request shape, output
   parsing, and error envelopes without needing the package installed.
2. Empty-audio short-circuit (no model load).
3. Optional live test gated on ``faster_whisper`` actually being importable.
"""
from __future__ import annotations

import base64
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── unit tests with mocked faster_whisper ──────────────────────────────


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeInfo:
    language = "zh"
    language_probability = 0.95


class _FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel."""
    last_init: dict | None = None
    last_call: dict | None = None

    def __init__(self, model_name, device="cpu", compute_type="int8"):
        type(self).last_init = {
            "model": model_name, "device": device, "compute_type": compute_type,
        }

    def transcribe(self, audio, **kwargs):
        type(self).last_call = {
            "audio_type": type(audio).__name__,
            "kwargs": dict(kwargs),
        }
        return iter([_FakeSegment("你好"), _FakeSegment("世界")]), _FakeInfo()


@pytest.fixture(autouse=True)
def _reset_model_cache():
    from llmcore.workers import local_whisper_worker as lw
    lw._MODEL_CACHE.clear()
    _FakeWhisperModel.last_init = None
    _FakeWhisperModel.last_call = None
    yield
    lw._MODEL_CACHE.clear()


def _patch_fw(monkeypatch):
    """Inject a fake faster_whisper module."""
    fw_module = MagicMock()
    fw_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw_module)


def test_local_whisper_request_shape_and_parse(monkeypatch):
    from llmcore.worker import InvokeRequest
    from llmcore.workers import local_whisper_worker as lw
    _patch_fw(monkeypatch)

    worker = lw.LocalWhisperSTT(
        name="lw", model_name="base", language="zh",
        compute_type="int8", device="cpu", beam_size=5,
    )
    audio = base64.b64encode(b"fake-opus-bytes").decode("ascii")
    resp = worker.invoke(InvokeRequest(
        call_id="c1", capability="voice.stt.v1",
        payload={"audio": audio, "format": "opus"},
    ))

    assert resp.ok is True
    assert resp.result["text"] == "你好世界"
    assert resp.result["is_final"] is True
    # confidence is mapped from language_probability (0.0-1.0)
    assert 0.0 <= resp.result["confidence"] <= 1.0
    # Verify model was instantiated with our config
    assert _FakeWhisperModel.last_init == {
        "model": "base", "device": "cpu", "compute_type": "int8",
    }
    # Verify transcribe kwargs (language='zh', beam_size=5, vad_filter=True)
    call = _FakeWhisperModel.last_call
    assert call is not None
    assert call["kwargs"]["language"] == "zh"
    assert call["kwargs"]["beam_size"] == 5
    assert call["kwargs"]["vad_filter"] is True


def test_local_whisper_caches_model_across_calls(monkeypatch):
    from llmcore.worker import InvokeRequest
    from llmcore.workers import local_whisper_worker as lw
    _patch_fw(monkeypatch)

    audio = base64.b64encode(b"fake").decode("ascii")
    init_count = {"n": 0}
    orig = _FakeWhisperModel.__init__
    def _counting_init(self, *a, **kw):
        init_count["n"] += 1
        orig(self, *a, **kw)
    _FakeWhisperModel.__init__ = _counting_init
    try:
        for _ in range(3):
            w = lw.LocalWhisperSTT(
                name="lw", model_name="base", language="zh",
                compute_type="int8", device="cpu", beam_size=5,
            )
            r = w.invoke(InvokeRequest(call_id="x", capability="voice.stt.v1",
                                        payload={"audio": audio, "format": "opus"}))
            assert r.ok
        # Model must be loaded only once across 3 invocations (singleton cache)
        assert init_count["n"] == 1
    finally:
        _FakeWhisperModel.__init__ = orig


def test_local_whisper_empty_audio_short_circuits(monkeypatch):
    from llmcore.worker import InvokeRequest
    from llmcore.workers import local_whisper_worker as lw
    _patch_fw(monkeypatch)

    worker = lw.LocalWhisperSTT(
        name="lw", model_name="base", language="zh",
        compute_type="int8", device="cpu", beam_size=5,
    )
    resp = worker.invoke(InvokeRequest(
        call_id="c", capability="voice.stt.v1", payload={"audio": ""},
    ))
    assert resp.ok is True
    assert resp.result["text"] == ""
    # Empty audio must NOT trigger model load (would cost ~700ms)
    assert _FakeWhisperModel.last_init is None


def test_local_whisper_missing_package_returns_clear_error(monkeypatch):
    from llmcore.worker import InvokeRequest, ErrorCodes
    from llmcore.workers import local_whisper_worker as lw

    # Make sure the import probe fails
    monkeypatch.setattr(lw, "_faster_whisper_available", lambda: False)
    worker = lw.LocalWhisperSTT(
        name="lw", model_name="base", language="zh",
        compute_type="int8", device="cpu", beam_size=5,
    )
    audio = base64.b64encode(b"x").decode("ascii")
    resp = worker.invoke(InvokeRequest(
        call_id="c", capability="voice.stt.v1",
        payload={"audio": audio, "format": "opus"}))
    assert resp.ok is False
    assert resp.error["code"] == ErrorCodes.NOT_IMPLEMENTED
    assert "stt-local" in resp.error["message"]


def test_local_whisper_transcribe_exception_wrapped(monkeypatch):
    from llmcore.worker import InvokeRequest, ErrorCodes
    from llmcore.workers import local_whisper_worker as lw

    class _BoomingModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, *a, **k):
            raise RuntimeError("CUDA OOM")

    fw_module = MagicMock()
    fw_module.WhisperModel = _BoomingModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw_module)
    monkeypatch.setattr(lw, "_faster_whisper_available", lambda: True)

    worker = lw.LocalWhisperSTT(
        name="lw", model_name="base", language="zh",
        compute_type="int8", device="cpu", beam_size=5,
    )
    audio = base64.b64encode(b"x").decode("ascii")
    resp = worker.invoke(InvokeRequest(
        call_id="c", capability="voice.stt.v1",
        payload={"audio": audio, "format": "opus"}))
    assert resp.ok is False
    assert resp.error["code"] == ErrorCodes.UPSTREAM_FAILURE
    assert "CUDA OOM" in resp.error["message"]


# ── kernel-level fallback chain ─────────────────────────────────────────


def test_kernel_falls_through_on_primary_stt_error(monkeypatch):
    """When a higher-priority STT worker returns ok=False, the kernel iterates
    candidates and the local fallback worker handles the request — this is
    the contract our voice_ws relies on (no orchestrator-level retry needed).
    """
    from llmcore.worker import (
        InvokeRequest, InvokeResponse, FactoryDescription, HealthReport,
        WorkerMetadata, API_VERSION, ErrorCodes, make_error,
    )
    from llmcore.kernel import Kernel

    class FailingSTT:
        @property
        def metadata(self):
            return WorkerMetadata(
                name="failing_primary", kind="failing_primary",
                capabilities=("voice.stt.v1",), api_version=API_VERSION,
                owner_plugin="test",
            )
        def health(self): return HealthReport(state="ready")
        def invoke(self, req):
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE, "primary down", retryable=True))
        def shutdown(self, **kw): pass

    class FailingFactory:
        def describe(self):
            return FactoryDescription(
                factory_id="test.failing_primary", api_version=API_VERSION,
                capabilities_offered=("voice.stt.v1",), transport="in_process",
            )
        def build(self, config, kernel): return FailingSTT()

    class GoodSTT:
        @property
        def metadata(self):
            return WorkerMetadata(
                name="good_fallback", kind="good_fallback",
                capabilities=("voice.stt.v1",), api_version=API_VERSION,
                owner_plugin="test",
            )
        def health(self): return HealthReport(state="ready")
        def invoke(self, req):
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "text": "from-fallback", "confidence": 0.8, "is_final": True,
            })
        def shutdown(self, **kw): pass

    class GoodFactory:
        def describe(self):
            return FactoryDescription(
                factory_id="test.good_fallback", api_version=API_VERSION,
                capabilities_offered=("voice.stt.v1",), transport="in_process",
            )
        def build(self, config, kernel): return GoodSTT()

    k = Kernel()
    k.register_factory(FailingFactory())
    k.register_factory(GoodFactory())
    # Also need to map kind→factory_id so add_worker resolves it.
    from llmcore.kernel import _KIND_TO_FACTORY
    _KIND_TO_FACTORY["failing_primary"] = "test.failing_primary"
    _KIND_TO_FACTORY["good_fallback"] = "test.good_fallback"
    try:
        # Order matters: failing first → good second.
        k.add_worker({"name": "primary", "kind": "failing_primary"})
        k.add_worker({"name": "fallback", "kind": "good_fallback"})
        r = k.dispatch(capability="voice.stt.v1",
                       payload={"audio": "", "format": "opus"},
                       deadline_ms=5_000)
        assert r.ok is True
        assert r.result["text"] == "from-fallback"
    finally:
        _KIND_TO_FACTORY.pop("failing_primary", None)
        _KIND_TO_FACTORY.pop("good_fallback", None)


# ── live test, opt-in ───────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("WLWL_LIVE_WHISPER_TEST"),
    reason="opt-in (set WLWL_LIVE_WHISPER_TEST=1; also downloads model on first run)",
)
def test_local_whisper_live_silence_returns_empty():
    """End-to-end: feed a real (silent) WAV through the real model and
    expect either empty text or trivially short text — proves the package
    is importable and the audio decoding path works.

    Gated behind WLWL_LIVE_WHISPER_TEST because:
      - first run downloads ~74 MB ('base') from HF mirror
      - tiny model used here is still ~39 MB on first download
      - run takes 5-30s depending on network + CPU
    """
    pytest.importorskip("faster_whisper")
    import wave
    from llmcore.worker import InvokeRequest
    from llmcore.workers.local_whisper_worker import LocalWhisperSTTFactory
    from llmcore.kernel import Kernel

    # 100ms 16kHz mono silence WAV
    import io as _io
    with _io.BytesIO() as out:
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)
        wav_bytes = out.getvalue()

    factory = LocalWhisperSTTFactory()
    worker = factory.build({"name": "live", "model": "tiny"}, Kernel())
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    resp = worker.invoke(InvokeRequest(
        call_id="live", capability="voice.stt.v1",
        payload={"audio": audio_b64, "format": "wav"}))
    assert resp.ok is True, resp.error
    # Silent audio: text should be empty or trivially short.
    assert len(resp.result["text"]) <= 20
