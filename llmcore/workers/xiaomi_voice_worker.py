"""Xiaomi MiMo Open Platform voice workers (TTS + audio understanding STT).

Endpoint: ``$WLWL_XIAOMI_BASE_URL`` (default ``https://api.xiaomimimo.com/v1``)
which speaks an OpenAI-compatible Chat Completions protocol with a few
platform-specific extensions for audio.

TTS — ``mimo-v2.5-tts`` model
    Request shape (POST /chat/completions)::

        {
          "model": "mimo-v2.5-tts",
          "messages": [{"role": "assistant", "content": "<text-to-speak>"}]
        }

    Response carries the synthesized audio inline at
    ``choices[0].message.audio.data`` as base64-encoded **WAV** (24 kHz mono
    PCM 16-bit). We pass the WAV bytes straight through to the orchestrator
    as a single audio chunk — the browser's ``AudioContext.decodeAudioData``
    handles the WAV container natively.

    The platform requires the text to be carried by an ``assistant`` role
    message (an unusual convention; a ``user`` role returns 400 "messages
    must contain an assistant role for TTS model").

STT — ``mimo-v2-omni`` model (multimodal Chat Completions with audio input)
    Audio is sent as an OpenAI-style ``input_audio`` content part. We inline
    a system prompt instructing the model to transcribe verbatim, then read
    the transcript from ``choices[0].message.content``.

Both workers fail closed (return InvokeResponse with ok=False) on missing
API key, HTTP error, or 4xx/5xx so the orchestrator can degrade gracefully
to its silence/handle-error path rather than crash the WS session.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from typing import Iterator

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, StreamingWorker,
    Worker, WorkerFactory, WorkerMetadata, make_error,
)


_DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
_DEFAULT_TTS_MODEL = "mimo-v2.5-tts"
_DEFAULT_TTS_VOICE = "mimo_default"
_DEFAULT_STT_MODEL = "mimo-v2-omni"
_DEFAULT_STT_PROMPT = (
    "请把以下音频内容**逐字**转写为中文文本。只输出转写结果本身，不要加解释、"
    "标点提示或任何其他文字。如果音频是静音或无法识别，输出空字符串。"
)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def _http_post_json(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    """POST JSON, return (status, parsed_json_or_error). No raise."""
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"error": {"message": str(e), "code": str(e.code)}}
        return e.code, payload
    except Exception as e:
        return 0, {"error": {"message": f"{type(e).__name__}: {e}", "code": "network"}}


# ── TTS worker ─────────────────────────────────────────────────────────────


class XiaomiTTS:
    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        voice: str,
        timeout_s: float,
    ) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="xiaomi_tts", capabilities=("voice.tts.v1",),
            api_version=API_VERSION, streaming=True,
            owner_plugin="wlwl-ass-builtin",
            description="Xiaomi MiMo TTS via /v1/chat/completions (mimo-v2.5-tts).",
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._timeout = timeout_s
        self._lock = threading.Lock()

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        if not self._api_key:
            return HealthReport(state="unhealthy", details={"reason": "no api key"})
        return HealthReport(state="ready")

    def _synthesize(self, text: str) -> tuple[bytes | None, dict | None]:
        """Returns (wav_bytes, error_dict). One of the two is None."""
        if not self._api_key:
            return None, make_error(ErrorCodes.TOKEN_INVALID, "WLWL_XIAOMI_API_KEY not set")
        text = (text or "").strip()
        if not text:
            return b"", None  # nothing to synthesize, succeed silently
        body = {
            "model": self._model,
            "messages": [{"role": "assistant", "content": text}],
            # Platform-specific knob; safe to include even when ignored.
            "audio": {"voice": self._voice, "format": "wav"},
        }
        with self._lock:
            status, payload = _http_post_json(
                f"{self._base_url}/chat/completions",
                {"Authorization": f"Bearer {self._api_key}"},
                body, timeout=self._timeout,
            )
        if status != 200:
            err = (payload or {}).get("error") or {}
            return None, make_error(
                ErrorCodes.UPSTREAM_FAILURE,
                f"TTS HTTP {status}: {err.get('message', payload)}",
                retryable=True,  # let the kernel try the next candidate (e.g. local fallback)
            )
        try:
            audio_b64 = payload["choices"][0]["message"]["audio"]["data"]
            return base64.b64decode(audio_b64), None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return None, make_error(
                ErrorCodes.UPSTREAM_FAILURE,
                f"TTS response missing message.audio.data: {e}",
                retryable=True,
            )

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        text = req.payload.get("text", "")
        wav, err = self._synthesize(text)
        if err is not None:
            return InvokeResponse(call_id=req.call_id, ok=False, error=err)
        return InvokeResponse(call_id=req.call_id, ok=True, is_final=True, result={
            "audio_chunk": base64.b64encode(wav).decode("ascii"),
            "seq": 0, "is_final": True, "text": text, "format": "wav",
        })

    def invoke_stream(self, req: InvokeRequest) -> Iterator[InvokeResponse]:
        # MiMo's HTTP TTS is non-streaming — synthesize once, emit one chunk.
        # If/when the platform exposes a WS streaming endpoint, plug it here.
        yield self.invoke(req)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        pass


class XiaomiTTSFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.xiaomi_tts", api_version=API_VERSION,
            capabilities_offered=("voice.tts.v1",), transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        return XiaomiTTS(
            name=config.get("name") or "xiaomi_tts",
            api_key=config.get("api_key") or _env("WLWL_XIAOMI_API_KEY", "") or "",
            base_url=config.get("base_url") or _env("WLWL_XIAOMI_BASE_URL", _DEFAULT_BASE_URL),
            model=config.get("model") or _env("WLWL_XIAOMI_TTS_MODEL", _DEFAULT_TTS_MODEL),
            voice=config.get("voice") or _env("WLWL_XIAOMI_TTS_VOICE", _DEFAULT_TTS_VOICE),
            timeout_s=float(config.get("timeout_s") or 30.0),
        )


# ── STT worker (multimodal audio understanding via omni model) ─────────────


class XiaomiSTT:
    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        prompt: str,
        timeout_s: float,
    ) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="xiaomi_stt", capabilities=("voice.stt.v1",),
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Xiaomi MiMo audio understanding (multimodal chat) for STT.",
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._prompt = prompt
        self._timeout = timeout_s
        self._lock = threading.Lock()

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        if not self._api_key:
            return HealthReport(state="unhealthy", details={"reason": "no api key"})
        return HealthReport(state="ready")

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        if not self._api_key:
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.TOKEN_INVALID, "WLWL_XIAOMI_API_KEY not set"))
        audio_b64 = req.payload.get("audio", "")
        fmt = req.payload.get("format", "opus")
        if not audio_b64:
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "text": "", "confidence": 0.0, "is_final": True,
            })
        body = {
            "model": self._model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_b64, "format": fmt}},
                    {"type": "text", "text": self._prompt},
                ],
            }],
            "temperature": 0.0,
        }
        with self._lock:
            status, payload = _http_post_json(
                f"{self._base_url}/chat/completions",
                {"Authorization": f"Bearer {self._api_key}"},
                body, timeout=self._timeout,
            )
        if status != 200:
            err = (payload or {}).get("error") or {}
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE,
                f"STT HTTP {status}: {err.get('message', payload)}",
                retryable=True,  # let the kernel try the next candidate (local Whisper fallback)
            ))
        try:
            text = (payload["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.UPSTREAM_FAILURE,
                f"STT response missing message.content: {e}",
                retryable=True,
            ))
        return InvokeResponse(call_id=req.call_id, ok=True, result={
            "text": text, "confidence": 0.9, "is_final": True,
        })

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        pass


class XiaomiSTTFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.xiaomi_stt", api_version=API_VERSION,
            capabilities_offered=("voice.stt.v1",), transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        return XiaomiSTT(
            name=config.get("name") or "xiaomi_stt",
            api_key=config.get("api_key") or _env("WLWL_XIAOMI_API_KEY", "") or "",
            base_url=config.get("base_url") or _env("WLWL_XIAOMI_BASE_URL", _DEFAULT_BASE_URL),
            model=config.get("model") or _env("WLWL_XIAOMI_STT_MODEL", _DEFAULT_STT_MODEL),
            prompt=config.get("prompt") or _DEFAULT_STT_PROMPT,
            timeout_s=float(config.get("timeout_s") or 30.0),
        )
