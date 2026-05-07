"""Tests for the Xiaomi MiMo voice workers — both unit (mocked HTTP) and
optional live tests gated on WLWL_XIAOMI_API_KEY.

The live tests cost API quota and don't run by default (skipped without key).
The unit tests verify the request shape we send to the platform and the
response parsing, since those are the most likely things to break if the
platform's contract shifts.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── unit tests with mocked HTTP ─────────────────────────────────────────


def _fake_post(captured: list, status: int, payload: dict):
    def _impl(url, headers, body, timeout):
        captured.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        return status, payload
    return _impl


def test_xiaomi_tts_request_shape_and_parse():
    from llmcore.worker import InvokeRequest
    from llmcore.workers import xiaomi_voice_worker as xv

    fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt ..."
    captured: list = []
    with patch.object(xv, "_http_post_json",
                      _fake_post(captured, 200, {
                          "choices": [{
                              "message": {"audio": {"data": base64.b64encode(fake_wav).decode("ascii")}}
                          }]
                      })):
        worker = xv.XiaomiTTS(
            name="t", api_key="sk-test", base_url="https://example.invalid/v1",
            model="mimo-v2.5-tts", voice="mimo_default", timeout_s=5.0,
        )
        resp = worker.invoke(InvokeRequest(
            call_id="c1", capability="voice.tts.v1", payload={"text": "你好"}))

    assert resp.ok is True
    assert resp.is_final is True
    audio = base64.b64decode(resp.result["audio_chunk"])
    assert audio == fake_wav
    assert resp.result["format"] == "wav"
    # Verify request body contract — assistant role is non-negotiable per platform.
    assert len(captured) == 1
    sent = captured[0]
    assert sent["url"] == "https://example.invalid/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["body"]["model"] == "mimo-v2.5-tts"
    assert sent["body"]["messages"] == [{"role": "assistant", "content": "你好"}]


def test_xiaomi_tts_no_api_key_returns_unauthorized():
    from llmcore.worker import InvokeRequest, ErrorCodes
    from llmcore.workers import xiaomi_voice_worker as xv

    worker = xv.XiaomiTTS(
        name="t", api_key="", base_url="https://example.invalid/v1",
        model="mimo-v2.5-tts", voice="mimo_default", timeout_s=5.0,
    )
    resp = worker.invoke(InvokeRequest(
        call_id="c1", capability="voice.tts.v1", payload={"text": "x"}))
    assert resp.ok is False
    assert resp.error["code"] == ErrorCodes.TOKEN_INVALID


def test_xiaomi_tts_http_error_propagates_message():
    from llmcore.worker import InvokeRequest, ErrorCodes
    from llmcore.workers import xiaomi_voice_worker as xv

    captured: list = []
    with patch.object(xv, "_http_post_json",
                      _fake_post(captured, 402, {"error": {"message": "Insufficient account balance"}})):
        worker = xv.XiaomiTTS(
            name="t", api_key="sk-x", base_url="https://example.invalid/v1",
            model="mimo-v2.5-tts", voice="mimo_default", timeout_s=5.0,
        )
        resp = worker.invoke(InvokeRequest(
            call_id="c1", capability="voice.tts.v1", payload={"text": "x"}))

    assert resp.ok is False
    assert resp.error["code"] == ErrorCodes.UPSTREAM_FAILURE
    assert "Insufficient account balance" in resp.error["message"]


def test_xiaomi_stt_request_shape():
    from llmcore.worker import InvokeRequest
    from llmcore.workers import xiaomi_voice_worker as xv

    captured: list = []
    with patch.object(xv, "_http_post_json",
                      _fake_post(captured, 200, {
                          "choices": [{"message": {"content": "你好世界"}}]
                      })):
        worker = xv.XiaomiSTT(
            name="s", api_key="sk-x", base_url="https://example.invalid/v1",
            model="mimo-v2-omni", prompt="转写", timeout_s=5.0,
        )
        b64audio = base64.b64encode(b"opusbytes").decode("ascii")
        resp = worker.invoke(InvokeRequest(
            call_id="c2", capability="voice.stt.v1",
            payload={"audio": b64audio, "format": "opus"}))

    assert resp.ok is True
    assert resp.result["text"] == "你好世界"
    assert resp.result["is_final"] is True
    sent = captured[0]
    assert sent["body"]["model"] == "mimo-v2-omni"
    msg = sent["body"]["messages"][0]
    assert msg["role"] == "user"
    parts = msg["content"]
    audio_part = next(p for p in parts if p["type"] == "input_audio")
    assert audio_part["input_audio"]["data"] == b64audio
    assert audio_part["input_audio"]["format"] == "opus"


def test_xiaomi_stt_empty_audio_returns_empty_text():
    from llmcore.worker import InvokeRequest
    from llmcore.workers import xiaomi_voice_worker as xv

    worker = xv.XiaomiSTT(
        name="s", api_key="sk-x", base_url="https://example.invalid/v1",
        model="mimo-v2-omni", prompt="转写", timeout_s=5.0,
    )
    resp = worker.invoke(InvokeRequest(
        call_id="c", capability="voice.stt.v1", payload={"audio": ""}))
    assert resp.ok is True
    assert resp.result["text"] == ""


# ── live API tests (only when key is set, opt-in via ENV) ───────────────


@pytest.mark.skipif(
    not os.environ.get("WLWL_XIAOMI_API_KEY"),
    reason="needs WLWL_XIAOMI_API_KEY in environment",
)
def test_xiaomi_tts_live_synthesizes_real_wav():
    from llmcore.worker import InvokeRequest
    from llmcore.workers.xiaomi_voice_worker import XiaomiTTSFactory
    from llmcore.kernel import Kernel

    kernel = Kernel()
    factory = XiaomiTTSFactory()
    worker = factory.build({"name": "live_tts"}, kernel)
    resp = worker.invoke(InvokeRequest(
        call_id="live1", capability="voice.tts.v1", payload={"text": "你好"}))
    assert resp.ok is True, f"live TTS failed: {resp.error}"
    audio = base64.b64decode(resp.result["audio_chunk"])
    assert len(audio) > 1000, "synthesized WAV implausibly small"
    # WAV magic bytes
    assert audio[:4] == b"RIFF"
    assert audio[8:12] == b"WAVE"
