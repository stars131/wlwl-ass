"""End-to-end integration test: spin up the voice WebSocket server, connect
a websockets client, walk through the full conversation, assert state
transitions + intent dispatch + persistence.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import struct
import tempfile

import pytest
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.mark.asyncio
async def test_voice_ws_e2e_full_session(tmp_path):
    from llmcore.kernel import get_kernel, reset_kernel
    from launcher.voice_ws import serve_forever

    reset_kernel()
    k = get_kernel()
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(tmp_path / "cal.db")})
    k.add_worker({"name": "insp", "kind": "inspiration", "db_path": str(tmp_path / "insp.db")})
    k.add_worker({"name": "stt", "kind": "mock_stt", "canned_responses": [
        "我对三体世界说话",
        "明天下午三点提醒我去拿快递",
        "记一下：研究 Tauri 2 的更新机制",
        "拜拜",
    ]})
    k.add_worker({"name": "tts", "kind": "mock_tts"})

    server_task = asyncio.create_task(
        serve_forever(host="127.0.0.1", port=9799, auth_token=None, kernel=k)
    )
    await asyncio.sleep(0.4)  # let server bind

    try:
        async with websockets.connect("ws://127.0.0.1:9799/api/voice/session") as ws:
            await ws.send(json.dumps({
                "type": "hello", "client": "test@1.0",
                "audio_format": {"codec": "opus", "sample_rate": 16000, "channels": 1},
            }))

            states = []
            transcripts = []
            intents = []
            tts_chunks = 0
            session_id = None
            saw_error = False

            async def reader():
                nonlocal tts_chunks, session_id, saw_error
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        # binary frame = TTS chunk; first 4 bytes = seq
                        if len(msg) >= 4:
                            tts_chunks += 1
                        continue
                    frame = json.loads(msg)
                    t = frame.get("type")
                    if t == "ready":
                        session_id = frame["session_id"]
                    elif t == "state":
                        states.append(frame["value"])
                    elif t == "transcript.final":
                        transcripts.append((frame["role"], frame["text"]))
                    elif t == "intent":
                        intents.append(frame["intent"])
                    elif t == "error":
                        saw_error = True

            reader_task = asyncio.create_task(reader())
            try:
                # Wake (need ~32000 audio bytes to trip server-side STT)
                await ws.send(b"\x00" * 33000)
                await asyncio.sleep(0.4)
                # Three more utterances; the server's VAD timeout is 800ms by
                # default, so wait that long after each blob.
                for _ in range(3):
                    await ws.send(b"\x01" * 5000)
                    await asyncio.sleep(1.1)
                await ws.send(json.dumps({"type": "goodbye"}))
                await asyncio.sleep(0.3)
            finally:
                reader_task.cancel()
                with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                    await reader_task

            # Assertions
            assert not saw_error, f"server reported an error frame"
            assert session_id is not None, "no ready frame received"
            assert "ARMED" in states
            assert "LISTENING" in states
            assert "PROCESSING" in states or "RESPONDING" in states  # at least one busy state
            assert "calendar.create_event.v1" in intents
            assert "inspiration.record.v1" in intents
            assert tts_chunks > 0, "no TTS audio chunks received"

            # Persistence: data made it to the DB
            cal = k.dispatch(capability="calendar.query_events.v1", payload={})
            assert cal.ok and len(cal.result["events"]) >= 1, "calendar event missing"
            notes = k.dispatch(capability="inspiration.list_recent.v1", payload={})
            assert notes.ok and len(notes.result["notes"]) >= 1, "inspiration note missing"

            print(f"\nE2E pass: states={states} intents={intents} tts_chunks={tts_chunks}")
    finally:
        server_task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
            await server_task
        reset_kernel()
