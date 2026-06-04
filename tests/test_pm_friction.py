"""Tests for tools/pm_friction_miner.py.

Activity logs are constructed by writing fake jsonl day files into a tmp
``WLWL_ACTIVITY_LOG_DIR``, so we exercise the full pipeline (file IO →
signal detection → clustering) without spinning up the agent or recording
real events.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture
def tmp_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_ACTIVITY_LOG_DIR", str(tmp_path / "activity"))
    monkeypatch.setenv("WLWL_ACTIVITY_LOG_OFF", "1")  # safety: nothing else writes here
    (tmp_path / "activity").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_day(tmp_path, day: str, events: list[dict]) -> None:
    path = tmp_path / "activity" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _today() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _recent_days(count: int) -> list[str]:
    today = _dt.datetime.now().date()
    start = today - _dt.timedelta(days=max(0, count - 1))
    return [
        (start + _dt.timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(count)
    ]


# ── Signal extraction ─────────────────────────────────────────────────


def test_retry_burst_detected_when_same_tool_called_three_times(tmp_activity):
    from tools import pm_friction_miner

    day = _today()
    events = [
        {"ts": "2026-05-13T00:00:00Z", "phase": "tool_start", "turn": 1, "tool": "file_patch"},
        {"ts": "2026-05-13T00:00:01Z", "phase": "tool_end", "turn": 1, "tool": "file_patch", "status": "success"},
        {"ts": "2026-05-13T00:00:02Z", "phase": "tool_start", "turn": 1, "tool": "file_patch"},
        {"ts": "2026-05-13T00:00:03Z", "phase": "tool_end", "turn": 1, "tool": "file_patch", "status": "success"},
        {"ts": "2026-05-13T00:00:04Z", "phase": "tool_start", "turn": 1, "tool": "file_patch"},
        {"ts": "2026-05-13T00:00:05Z", "phase": "tool_end", "turn": 1, "tool": "file_patch", "status": "success"},
        {"ts": "2026-05-13T00:00:06Z", "phase": "turn_end", "turn": 1, "summary": "ok"},
    ]
    _write_day(tmp_activity, day, events)
    signals = pm_friction_miner.collect_signals(days_window=1)
    bursts = [s for s in signals if s.signal_type == "retry_burst"]
    assert len(bursts) == 1
    assert bursts[0].tool == "file_patch"
    assert bursts[0].detail["count"] == 3


def test_error_retry_signal(tmp_activity):
    from tools import pm_friction_miner

    day = _today()
    events = [
        {"ts": "2026-05-13T00:00:00Z", "phase": "tool_start", "turn": 1, "tool": "code_run"},
        {"ts": "2026-05-13T00:00:01Z", "phase": "tool_end", "turn": 1, "tool": "code_run", "status": "error"},
        {"ts": "2026-05-13T00:00:02Z", "phase": "tool_start", "turn": 1, "tool": "code_run"},
        {"ts": "2026-05-13T00:00:03Z", "phase": "tool_end", "turn": 1, "tool": "code_run", "status": "success"},
        {"ts": "2026-05-13T00:00:04Z", "phase": "turn_end", "turn": 1, "summary": "ok"},
    ]
    _write_day(tmp_activity, day, events)
    signals = pm_friction_miner.collect_signals(days_window=1)
    error_retries = [s for s in signals if s.signal_type == "error_retry"]
    assert len(error_retries) == 1
    assert error_retries[0].tool == "code_run"
    assert "error" in error_retries[0].detail["prev_status"]


def test_long_turn_signal(tmp_activity):
    from tools import pm_friction_miner

    day = _today()
    events = []
    for i in range(pm_friction_miner.LONG_TURN_THRESHOLD):
        events.append({"ts": f"2026-05-13T00:00:{i:02d}Z", "phase": "tool_start", "turn": 1, "tool": f"t{i}"})
        events.append({"ts": f"2026-05-13T00:00:{i:02d}Z", "phase": "tool_end", "turn": 1, "tool": f"t{i}", "status": "success"})
    events.append({"ts": "2026-05-13T00:30:00Z", "phase": "turn_end", "turn": 1})
    _write_day(tmp_activity, day, events)
    signals = pm_friction_miner.collect_signals(days_window=1)
    longs = [s for s in signals if s.signal_type == "long_turn"]
    assert len(longs) == 1
    assert longs[0].detail["tool_calls"] >= pm_friction_miner.LONG_TURN_THRESHOLD


def test_bad_exit_signal(tmp_activity):
    from tools import pm_friction_miner

    day = _today()
    events = [
        {"ts": "2026-05-13T00:00:00Z", "phase": "turn_end", "turn": 1, "summary": "x",
         "exit_reason": {"why": "user_abort"}},
    ]
    _write_day(tmp_activity, day, events)
    signals = pm_friction_miner.collect_signals(days_window=1)
    bad = [s for s in signals if s.signal_type == "bad_exit"]
    assert len(bad) == 1
    assert bad[0].detail["reason"] == "abort"


def test_clean_turn_emits_no_signals(tmp_activity):
    from tools import pm_friction_miner

    day = _today()
    events = [
        {"ts": "2026-05-13T00:00:00Z", "phase": "tool_start", "turn": 1, "tool": "file_read"},
        {"ts": "2026-05-13T00:00:01Z", "phase": "tool_end", "turn": 1, "tool": "file_read", "status": "success"},
        {"ts": "2026-05-13T00:00:02Z", "phase": "turn_end", "turn": 1, "summary": "ok"},
    ]
    _write_day(tmp_activity, day, events)
    assert pm_friction_miner.collect_signals(days_window=1) == []


# ── Clustering + provenance gate ──────────────────────────────────────


def test_provenance_gate_drops_clusters_with_too_few_runs(tmp_activity):
    """A retry_burst that only appears in one run should NOT survive
    ``min_distinct_runs=3``."""
    from tools import pm_friction_miner

    day = _today()
    # Single run with one retry burst — three tool_start of same tool same turn
    events = []
    for i in range(3):
        events.append({"ts": f"2026-05-13T00:00:0{i}Z", "phase": "tool_start", "turn": 1, "tool": "file_patch"})
        events.append({"ts": f"2026-05-13T00:00:0{i}Z", "phase": "tool_end", "turn": 1, "tool": "file_patch", "status": "success"})
    events.append({"ts": "2026-05-13T00:01:00Z", "phase": "turn_end", "turn": 1})
    _write_day(tmp_activity, day, events)

    signals = pm_friction_miner.collect_signals(days_window=1)
    assert any(s.signal_type == "retry_burst" for s in signals)

    clusters = pm_friction_miner.cluster_signals(signals, min_distinct_runs=3)
    assert clusters == []


def test_provenance_gate_passes_when_three_runs_show_same_pattern(tmp_activity):
    from tools import pm_friction_miner

    # Three independent runs on three different days, each with a retry burst
    # on file_patch.
    for day in _recent_days(3):
        events = []
        for j in range(3):
            events.append({"ts": f"{day}T00:00:0{j}Z", "phase": "tool_start", "turn": 1, "tool": "file_patch"})
            events.append({"ts": f"{day}T00:00:0{j}Z", "phase": "tool_end", "turn": 1, "tool": "file_patch", "status": "success"})
        events.append({"ts": f"{day}T00:01:00Z", "phase": "turn_end", "turn": 1})
        _write_day(tmp_activity, day, events)

    signals = pm_friction_miner.collect_signals(days_window=10)
    clusters = pm_friction_miner.cluster_signals(signals, min_distinct_runs=3)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.signal_type == "retry_burst"
    assert c.tool == "file_patch"
    assert len(c.distinct_runs) >= 3


def test_scan_returns_full_structure(tmp_activity):
    from tools import pm_friction_miner

    # Quick smoke: build minimal evidence, call scan(), assert shape.
    for day in _recent_days(3):
        _write_day(tmp_activity, day, [
            *[{"ts": f"{day}T00:00:0{j}Z", "phase": "tool_start", "turn": 1, "tool": "x"} for j in range(3)],
            {"ts": f"{day}T00:01:00Z", "phase": "turn_end", "turn": 1},
        ])

    out = pm_friction_miner.scan(days_window=10, min_distinct_runs=3)
    assert out["signal_total"] >= 3
    assert out["cluster_count_qualified"] == 1
    assert out["clusters"][0]["tool"] == "x"
    assert out["clusters"][0]["distinct_run_count"] >= 3


# ── Accept-rate tracking + cadence ────────────────────────────────────


@pytest.fixture
def tmp_pm_paths(tmp_path, monkeypatch):
    from tools import pm_friction_miner

    monkeypatch.setattr(pm_friction_miner, "_project_root", lambda: tmp_path)
    (tmp_path / "temp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_accept_rate_with_no_log_returns_none(tmp_pm_paths):
    from tools import pm_friction_miner

    stats = pm_friction_miner.compute_accept_rate(track="A")
    assert stats["rate"] is None
    assert stats["accepted"] == 0
    assert stats["rejected"] == 0


def test_accept_rate_counts_only_decided(tmp_pm_paths):
    from tools import pm_friction_miner

    pm_friction_miner.log_proposal({"id": "PM-A-0001", "title": "a", "evidence_count": 3})
    pm_friction_miner.log_proposal({"id": "PM-A-0002", "title": "b", "evidence_count": 3})
    pm_friction_miner.log_proposal({"id": "PM-A-0003", "title": "c", "evidence_count": 3})
    pm_friction_miner.record_decision("PM-A-0001", status="accepted")
    pm_friction_miner.record_decision("PM-A-0002", status="rejected")
    pm_friction_miner.record_decision("PM-A-0003", status="deferred")
    stats = pm_friction_miner.compute_accept_rate(track="A")
    assert stats["accepted"] == 1
    assert stats["rejected"] == 1
    assert stats["deferred"] == 1
    assert stats["rate"] == 0.5


def test_cadence_blocks_when_within_interval(tmp_pm_paths):
    from tools import pm_friction_miner

    now = _dt.datetime.now()
    just_now = (now - _dt.timedelta(hours=6)).isoformat(timespec="seconds")
    pm_friction_miner.mark_run(track="A", when=just_now)
    cadence = pm_friction_miner.check_cadence(track="A", now=now)
    assert cadence["allowed"] is False
    assert cadence["mode"] == "blocked"


def test_cadence_allows_when_interval_passed(tmp_pm_paths):
    from tools import pm_friction_miner

    now = _dt.datetime.now()
    old = (now - _dt.timedelta(days=3)).isoformat(timespec="seconds")
    pm_friction_miner.mark_run(track="A", when=old)
    cadence = pm_friction_miner.check_cadence(track="A", now=now)
    assert cadence["allowed"] is True
    assert cadence["mode"] == "normal"


def test_cadence_switches_to_reflection_when_accept_rate_low(tmp_pm_paths):
    from tools import pm_friction_miner

    # 1 accepted, 9 rejected → rate 0.1 < 0.3 floor
    for i in range(1, 11):
        pm_friction_miner.log_proposal({"id": f"PM-A-{i:04d}", "title": "x", "evidence_count": 3})
    pm_friction_miner.record_decision("PM-A-0001", status="accepted")
    for i in range(2, 11):
        pm_friction_miner.record_decision(f"PM-A-{i:04d}", status="rejected")

    now = _dt.datetime.now()
    old = (now - _dt.timedelta(days=40)).isoformat(timespec="seconds")  # past throttled interval
    pm_friction_miner.mark_run(track="A", when=old)
    cadence = pm_friction_miner.check_cadence(track="A", now=now)
    assert cadence["allowed"] is True
    assert cadence["mode"] == "reflection"
    assert cadence["min_interval_days"] == pm_friction_miner.THROTTLED_INTERVAL_DAYS


def test_next_proposal_id_increments(tmp_pm_paths):
    from tools import pm_friction_miner

    assert pm_friction_miner.next_proposal_id("A") == "PM-A-0001"
    pm_friction_miner.log_proposal({"id": "PM-A-0001", "title": "x", "evidence_count": 3})
    pm_friction_miner.log_proposal({"id": "PM-A-0002", "title": "y", "evidence_count": 3})
    assert pm_friction_miner.next_proposal_id("A") == "PM-A-0003"
