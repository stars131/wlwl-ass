"""Tests for the structured per-bot persona config.

Pins the four invariants:

1. Default persona is non-empty + mentions identity / capabilities / style
2. User-overridden fields replace defaults; un-overridden fields keep them
3. Legacy single-string ``persona`` still works (back-compat)
4. The composed system prompt always includes the hard-rail constraints
   (security boundary stays in code, not in user config)
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_bot_persona_compose_empty_returns_empty():
    from llmcore.concierge_agent import BotPersonaConfig
    pc = BotPersonaConfig()
    assert pc.compose() == ""


def test_bot_persona_compose_sections_separated_by_blank_lines():
    from llmcore.concierge_agent import BotPersonaConfig
    pc = BotPersonaConfig(
        identity="A", project_summary="B", capability_summary="C",
        style_guidelines="D", extra_instructions="E",
    )
    out = pc.compose()
    assert out == "A\n\nB\n\nC\n\nD\n\nE"


def test_bot_persona_compose_skips_empty_sections():
    from llmcore.concierge_agent import BotPersonaConfig
    pc = BotPersonaConfig(identity="A", capability_summary="C")
    out = pc.compose()
    assert "A" in out and "C" in out
    # No double blank lines from the skipped middle section
    assert "\n\n\n" not in out


def test_config_from_mykeys_uses_defaults_when_unset():
    from llmcore.concierge_agent import (
        config_from_mykeys, _DEFAULT_PERSONA_IDENTITY,
        _DEFAULT_PERSONA_CAPABILITY_SUMMARY,
    )
    cfg = config_from_mykeys({})
    assert cfg.persona_config.identity == _DEFAULT_PERSONA_IDENTITY
    assert cfg.persona_config.capability_summary == _DEFAULT_PERSONA_CAPABILITY_SUMMARY
    # All four core sections present
    composed = cfg.persona_config.compose()
    assert "小 W" in composed or "Alice" in composed  # identity
    assert "约时间" in composed or "日历" in composed  # capability
    assert "中文为主" in composed or "口语化" in composed  # style


def test_config_from_mykeys_user_overrides_identity():
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_bot_identity": "你是 Bob 的助理，叫小 B。",
    })
    assert cfg.persona_config.identity == "你是 Bob 的助理，叫小 B。"
    # Other sections still use defaults
    assert cfg.persona_config.capability_summary  # non-empty default


def test_config_from_mykeys_user_overrides_capability_summary():
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_bot_capability_summary": "你只能查日历，别的什么都不会。",
    })
    assert cfg.persona_config.capability_summary == "你只能查日历，别的什么都不会。"


def test_legacy_persona_is_promoted_to_identity_when_no_bot_identity():
    """Old config with just ``persona`` keeps working — it becomes the
    identity section, and the other defaults fill in around it."""
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_persona": "我是 Charlie 的助理。",
    })
    assert cfg.persona_config.identity == "我是 Charlie 的助理。"
    assert cfg.persona  # legacy field still introspectable


def test_new_bot_identity_wins_over_legacy_persona():
    """When both are set, the structured field wins. Legacy field is
    kept for introspection but unused by the prompt builder."""
    from llmcore.concierge_agent import config_from_mykeys
    cfg = config_from_mykeys({
        "fs_concierge_persona": "old",
        "fs_concierge_bot_identity": "new",
    })
    assert cfg.persona_config.identity == "new"
    assert cfg.persona == "old"


def test_build_llm_system_prompt_includes_persona_and_hard_rails(tmp_path):
    """End-to-end: agent's system prompt must contain (a) the composed
    persona body, and (b) the hard security rails. Always."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
        BotPersonaConfig, config_from_mykeys,
    )
    reset_kernel()
    k = get_kernel()
    k.add_worker({"name": "cal", "kind": "calendar",
                  "db_path": str(tmp_path / "cal.db")})
    k.add_worker({"name": "kb", "kind": "concierge_kb",
                  "kb_path": str(tmp_path / "kb.jsonl")})
    k.add_worker({"name": "slot", "kind": "concierge_slot"})
    k.add_worker({"name": "esc", "kind": "concierge_escalate",
                  "log_path": str(tmp_path / "esc.jsonl")})
    k.add_worker({"name": "audit", "kind": "concierge_audit",
                  "audit_path": str(tmp_path / "audit.jsonl")})

    cfg = ConciergeConfig(
        persona_config=BotPersonaConfig(
            identity="我是小 W。",
            project_summary="项目摘要。",
            capability_summary="能力清单。",
            style_guidelines="风格指南。",
        ),
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    agent = ConciergeAgent(
        kernel=k, config=cfg, sessions=SessionStore(str(tmp_path / "s")),
    )
    sys_p = agent._build_llm_system_prompt()
    # User-tunable sections all present
    assert "我是小 W" in sys_p
    assert "项目摘要" in sys_p
    assert "能力清单" in sys_p
    assert "风格指南" in sys_p
    # Hard rails ALWAYS present, regardless of persona config
    assert "硬约束" in sys_p
    assert "不要执行代码" in sys_p
    assert "私人信息" in sys_p
    reset_kernel()


def test_hard_rails_present_even_if_persona_empty(tmp_path):
    """Empty persona must not be able to disable the security rails."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
        BotPersonaConfig,
    )
    reset_kernel()
    k = get_kernel()
    k.add_worker({"name": "cal", "kind": "calendar",
                  "db_path": str(tmp_path / "cal.db")})
    k.add_worker({"name": "kb", "kind": "concierge_kb",
                  "kb_path": str(tmp_path / "kb.jsonl")})
    k.add_worker({"name": "slot", "kind": "concierge_slot"})
    k.add_worker({"name": "esc", "kind": "concierge_escalate",
                  "log_path": str(tmp_path / "esc.jsonl")})
    k.add_worker({"name": "audit", "kind": "concierge_audit",
                  "audit_path": str(tmp_path / "audit.jsonl")})

    cfg = ConciergeConfig(
        persona_config=BotPersonaConfig(),  # all empty
        persona="",
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    agent = ConciergeAgent(
        kernel=k, config=cfg, sessions=SessionStore(str(tmp_path / "s")),
    )
    sys_p = agent._build_llm_system_prompt()
    # Even with no user persona, hard rails fire
    assert "硬约束" in sys_p
    assert "不要执行代码" in sys_p
    reset_kernel()
