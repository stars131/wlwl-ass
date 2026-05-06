"""Voice WebSocket endpoint — exposes /api/voice/session.

Uses the ``websockets`` library (one of the few non-stdlib deps; declared in
the ``voice`` extra in pyproject.toml).

Design notes:
  - One ConversationOrchestrator instance per connection.
  - Binary frames = audio inbound. Server-emitted binary = TTS chunks
    prefixed with a 4-byte little-endian seq number (PRD §6.6).
  - Background tick task drives VAD + idle timeout.
  - Bearer-token auth opt-in via ``settings.voice.auth_token``; localhost-only
    connections may skip it.

Run as a standalone server for testing:
    python -m launcher.voice_ws --host 127.0.0.1 --port 9700

In production it's mounted by ``launcher/api_server.py`` (separate ADR-0008
HTTP server) — but for MVP we run it standalone since the existing api_server
uses ``http.server`` which doesn't speak WebSocket.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import struct
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import websockets  # noqa: F401 — kept for ConnectionClosed exception type below
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:
    raise ImportError(
        "voice_ws requires the `websockets` package — install with `pip install websockets` "
        "or `pip install -e \".[voice]\"`."
    ) from exc

from launcher.correlation import bind as correlation_bind
from launcher.logging_config import setup_logging
from llmcore.kernel import Kernel, get_kernel
from voice.intent import LLMIntentClassifier, RuleIntentClassifier
from voice.orchestrator import (
    ConversationOrchestrator, OrchestratorConfig, OrchestratorHooks,
)
from voice.storage import VoiceSession, gc, list_sessions, forget_session

log = logging.getLogger("wlwl_ass.voice.ws")


# ── Per-connection handler ──────────────────────────────────────────────


class _Connection:
    def __init__(self, ws: ServerConnection, kernel: Kernel,
                 *, auth_token: str | None = None) -> None:
        self.ws = ws
        self.kernel = kernel
        self.auth_token = auth_token
        self.session = VoiceSession()
        self.orch: ConversationOrchestrator | None = None
        self._tick_task: asyncio.Task | None = None
        self._closed = False

    async def run(self) -> None:
        peer = self._peer_addr()
        if not self._auth_ok():
            self.kernel._publish_audit("voice_ws_auth_failed", {
                "session_id": self.session.session_id, "peer": peer,
            })
            await self._send_error("auth_required", "missing or invalid bearer token", fatal=True)
            await self.ws.close(code=4401, reason="auth_required")
            return

        self.kernel._publish_audit("voice_ws_opened", {
            "session_id": self.session.session_id, "peer": peer,
        })

        # Build orchestrator wired to kernel-routed STT/TTS/intent.
        hooks = self._build_hooks()
        cfg = OrchestratorConfig()
        with correlation_bind(session_id=self.session.session_id):
            self.orch = ConversationOrchestrator(
                config=cfg, hooks=hooks, session=self.session,
                out=self._send_text, out_audio=self._send_binary,
                audit=self.kernel._publish_audit,
            )
            await self.orch.start()
            self._tick_task = asyncio.create_task(self._tick_loop())

            close_reason = "client_close"
            try:
                async for frame in self.ws:
                    if isinstance(frame, (bytes, bytearray)):
                        await self.orch.feed_audio(bytes(frame))
                    else:
                        try:
                            msg = json.loads(frame)
                        except json.JSONDecodeError:
                            await self._send_error("bad_frame", "control frame must be JSON")
                            continue
                        await self.orch.feed_control(msg)
            except ConnectionClosed:
                close_reason = "connection_closed"
            except (KeyboardInterrupt, SystemExit):
                close_reason = "process_exit"
                raise
            except Exception as exc:  # noqa: BLE001 — last-resort guard at WS boundary
                log.exception("connection loop crashed")
                close_reason = "internal_error"
                await self._send_error("internal_error", "server bug")
            finally:
                self.kernel._publish_audit("voice_ws_closed", {
                    "session_id": self.session.session_id, "peer": peer,
                    "reason": close_reason,
                })
                await self._cleanup()

    def _peer_addr(self) -> str:
        try:
            host, port = self.ws.remote_address[:2]
            return f"{host}:{port}"
        except (AttributeError, TypeError, ValueError):
            return "unknown"

    # ── auth ───────

    def _auth_ok(self) -> bool:
        if not self.auth_token:
            return True  # bound to localhost in MVP per PRD §8.2
        # websockets passes headers in self.ws.request_headers (12.x: request.headers via newer API).
        path = ""
        try:
            headers = getattr(self.ws, "request_headers", None) or self.ws.request.headers
            path = getattr(self.ws.request, "path", "")
        except AttributeError:
            headers = {}
        auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth.split(None, 1)[1].strip() == self.auth_token
        token = (parse_qs(urlparse(path).query).get("token") or [""])[0]
        return token == self.auth_token

    # ── output paths ───

    async def _send_text(self, payload: dict) -> None:
        if self._closed:
            return
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
        except websockets.ConnectionClosed:
            self._closed = True

    async def _send_binary(self, chunk: bytes, seq: int, turn_id: str) -> None:
        if self._closed:
            return
        try:
            framed = struct.pack("<I", seq) + chunk
            await self.ws.send(framed)
        except websockets.ConnectionClosed:
            self._closed = True

    async def _send_error(self, code: str, message: str, *, fatal: bool = False) -> None:
        await self._send_text({"type": "error", "code": code, "message": message, "fatal": fatal})

    # ── orchestrator hooks ──

    def _build_hooks(self) -> OrchestratorHooks:
        rule_classifier = RuleIntentClassifier()

        async def transcribe(blob: bytes) -> tuple[str, float, bool]:
            r = self.kernel.dispatch(
                capability="voice.stt.v1",
                payload={
                    "audio": base64.b64encode(blob).decode("ascii"),
                    "format": "opus", "sample_rate": 16000,
                },
                deadline_ms=10_000,
            )
            if not r.ok:
                log.warning("STT dispatch failed: %s", r.error)
                return ("", 0.0, True)
            return (r.result.get("text", ""),
                    float(r.result.get("confidence", 0.0)),
                    bool(r.result.get("is_final", True)))

        async def classify(text: str):
            return rule_classifier.classify(text)

        async def handle(intent) -> str:
            if intent.intent == "_chat":
                # try kernel chat completion if a worker offers it
                r = self.kernel.dispatch(
                    capability="chat.completion.v1",
                    payload={"prompt": intent.fallback_reply or "你刚说的我听到了。",
                             "system": "你是一个简短的中文语音助手"},
                    deadline_ms=15_000,
                )
                if r.ok:
                    return (r.result.get("text") or "").strip() or "好的。"
                return intent.fallback_reply or "好的。"
            r = self.kernel.dispatch(
                capability=intent.intent, payload=intent.args, deadline_ms=10_000,
            )
            if not r.ok:
                return f"抱歉，没办成 ({(r.error or {}).get('message')})"
            return _natural_reply(intent.intent, r.result)

        async def synthesize(text: str):
            async for chunk_resp in self.kernel.dispatch_stream(
                capability="voice.tts.v1",
                payload={"text": text, "format": "opus"},
                deadline_ms=30_000,
            ):
                if chunk_resp.ok and chunk_resp.result:
                    chunk_b64 = chunk_resp.result.get("audio_chunk")
                    if chunk_b64:
                        try:
                            yield base64.b64decode(chunk_b64)
                        except Exception:
                            continue

        return OrchestratorHooks(
            transcribe=transcribe, classify_intent=classify,
            handle_intent=handle, synthesize=synthesize,
        )

    # ── cleanup ───

    async def _tick_loop(self) -> None:
        try:
            while not self._closed and self.orch and not self.orch.closed:
                await self.orch.tick()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("tick loop crashed")

    async def _cleanup(self) -> None:
        self._closed = True
        if self._tick_task:
            self._tick_task.cancel()
        if self.orch and not self.orch.closed:
            await self.orch.shutdown(reason="connection_closed")


def _natural_reply(intent: str, result: dict) -> str:
    if intent == "calendar.create_event.v1":
        ev = result.get("event") or {}
        return f"好的，已添加日程：{ev.get('title', '')}，时间 {ev.get('start_at', '')[:16]}。"
    if intent == "calendar.update_event.v1":
        after = result.get("after") or {}
        return f"已修改：{after.get('title', '')} 改为 {after.get('start_at', '')[:16]}。"
    if intent == "calendar.delete_event.v1":
        return "已删除。"
    if intent == "calendar.query_events.v1":
        events = result.get("events") or []
        if not events:
            return "这段时间没有安排。"
        return f"共 {len(events)} 项：" + "；".join(
            f"{e.get('start_at', '')[:16]} {e.get('title', '')}" for e in events[:5]
        ) + ("。" if len(events) <= 5 else "等。")
    if intent == "inspiration.record.v1":
        return "已记下来。"
    if intent == "inspiration.query.v1":
        notes = result.get("notes") or []
        if not notes:
            return "没有找到相关笔记。"
        return f"找到 {len(notes)} 条：" + "；".join(n.get("text", "")[:30] for n in notes[:3]) + "。"
    return "好的。"


# ── Server ──────────────────────────────────────────────────────────────


async def voice_session_handler(ws: ServerConnection, *, kernel: Kernel,
                                 auth_token: str | None) -> None:
    actual_path = "/"
    try:
        actual_path = ws.request.path  # websockets v15+
    except AttributeError:
        pass
    if not actual_path.startswith("/api/voice/session"):
        await ws.close(code=4404, reason="not_found")
        return
    conn = _Connection(ws, kernel, auth_token=auth_token)
    await conn.run()


async def serve_forever(*, host: str, port: int, auth_token: str | None,
                        kernel: Kernel | None = None) -> None:
    kernel = kernel or get_kernel()
    # one-shot retention sweep on boot
    try:
        gc()
    except Exception:
        log.exception("voice gc failed")

    async def handler(ws: ServerConnection) -> None:
        await voice_session_handler(ws, kernel=kernel, auth_token=auth_token)

    async with serve(handler, host, port, max_size=2**22):  # 4 MiB max frame
        log.info("voice WS listening on ws://%s:%d/api/voice/session", host, port)
        await asyncio.Future()  # run forever


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="wlwl-ass voice WebSocket server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9700)
    p.add_argument("--auth-token", default=os.environ.get("WLWL_VOICE_AUTH_TOKEN") or None)
    args = p.parse_args(argv)
    setup_logging()
    # Ensure default workers exist for MVP / standalone runs.
    k = get_kernel()
    if not any(w.kind == "calendar" for w in k.list_workers()):
        k.add_worker({"name": "calendar", "kind": "calendar"})
    if not any(w.kind == "inspiration" for w in k.list_workers()):
        k.add_worker({"name": "inspiration", "kind": "inspiration"})
    if not any(w.kind in ("mock_stt", "minimax_stt") for w in k.list_workers()):
        k.add_worker({"name": "stt", "kind": "mock_stt"})
    if not any(w.kind in ("mock_tts", "minimax_tts") for w in k.list_workers()):
        k.add_worker({"name": "tts", "kind": "mock_tts"})

    try:
        asyncio.run(serve_forever(host=args.host, port=args.port, auth_token=args.auth_token, kernel=k))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
