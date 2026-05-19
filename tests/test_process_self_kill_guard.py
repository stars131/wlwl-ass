"""Tests for ``WlwlAssHandler.do_process`` self-kill guard.

Locks in the fix for the 2026-05-17 turn-23 incident where the OWNER bot's
agent ran ``process kill bot:feishu`` and silently killed itself. The agent
must refuse to kill its own pid or its own ``bot:$WLWL_BOT_KEY`` label.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agent_loop import exhaust  # noqa: E402


@pytest.fixture
def handler():
    from wlwl_ass import WlwlAssHandler
    # do_process doesn't touch self.parent / working state — passing a
    # placeholder is enough to exercise it in isolation.
    return WlwlAssHandler(parent=None)


def _run(gen):
    """Drain a generator, return its StepOutcome."""
    return exhaust(gen)


def test_kill_self_pid_is_refused(handler, monkeypatch):
    """Agent can't kill its own process even if it tries."""
    self_pid = os.getpid()
    gen = handler.do_process({"action": "kill", "pid": self_pid}, response=None)
    outcome = _run(gen)
    assert outcome.data["error"] == "self_kill_refused"
    assert outcome.data["pid"] == self_pid
    assert "agent" in outcome.next_prompt and "kill" in outcome.next_prompt.lower()


def test_kill_self_bot_label_is_refused(handler, monkeypatch):
    """Agent running inside ``bot:feishu`` refuses ``kill bot:feishu``."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu")
    gen = handler.do_process(
        {"action": "kill", "label": "bot:feishu", "force": False},
        response=None,
    )
    outcome = _run(gen)
    assert outcome.data["error"] == "self_kill_refused"
    assert outcome.data["label"] == "bot:feishu"


def test_kill_other_bot_label_is_allowed_when_target_dead(handler, monkeypatch, tmp_path):
    """The guard ONLY refuses self-kill — other bots / labels still go
    through reg.kill (which returns 'no such label' for a non-existent
    target; we just verify the guard didn't short-circuit)."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu")
    # Point process_registry at an empty temp path so kill returns false
    from launcher import process_registry as pr
    fake_reg = pr.ProcessRegistry(str(tmp_path / "registry.json"))
    with patch.object(pr, "get_registry", return_value=fake_reg):
        gen = handler.do_process(
            {"action": "kill", "label": "bot:feishu_concierge"},
            response=None,
        )
        outcome = _run(gen)
    # NOT a self_kill_refused — the agent let it pass through to the
    # registry, which simply reports no such label.
    assert outcome.data.get("error") != "self_kill_refused"


def test_kill_without_bot_key_env_does_not_guard_labels(handler, monkeypatch, tmp_path):
    """When the agent isn't running inside a bot process (no WLWL_BOT_KEY
    set, e.g. via the CLI), the label guard is inert. Only pid==self-pid
    still triggers."""
    monkeypatch.delenv("WLWL_BOT_KEY", raising=False)
    from launcher import process_registry as pr
    fake_reg = pr.ProcessRegistry(str(tmp_path / "registry.json"))
    with patch.object(pr, "get_registry", return_value=fake_reg):
        gen = handler.do_process(
            {"action": "kill", "label": "bot:feishu"},
            response=None,
        )
        outcome = _run(gen)
    assert outcome.data.get("error") != "self_kill_refused"


# ── cross-bot path-write fence ────────────────────────────────────────

def test_cross_bot_write_refused_when_in_bot(handler, monkeypatch, tmp_path):
    """Owner-bot agent tries to overwrite concierge state — refused."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu")
    handler.project_root = str(tmp_path)
    # The fence treats paths under tmp/temp/concierge_state/ as owned
    # by bot:feishu_concierge, so a write attempt by bot:feishu fails.
    target = os.path.join(str(tmp_path), "temp", "concierge_state", "ou_x.json")
    reason = handler._check_cross_bot_write(target)
    assert reason
    assert "feishu_concierge" in reason
    assert "feishu" in reason


def test_cross_bot_write_allowed_for_own_bot(handler, monkeypatch, tmp_path):
    """Concierge-bot agent writing into its own state — allowed."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu_concierge")
    handler.project_root = str(tmp_path)
    target = os.path.join(str(tmp_path), "temp", "concierge_state", "ou_x.json")
    reason = handler._check_cross_bot_write(target)
    assert reason == ""


def test_cross_bot_write_allowed_without_env(handler, monkeypatch, tmp_path):
    """No WLWL_BOT_KEY set → CLI / test mode → fence is inert."""
    monkeypatch.delenv("WLWL_BOT_KEY", raising=False)
    handler.project_root = str(tmp_path)
    target = os.path.join(str(tmp_path), "temp", "concierge_state", "ou_x.json")
    assert handler._check_cross_bot_write(target) == ""


def test_cross_bot_write_override_via_env(handler, monkeypatch, tmp_path):
    """WLWL_CROSS_BOT_WRITE=1 lets the agent bypass the fence (rare,
    e.g. a migration tool that intentionally edits another bot's data)."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu")
    monkeypatch.setenv("WLWL_CROSS_BOT_WRITE", "1")
    handler.project_root = str(tmp_path)
    target = os.path.join(str(tmp_path), "temp", "concierge_state", "ou_x.json")
    assert handler._check_cross_bot_write(target) == ""


def test_cross_bot_write_ignores_unrelated_paths(handler, monkeypatch, tmp_path):
    """Generic project files (not in the bot-owned registry) are always allowed."""
    monkeypatch.setenv("WLWL_BOT_KEY", "feishu")
    handler.project_root = str(tmp_path)
    target = os.path.join(str(tmp_path), "docs", "notes.md")
    assert handler._check_cross_bot_write(target) == ""
