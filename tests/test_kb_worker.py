"""Tests for kb_worker — BM25 lookup + topic-allowlist gating + JSONL upsert/delete."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from llmcore.workers.kb_worker import (
    KBFactory, KBStorage, KBWorker, _tokenize, DEFAULT_VISIBILITY,
)
from llmcore.worker import InvokeRequest, KernelHandle


@pytest.fixture
def kb_path(tmp_path):
    return str(tmp_path / "kb.jsonl")


@pytest.fixture
def worker(kb_path):
    storage = KBStorage(kb_path)
    storage.upsert({"topic": "employer",
                    "summary": "她现在在 ABC 公司做 ML，2025 年开始的。",
                    "long": "Alice 自 2025-08 起在 ABC 公司机器学习平台组担任 SDE-II。"})
    storage.upsert({"topic": "contact_window",
                    "summary": "工作日 19:30 之后比较方便。",
                    "long": "周一到周五晚上 7 点半后她下班；周末白天她会回消息。"})
    storage.upsert({"topic": "id_number",
                    "summary": "身份证号是私人信息。",
                    "long": "身份证 / 银行卡 / 银行账号属于绝对不公开的私人信息。"})
    return KBWorker(name="kb-test", storage=storage, score_floor=0.1)


def _req(payload: dict) -> InvokeRequest:
    return InvokeRequest(call_id="t", capability="concierge.kb_answer.v1",
                         payload=payload)


def test_tokenize_cjk_bigrams():
    toks = _tokenize("吃饭 lunch")
    assert "吃" in toks and "饭" in toks
    assert "吃饭" in toks  # bigram
    assert "lunch" in toks


def test_tokenize_empty():
    assert _tokenize("") == []


def test_kb_match_basic(worker):
    resp = worker.invoke(_req({"q": "她现在在哪个公司"}))
    assert resp.ok
    r = resp.result
    assert r["matched"]
    assert r["topic"] == "employer"
    assert "ABC" in r["text"]
    assert r["in_allowlist"] is True  # no allowlist supplied → True


def test_kb_match_with_long_field_only(worker):
    # "下班" only appears in long, not summary
    resp = worker.invoke(_req({"q": "她平时几点下班"}))
    assert resp.ok
    assert resp.result["matched"]
    assert resp.result["topic"] == "contact_window"


def test_kb_allowlist_gates_match(worker):
    # id_number IS in KB but not allowed → matched=True, in_allowlist=False
    resp = worker.invoke(_req({
        "q": "她身份证号多少",
        "topics_allowed": ["employer", "contact_window"],
    }))
    assert resp.ok
    r = resp.result
    assert r["matched"]
    assert r["topic"] == "id_number"
    assert r["in_allowlist"] is False


def test_kb_allowlist_passes_when_topic_listed(worker):
    resp = worker.invoke(_req({
        "q": "她现在在哪上班",
        "topics_allowed": ["employer"],
    }))
    assert resp.ok
    r = resp.result
    assert r["matched"] and r["in_allowlist"]


def test_kb_no_match(worker):
    # Use a query with zero token overlap (no CJK, no shared latin words)
    # so even at score_floor=0.1 nothing fires.
    resp = worker.invoke(_req({"q": "xyzzy quux frobnicate"}))
    assert resp.ok
    assert resp.result["matched"] is False
    assert resp.result["in_allowlist"] is False


def test_kb_empty_query(worker):
    resp = worker.invoke(_req({"q": ""}))
    assert resp.ok
    assert resp.result["matched"] is False


def test_kb_storage_upsert_and_delete(kb_path):
    s = KBStorage(kb_path)
    s.upsert({"topic": "t1", "summary": "first"})
    s.upsert({"topic": "t1", "summary": "second"})   # overwrite
    rows = s.rows()
    assert len(rows) == 1
    assert rows[0]["summary"] == "second"
    assert rows[0]["visibility"] == DEFAULT_VISIBILITY

    assert s.delete("t1") is True
    assert s.delete("t1") is False  # already gone
    assert s.rows() == []


def test_kb_storage_persists_to_disk(kb_path):
    s1 = KBStorage(kb_path)
    s1.upsert({"topic": "persist", "summary": "hello"})
    # New instance reads from file
    s2 = KBStorage(kb_path)
    rows = s2.rows()
    assert len(rows) == 1
    assert rows[0]["topic"] == "persist"


def test_kb_score_floor_filters_weak_matches(kb_path):
    s = KBStorage(kb_path)
    s.upsert({"topic": "test", "summary": "完全不相关"})
    w = KBWorker(name="kb-strict", storage=s, score_floor=10.0)  # impossibly high
    resp = w.invoke(_req({"q": "完全"}))
    assert resp.ok
    assert resp.result["matched"] is False


def test_kb_factory_builds_worker(tmp_path):
    factory = KBFactory()
    # KernelHandle is required by Worker protocol but not used by kb_worker.
    handle = KernelHandle(
        worker_name="kb", log=None,  # type: ignore[arg-type]
        now=lambda: 0.0,
        forum_post=lambda *a, **kw: None,
        forum_subscribe=lambda *a, **kw: None,
        verify_token=lambda *a, **kw: True,
    )
    worker = factory.build(
        {"name": "kb-built", "kb_path": str(tmp_path / "f.jsonl")},
        handle,
    )
    assert "concierge.kb_answer.v1" in worker.metadata.capabilities
