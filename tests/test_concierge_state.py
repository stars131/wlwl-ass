"""Tests for ConciergeAgent per-friend state, rate limit, intent classifier."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from llmcore.concierge_agent import (
    CONCIERGE_CAPABILITIES,
    FriendSession, GlobalRateLimiter, RateLimit, SessionStore,
    classify_intent,
    INTENT_SCHEDULE, INTENT_QA, INTENT_OUT_OF_SCOPE, INTENT_SMALLTALK,
    INTENT_SCHEDULE_PICK, INTENT_CANCEL, INTENT_ESCALATE_NOW,
)


# ── Privilege boundary ─────────────────────────────────────────────────


def test_capabilities_are_locked_down():
    forbidden = {"code_run", "file_write", "file_patch", "web_execute_js",
                 "start_long_term_update", "ask_user"}
    assert not (set(CONCIERGE_CAPABILITIES) & forbidden)


def test_capabilities_count_matches_spec():
    # Spec § 4.2 hard 6
    assert len(CONCIERGE_CAPABILITIES) == 6


# ── SessionStore ──────────────────────────────────────────────────────


def test_session_store_roundtrip(tmp_path):
    store = SessionStore(str(tmp_path))
    sess = store.load("ou_x")
    assert sess.open_id == "ou_x"
    assert sess.state == "NEW"

    sess.in_flight_intent = "schedule"
    sess.proposed_slots = [{"start": "2026-05-21T19:00:00", "end": "2026-05-21T20:00:00"}]
    sess.topic_summary = "吃饭"
    store.save(sess)

    sess2 = store.load("ou_x")
    assert sess2.in_flight_intent == "schedule"
    assert sess2.proposed_slots == sess.proposed_slots
    assert sess2.topic_summary == "吃饭"


def test_session_store_isolates_users(tmp_path):
    store = SessionStore(str(tmp_path))
    a = store.load("ou_a")
    a.topic_summary = "a-topic"
    store.save(a)
    b = store.load("ou_b")
    b.topic_summary = "b-topic"
    store.save(b)
    assert store.load("ou_a").topic_summary == "a-topic"
    assert store.load("ou_b").topic_summary == "b-topic"


def test_session_store_handles_path_unsafe_ids(tmp_path):
    store = SessionStore(str(tmp_path))
    # path-traversal-style id — must be sanitized
    sess = store.load("../evil/ou_x")
    sess.topic_summary = "x"
    store.save(sess)
    files = os.listdir(str(tmp_path))
    # No file should escape the directory
    assert all(not f.startswith("..") for f in files)


# ── Rate limiter ──────────────────────────────────────────────────────


def test_global_rate_limiter_blocks_after_max():
    rl = GlobalRateLimiter(window_s=60, max_msgs=3)
    assert rl.hit() is False
    assert rl.hit() is False
    assert rl.hit() is False
    assert rl.hit() is True  # 4th hit blocked


def test_global_rate_limiter_recovers_after_window():
    rl = GlobalRateLimiter(window_s=1, max_msgs=2)
    assert rl.hit() is False
    assert rl.hit() is False
    assert rl.hit() is True
    time.sleep(1.1)
    assert rl.hit() is False


# ── Intent classifier (rule-based) ────────────────────────────────────


def _empty_session(open_id="ou_x"):
    return FriendSession(open_id=open_id)


def test_intent_schedule_basic():
    s = _empty_session()
    i = classify_intent("下周想找你吃个饭，方便吗", s)
    assert i.kind == INTENT_SCHEDULE


def test_intent_schedule_with_duration():
    s = _empty_session()
    i = classify_intent("约个 30 分钟的咖啡", s)
    assert i.kind == INTENT_SCHEDULE
    assert i.payload.get("duration_minutes") == 30


def test_intent_schedule_with_window_hint():
    s = _empty_session()
    i = classify_intent("晚上有空约个饭吗", s)
    assert i.kind == INTENT_SCHEDULE
    assert i.payload.get("preferred_window") == "evening"


def test_intent_qa_about_employer():
    s = _empty_session()
    i = classify_intent("她现在在哪上班啊", s)
    assert i.kind == INTENT_QA


def test_intent_out_of_scope_code_request():
    s = _empty_session()
    i = classify_intent("帮我用 python 写个爬虫", s)
    assert i.kind == INTENT_OUT_OF_SCOPE


def test_intent_out_of_scope_sensitive():
    s = _empty_session()
    i = classify_intent("她身份证号多少", s)
    assert i.kind == INTENT_OUT_OF_SCOPE


def test_intent_smalltalk_greeting():
    s = _empty_session()
    i = classify_intent("你好", s)
    assert i.kind == INTENT_SMALLTALK


def test_intent_pick_in_schedule_flow():
    s = _empty_session()
    s.in_flight_intent = INTENT_SCHEDULE
    s.proposed_slots = [
        {"start": "2026-05-21T19:00:00", "end": "2026-05-21T20:00:00"},
        {"start": "2026-05-22T19:00:00", "end": "2026-05-22T20:00:00"},
    ]
    i = classify_intent("第二个", s)
    assert i.kind == INTENT_SCHEDULE_PICK
    assert i.payload["pick_index"] == 1


def test_intent_pick_bare_ok_picks_first():
    s = _empty_session()
    s.in_flight_intent = INTENT_SCHEDULE
    s.proposed_slots = [{"start": "x", "end": "y"}]
    i = classify_intent("可以", s)
    assert i.kind == INTENT_SCHEDULE_PICK
    assert i.payload["pick_index"] == 0


def test_intent_cancel_in_schedule_flow():
    s = _empty_session()
    s.in_flight_intent = INTENT_SCHEDULE
    s.proposed_slots = [{"start": "x", "end": "y"}]
    i = classify_intent("算了，改天吧", s)
    assert i.kind == INTENT_CANCEL


def test_intent_escalate_now():
    s = _empty_session()
    i = classify_intent("急事，让她马上联系我", s)
    assert i.kind == INTENT_ESCALATE_NOW


def test_intent_out_of_scope_beats_schedule():
    # "帮我下载" matches out_of_scope; should NOT route to schedule even though
    # it could otherwise pattern-match nothing
    s = _empty_session()
    i = classify_intent("帮我下载一下", s)
    assert i.kind == INTENT_OUT_OF_SCOPE
