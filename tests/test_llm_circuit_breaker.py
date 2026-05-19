"""Tests for the LLM hook circuit breaker.

After 3 consecutive failures, each hook's breaker OPENS — subsequent calls
within a 60 s cooldown short-circuit to None without paying the LLM
round-trip. This protects friend-facing latency when the LLM endpoint
goes down: the bot keeps replying, just via rule-based fallback.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture
def stack(tmp_path):
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, LLMHooks, SessionStore, RateLimit,
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

    # Inject a controllable clock so we can test cooldown without sleeping.
    clock = [1_000_000.0]

    def fake_now():
        return clock[0]

    cfg = ConciergeConfig(
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    chat = MagicMock(side_effect=RuntimeError("LLM down"))
    classify = MagicMock(side_effect=RuntimeError("LLM down"))
    render_slots = MagicMock(side_effect=RuntimeError("LLM down"))
    hooks = LLMHooks(chat=chat, classify=classify, render_slots=render_slots)
    agent = ConciergeAgent(
        kernel=k, config=cfg,
        sessions=SessionStore(str(tmp_path / "state")),
        llm_hooks=hooks, now_fn=fake_now,
    )
    yield {"agent": agent, "hooks": hooks, "clock": clock,
           "chat": chat, "classify": classify, "render_slots": render_slots}
    reset_kernel()


def test_breaker_opens_after_three_failures(stack):
    """After 3 raises, the 4th call is short-circuited (chat NOT invoked)."""
    agent = stack["agent"]
    chat = stack["chat"]
    # Each handle() with smalltalk text → invokes chat once
    for i in range(3):
        agent.handle(f"ou_{i}", "今天天气不错")
    assert chat.call_count == 3
    # 4th friend's message: breaker should now be OPEN
    agent.handle("ou_4th", "今天天气不错")
    assert chat.call_count == 3  # did NOT increment


def test_breaker_closes_after_cooldown(stack):
    agent = stack["agent"]
    chat = stack["chat"]
    clock = stack["clock"]
    for i in range(3):
        agent.handle(f"ou_{i}", "今天天气不错")
    assert chat.call_count == 3
    # Advance clock past 60s cooldown
    clock[0] += 61.0
    # First call after cooldown lets the LLM try again (half-open trial)
    chat.side_effect = lambda *a, **kw: "嗯嗯，你说"  # success
    reply = agent.handle("ou_after", "今天天气不错")
    assert chat.call_count == 4
    assert reply == "嗯嗯，你说"


def test_breaker_stays_open_on_failed_half_open_trial(stack):
    agent = stack["agent"]
    chat = stack["chat"]
    clock = stack["clock"]
    for i in range(3):
        agent.handle(f"ou_{i}", "今天天气不错")
    clock[0] += 61.0
    # Half-open trial fails again
    agent.handle("ou_trial", "今天天气不错")
    assert chat.call_count == 4  # the half-open trial DID fire
    # Subsequent call within new cooldown is short-circuited again
    agent.handle("ou_post_trial", "今天天气不错")
    assert chat.call_count == 4


def test_breakers_are_independent_per_hook(stack):
    """Failures on 'chat' shouldn't open the 'classify' breaker."""
    agent = stack["agent"]
    chat = stack["chat"]
    classify = stack["classify"]
    # Burn 3 chat failures
    for i in range(3):
        agent.handle(f"ou_{i}", "今天天气不错")
    assert chat.call_count == 3
    # classify hook should still be live (called when rule says smalltalk)
    agent.handle("ou_classify_test", "嗯嗯")
    # The classify hook should have been invoked — independent breaker
    assert classify.call_count >= 1


def test_breaker_returns_none_does_not_crash(stack):
    """Even with breaker OPEN, agent.handle() returns a sensible reply
    via the rule fallback."""
    agent = stack["agent"]
    for i in range(5):
        reply = agent.handle(f"ou_{i}", "今天天气不错")
        assert reply  # always replies, never crashes
