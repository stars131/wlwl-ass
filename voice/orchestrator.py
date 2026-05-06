"""ConversationOrchestrator — the 5-state machine that drives one voice
WebSocket session.

States: IDLE → ARMED → LISTENING → PROCESSING → RESPONDING

The orchestrator is **transport-agnostic**: it doesn't know what a WebSocket
is. It receives ``feed_audio(bytes)`` and ``feed_control(json_dict)``; it
emits events through an ``out`` async callback. ``launcher/voice_ws.py``
glues it to the actual WebSocket.

This makes the orchestrator unit-testable — the test feeds canned audio +
fake STT/TTS workers and asserts the emitted event sequence.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from voice.intent import IntentResult, RuleIntentClassifier, CHAT_INTENT
from voice.storage import TurnEvent, VoiceSession
from voice.wake import PhraseMatcher

log = logging.getLogger("wlwl_ass.voice.orch")


# ── States ──────────────────────────────────────────────────────────────


class State:
    IDLE = "IDLE"
    ARMED = "ARMED"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    RESPONDING = "RESPONDING"


# ── Config ──────────────────────────────────────────────────────────────


@dataclass
class OrchestratorConfig:
    wake_phrases: list[str] = field(default_factory=lambda: ["我对三体世界说话"])
    exit_phrases: list[str] = field(default_factory=lambda: ["拜拜", "退出", "再见", "谢谢就这样"])
    wake_min_confidence: float = 0.6
    vad_silence_ms: int = 800                 # how long of silence ends LISTENING
    session_idle_timeout_s: int = 60
    armed_greeting: str = "请讲"
    farewell: str = "嗯，再叫我"
    chunk_size_ms: int = 20                   # nominal audio frame length


# ── External hooks the orchestrator depends on ─────────────────────────


@dataclass
class OrchestratorHooks:
    """The orchestrator calls these to do real work; they are typically
    bound to ``kernel.dispatch`` calls.
    """
    transcribe: Callable[[bytes], Awaitable[tuple[str, float, bool]]]
    """audio bytes (Opus) -> (text, confidence, is_final)."""

    classify_intent: Callable[[str], Awaitable[IntentResult]]

    handle_intent: Callable[[IntentResult], Awaitable[str]]
    """Run the intent + return a natural-language reply string."""

    synthesize: Callable[[str], "AsyncIterator[bytes]"]
    """text -> async iterator of audio chunks (Opus)."""


# ── Orchestrator ────────────────────────────────────────────────────────


@dataclass
class _Buffered:
    bytes: bytearray = field(default_factory=bytearray)
    last_voice_ms: float = 0.0
    in_voice: bool = False


class ConversationOrchestrator:
    def __init__(
        self, *,
        config: OrchestratorConfig,
        hooks: OrchestratorHooks,
        out: Callable[[dict], Awaitable[None]],
        out_audio: Callable[[bytes, int, str], Awaitable[None]],   # (chunk, seq, turn_id)
        session: VoiceSession,
        audit: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.config = config
        self.hooks = hooks
        self.out = out
        self.out_audio = out_audio
        self.session = session
        self._audit = audit or (lambda _t, _p: None)
        self.state = State.IDLE
        self._wake = PhraseMatcher(config.wake_phrases, min_confidence=config.wake_min_confidence)
        self._exit = PhraseMatcher(config.exit_phrases, min_confidence=0.5)
        self._lock = asyncio.Lock()
        self._buf = _Buffered()
        self._last_user_ms = self._now_ms()
        self._idle_timer: asyncio.Task | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        await self._set_state(State.IDLE)
        self._reset_idle_timer()

    async def shutdown(self, reason: str = "client_close") -> None:
        if self._closed:
            return
        self._closed = True
        if self._idle_timer:
            self._idle_timer.cancel()
        self.session.set_exit_reason(reason)
        self.session.close()
        await self.out({"type": "state", "value": State.IDLE})

    # ── inputs ────────────────────────────────────────────────────────

    async def feed_audio(self, blob: bytes) -> None:
        if self._closed:
            return
        # In states where we're talking to the user, the server discards
        # audio — but we still persist it for the recording. The browser is
        # supposed to mute too (PRD FR-2.4) but trust no client.
        self.session.append_audio(blob)
        if self.state in (State.IDLE, State.LISTENING):
            self._buf.bytes.extend(blob)
            self._buf.last_voice_ms = self._now_ms()
            self._buf.in_voice = True
            await self._maybe_finalize_utterance()

    async def feed_control(self, frame: dict) -> None:
        t = frame.get("type")
        if t == "hello":
            await self.out({
                "type": "ready",
                "session_id": self.session.session_id,
                "tts_format": {"codec": "mock", "sample_rate": 24000, "channels": 1},
            })
            return
        if t == "pause":
            self._buf = _Buffered()
            await self.out({"type": "state", "value": "PAUSED"})
            return
        if t == "resume":
            await self.out({"type": "state", "value": self.state})
            return
        if t == "forget_session":
            self.session.mark_forget()
            await self.out({"type": "state", "value": self.state, "note": "session_will_be_forgotten"})
            return
        if t == "goodbye":
            await self.shutdown(reason="client_goodbye")
            return

    async def tick(self) -> None:
        """Caller polls this regularly (e.g. every 100 ms) to drive timing-
        based transitions: VAD silence-end and idle timeout.
        """
        if self._closed:
            return
        if self.state == State.LISTENING and self._buf.in_voice:
            elapsed = self._now_ms() - self._buf.last_voice_ms
            if elapsed >= self.config.vad_silence_ms:
                await self._finalize_listening()
        if self.state in (State.ARMED, State.LISTENING):
            elapsed_s = (self._now_ms() - self._last_user_ms) / 1000.0
            if elapsed_s >= self.config.session_idle_timeout_s:
                await self._handle_idle_timeout()

    # ── transitions ────────────────────────────────────────────────────

    async def _maybe_finalize_utterance(self) -> None:
        # In MVP we don't do partial-STT push; we wait until the chunk has
        # accumulated ~2s of audio in IDLE and run STT to check for a wake
        # phrase, OR until VAD silence in LISTENING.
        if self.state == State.IDLE and len(self._buf.bytes) >= 32_000:  # ~2s at 16kHz mono
            await self._try_wake()

    async def _try_wake(self) -> None:
        blob = bytes(self._buf.bytes)
        self._buf = _Buffered()
        text, conf, _ = await self._safe_transcribe(blob)
        if not text:
            return
        await self.out({"type": "transcript.partial", "text": text, "conf": conf})
        match = self._wake.feed(text, conf)
        if match is None:
            return
        await self.out({"type": "transcript.final", "text": text, "conf": conf,
                        "role": "user", "turn_id": _new_turn_id()})
        self.session.append_event(TurnEvent(role="system", kind="event",
                                            extra={"event": "wake_matched", "phrase": match.phrase}))
        await self._goto_armed_then_listen()

    async def _goto_armed_then_listen(self) -> None:
        await self._set_state(State.ARMED)
        await self._speak(self.config.armed_greeting, role_in_transcript="agent")
        await self._set_state(State.LISTENING)
        self._buf = _Buffered()
        self._last_user_ms = self._now_ms()
        self._reset_idle_timer()

    async def _finalize_listening(self) -> None:
        blob = bytes(self._buf.bytes)
        self._buf = _Buffered()
        if len(blob) < 1_600:  # < 100ms — likely a hiccup, not an utterance
            return
        await self._set_state(State.PROCESSING)
        text, conf, _ = await self._safe_transcribe(blob)
        if not text:
            await self._set_state(State.LISTENING)
            return
        turn_id = _new_turn_id()
        await self.out({"type": "transcript.final", "text": text, "conf": conf,
                        "role": "user", "turn_id": turn_id})
        self.session.append_event(TurnEvent(role="user", kind="stt", text=text,
                                            extra={"conf": conf, "turn_id": turn_id}))
        # exit phrase check first
        if self._exit.feed(text, conf):
            self.session.append_event(TurnEvent(role="system", kind="event",
                                                extra={"event": "exit_matched"}))
            await self._speak(self.config.farewell, role_in_transcript="agent")
            await self._set_state(State.IDLE)
            self.session.set_exit_reason("exit_phrase")
            return
        # intent + dispatch
        intent = await self._safe_classify(text)
        await self.out({"type": "intent", "intent": intent.intent,
                        "confidence": intent.confidence, "args": intent.args})
        self.session.append_event(TurnEvent(role="system", kind="event",
                                            extra={"event": "intent", "intent": intent.intent,
                                                   "args": intent.args}))
        reply = await self._safe_handle(intent)
        await self._speak(reply, role_in_transcript="agent")
        await self._set_state(State.LISTENING)
        self._last_user_ms = self._now_ms()
        self._reset_idle_timer()

    async def _handle_idle_timeout(self) -> None:
        await self._speak(self.config.farewell, role_in_transcript="agent")
        await self._set_state(State.IDLE)
        self.session.set_exit_reason("idle_timeout")
        self._buf = _Buffered()

    async def _speak(self, text: str, *, role_in_transcript: str) -> None:
        if not text:
            return
        await self._set_state(State.RESPONDING)
        turn_id = _new_turn_id()
        await self.out({"type": "tts.start", "turn_id": turn_id})
        seq = 0
        async for chunk in self.hooks.synthesize(text):
            await self.out_audio(chunk, seq, turn_id)
            seq += 1
        await self.out({"type": "tts.end", "turn_id": turn_id})
        await self.out({"type": "transcript.final", "text": text, "conf": 1.0,
                        "role": role_in_transcript, "turn_id": turn_id})
        self.session.append_event(TurnEvent(role=role_in_transcript, kind="tts",
                                            text=text, extra={"turn_id": turn_id}))

    async def _set_state(self, new_state: str) -> None:
        if self.state == new_state:
            return
        self.state = new_state
        await self.out({"type": "state", "value": new_state})

    def _reset_idle_timer(self) -> None:
        self._last_user_ms = self._now_ms()

    # ── safe wrappers around hooks ─────────────────────────────────────

    async def _safe_transcribe(self, blob: bytes) -> tuple[str, float, bool]:
        try:
            return await self.hooks.transcribe(blob)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 — STT third-party path
            log.exception("STT failed")
            self._audit("voice_stt_error", {
                "session_id": self.session.session_id,
                "error": repr(exc), "audio_len": len(blob),
            })
            return ("", 0.0, True)

    async def _safe_classify(self, text: str) -> IntentResult:
        try:
            return await self.hooks.classify_intent(text)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("intent classify failed")
            self._audit("voice_intent_error", {
                "session_id": self.session.session_id,
                "error": repr(exc), "text_len": len(text),
            })
            return IntentResult(CHAT_INTENT, 0.0, {})

    async def _safe_handle(self, intent: IntentResult) -> str:
        try:
            return await self.hooks.handle_intent(intent)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("intent handler failed")
            self._audit("voice_handle_error", {
                "session_id": self.session.session_id,
                "intent": intent.intent, "error": repr(exc),
            })
            return f"抱歉，处理出错了。"

    @staticmethod
    def _now_ms() -> float:
        return time.time() * 1000.0


def _new_turn_id() -> str:
    return uuid.uuid4().hex[:12]
