"""Tests for launcher/launch_config.py — lock in the bot-key removal.

Bot opt-in flags (tg/qq/feishu/wecom/dingtalk/wechat) used to live in
launch_options.json and were honored by qt_launcher / launch.pyw. They were
removed when bot startup moved to credential-based auto-start in
api_server._auto_start_configured_bots; these tests ensure those keys stay
gone.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_default_options_has_no_bot_keys():
    from launcher.launch_config import DEFAULT_OPTIONS
    for key in ("tg", "qq", "feishu", "wecom", "dingtalk", "wechat"):
        assert key not in DEFAULT_OPTIONS, f"{key} should be removed from DEFAULT_OPTIONS"


def test_normalize_drops_legacy_bot_keys():
    from launcher.launch_config import normalize_options

    result = normalize_options({
        "tg": True, "qq": True, "feishu": True,
        "wecom": True, "dingtalk": True, "wechat": True,
        "scheduler": False,
    })

    for key in ("tg", "qq", "feishu", "wecom", "dingtalk", "wechat"):
        assert key not in result
    assert result["scheduler"] is False


def test_normalize_keeps_supported_keys():
    from launcher.launch_config import normalize_options
    result = normalize_options(None)
    for key in ("scheduler", "llm_no", "permission_mode", "project_root",
                "use_project_context", "autonomous_enabled"):
        assert key in result


def test_no_BOT_KEYS_export():
    import launcher.launch_config as lc
    assert not hasattr(lc, "BOT_KEYS"), "BOT_KEYS constant should be removed"
