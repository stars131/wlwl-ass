from __future__ import annotations

import subprocess


def test_base_session_uses_connect_timeout_field():
    from llmcore.base import BaseSession

    session = BaseSession({
        "apikey": "k",
        "apibase": "https://example.invalid/v1",
        "model": "m",
        "connect_timeout": 12,
        "read_timeout": 45,
    })

    assert session.connect_timeout == 12
    assert session.read_timeout == 45


def test_base_session_keeps_legacy_timeout_fallback():
    from llmcore.base import BaseSession

    session = BaseSession({
        "apikey": "k",
        "apibase": "https://example.invalid/v1",
        "model": "m",
        "timeout": 9,
    })

    assert session.connect_timeout == 9


def test_voice_category_alone_does_not_pick_plain_chat_config(monkeypatch):
    from launcher import api_config
    from tools import voice_tools

    monkeypatch.setattr(
        api_config,
        "load_api_configs",
        lambda _base: [
            {
                "kind": "native_oai",
                "name": "plain-chat-marked-voice",
                "category": "voice",
                "apibase": "https://example.invalid/v1",
                "model": "gpt-4.1",
            },
        ],
    )

    assert voice_tools._pick_audio_config() is None


def test_voice_picker_accepts_audio_hints_inside_voice_category(monkeypatch):
    from launcher import api_config
    from tools import voice_tools

    monkeypatch.setattr(
        api_config,
        "load_api_configs",
        lambda _base: [
            {
                "kind": "native_oai",
                "name": "plain-chat-marked-voice",
                "category": "voice",
                "apibase": "https://example.invalid/v1",
                "model": "gpt-4.1",
            },
            {
                "kind": "native_oai",
                "name": "whisper",
                "category": "voice",
                "apibase": "https://example.invalid/v1",
                "model": "whisper-1",
            },
        ],
    )

    assert voice_tools._pick_audio_config()["name"] == "whisper"


def test_radar_stop_reports_failure_when_process_survives(monkeypatch):
    from launcher import radar_control

    monkeypatch.setattr(radar_control, "read_pid", lambda: 12345)
    monkeypatch.setattr(radar_control, "is_alive", lambda _pid: True)
    monkeypatch.setattr(radar_control.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        radar_control.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    result = radar_control.stop(timeout_s=0.01)

    assert result["ok"] is False
    assert result["killed"] is False
    assert result["pid"] == 12345
    assert "still alive" in result["message"]
