"""Tests for kernel + workers + voice subsystem.

Run with: pytest tests/test_voice_stack.py -v
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile

import pytest

# Ensure the repo root is importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── Worker contracts ────────────────────────────────────────────────────


def test_captoken_roundtrip():
    from llmcore import captoken

    key = captoken.random_signing_key()
    tok, wire = captoken.mint(
        issuer="kernel", subject="vision",
        capabilities=("vision.ocr.v1", "forum.read.route"),
        signing_key=key, ttl_sec=60,
    )
    parsed = captoken.verify(wire, key)
    assert parsed.subject == "vision"
    assert parsed.covers("vision.ocr.v1")
    assert parsed.covers("vision.ocr.*")
    assert not parsed.covers("audio.tts.v1")


def test_captoken_signature_mismatch():
    from llmcore import captoken
    key1 = captoken.random_signing_key()
    key2 = captoken.random_signing_key()
    _, wire = captoken.mint(
        issuer="k", subject="s", capabilities=("x",),
        signing_key=key1, ttl_sec=60,
    )
    with pytest.raises(ValueError):
        captoken.verify(wire, key2)


def test_capability_registry_loads_builtins():
    from llmcore.capabilities import default_registry, reset_default_registry
    reset_default_registry()
    reg = default_registry()
    ids = [d.id for d in reg.all()]
    assert "calendar.create_event.v1" in ids
    assert "voice.stt.v1" in ids
    assert "voice.tts.v1" in ids
    assert "inspiration.record.v1" in ids


def test_capability_request_validation():
    from llmcore.capabilities import default_registry
    reg = default_registry()
    ok, _ = reg.validate_request(
        "calendar.create_event.v1",
        {"title": "x", "start_at": "2026-05-01"},
    )
    assert ok
    ok, msg = reg.validate_request("calendar.create_event.v1", {"title": "x"})
    assert not ok and "start_at" in msg


# ── Kernel + workers ────────────────────────────────────────────────────


@pytest.fixture
def fresh_kernel(tmp_path):
    from llmcore.kernel import reset_kernel, get_kernel
    reset_kernel()
    k = get_kernel()
    yield k, tmp_path
    reset_kernel()


def test_kernel_factory_list(fresh_kernel):
    k, _ = fresh_kernel
    factory_ids = [f.factory_id for f in k.list_factories()]
    assert "wlwl_ass.workers.calendar" in factory_ids
    assert "wlwl_ass.workers.inspiration" in factory_ids
    assert "wlwl_ass.workers.mock_stt" in factory_ids
    assert "wlwl_ass.workers.mock_tts" in factory_ids


def test_kernel_add_remove_calendar(fresh_kernel):
    k, td = fresh_kernel
    meta = k.add_worker({"name": "cal-1", "kind": "calendar",
                         "db_path": str(td / "cal.db")})
    assert meta.kind == "calendar"
    assert "calendar.create_event.v1" in meta.capabilities
    assert any(w.name == "cal-1" for w in k.list_workers())
    k.remove_worker("cal-1")
    assert not any(w.name == "cal-1" for w in k.list_workers())


def test_kernel_dispatch_calendar_crud(fresh_kernel):
    k, td = fresh_kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    r = k.dispatch(capability="calendar.create_event.v1",
                   payload={"title": "T1", "start_at": "2026-01-01T10:00:00"})
    assert r.ok
    eid = r.result["id"]

    r = k.dispatch(capability="calendar.update_event.v1",
                   payload={"match": {"by_id": eid},
                            "patch": {"title": "T1-renamed"}})
    assert r.ok
    assert r.result["after"]["title"] == "T1-renamed"

    r = k.dispatch(capability="calendar.query_events.v1", payload={})
    assert r.ok
    assert len(r.result["events"]) == 1

    r = k.dispatch(capability="calendar.delete_event.v1",
                   payload={"match": {"by_id": eid}})
    assert r.ok and r.result["deleted"]

    r = k.dispatch(capability="calendar.query_events.v1", payload={})
    assert r.ok
    assert len(r.result["events"]) == 0


def test_kernel_payload_validation_rejects_bad_request(fresh_kernel):
    k, td = fresh_kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    r = k.dispatch(capability="calendar.create_event.v1", payload={"title": "no-start"})
    assert not r.ok
    assert r.error["code"] == "payload_invalid"


def test_kernel_no_worker_for_capability(fresh_kernel):
    k, _ = fresh_kernel
    r = k.dispatch(capability="calendar.create_event.v1",
                   payload={"title": "x", "start_at": "2026-01-01"})
    assert not r.ok
    assert r.error["code"] == "capability_unavailable"


def test_kernel_silence_routing(fresh_kernel):
    k, td = fresh_kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    k.silence("cal", reason="test")
    r = k.dispatch(capability="calendar.create_event.v1",
                   payload={"title": "x", "start_at": "2026-01-01"})
    assert not r.ok
    assert r.error["code"] == "capability_unavailable"
    k.unsilence("cal")
    r = k.dispatch(capability="calendar.create_event.v1",
                   payload={"title": "x", "start_at": "2026-01-01"})
    assert r.ok


def test_kernel_inspiration_record_query(fresh_kernel):
    k, td = fresh_kernel
    k.add_worker({"name": "insp", "kind": "inspiration",
                  "db_path": str(td / "insp.db")})
    r1 = k.dispatch(capability="inspiration.record.v1",
                    payload={"text": "记一下：研究 Tauri"})
    assert r1.ok
    r2 = k.dispatch(capability="inspiration.list_recent.v1", payload={})
    assert r2.ok
    assert len(r2.result["notes"]) == 1
    # verb stripping
    assert "记一下" not in r2.result["notes"][0]["text"]


def test_kernel_audit_topic_records_dispatch(fresh_kernel):
    k, td = fresh_kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    k.dispatch(capability="calendar.query_events.v1", payload={})
    audit = k.forum.tail("audit", reader="kernel", n=10)
    types = [m.type for m in audit]
    assert "worker_registered" in types
    assert "dispatch" in types


# ── Forum bus ──────────────────────────────────────────────────────────


def test_forum_kernel_only_topic_blocks_workers():
    from llmcore.forum import ForumBus
    bus = ForumBus()
    with pytest.raises(PermissionError):
        bus.publish("control", {"x": 1}, author="vision")


def test_forum_global_topic_allows_anyone():
    from llmcore.forum import ForumBus
    bus = ForumBus()
    msg = bus.publish("lounge", {"x": 1}, author="vision")
    assert msg.seq == 1


def test_forum_subscribe_callback_receives_post():
    from llmcore.forum import ForumBus
    bus = ForumBus()
    received = []
    bus.subscribe("lounge", subscriber="reader", callback=received.append)
    bus.publish("lounge", {"hello": "world"}, author="speaker")
    assert len(received) == 1


# ── Voice subsystem ────────────────────────────────────────────────────


def test_wake_matcher_basic():
    from voice.wake import PhraseMatcher
    pm = PhraseMatcher(["我对三体世界说话"])
    assert pm.feed("我对三体世界说话") is not None
    assert pm.feed("早晨好") is None
    assert pm.feed("我对三体世界说话", conf=0.4) is None  # below threshold


def test_rule_intent_classifier_covers_main_cases():
    from voice.intent import RuleIntentClassifier, CHAT_INTENT
    ic = RuleIntentClassifier()
    cases = [
        ("记一下：要研究 Tauri 2 的更新机制", "inspiration.record.v1"),
        ("明天下午三点提醒我去拿快递", "calendar.create_event.v1"),
        ("把咖啡会议改到四点", "calendar.update_event.v1"),
        ("明天有什么安排", "calendar.query_events.v1"),
        ("删除明天的咖啡会议", "calendar.delete_event.v1"),
        ("天气怎么样", CHAT_INTENT),
        ("找一下关于 Tauri 的笔记", "inspiration.query.v1"),
    ]
    for utt, expected in cases:
        r = ic.classify(utt)
        assert r.intent == expected, f"{utt!r} -> {r.intent}, want {expected}"


def test_voice_session_persists_manifest(tmp_path):
    from voice.storage import VoiceSession, list_sessions, TurnEvent
    sess = VoiceSession(root=str(tmp_path))
    sess.append_audio(b"\x00" * 100)
    sess.append_event(TurnEvent(role="user", kind="stt", text="hi"))
    sess.set_exit_reason("test_done")
    sess.close()

    listed = list_sessions(root=str(tmp_path))
    assert len(listed) == 1
    assert listed[0]["manifest"]["exit_reason"] == "test_done"


def test_voice_session_forget_drops_dir(tmp_path):
    from voice.storage import VoiceSession, list_sessions
    sess = VoiceSession(root=str(tmp_path))
    sess.append_audio(b"x" * 10)
    sess.mark_forget()
    sess.close()
    assert list_sessions(root=str(tmp_path)) == []


# ── End-to-end orchestrator ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_voice_loop_e2e(fresh_kernel, tmp_path):
    """Canned-STT walks: wake → create event → record note → exit."""
    from voice.intent import RuleIntentClassifier
    from voice.orchestrator import (
        ConversationOrchestrator, OrchestratorConfig, OrchestratorHooks,
        State,
    )
    from voice.storage import VoiceSession

    k, td = fresh_kernel
    k.add_worker({"name": "cal", "kind": "calendar", "db_path": str(td / "cal.db")})
    k.add_worker({"name": "insp", "kind": "inspiration", "db_path": str(td / "insp.db")})
    k.add_worker({"name": "stt", "kind": "mock_stt", "canned_responses": [
        "我对三体世界说话",
        "明天下午三点提醒我去拿快递",
        "记一下：研究 Tauri 2 的更新机制",
        "拜拜",
    ]})
    k.add_worker({"name": "tts", "kind": "mock_tts"})

    rule = RuleIntentClassifier()

    async def transcribe(blob):
        r = k.dispatch(capability="voice.stt.v1",
                       payload={"audio": base64.b64encode(blob).decode("ascii"),
                                "format": "opus", "sample_rate": 16000})
        if not r.ok:
            return ("", 0.0, True)
        return (r.result["text"], r.result.get("confidence", 0.9),
                r.result.get("is_final", True))

    async def classify(text):
        return rule.classify(text)

    async def handle(intent):
        if intent.intent == "_chat":
            return "好的"
        r = k.dispatch(capability=intent.intent, payload=intent.args)
        if not r.ok:
            return "失败"
        return "好的"

    async def synthesize(text):
        async for r in k.dispatch_stream(capability="voice.tts.v1",
                                          payload={"text": text}):
            if r.ok and r.result and r.result.get("audio_chunk"):
                yield base64.b64decode(r.result["audio_chunk"])

    events = []
    audio = []

    async def out(ev): events.append(ev)
    async def out_audio(ch, seq, turn): audio.append((seq, len(ch)))

    sess = VoiceSession(root=str(tmp_path / "sessions"))
    cfg = OrchestratorConfig(vad_silence_ms=10, session_idle_timeout_s=99999)
    orch = ConversationOrchestrator(
        config=cfg, hooks=OrchestratorHooks(transcribe, classify, handle, synthesize),
        out=out, out_audio=out_audio, session=sess,
    )
    await orch.start()

    # Wake (~2s of audio)
    await orch.feed_audio(b"\x00" * 33000)
    # Three more utterances; each gets the next canned STT response.
    for _ in range(3):
        await orch.feed_audio(b"\x01" * 5000)
        await asyncio.sleep(0.05)
        await orch.tick()

    await orch.shutdown(reason="test_done")

    states = [e["value"] for e in events if e.get("type") == "state"]
    assert State.ARMED in states
    assert State.LISTENING in states
    assert State.PROCESSING in states
    assert State.RESPONDING in states

    intents = [e for e in events if e.get("type") == "intent"]
    intent_ids = {i["intent"] for i in intents}
    assert "calendar.create_event.v1" in intent_ids
    assert "inspiration.record.v1" in intent_ids

    cal = k.dispatch(capability="calendar.query_events.v1", payload={})
    assert cal.ok and len(cal.result["events"]) == 1

    notes = k.dispatch(capability="inspiration.list_recent.v1", payload={})
    assert notes.ok and len(notes.result["notes"]) == 1
