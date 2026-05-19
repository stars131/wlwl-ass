"""Tests for escalate_worker (Phase-1 stub)."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from llmcore.workers.escalate_worker import (
    EscalateFactory, EscalateWorker, EscalationLog,
    _render_schedule_card, _render_qa_card,
)
from llmcore.worker import InvokeRequest, KernelHandle


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "escalations.jsonl")


@pytest.fixture
def worker(log_path):
    return EscalateWorker(
        name="esc-test", log=EscalationLog(log_path),
        owner_open_id="ou_owner",
    )


def _req(payload: dict) -> InvokeRequest:
    return InvokeRequest(call_id="t", capability="concierge.escalate.v1",
                         payload=payload)


def test_schedule_escalation_writes_log(worker, log_path):
    resp = worker.invoke(_req({
        "kind": "schedule_request",
        "friend_open_id": "ou_zhangsan",
        "friend_alias": "张三",
        "summary": "周三 19:00 国贸 吃饭",
        "proposed_slot": {"start": "2026-05-21T19:00:00", "end": "2026-05-21T20:00:00"},
    }))
    assert resp.ok
    assert resp.result["sent"] is True
    assert resp.result["escalation_id"].startswith("esc_")

    with open(log_path, "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "schedule_request"
    assert row["friend_alias"] == "张三"
    assert row["owner_open_id"] == "ou_owner"
    assert row["card"]["schema"] == "2.0"
    # Buttons present
    actions = row["card"]["body"]["elements"][-1]["actions"]
    decisions = [a["value"]["decision"] for a in actions]
    assert "confirm" in decisions and "decline" in decisions and "reschedule" in decisions


def test_qa_escalation(worker, log_path):
    resp = worker.invoke(_req({
        "kind": "qa_unsure",
        "friend_open_id": "ou_lisi",
        "friend_alias": "李四",
        "summary": "她身份证号多少？",
    }))
    assert resp.ok
    with open(log_path, "r", encoding="utf-8") as f:
        row = json.loads(f.readlines()[-1])
    assert row["kind"] == "qa_unsure"
    # qa cards don't have action buttons
    elements = row["card"]["body"]["elements"]
    assert not any(e.get("tag") == "action" for e in elements)


def test_invalid_kind_rejected(worker):
    resp = worker.invoke(_req({
        "kind": "nuclear_launch",  # not in VALID_KINDS
        "friend_open_id": "ou_x",
        "summary": "test",
    }))
    assert not resp.ok
    assert resp.error["code"] == "payload_invalid"


def test_escalation_ids_unique(worker):
    ids = set()
    for i in range(20):
        resp = worker.invoke(_req({
            "kind": "manual_relay",
            "friend_open_id": "ou_x",
            "summary": f"test {i}",
        }))
        assert resp.ok
        ids.add(resp.result["escalation_id"])
    assert len(ids) == 20


def test_render_schedule_card_shape():
    card = _render_schedule_card(
        friend_alias="张三", summary="吃饭",
        proposed_slot={"start": "2026-05-21T19:00:00", "end": "2026-05-21T20:00:00"},
        escalation_id="esc_test",
    )
    assert card["schema"] == "2.0"
    actions = card["body"]["elements"][-1]["actions"]
    assert len(actions) == 3
    assert all(a["value"]["esc_id"] == "esc_test" for a in actions)


def test_factory_builds_worker(tmp_path):
    handle = KernelHandle(
        worker_name="esc", log=None,  # type: ignore[arg-type]
        now=lambda: 0.0,
        forum_post=lambda *a, **kw: None,
        forum_subscribe=lambda *a, **kw: None,
        verify_token=lambda *a, **kw: True,
    )
    w = EscalateFactory().build(
        {"name": "esc-built", "log_path": str(tmp_path / "e.jsonl"),
         "owner_open_id": "ou_owner"},
        handle,
    )
    assert "concierge.escalate.v1" in w.metadata.capabilities
