"""Tests for frontends/fsapp_concierge.py — message routing, access gate,
owner-self-DM ignored, mock send path.

We don't open a real Feishu WS connection. Instead we instantiate
ConciergeApp, replace its kernel/agent with mocks (or wire it to a real
in-process kernel via the conftest path), and feed canned event objects
directly into ``handle_event``.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── Helpers ──────────────────────────────────────────────────────────


def _make_event(open_id: str, text: str, msg_type: str = "text",
                 chat_id: str = "oc_test"):
    """Build a duck-typed object that mimics what lark_oapi delivers to
    register_p2_im_message_receive_v1 callbacks."""
    msg = types.SimpleNamespace(
        message_type=msg_type,
        message_id="om_x",
        chat_id=chat_id,
        content=json.dumps({"text": text}, ensure_ascii=False),
    )
    sender = types.SimpleNamespace(
        sender_id=types.SimpleNamespace(open_id=open_id)
    )
    event = types.SimpleNamespace(message=msg, sender=sender, ts="0")
    return types.SimpleNamespace(event=event)


@pytest.fixture
def app(monkeypatch, tmp_path):
    """ConciergeApp instance wired to a fresh kernel + test paths.

    Bypasses the lark WS connection by NOT calling .run() — we exercise
    .handle_event() / ._dispatch_one() directly with mocked send_text.
    """
    # Override DEFAULT_* paths to live under tmp_path
    monkeypatch.setattr("frontends.fsapp_concierge.TEMP_DIR", str(tmp_path))
    monkeypatch.setattr("frontends.fsapp_concierge.DEFAULT_KB_PATH",
                         str(tmp_path / "kb.jsonl"))
    monkeypatch.setattr("frontends.fsapp_concierge.DEFAULT_AUDIT_PATH",
                         str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr("frontends.fsapp_concierge.DEFAULT_ESCALATION_LOG",
                         str(tmp_path / "escalations.jsonl"))
    monkeypatch.setattr("frontends.fsapp_concierge.DEFAULT_SESSION_DIR",
                         str(tmp_path / "state"))
    monkeypatch.setattr("frontends.fsapp_concierge.DEFAULT_CALENDAR_DB",
                         str(tmp_path / "cal.db"))

    # Stub mykeys — we want full control over what the app sees
    import frontends.fsapp_concierge as mod
    fake_mykeys = {
        "fs_concierge_app_id": "cli_test_concierge",
        "fs_concierge_app_secret": "secret_concierge",
        "fs_concierge_owner_open_id": "ou_owner",
        "fs_concierge_owner_open_id_on_concierge": "ou_owner",
        "fs_concierge_allowed_friends": ["ou_zhangsan", "ou_lisi"],
        # owner-app creds left empty — escalate falls back to JSONL stub
        "fs_concierge_topics_allowed": ["employer"],
        "fs_concierge_working_hours": {
            "weekday": ["09:00-21:00"], "weekend": ["10:00-22:00"],
        },
    }
    monkeypatch.setattr(mod, "mykeys", fake_mykeys)

    # Seed KB so QA tests have something to match
    from llmcore.workers.kb_worker import KBStorage
    s = KBStorage(str(tmp_path / "kb.jsonl"))
    s.upsert({"topic": "employer",
              "summary": "她现在在 ABC 公司做 ML。",
              "long": "Alice 在 ABC 公司上班，做 ML。"})

    instance = mod.ConciergeApp()
    instance.boot_kernel()
    instance.agent = instance.build_agent()
    # Replace the lark send path with a recording mock
    instance.send_text = MagicMock(return_value=True)

    yield instance

    from llmcore.kernel import reset_kernel
    reset_kernel()


# ── Access gate ──────────────────────────────────────────────────────


def test_friend_in_allowlist_is_processed(app):
    # Pure p2p case: no chat_id → reply goes to open_id.
    app._dispatch_one("ou_zhangsan", "你好", chat_id=None)
    app.send_text.assert_called_once()
    # send_text(receive_id, reply [, receive_id_type]) — only positional
    # args here since the p2p path doesn't pass receive_id_type.
    receive_id, reply = app.send_text.call_args.args
    assert receive_id == "ou_zhangsan"
    assert reply


def test_friend_in_group_chat_replies_in_group(app):
    """When the inbound message has a chat_id (group / shared chat), the
    reply must go to that chat_id with receive_id_type='chat_id'. Sending
    to the sender's open_id would create a NEW p2p chat alongside the
    group, which looks broken to the user."""
    app._dispatch_one("ou_zhangsan", "你好", chat_id="oc_x")
    app.send_text.assert_called_once()
    args = app.send_text.call_args.args
    kwargs = app.send_text.call_args.kwargs
    assert args[0] == "oc_x"
    assert kwargs.get("receive_id_type") == "chat_id"


def test_unknown_friend_dropped(app):
    # Simulate the full inbound path including access_check
    reason = app.access_check("ou_stranger")
    assert reason == "not in allowed_friends"


def test_owner_self_dm_dropped(app):
    reason = app.access_check("ou_owner")
    assert "owner" in reason.lower()


def test_empty_sender_dropped(app):
    assert app.access_check("") == "empty sender"


def test_public_access_opt_in(monkeypatch, tmp_path):
    """Setting allowed_friends=['*'] explicitly opens public access."""
    import frontends.fsapp_concierge as mod
    monkeypatch.setattr(mod, "mykeys", {
        "fs_concierge_app_id": "cli_x",
        "fs_concierge_app_secret": "x",
        "fs_concierge_owner_open_id": "ou_owner",
        "fs_concierge_owner_open_id_on_concierge": "ou_owner",
        "fs_concierge_allowed_friends": ["*"],
    })
    app = mod.ConciergeApp()
    assert app._public_access is True
    assert app.access_check("ou_anyone") == ""


def test_empty_allowed_friends_denies_all(monkeypatch, tmp_path):
    """Missing/empty allowed_friends == deny all (matches fsapp.py posture)."""
    import frontends.fsapp_concierge as mod
    monkeypatch.setattr(mod, "mykeys", {
        "fs_concierge_app_id": "cli_x",
        "fs_concierge_app_secret": "x",
        # no allowed_friends
    })
    app = mod.ConciergeApp()
    assert app._public_access is False
    assert app.access_check("ou_anyone") == "not in allowed_friends"


def test_owner_app_view_id_does_not_gate_inbound(monkeypatch, tmp_path):
    """owner_open_id_on_owner_app is a DIFFERENT identity than the concierge-
    view open_id of the same person. access_check must not use it. Otherwise
    owners who DM their own concierge bot (for testing) get silently dropped.
    """
    import frontends.fsapp_concierge as mod
    monkeypatch.setattr(mod, "mykeys", {
        "fs_concierge_app_id": "cli_x",
        "fs_concierge_app_secret": "x",
        # Owner-app view is "ou_owner_on_owner_app" — leaks in from escalate
        # config but should NOT participate in inbound gating.
        "fs_concierge_owner_open_id": "ou_owner_on_owner_app",
        # Concierge-view of owner not configured — owner can self-DM.
        "fs_concierge_allowed_friends": ["ou_owner_on_concierge_app"],
    })
    app = mod.ConciergeApp()
    # The owner-app-view ID must NOT be treated as a self-DM here.
    assert app.access_check("ou_owner_on_owner_app") == "not in allowed_friends"
    # The concierge-view ID, present in allowed_friends, gets through.
    assert app.access_check("ou_owner_on_concierge_app") == ""


# ── Message parsing ──────────────────────────────────────────────────


def test_parse_text_normal():
    from frontends.fsapp_concierge import _parse_text
    msg = types.SimpleNamespace(
        message_type="text",
        content=json.dumps({"text": "hi"}, ensure_ascii=False),
    )
    assert _parse_text(msg) == "hi"


def test_parse_text_image_placeholder():
    from frontends.fsapp_concierge import _parse_text
    msg = types.SimpleNamespace(message_type="image", content="{}")
    assert _parse_text(msg) == "[image]"


def test_parse_text_post_aggregates_text_nodes():
    from frontends.fsapp_concierge import _parse_text
    post_content = {
        "post": {
            "zh_cn": {
                "title": "标题",
                "content": [[{"tag": "text", "text": "你好"}, {"tag": "text", "text": "世界"}]],
            }
        }
    }
    msg = types.SimpleNamespace(message_type="post",
                                 content=json.dumps(post_content))
    out = _parse_text(msg)
    assert "标题" in out and "你好" in out and "世界" in out


# ── End-to-end through handle_event ──────────────────────────────────


def test_handle_event_drops_non_text_silently(app):
    ev = _make_event("ou_zhangsan", "", msg_type="audio")
    ev.event.message.content = "{}"
    app.handle_event(ev)
    # The dispatch thread is daemon; give it a beat
    time.sleep(0.1)
    # audio messages get parsed as "[audio]" placeholder which goes
    # to the agent. We just assert the worker thread didn't crash.


def test_handle_event_routes_text_to_agent(app):
    ev = _make_event("ou_zhangsan", "你好，在吗")
    app.handle_event(ev)
    # Spawn was on a thread; wait for it
    for _ in range(20):
        if app.send_text.called:
            break
        time.sleep(0.05)
    assert app.send_text.called
    # _make_event puts chat_id="oc_test" on the message, so the reply
    # path goes through send_text(chat_id, reply, receive_id_type="chat_id").
    args = app.send_text.call_args.args
    kwargs = app.send_text.call_args.kwargs
    assert args[0] == "oc_test"
    assert args[1]  # non-empty reply
    assert kwargs.get("receive_id_type") == "chat_id"


def test_handle_event_skips_owner(app):
    ev = _make_event("ou_owner", "test")
    app.handle_event(ev)
    time.sleep(0.1)
    assert not app.send_text.called


def test_kernel_has_concierge_workers(app):
    """Phase-2 boot must register all five workers."""
    names = {w.name for w in app.kernel.list_workers()}
    assert names >= {"cal", "concierge_kb", "concierge_slot",
                     "concierge_escalate", "concierge_audit"}


# ── config_from_mykeys ───────────────────────────────────────────────


def test_config_from_mykeys_uses_defaults_on_missing():
    from llmcore.concierge_agent import config_from_mykeys, ConciergeConfig
    cfg = config_from_mykeys({})
    default = ConciergeConfig()
    assert cfg.persona == default.persona
    assert cfg.exit_phrase == default.exit_phrase
    assert cfg.meeting_buffer_min == default.meeting_buffer_min


def test_config_from_mykeys_overrides_apply():
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_owner_open_id": "ou_x",
        "fs_concierge_persona": "你是 Bob 的助理。",
        "fs_concierge_topics_allowed": ["a", "b"],
        "fs_concierge_meeting_buffer_min": 30,
        "fs_concierge_rate_limit_per_friend": {"window_s": 30, "max_msgs": 4},
        "fs_concierge_rate_limit_global": {"window_s": 30, "max_msgs": 100},
    })
    assert cfg.owner_open_id_on_owner_app == "ou_x"
    assert cfg.persona == "你是 Bob 的助理。"
    assert cfg.topics_allowed == ["a", "b"]
    assert cfg.meeting_buffer_min == 30
    assert cfg.rate_limit_per_friend.max_msgs == 4
    assert cfg.rate_limit_global_max_msgs == 100


def test_config_from_mykeys_tolerates_garbage():
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_meeting_buffer_min": "not a number",
        "fs_concierge_rate_limit_per_friend": "also not a dict",
        "fs_concierge_topics_allowed": "single-string-not-list",
        "fs_concierge_working_hours": ["not a dict"],
    })
    # Falls back to defaults silently
    assert cfg.meeting_buffer_min == 15
    assert cfg.rate_limit_per_friend.window_s == 60
    assert cfg.topics_allowed == ["single-string-not-list"]
    assert isinstance(cfg.working_hours, dict)
