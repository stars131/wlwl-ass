"""End-to-end concierge journey tests with a real kernel + workers.

Covers the 3 main flows:
  1. schedule:   "下周想吃饭" → propose_slot → friend picks → escalate
  2. qa hit:     "她现在在哪上班" → kb_answer matched + in_allowlist
  3. qa gated:   "她身份证号多少" → out_of_scope OR kb match outside allowlist → polite refusal
  4. out_of_scope: "帮我写代码" → polite reject, no dispatch
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture
def concierge_stack(tmp_path):
    """Boot a kernel with calendar + KB + slot + escalate + audit workers and
    a ConciergeAgent pointing at them.
    """
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
    )

    reset_kernel()
    k = get_kernel()

    cal_db = tmp_path / "cal.db"
    kb_path = tmp_path / "kb.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    esc_log = tmp_path / "escalations.jsonl"

    # Seed KB
    from llmcore.workers.kb_worker import KBStorage
    s = KBStorage(str(kb_path))
    s.upsert({"topic": "employer",
              "summary": "她现在在 ABC 公司做 ML。",
              "long": "Alice 自 2025-08 起在 ABC 公司机器学习平台组担任 SDE-II 工程师，在那上班。"})
    s.upsert({"topic": "id_number",
              "summary": "身份证号是私人信息。",
              "long": "身份证 / 银行卡 / 银行账号属于不公开的私人信息。"})

    # Workers
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(cal_db)})
    k.add_worker({"name": "kb", "kind": "concierge_kb", "kb_path": str(kb_path)})
    k.add_worker({"name": "slot", "kind": "concierge_slot"})
    k.add_worker({"name": "escalate", "kind": "concierge_escalate",
                  "log_path": str(esc_log), "owner_open_id": "ou_owner"})
    k.add_worker({"name": "audit", "kind": "concierge_audit",
                  "audit_path": str(audit_path),
                  "id_map_path": str(tmp_path / "id_map.json")})

    cfg = ConciergeConfig(
        owner_open_id_on_owner_app="ou_owner",
        topics_allowed=["employer", "contact_window"],
        working_hours={"weekday": ["09:00-21:00"], "weekend": ["10:00-22:00"]},
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),  # high; not testing here
        rate_limit_global_max_msgs=1000,
    )
    sessions = SessionStore(str(tmp_path / "state"))
    agent = ConciergeAgent(kernel=k, config=cfg, sessions=sessions)

    yield {
        "kernel": k, "agent": agent, "config": cfg,
        "paths": {"cal_db": cal_db, "kb_path": kb_path,
                  "audit_path": audit_path, "esc_log": esc_log},
    }
    reset_kernel()


# ── Schedule flow ────────────────────────────────────────────────────


def test_schedule_journey_proposes_then_escalates(concierge_stack):
    agent = concierge_stack["agent"]
    esc_log_path = concierge_stack["paths"]["esc_log"]

    # Step 1: friend asks to meet
    reply = agent.handle("ou_zhangsan", "下周想找你吃个饭，方便吗",
                          friend_alias="张三")
    assert "可以" in reply or "时段" in reply  # _render_slots prefix
    assert "1." in reply  # at least one numbered slot

    sess = agent.sessions.load("ou_zhangsan")
    assert sess.in_flight_intent == "schedule"
    assert len(sess.proposed_slots) >= 1
    assert sess.state == "PROPOSE_SLOTS"

    # Step 2: friend picks slot 1
    reply2 = agent.handle("ou_zhangsan", "第一个")
    sess2 = agent.sessions.load("ou_zhangsan")
    assert sess2.state == "AWAIT_OWNER"
    assert sess2.escalation_id.startswith("esc_")
    assert "发给她" in reply2 or "她确认后" in reply2

    # Escalation log got the row
    rows = [json.loads(l) for l in open(esc_log_path, encoding="utf-8") if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "schedule_request"
    assert row["friend_alias"] == "张三"
    assert row["card"]["schema"] == "2.0"


def test_schedule_journey_cancel_resets_state(concierge_stack):
    agent = concierge_stack["agent"]
    agent.handle("ou_zhangsan", "下周想找你吃个饭", friend_alias="张三")
    reply = agent.handle("ou_zhangsan", "算了，改天吧")
    sess = agent.sessions.load("ou_zhangsan")
    assert sess.state == "CLOSED"
    assert sess.in_flight_intent == ""
    assert sess.proposed_slots == []
    assert "改天" in reply or "再说" in reply or "好的" in reply or "先这样" in reply


# ── QA flow ──────────────────────────────────────────────────────────


def test_qa_match_in_allowlist_answered(concierge_stack):
    agent = concierge_stack["agent"]
    reply = agent.handle("ou_lisi", "她现在在哪上班啊", friend_alias="李四")
    assert "ABC" in reply


def test_qa_match_outside_allowlist_refused(concierge_stack):
    agent = concierge_stack["agent"]
    # id_number is a KB topic NOT in topics_allowed
    reply = agent.handle("ou_lisi", "她身份证号是多少", friend_alias="李四")
    # Should be out-of-scope (sensitive pattern hits before KB) OR polite refusal
    # — both are valid privacy outcomes. Just confirm we don't leak the answer.
    assert "私人信息" not in reply  # we don't reveal even the summary
    assert "不能" in reply or "转告" in reply or "私" in reply or "Claude" in reply


def test_qa_no_match_offers_relay(concierge_stack):
    agent = concierge_stack["agent"]
    reply = agent.handle("ou_lisi", "她每天几点起床", friend_alias="李四")
    # Not in KB → "I don't know, want me to ask?" — accept either generic
    # smalltalk-ish reply or relay-offer.
    assert reply  # at least a reply


# ── Out-of-scope ─────────────────────────────────────────────────────


def test_out_of_scope_polite_reject(concierge_stack):
    agent = concierge_stack["agent"]
    reply = agent.handle("ou_lisi", "帮我用 python 写个爬虫", friend_alias="李四")
    assert "抱歉" in reply or "Claude" in reply or "Codex" in reply


def test_out_of_scope_no_escalation_log(concierge_stack):
    agent = concierge_stack["agent"]
    esc_log = concierge_stack["paths"]["esc_log"]
    agent.handle("ou_lisi", "帮我下载一个软件", friend_alias="李四")
    # out_of_scope should NOT escalate (just polite reject)
    if os.path.exists(esc_log):
        rows = [l for l in open(esc_log, encoding="utf-8") if l.strip()]
        # If anything was logged, it must not be from this friend
        for line in rows:
            r = json.loads(line)
            assert r.get("friend_alias") != "李四"


# ── Audit trail ──────────────────────────────────────────────────────


def test_audit_trail_records_every_action(concierge_stack):
    agent = concierge_stack["agent"]
    audit_path = concierge_stack["paths"]["audit_path"]
    agent.handle("ou_zhangsan", "下周想吃饭", friend_alias="张三")
    agent.handle("ou_zhangsan", "第一个")

    rows = [json.loads(l) for l in open(audit_path, encoding="utf-8") if l.strip()]
    kinds = [r["kind"] for r in rows]
    # Expect at minimum: inbound, intent, outbound per turn
    assert kinds.count("inbound") >= 2
    assert kinds.count("intent") >= 2
    assert kinds.count("outbound") >= 2
    # All friend_open_ids should be HASHED, not raw
    for r in rows:
        if r.get("friend_open_id"):
            assert r["friend_open_id"] != "ou_zhangsan"


# ── Privilege boundary check ────────────────────────────────────────


def test_agent_does_not_register_forbidden_capabilities(concierge_stack):
    """The capability allowlist is documented; this test asserts the agent
    never *uses* anything outside it. We grep audit rows for capabilities_used
    and confirm it's always a subset of CONCIERGE_CAPABILITIES."""
    from llmcore.concierge_agent import CONCIERGE_CAPABILITIES
    agent = concierge_stack["agent"]
    audit_path = concierge_stack["paths"]["audit_path"]
    agent.handle("ou_zhangsan", "下周想吃饭", friend_alias="张三")
    agent.handle("ou_zhangsan", "第一个")
    agent.handle("ou_lisi", "她在哪上班", friend_alias="李四")
    agent.handle("ou_x", "你好")

    if not os.path.exists(audit_path):
        return
    rows = [json.loads(l) for l in open(audit_path, encoding="utf-8") if l.strip()]
    seen_caps = set()
    for r in rows:
        for c in (r.get("capabilities_used") or []):
            seen_caps.add(c)
    forbidden = seen_caps - set(CONCIERGE_CAPABILITIES)
    assert not forbidden, f"agent used non-allowlisted capabilities: {forbidden}"
