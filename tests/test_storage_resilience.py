"""Tests for storage corruption / failure resilience.

ConciergeAgent must keep replying even when:
  - SessionStore.load hits a corrupted JSON file (fall back to fresh session)
  - SessionStore.save hits a disk error (log, return False, never raise)
  - The session file has permission issues
  - The state directory disappears

These are the kinds of failures that ate other bots' uptime in production.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_session_store_load_handles_corrupted_json(tmp_path):
    from llmcore.concierge_agent import SessionStore
    store = SessionStore(str(tmp_path))
    # Write a corrupted session file directly
    path = store._path_for("ou_x")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{this is not, valid: json")
    sess = store.load("ou_x")
    # Fresh session, no crash
    assert sess.open_id == "ou_x"
    assert sess.state == "NEW"


def test_session_store_load_handles_empty_file(tmp_path):
    from llmcore.concierge_agent import SessionStore
    store = SessionStore(str(tmp_path))
    path = store._path_for("ou_x")
    open(path, "w").close()  # empty file
    sess = store.load("ou_x")
    assert sess.open_id == "ou_x"


def test_session_store_load_handles_missing_directory(tmp_path):
    """If the state dir vanished mid-run (someone rm'd it), load still works."""
    from llmcore.concierge_agent import SessionStore
    store = SessionStore(str(tmp_path / "nonexistent" / "deeper"))
    sess = store.load("ou_x")
    assert sess.open_id == "ou_x"


def test_session_store_save_returns_true_on_success(tmp_path):
    from llmcore.concierge_agent import SessionStore, FriendSession
    store = SessionStore(str(tmp_path))
    sess = FriendSession(open_id="ou_x", state="CLOSED")
    assert store.save(sess) is True
    assert os.path.exists(store._path_for("ou_x"))


def test_session_store_save_returns_false_on_disk_error(tmp_path, monkeypatch):
    """When os.replace raises (e.g. disk full, antivirus interference),
    save() returns False instead of letting OSError escape and crash the
    reply path."""
    from llmcore.concierge_agent import SessionStore, FriendSession
    store = SessionStore(str(tmp_path))
    sess = FriendSession(open_id="ou_x")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    assert store.save(sess) is False


def test_session_store_save_cleans_up_tmp_file_on_failure(tmp_path, monkeypatch):
    """tmp file shouldn't accumulate if the final rename failed."""
    from llmcore.concierge_agent import SessionStore, FriendSession
    store = SessionStore(str(tmp_path))
    sess = FriendSession(open_id="ou_x")

    def boom(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr("os.replace", boom)
    store.save(sess)
    # No .tmp leftover
    leftover = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftover == []


def test_agent_keeps_replying_when_save_fails(tmp_path, monkeypatch):
    """End-to-end: friend sends message, save fails, friend still gets a reply."""
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

    sessions = SessionStore(str(tmp_path / "state"))
    # Patch save() to always fail
    monkeypatch.setattr(sessions, "save", lambda s: False)
    cfg = ConciergeConfig(
        rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
        rate_limit_global_max_msgs=1000,
    )
    agent = ConciergeAgent(kernel=k, config=cfg, sessions=sessions)
    reply = agent.handle("ou_x", "你好")
    assert reply  # didn't crash
    reset_kernel()


def test_audit_worker_failure_doesnt_break_replies(tmp_path):
    """If the audit storage path is read-only, the agent should still reply."""
    from llmcore.kernel import reset_kernel, get_kernel
    from llmcore.concierge_agent import (
        ConciergeAgent, ConciergeConfig, SessionStore, RateLimit,
    )
    from llmcore.workers.audit_worker import AuditStorage

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

    # Patch AuditStorage.append to raise — simulates a disk failure.
    with patch.object(AuditStorage, "append", side_effect=OSError("disk full")):
        cfg = ConciergeConfig(
            rate_limit_per_friend=RateLimit(window_s=60, max_msgs=100),
            rate_limit_global_max_msgs=1000,
        )
        agent = ConciergeAgent(
            kernel=k, config=cfg,
            sessions=SessionStore(str(tmp_path / "state")),
        )
        reply = agent.handle("ou_x", "你好")
    assert reply  # the reply path survives a busted audit
    reset_kernel()


def test_kb_worker_handles_corrupted_jsonl(tmp_path):
    """Corrupted lines in concierge_kb.jsonl are skipped, valid ones survive."""
    from llmcore.workers.kb_worker import KBStorage
    kb_path = tmp_path / "kb.jsonl"
    # Mix of valid + garbage + valid
    with open(kb_path, "w", encoding="utf-8") as f:
        f.write('{"topic": "employer", "summary": "Alice works at ABC"}\n')
        f.write('this line is not json\n')
        f.write('{"topic": "hobby", "summary": "Alice likes hiking"}\n')
        f.write('{"missing_topic": true}\n')  # has no "topic" key
    storage = KBStorage(str(kb_path))
    # Two valid rows loaded; corrupted line + no-topic row skipped
    assert len(storage._rows) == 2
    topics = {r["topic"] for r in storage._rows}
    assert topics == {"employer", "hobby"}
