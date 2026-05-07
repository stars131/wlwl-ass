"""Local fallback STT using faster-whisper (CTranslate2-backed Whisper).

This worker implements the ``voice.stt.v1`` capability so the orchestrator
can dispatch to it identically to the cloud STT (Xiaomi MiMo). The default
fallback chain in ``launcher/voice_ws.py`` tries ``xiaomi_stt`` first; if
that returns an error envelope (account balance exhausted, network down,
timeout, etc.), it dispatches the same payload to this local worker so the
voice loop keeps working.

Why faster-whisper rather than openai/whisper, whisper.cpp, or vosk:

- pip-installable wheel (CTranslate2 + PyAV bundled — no system ffmpeg).
- 4× faster than openai/whisper for the same accuracy on CPU.
- INT8 quantization on CPU keeps a 4-core laptop near-real-time on the
  ``base`` (74 MB) model.
- Multi-language including Mandarin out of the box.

Configuration via env (or per-worker config dict):

  WLWL_LOCAL_WHISPER_MODEL  default "base"  (tiny / base / small / medium / large-v3)
  WLWL_LOCAL_WHISPER_LANG   default "zh"    (auto-detect: leave empty)
  WLWL_LOCAL_WHISPER_COMPUTE_TYPE  default "int8"  (int8 / int8_float32 / float16 / float32)
  WLWL_LOCAL_WHISPER_DEVICE default "cpu"   (cpu / cuda)
  WLWL_LOCAL_WHISPER_BEAM_SIZE   default 5  (5 = quality, 1 = speed)

Model files are downloaded on first use from HuggingFace to
``~/.cache/huggingface/``. Air-gapped users can pre-snapshot the model.

Attribution: see ATTRIBUTION.md entry #7 for license/source/scope.
"""
from __future__ import annotations

import base64
import io
import os
import threading
import wave
from typing import Any

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker, WorkerFactory,
    WorkerMetadata, make_error,
)


_DEFAULT_MODEL = "base"
_DEFAULT_LANG = "zh"
_DEFAULT_COMPUTE_TYPE = "int8"
_DEFAULT_DEVICE = "cpu"
_DEFAULT_BEAM_SIZE = 5


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


# Process-level singleton so the model loads once across multiple workers.
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()


def _load_model(model_name: str, device: str, compute_type: str):
    """Lazy-load and cache a faster-whisper WhisperModel."""
    key = (model_name, device, compute_type)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        # Import inside the function so the module imports cleanly even if
        # faster-whisper isn't installed — the error surfaces at first use.
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
        return model


def _faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


class LocalWhisperSTT:
    def __init__(
        self,
        *,
        name: str,
        model_name: str,
        language: str,
        compute_type: str,
        device: str,
        beam_size: int,
    ) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="local_whisper_stt", capabilities=("voice.stt.v1",),
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Local STT via faster-whisper (Whisper + CTranslate2).",
        )
        self._model_name = model_name
        self._language = language or None  # None = auto-detect
        self._compute_type = compute_type
        self._device = device
        self._beam_size = beam_size
        self._lock = threading.Lock()

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        if not _faster_whisper_available():
            return HealthReport(
                state="unhealthy",
                details={"reason": "faster-whisper not installed; "
                                   "pip install -e .[stt-local]"},
            )
        return HealthReport(state="ready")

    def _decode_to_pcm(self, audio_bytes: bytes, fmt: str) -> bytes:
        """Best-effort: hand the raw bytes (opus/webm/wav/...) to PyAV.

        faster-whisper's transcribe() accepts a file path OR a file-like.
        For a file-like it reads with PyAV which handles opus/webm/wav/mp3.
        We just wrap the bytes in BytesIO; no manual decoding needed.

        For raw PCM (no container) we'd need different handling, but the
        browser always sends a container (Opus-in-WebM via MediaRecorder),
        and Xiaomi STT returned text from the same payload, so we know it
        carries a container header.
        """
        return audio_bytes

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        if not _faster_whisper_available():
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.NOT_IMPLEMENTED,
                "faster-whisper not installed (pip install -e \".[stt-local]\")",
            ))
        audio_b64 = req.payload.get("audio", "") or ""
        fmt = req.payload.get("format", "opus")
        if not audio_b64:
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "text": "", "confidence": 0.0, "is_final": True,
            })
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as exc:
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.PAYLOAD_INVALID, f"audio not valid base64: {exc}"))
        if not audio_bytes:
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "text": "", "confidence": 0.0, "is_final": True,
            })
        # faster-whisper accepts a file-like; wrap the container bytes.
        try:
            with self._lock:
                model = _load_model(self._model_name, self._device, self._compute_type)
            stream = io.BytesIO(self._decode_to_pcm(audio_bytes, fmt))
            segments, info = model.transcribe(
                stream,
                beam_size=self._beam_size,
                language=self._language,
                vad_filter=True,
            )
            # segments is a generator; materialize to drive the actual decode
            text = "".join(seg.text for seg in segments).strip()
            avg_logprob = float(getattr(info, "language_probability", 0.0) or 0.0)
            confidence = max(0.0, min(1.0, avg_logprob))
        except Exception as exc:
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE,
                f"faster-whisper transcribe failed: {type(exc).__name__}: {exc}",
            ))
        return InvokeResponse(call_id=req.call_id, ok=True, result={
            "text": text, "confidence": confidence, "is_final": True,
        })

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        # Model is shared across workers via _MODEL_CACHE; let the GC clean
        # up at process exit. Don't drop the cache here in case other
        # workers are still using it.
        pass


class LocalWhisperSTTFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.local_whisper_stt", api_version=API_VERSION,
            capabilities_offered=("voice.stt.v1",), transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        return LocalWhisperSTT(
            name=config.get("name") or "local_whisper_stt",
            model_name=config.get("model") or _env("WLWL_LOCAL_WHISPER_MODEL", _DEFAULT_MODEL),
            language=config.get("language") if "language" in config
                      else _env("WLWL_LOCAL_WHISPER_LANG", _DEFAULT_LANG),
            compute_type=config.get("compute_type") or
                          _env("WLWL_LOCAL_WHISPER_COMPUTE_TYPE", _DEFAULT_COMPUTE_TYPE),
            device=config.get("device") or _env("WLWL_LOCAL_WHISPER_DEVICE", _DEFAULT_DEVICE),
            beam_size=int(config.get("beam_size") or
                          _env("WLWL_LOCAL_WHISPER_BEAM_SIZE", str(_DEFAULT_BEAM_SIZE))),
        )
