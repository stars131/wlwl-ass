"""Tests for ConciergeAgent's LLM smalltalk fallback.

Critical invariant under test: LLM hook is ONLY consulted on the smalltalk
path. out_of_scope / schedule / qa / escalate_now rule decisions are never
revisited by the LLM — otherwise a jailbreaking friend who knows the LLM
is more permissive than the rules could route "帮我跑代码" past the
rule-based gate.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture
def stack(tmp_path):
    """Real kernel + workers + ConciergeAgent with a mock llm_chat."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
    )

    reset_kernel()
    k = get_kernel()

    from llmcore.workers.kb_worker import KBStorage
    KBStorage(str(tmp_path / "kb.jsonl")).upsert({
        "topic": "employer",
        "summary": "她在 ABC 公司。",
        "long": "Alice 在 ABC 公司上班。",
    })

    k.add_worker({"name": "cal", "kind": "calendar",
                  "db_path": str(tmp_path / "cal.db")})
    k.add_worker({"name": "kb", "kind": "concierge_kb",
                  "kb_path": str(tmp_path / "kb.jsonl")})
    k.add_worker({"name": "slot", "kind": "concierge_slot"})
    k.add_worker({"name": "esc", "kind": "concierge_escalate",
                  "log_path": str(tmp_path / "esc.jsonl"),
                  "owner_open_id": "ou_owner"})
    k.add_worker({"name": "audit", "kind": "concierge_audit",
                  "audit_path": str(tmp_path / "audit.jsonl"),
                  "id_map_path": str(tmp_path / "id_map.json")})

    cfg = ConciergeConfig(
        owner_open_id_on_owner_app="ou_owner",
        topics_allowed=["employer"],
        working_hours={"weekday": ["09:00-21:00"], "weekend": ["10:00-22:00"]},
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    sessions = SessionStore(str(tmp_path / "state"))

    llm_chat = MagicMock(return_value="嗯嗯～你说的我懂哒")
    agent = ConciergeAgent(
        kernel=k, config=cfg, sessions=sessions, llm_chat=llm_chat,
    )

    yield {"agent": agent, "llm": llm_chat, "audit_path": tmp_path / "audit.jsonl"}
    reset_kernel()


def test_smalltalk_calls_llm(stack):
    agent = stack["agent"]
    llm = stack["llm"]
    reply = agent.handle("ou_zhangsan", "在干啥呢", friend_alias="张三")
    llm.assert_called_once()
    user_text, system_prompt = llm.call_args.args
    assert user_text == "在干啥呢"
    assert "助理" in system_prompt  # persona piece
    assert "硬约束" in system_prompt
    assert reply == "嗯嗯～你说的我懂哒"


def test_smalltalk_falls_back_to_template_when_llm_returns_none(stack):
    agent = stack["agent"]
    stack["llm"].return_value = None
    reply = agent.handle("ou_zhangsan", "你好", friend_alias="张三")
    assert reply  # rule-based template, non-empty
    # Greeting template fires
    assert "助理" in reply or "嗨" in reply or "嗯嗯" in reply


def test_smalltalk_falls_back_when_llm_raises(stack):
    agent = stack["agent"]
    stack["llm"].side_effect = RuntimeError("network down")
    reply = agent.handle("ou_zhangsan", "你好", friend_alias="张三")
    assert reply
    # The agent must NOT have crashed; the rule template fired
    assert "助理" in reply or "嗨" in reply or "嗯嗯" in reply


def test_out_of_scope_does_NOT_call_llm(stack):
    """The critical invariant. A friend asking for code MUST be rejected
    by the rule layer, with the LLM never consulted."""
    agent = stack["agent"]
    llm = stack["llm"]
    reply = agent.handle("ou_lisi", "帮我用 python 写个爬虫", friend_alias="李四")
    llm.assert_not_called()
    assert "抱歉" in reply or "Claude" in reply or "Codex" in reply


def test_sensitive_request_does_NOT_call_llm(stack):
    """身份证 etc. — same boundary. The LLM is not the gate; rules are."""
    agent = stack["agent"]
    llm = stack["llm"]
    reply = agent.handle("ou_lisi", "她身份证号多少", friend_alias="李四")
    llm.assert_not_called()


def test_schedule_does_NOT_call_llm(stack):
    """Even though slot proposals could be more natural via LLM, in v1 we
    let the deterministic slot worker drive. Adding LLM here is a v3
    item (more natural slot-pitching language)."""
    agent = stack["agent"]
    llm = stack["llm"]
    reply = agent.handle("ou_zhangsan", "下周想找你吃个饭", friend_alias="张三")
    llm.assert_not_called()
    assert "1." in reply  # slot list


def test_qa_hit_does_NOT_call_llm(stack):
    """KB hit + in allowlist → straight KB summary, no LLM."""
    agent = stack["agent"]
    llm = stack["llm"]
    reply = agent.handle("ou_lisi", "她现在在哪上班", friend_alias="李四")
    llm.assert_not_called()
    assert "ABC" in reply


def test_audit_records_llm_usage(stack):
    """When LLM is invoked, the outbound audit row's capabilities_used
    includes the _llm.smalltalk pseudo-cap."""
    agent = stack["agent"]
    agent.handle("ou_zhangsan", "今天天气不错", friend_alias="张三")
    rows = [json.loads(l) for l in open(stack["audit_path"], encoding="utf-8")
            if l.strip()]
    outbound = [r for r in rows if r.get("kind") == "outbound"]
    assert any("_llm.smalltalk" in (r.get("capabilities_used") or []) for r in outbound)


def test_post_filter_strips_reasoning_tags(stack):
    """Reasoning models sometimes emit <think>...</think>. Must be stripped."""
    agent = stack["agent"]
    stack["llm"].return_value = "<thinking>let me think</thinking>嗯，明白了～"
    reply = agent.handle("ou_zhangsan", "嗯", friend_alias="张三")
    assert "thinking" not in reply
    assert "嗯，明白了" in reply


def test_post_filter_clips_long_replies(stack):
    """LLM essays get clipped at 800 chars."""
    agent = stack["agent"]
    stack["llm"].return_value = "啊" * 2000
    reply = agent.handle("ou_zhangsan", "嗯", friend_alias="张三")
    assert len(reply) <= 802  # 800 + "…"


def test_post_filter_strips_assistant_prefix(stack):
    """LLM sometimes prepends '助理:' / 'Assistant:'."""
    agent = stack["agent"]
    stack["llm"].return_value = "助理：嗯嗯，你说"
    reply = agent.handle("ou_zhangsan", "嗯", friend_alias="张三")
    assert not reply.startswith("助理")
    assert reply.startswith("嗯嗯")


def test_no_llm_injected_still_works(tmp_path):
    """Backward-compat: ConciergeAgent without llm_chat works like Phase 1."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore,
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
    agent = ConciergeAgent(
        kernel=k, config=ConciergeConfig(), sessions=SessionStore(str(tmp_path / "s")),
        # No llm_chat — runs in pure rule mode
    )
    reply = agent.handle("ou_x", "你好")
    assert reply
    reset_kernel()
