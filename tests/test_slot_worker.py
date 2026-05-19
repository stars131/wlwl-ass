"""Tests for slot_worker — free-slot computation, working_hours, buffer."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from llmcore.workers.slot_worker import (
    _Interval, _busy_from_events, _compute_slots, _parse_iso, _parse_range_spec,
)


def test_parse_range_spec_valid():
    assert _parse_range_spec("09:00-12:00") is not None
    s, e = _parse_range_spec("18:30-21:00")
    assert s.hour == 18 and s.minute == 30
    assert e.hour == 21 and e.minute == 0


def test_parse_range_spec_invalid():
    assert _parse_range_spec("not-a-time") is None
    assert _parse_range_spec("25:00-26:00") is None  # hours out of range


def test_interval_subtract_no_overlap():
    a = _Interval(datetime(2026, 5, 20, 10), datetime(2026, 5, 20, 12))
    b = _Interval(datetime(2026, 5, 20, 14), datetime(2026, 5, 20, 16))
    assert a.subtract(b) == [a]


def test_interval_subtract_full_overlap():
    a = _Interval(datetime(2026, 5, 20, 10), datetime(2026, 5, 20, 12))
    b = _Interval(datetime(2026, 5, 20, 9), datetime(2026, 5, 20, 13))
    assert a.subtract(b) == []


def test_interval_subtract_partial_overlap_left():
    a = _Interval(datetime(2026, 5, 20, 10), datetime(2026, 5, 20, 14))
    b = _Interval(datetime(2026, 5, 20, 9), datetime(2026, 5, 20, 12))
    result = a.subtract(b)
    assert len(result) == 1
    assert result[0].start == datetime(2026, 5, 20, 12)
    assert result[0].end == datetime(2026, 5, 20, 14)


def test_interval_subtract_split():
    a = _Interval(datetime(2026, 5, 20, 9), datetime(2026, 5, 20, 18))
    b = _Interval(datetime(2026, 5, 20, 12), datetime(2026, 5, 20, 14))
    result = a.subtract(b)
    assert len(result) == 2
    assert result[0].end == datetime(2026, 5, 20, 12)
    assert result[1].start == datetime(2026, 5, 20, 14)


def test_busy_from_events_pads_buffer():
    events = [{"start_at": "2026-05-20T10:00:00", "end_at": "2026-05-20T11:00:00"}]
    busy = _busy_from_events(events, buffer_min=15)
    assert len(busy) == 1
    # buffer expanded by 15 min on each side
    assert busy[0].start == datetime(2026, 5, 20, 9, 45)
    assert busy[0].end == datetime(2026, 5, 20, 11, 15)


def test_busy_from_events_merges_overlapping():
    events = [
        {"start_at": "2026-05-20T10:00:00", "end_at": "2026-05-20T11:00:00"},
        {"start_at": "2026-05-20T10:30:00", "end_at": "2026-05-20T12:00:00"},
    ]
    busy = _busy_from_events(events, buffer_min=0)
    assert len(busy) == 1  # merged
    assert busy[0].start == datetime(2026, 5, 20, 10)
    assert busy[0].end == datetime(2026, 5, 20, 12)


def test_compute_slots_empty_calendar():
    # Wednesday 2026-05-20, 09:00-18:00 working hours, no busy
    slots, exhausted = _compute_slots(
        earliest=datetime(2026, 5, 20, 0),
        latest=datetime(2026, 5, 21, 0),
        duration_min=60,
        working_hours={"weekday": ["09:00-18:00"], "weekend": []},
        busy=[],
        preferred_window="any",
        max_slots=3,
    )
    assert len(slots) == 3
    assert slots[0].start == datetime(2026, 5, 20, 9)
    assert slots[1].start == datetime(2026, 5, 20, 10)
    assert slots[2].start == datetime(2026, 5, 20, 11)


def test_compute_slots_preferred_window_filters():
    slots, exhausted = _compute_slots(
        earliest=datetime(2026, 5, 20, 0),
        latest=datetime(2026, 5, 21, 0),
        duration_min=60,
        working_hours={"weekday": ["09:00-21:00"], "weekend": []},
        busy=[],
        preferred_window="evening",
        max_slots=3,
    )
    assert all(s.start.hour >= 18 for s in slots)


def test_compute_slots_respects_busy():
    busy = _busy_from_events(
        [{"start_at": "2026-05-20T10:00:00", "end_at": "2026-05-20T12:00:00"}],
        buffer_min=15,
    )
    slots, _ = _compute_slots(
        earliest=datetime(2026, 5, 20, 0),
        latest=datetime(2026, 5, 21, 0),
        duration_min=60,
        working_hours={"weekday": ["09:00-18:00"], "weekend": []},
        busy=busy,
        preferred_window="any",
        max_slots=10,
    )
    # No slot should overlap with 09:45–12:15
    for s in slots:
        assert s.end <= datetime(2026, 5, 20, 9, 45) or s.start >= datetime(2026, 5, 20, 12, 15)


def test_compute_slots_weekend_zero_window():
    # Saturday 2026-05-23, weekend has no working window
    slots, _ = _compute_slots(
        earliest=datetime(2026, 5, 23, 0),
        latest=datetime(2026, 5, 24, 0),
        duration_min=60,
        working_hours={"weekday": ["09:00-18:00"], "weekend": []},
        busy=[],
        preferred_window="any",
        max_slots=5,
    )
    assert slots == []


def test_compute_slots_exhausted_flag_true_when_underfull():
    slots, exhausted = _compute_slots(
        earliest=datetime(2026, 5, 20, 0),
        latest=datetime(2026, 5, 21, 0),
        duration_min=480,  # 8 hours per slot
        working_hours={"weekday": ["09:00-12:00"], "weekend": []},  # only 3h window
        busy=[],
        preferred_window="any",
        max_slots=3,
    )
    # 8h slots don't fit in 3h windows → 0 slots, exhausted=True
    assert slots == []
    assert exhausted is True


# ── Integration: SlotWorker against a real kernel + calendar_worker ──

@pytest.fixture
def kernel_with_calendar(tmp_path):
    """Spin a fresh kernel with calendar_worker + slot_worker registered."""
    from llmcore.kernel import reset_kernel, get_kernel
    reset_kernel()
    k = get_kernel()
    k.add_worker({"name": "cal", "kind": "calendar",
                  "db_path": str(tmp_path / "cal.db")})
    k.add_worker({"name": "slot", "kind": "concierge_slot"})
    yield k
    reset_kernel()


def test_slot_worker_via_kernel(kernel_with_calendar):
    k = kernel_with_calendar
    # Add one busy event
    r = k.dispatch(capability="calendar.create_event.v1",
                   payload={"title": "Coffee",
                            "start_at": "2026-05-20T10:00:00",
                            "end_at":   "2026-05-20T11:00:00"})
    assert r.ok

    r = k.dispatch(capability="concierge.propose_slot.v1",
                   payload={"duration_minutes": 60,
                            "earliest": "2026-05-20T00:00:00",
                            "latest":   "2026-05-21T00:00:00",
                            "working_hours": {"weekday": ["09:00-18:00"], "weekend": []},
                            "buffer_min": 15,
                            "preferred_window": "any",
                            "max_slots": 3})
    assert r.ok
    slots = r.result["slots"]
    assert 1 <= len(slots) <= 3
    for s in slots:
        st = _parse_iso(s["start"])
        en = _parse_iso(s["end"])
        # Should not overlap with 09:45–11:15 buffered busy
        assert en <= datetime(2026, 5, 20, 9, 45) or st >= datetime(2026, 5, 20, 11, 15)
        # Should be inside working hours
        assert 9 <= st.hour <= 17


def test_slot_worker_payload_validation(kernel_with_calendar):
    k = kernel_with_calendar
    # negative duration
    r = k.dispatch(capability="concierge.propose_slot.v1",
                   payload={"duration_minutes": -1,
                            "earliest": "2026-05-20T00:00:00",
                            "latest":   "2026-05-21T00:00:00"})
    assert not r.ok
    assert "duration_minutes" in (r.error or {}).get("message", "")

    # earliest > latest
    r = k.dispatch(capability="concierge.propose_slot.v1",
                   payload={"duration_minutes": 60,
                            "earliest": "2026-05-21T00:00:00",
                            "latest":   "2026-05-20T00:00:00"})
    assert not r.ok
