"""Tests for the concierge LLM live self-test CLI.

We don't hit a real LLM here — instead we mock _build_llm_hooks to return
a controllable LLMHooks bundle and verify the CLI's check logic catches
the right failure modes.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _make_hooks(*,
                chat=lambda u, s: "嗯嗯，你说",
                classify=lambda u, s: {"intent": "schedule", "payload": {}},
                extract_schedule=lambda u, s: {
                    "duration_minutes": 30,
                    "preferred_window": "evening",
                    "topic_summary": "晚饭",
                },
                render_slots=lambda slots, t, a: "19:00 或 20:00 都行",
                render_qa=lambda q, k, t: "她在 ABC 公司哦～"):
    from llmcore.concierge_agent import LLMHooks
    return LLMHooks(
        chat=chat, classify=classify,
        extract_schedule=extract_schedule,
        render_slots=render_slots, render_qa=render_qa,
    )


def test_returns_failure_when_no_llm_configured():
    from launcher import cli_concierge_llm_test as cli
    with patch.object(cli, "_build_hooks_for_test", return_value=(None, "no creds")):
        ok, results = cli.run_all()
    assert ok is False
    assert results[0].name == "build"
    assert results[0].ok is False


def test_passes_when_all_hooks_return_well_formed():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks()
    with patch.object(cli, "_build_hooks_for_test",
                      return_value=(hooks, "test-session")):
        ok, results = cli.run_all()
    assert ok is True
    # build + 5 hooks
    assert {r.name for r in results} == {
        "build", "chat", "classify", "extract_schedule",
        "render_slots", "render_qa",
    }


def test_detects_classify_returns_wrong_intent():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks(classify=lambda u, s: {"intent": "smalltalk"})
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        ok, results = cli.run_all(only=["classify"])
    assert ok is False
    classify_r = next(r for r in results if r.name == "classify")
    assert classify_r.ok is False
    assert "schedule" in classify_r.detail.lower()


def test_detects_render_slots_hides_options():
    from launcher import cli_concierge_llm_test as cli
    # LLM hides one of the slot times → validator-style check fails
    hooks = _make_hooks(render_slots=lambda slots, t, a: "她只能 19:00 那一个")
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        ok, results = cli.run_all(only=["render_slots"])
    assert ok is False
    r = next(r for r in results if r.name == "render_slots")
    assert "missing" in r.detail.lower()


def test_detects_render_qa_ignores_source():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks(render_qa=lambda q, k, t: "我不知道")
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        ok, results = cli.run_all(only=["render_qa"])
    assert ok is False
    r = next(r for r in results if r.name == "render_qa")
    assert "ABC" in r.detail


def test_handles_hook_exceptions():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks(chat=MagicMock(side_effect=RuntimeError("network down")))
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        ok, results = cli.run_all(only=["chat"])
    assert ok is False
    r = next(r for r in results if r.name == "chat")
    assert "RuntimeError" in r.detail


def test_only_filter_runs_subset():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks()
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        _, results = cli.run_all(only=["classify"])
    names = {r.name for r in results}
    assert names == {"build", "classify"}


def test_unknown_hook_filter_reports_error():
    from launcher import cli_concierge_llm_test as cli
    hooks = _make_hooks()
    with patch.object(cli, "_build_hooks_for_test", return_value=(hooks, "x")):
        ok, results = cli.run_all(only=["bogus"])
    assert ok is False
    r = next(r for r in results if r.name == "bogus")
    assert "unknown hook" in r.detail.lower()
