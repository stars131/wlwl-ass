"""Tests for audit_worker — PII redaction, hashing, append-only JSONL."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from llmcore.workers.audit_worker import (
    AuditFactory, AuditStorage, AuditWorker, _hash_open_id, _redact,
)
from llmcore.worker import InvokeRequest, KernelHandle


@pytest.fixture
def audit_path(tmp_path):
    return str(tmp_path / "audit.jsonl")


@pytest.fixture
def worker(audit_path, tmp_path):
    storage = AuditStorage(audit_path, id_map_path=str(tmp_path / "id_map.json"))
    return AuditWorker(name="audit-test", storage=storage)


def _req(payload: dict) -> InvokeRequest:
    return InvokeRequest(call_id="t", capability="concierge.audit.v1",
                         payload=payload)


def test_redact_phone():
    assert "[redacted-phone]" in _redact("我手机 13912345678 找她")
    assert _redact("ID 1390 123 4567") == "ID 1390 123 4567"  # only matches contiguous 11d
    # Bare 11-digit number gets caught only if it starts with 1
    assert _redact("987654321") == "987654321"


def test_redact_email():
    assert "[redacted-email]" in _redact("发她 alice@example.com 就行")
    assert _redact("no email here") == "no email here"


def test_redact_both():
    out = _redact("电话 13912345678 邮箱 a.b+c@x.co")
    assert "[redacted-phone]" in out
    assert "[redacted-email]" in out


def test_hash_open_id_stable():
    h1 = _hash_open_id("ou_zhangsan")
    h2 = _hash_open_id("ou_zhangsan")
    assert h1 == h2
    assert len(h1) == 12
    assert _hash_open_id("") == ""


def test_hash_open_id_different_inputs_differ():
    assert _hash_open_id("ou_zhangsan") != _hash_open_id("ou_lisi")


def test_audit_writes_row(worker, audit_path):
    resp = worker.invoke(_req({
        "kind": "outbound",
        "friend_open_id": "ou_zhangsan",
        "intent": "schedule",
        "in_text": "下周想找你吃个饭 13912345678",
        "out_text": "好的，alice@example.com 是她的邮箱",
        "state_before": "NEW", "state_after": "PROPOSE_SLOTS",
        "capabilities_used": ["concierge.propose_slot.v1"],
        "latency_ms": 412.0,
    }))
    assert resp.ok
    assert resp.result["written"] is True
    row = resp.result["row"]
    assert row["friend_open_id"] != "ou_zhangsan"
    assert len(row["friend_open_id"]) == 12

    with open(audit_path, "r", encoding="utf-8") as f:
        on_disk = [json.loads(line) for line in f if line.strip()]
    assert len(on_disk) == 1
    written = on_disk[0]
    # PII redacted in stored text
    assert "13912345678" not in written["in_text"]
    assert "[redacted-phone]" in written["in_text"]
    assert "alice@example.com" not in written["out_text"]
    assert "[redacted-email]" in written["out_text"]
    # Friend open_id is hashed, raw value NOT in JSONL row
    assert written["friend_open_id"] != "ou_zhangsan"
    assert "_raw_open_id" not in written


def test_audit_id_map_caches_raw(worker, tmp_path):
    worker.invoke(_req({
        "kind": "inbound", "friend_open_id": "ou_special",
        "in_text": "hi",
    }))
    id_map_path = tmp_path / "id_map.json"
    assert id_map_path.exists()
    data = json.loads(id_map_path.read_text(encoding="utf-8"))
    hashed = _hash_open_id("ou_special")
    assert data.get(hashed) == "ou_special"


def test_audit_append_only(worker, audit_path):
    for i in range(5):
        worker.invoke(_req({"kind": "inbound", "friend_open_id": f"ou_{i}",
                            "in_text": f"msg {i}"}))
    with open(audit_path, "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) == 5
    # Order preserved
    for i, row in enumerate(rows):
        assert f"msg {i}" in (row.get("in_text") or "")


def test_audit_unknown_capability_returns_error(worker):
    req = InvokeRequest(call_id="t", capability="x.y.z", payload={})
    resp = worker.invoke(req)
    assert not resp.ok


def test_audit_factory_builds(tmp_path):
    handle = KernelHandle(
        worker_name="audit", log=None,  # type: ignore[arg-type]
        now=lambda: 0.0,
        forum_post=lambda *a, **kw: None,
        forum_subscribe=lambda *a, **kw: None,
        verify_token=lambda *a, **kw: True,
    )
    w = AuditFactory().build(
        {"name": "audit-built",
         "audit_path": str(tmp_path / "a.jsonl"),
         "id_map_path": str(tmp_path / "i.json")},
        handle,
    )
    assert "concierge.audit.v1" in w.metadata.capabilities
