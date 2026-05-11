"""Tests for the per-open_id Feishu agent isolation in frontends/fsapp.py.

Run with: pytest tests/test_fsapp_isolation.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# fsapp imports lark_oapi at module level — skip the suite cleanly when missing.
pytest.importorskip("lark_oapi")
fsapp = pytest.importorskip("frontends.fsapp")


class _FakeAgent:
    """Stand-in for GeneraticAgent. `run()` blocks on an event so the
    thread spawned by `_get_agent` stays alive (mirrors the real agent's
    long-lived task loop). `abort()` releases it."""

    def __init__(self):
        self._stop = threading.Event()
        backends = [mock.MagicMock(), mock.MagicMock()]
        for b in backends:
            b.extra_sys_prompt = ""
        self.llmclients = [mock.MagicMock(backend=b) for b in backends]
        self.abort = mock.MagicMock(side_effect=self._stop.set)

    def run(self):
        self._stop.wait()


@pytest.fixture(autouse=True)
def _reset_state():
    saved_sys = fsapp.SYSTEM_PROMPT
    saved_user = dict(fsapp.USER_PROMPTS)
    yield
    # Release any blocked fake-agent threads so the test process exits cleanly.
    with fsapp._agent_lock:
        for slot in list(fsapp._agent_slots.values()):
            stop = getattr(slot.agent, "_stop", None)
            if stop is not None:
                stop.set()
        fsapp._agent_slots.clear()
    fsapp.SYSTEM_PROMPT = saved_sys
    fsapp.USER_PROMPTS = dict(saved_user)


def test_resolve_prompt_priority():
    fsapp.SYSTEM_PROMPT = "GLOBAL"
    fsapp.USER_PROMPTS = {"ou_alice": "ALICE_PROMPT"}
    assert "ALICE_PROMPT" in fsapp._resolve_extra_prompt("ou_alice")
    assert "GLOBAL" in fsapp._resolve_extra_prompt("ou_bob")
    fsapp.SYSTEM_PROMPT = ""
    fsapp.USER_PROMPTS = {}
    assert fsapp._resolve_extra_prompt("ou_anyone") == ""


def test_apply_prompt_writes_to_all_backends():
    a = _FakeAgent()
    fsapp._apply_prompt(a, "TEST_PROMPT")
    for c in a.llmclients:
        assert c.backend.extra_sys_prompt == "TEST_PROMPT"


def test_same_open_id_returns_same_agent():
    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        a1 = fsapp._get_agent("ou_alice")
        a2 = fsapp._get_agent("ou_alice")
    assert a1 is a2
    assert len(fsapp._agent_slots) == 1


def test_different_open_id_returns_different_agents():
    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        a = fsapp._get_agent("ou_alice")
        b = fsapp._get_agent("ou_bob")
    assert a is not b
    assert set(fsapp._agent_slots) == {"ou_alice", "ou_bob"}


def test_get_agent_applies_user_prompt():
    fsapp.SYSTEM_PROMPT = "DEFAULT"
    fsapp.USER_PROMPTS = {"ou_alice": "ALICE"}
    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        alice = fsapp._get_agent("ou_alice")
        bob = fsapp._get_agent("ou_bob")
    for c in alice.llmclients:
        assert "ALICE" in c.backend.extra_sys_prompt
    for c in bob.llmclients:
        assert "DEFAULT" in c.backend.extra_sys_prompt


def test_get_agent_concurrent_creates_one():
    """50 threads racing on the same open_id end up with one agent + one slot."""
    results: list[object] = [None] * 50
    barrier = threading.Barrier(50)

    def worker(i):
        barrier.wait()  # release everyone simultaneously to maximise contention
        results[i] = fsapp._get_agent("ou_race")

    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()

    first = results[0]
    assert all(r is first for r in results)
    assert len(fsapp._agent_slots) == 1


def test_reaper_removes_stale_slots():
    """Backdate a slot beyond IDLE_TIMEOUT_S, run one sweep inline."""
    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        a = fsapp._get_agent("ou_stale")
    fsapp._agent_slots["ou_stale"].last_used_ts = time.time() - fsapp.IDLE_TIMEOUT_S - 10

    # Inline sweep mirroring _reap_loop's body — avoids the 5-min sleep.
    now = time.time()
    with fsapp._agent_lock:
        stale = [k for k, s in fsapp._agent_slots.items()
                 if now - s.last_used_ts > fsapp.IDLE_TIMEOUT_S]
        for k in stale:
            try: fsapp._agent_slots[k].agent.abort()
            except Exception: pass
            fsapp._agent_slots.pop(k, None)

    assert "ou_stale" not in fsapp._agent_slots
    a.abort.assert_called_once()


def test_get_agent_empty_prompt_when_unconfigured():
    fsapp.SYSTEM_PROMPT = ""
    fsapp.USER_PROMPTS = {}
    with mock.patch.object(fsapp, "GeneraticAgent", _FakeAgent):
        a = fsapp._get_agent("ou_noconfig")
    for c in a.llmclients:
        assert c.backend.extra_sys_prompt == ""
