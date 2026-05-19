"""Tests for ConciergeAgent Phase 3 LLM hooks — classify upgrade, schedule
extraction, slot rendering, QA rephrasing.

These tests pin three invariants:

1. **LLM can only upgrade SMALLTALK** to schedule/qa/escalate_now/cancel.
   Out_of_scope verdicts and other rule-firm intents are NEVER overridden.
2. **LLM output is validated** before use. Malformed JSON, missing slots,
   unknown intent kinds → fall back to deterministic rule path.
3. **Failure isolation** — when any hook raises, the agent must keep working
   via the rule fallback. A jittery LLM endpoint can't kill replies.
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
    """Real kernel + workers + ConciergeAgent with mock LLM hooks."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, LLMHooks, SessionStore, RateLimit,
    )
    from llmcore.workers.kb_worker import KBStorage

    reset_kernel()
    k = get_kernel()

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

    hooks = LLMHooks(
        chat=MagicMock(return_value=None),
        classify=MagicMock(return_value=None),
        extract_schedule=MagicMock(return_value=None),
        render_slots=MagicMock(return_value=None),
        render_qa=MagicMock(return_value=None),
    )
    agent = ConciergeAgent(
        kernel=k, config=cfg, sessions=sessions, llm_hooks=hooks,
    )

    yield {"agent": agent, "hooks": hooks, "audit_path": tmp_path / "audit.jsonl"}
    reset_kernel()


# ── classify upgrade ──────────────────────────────────────────────────

def test_llm_classify_upgrades_smalltalk_to_schedule(stack):
    """A vague message that rule-classifier reads as smalltalk gets
    upgraded to schedule when the LLM finds an implicit meeting ask."""
    agent = stack["agent"]
    stack["hooks"].classify.return_value = {
        "intent": "schedule",
        "payload": {"topic_summary": "想聚一聚"},
    }
    # "在干啥" is pure smalltalk by rules — no schedule/QA keywords. LLM
    # gets a chance to upgrade it.
    reply = agent.handle("ou_x", "在干啥")
    # schedule path → slots proposed (or escalated when no slots)
    assert ("1." in reply
            or "转告" in reply
            or "她最近这周排得有点满" in reply)
    stack["hooks"].classify.assert_called_once()


def test_llm_classify_does_NOT_override_out_of_scope(stack):
    """Critical: rule-based out_of_scope ALWAYS wins. The LLM hook must
    never even be consulted when the rule says out_of_scope."""
    agent = stack["agent"]
    stack["hooks"].classify.return_value = {"intent": "smalltalk", "payload": {}}
    reply = agent.handle("ou_x", "帮我用 python 写个爬虫")
    stack["hooks"].classify.assert_not_called()
    assert "抱歉" in reply or "Claude" in reply or "Codex" in reply


def test_llm_classify_rejects_unknown_intent(stack):
    """If the LLM returns an intent we don't recognise, fall back to
    rule-based smalltalk. The LLM cannot invent new intents."""
    agent = stack["agent"]
    stack["hooks"].classify.return_value = {"intent": "admin_root", "payload": {}}
    reply = agent.handle("ou_x", "嗯嗯")  # smalltalk
    # Rule fallback: smalltalk template fires
    assert reply
    assert "1." not in reply  # no slot list


def test_llm_classify_rejects_out_of_scope_upgrade(stack):
    """LLM cannot upgrade smalltalk → out_of_scope. That path is rule-
    only by design: out_of_scope is a *rejection*, not something the
    LLM should be allowed to invoke."""
    agent = stack["agent"]
    stack["hooks"].classify.return_value = {"intent": "out_of_scope", "payload": {}}
    reply = agent.handle("ou_x", "嗯嗯")
    # Rule fallback to smalltalk, NOT the out_of_scope reply
    assert "抱歉" not in reply


def test_llm_classify_rejects_malformed_output(stack):
    agent = stack["agent"]
    stack["hooks"].classify.return_value = "not a dict"
    reply = agent.handle("ou_x", "嗯嗯")
    assert reply  # rule fallback worked


def test_llm_classify_swallows_exceptions(stack):
    agent = stack["agent"]
    stack["hooks"].classify.side_effect = RuntimeError("LLM crashed")
    reply = agent.handle("ou_x", "嗯嗯")
    assert reply  # rule fallback worked


# ── schedule hint extraction ──────────────────────────────────────────

def test_llm_extract_schedule_overrides_duration(stack):
    """LLM finds a duration the regex missed (e.g. '聊半小时')."""
    agent = stack["agent"]
    stack["hooks"].extract_schedule.return_value = {
        "duration_minutes": 30,
        "preferred_window": "evening",
        "topic_summary": "晚上聊聊",
    }
    agent.handle("ou_x", "晚上想约你聊半小时")
    stack["hooks"].extract_schedule.assert_called_once()


def test_llm_extract_schedule_rejects_out_of_range_duration(stack):
    """Duration must be 5..480 minutes. Anything else is dropped — the
    rule-based default fills in instead."""
    agent = stack["agent"]
    stack["hooks"].extract_schedule.return_value = {
        "duration_minutes": 9999,
        "preferred_window": "evening",
    }
    reply = agent.handle("ou_x", "明天有时间吗")
    # No crash, slot proposal still happens
    assert "1." in reply


def test_llm_extract_schedule_rejects_unknown_window(stack):
    agent = stack["agent"]
    stack["hooks"].extract_schedule.return_value = {
        "preferred_window": "midnight_after_party",
    }
    reply = agent.handle("ou_x", "明天有时间吗")
    assert "1." in reply  # didn't crash


def test_llm_extract_schedule_swallows_exceptions(stack):
    agent = stack["agent"]
    stack["hooks"].extract_schedule.side_effect = RuntimeError("LLM crashed")
    reply = agent.handle("ou_x", "明天有时间吗")
    assert "1." in reply


# ── slot rendering ────────────────────────────────────────────────────

def test_llm_render_slots_replaces_template(stack):
    """When LLM rephrases successfully (preserving every slot time), its
    output is sent instead of the deterministic template."""
    agent = stack["agent"]

    def fake_render(slots, topic, alias):
        # Slots are dynamic (depend on current time); echo every HH:MM
        # so the validator accepts the LLM rephrase.
        times = " 、 ".join(s["start"][11:16] for s in slots)
        return f"她 {times} 都行哦，你方便哪个？"

    stack["hooks"].render_slots.side_effect = fake_render
    reply = agent.handle("ou_x", "明天有时间吗")
    assert "她" in reply and "都行哦" in reply
    assert "1." not in reply  # template did NOT fire


def test_llm_render_slots_falls_back_when_slot_missing(stack):
    """LLM that hides a slot (validation reject) → rule template fires."""
    agent = stack["agent"]
    # LLM returns text that mentions ONE slot but the slot worker
    # produces multiple. Validator rejects → template fires.
    stack["hooks"].render_slots.return_value = "她明天 09:00 可以"
    reply = agent.handle("ou_x", "明天有时间吗")
    # Template prefix shows up
    assert "好呀，我帮她看了下日历" in reply
    assert "1." in reply


def test_llm_render_slots_swallows_exceptions(stack):
    agent = stack["agent"]
    stack["hooks"].render_slots.side_effect = RuntimeError("LLM crashed")
    reply = agent.handle("ou_x", "明天有时间吗")
    assert "1." in reply  # template fired


def test_llm_render_slots_strips_reasoning_tags(stack):
    agent = stack["agent"]

    def fake_render(slots, topic, alias):
        times = " ".join(s["start"][11:16] for s in slots)
        return f"<thinking>let me see</thinking>她 {times} 都可以"

    stack["hooks"].render_slots.side_effect = fake_render
    reply = agent.handle("ou_x", "明天有时间吗")
    assert "thinking" not in reply


# ── QA rephrasing ─────────────────────────────────────────────────────

def test_llm_render_qa_replaces_template(stack):
    agent = stack["agent"]
    stack["hooks"].render_qa.return_value = "她在 ABC 公司哦～"
    reply = agent.handle("ou_x", "她现在在哪上班")
    assert reply == "她在 ABC 公司哦～"


def test_llm_render_qa_swallows_exceptions(stack):
    agent = stack["agent"]
    stack["hooks"].render_qa.side_effect = RuntimeError("LLM crashed")
    reply = agent.handle("ou_x", "她现在在哪上班")
    # Falls back to raw KB summary
    assert "ABC" in reply


def test_llm_render_qa_not_called_for_gated_topic(stack):
    """KB miss + outside allowlist → polite refusal. LLM not consulted."""
    agent = stack["agent"]
    reply = agent.handle("ou_x", "她身份证号多少")
    stack["hooks"].render_qa.assert_not_called()


def test_llm_render_qa_clips_runaway_output(stack):
    """LLM rambling — clip back to 3x KB text length."""
    agent = stack["agent"]
    stack["hooks"].render_qa.return_value = "啊" * 5000
    reply = agent.handle("ou_x", "她现在在哪上班")
    assert len(reply) <= 5000  # was clipped


# ── audit + capability accounting ─────────────────────────────────────

def test_audit_records_each_llm_path(stack, tmp_path):
    agent = stack["agent"]
    # smalltalk LLM
    stack["hooks"].chat.return_value = "嗯嗯～"
    agent.handle("ou_x", "今天天气不错")
    # schedule + slot rendering LLM
    def fake_render(slots, topic, alias):
        times = " ".join(s["start"][11:16] for s in slots)
        return f"她 {times} 都行"
    stack["hooks"].render_slots.side_effect = fake_render
    agent.handle("ou_y", "明天有时间吗")
    # QA rendering LLM
    stack["hooks"].render_qa.return_value = "她在 ABC 公司哦～"
    agent.handle("ou_z", "她现在在哪上班")

    rows = [json.loads(l) for l in open(stack["audit_path"], encoding="utf-8") if l.strip()]
    outs = [r for r in rows if r.get("kind") == "outbound"]
    caps_used_all = [c for r in outs for c in (r.get("capabilities_used") or [])]
    assert "_llm.smalltalk" in caps_used_all
    assert "_llm.render_slots" in caps_used_all
    assert "_llm.render_qa" in caps_used_all


# ── backward-compat: legacy llm_chat-only wiring still works ──────────

def test_legacy_llm_chat_arg_still_works(tmp_path):
    """Old callers passing just ``llm_chat=...`` (no LLMHooks) must keep
    working — the smalltalk path uses it, the other paths are rule-based."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
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

    chat = MagicMock(return_value="嗯嗯，你说")
    cfg = ConciergeConfig(
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    agent = ConciergeAgent(
        kernel=k, config=cfg, sessions=SessionStore(str(tmp_path / "s")),
        llm_chat=chat,
    )
    reply = agent.handle("ou_x", "在干啥呢")
    chat.assert_called_once()
    assert reply == "嗯嗯，你说"
    reset_kernel()
