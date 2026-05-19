"""slot_worker — find free slots in the owner's calendar.

Capability offered:
  - concierge.propose_slot.v1

Algorithm:
  1. Dispatch ``calendar.query_events.v1`` via the injected KernelHandle to
     read every event inside ``[earliest, latest]``.
  2. Build a busy-interval list, padded by ``buffer_min`` on each side.
  3. For each day in the window, intersect with the configured
     ``working_hours`` for that day-of-week, subtract busy intervals,
     and emit any sub-interval ≥ ``duration_minutes`` as a candidate slot.
  4. Filter by ``preferred_window`` (morning / afternoon / evening / any).
  5. Return up to ``max_slots`` (default 3) earliest first.

Time model:
  All times are local ISO-8601 strings, optionally with ``+HH:MM``
  offset. Parsing falls back to a naive parse if the offset is omitted,
  which matches what calendar_worker stores. We don't pull tzdata in;
  the comparison is whatever Python's ``datetime.fromisoformat`` does.

Why a worker that calls another worker (not a tool calling the agent):
the concierge holds capability tokens covering both ``propose_slot`` and
``calendar.query_events`` — but the agent dispatches one capability per
turn. Slot computation is non-trivial (busy intersection, working hours,
buffers), and we want it cached + audited as one kernel dispatch, not 5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Any

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)

CAPS = ("concierge.propose_slot.v1",)

DEFAULT_WORKING_HOURS = {
    "weekday": ["09:00-12:00", "14:00-18:00"],
    "weekend": [],
}
WINDOW_RANGES = {
    "morning":   (dt_time(6, 0),  dt_time(12, 0)),
    "afternoon": (dt_time(12, 0), dt_time(18, 0)),
    "evening":   (dt_time(18, 0), dt_time(23, 0)),
    "any":       (dt_time(0, 0),  dt_time(23, 59)),
}

_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


@dataclass
class _Interval:
    start: datetime
    end: datetime

    def length_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    def intersects(self, other: "_Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def subtract(self, other: "_Interval") -> list["_Interval"]:
        """Return self minus other; may be 0, 1, or 2 intervals."""
        if not self.intersects(other):
            return [self]
        out: list[_Interval] = []
        if self.start < other.start:
            out.append(_Interval(self.start, other.start))
        if other.end < self.end:
            out.append(_Interval(other.end, self.end))
        return out


def _parse_iso(s: str) -> datetime:
    """Parse ISO-8601; if naive (no tz), keep it naive. Mixing naive and aware
    will raise — but the calendar_worker stores naive timestamps, so as long
    as inputs are consistent within one dispatch we're fine."""
    if not s:
        raise ValueError("empty datetime string")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Tolerate trailing 'Z' (treat as UTC); strip and parse naive.
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1])
        raise


def _parse_range_spec(spec: str) -> tuple[dt_time, dt_time] | None:
    m = _RANGE_RE.match(spec.strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    try:
        return dt_time(h1, m1), dt_time(h2, m2)
    except ValueError:
        return None


def _day_window(day: datetime, working_hours: dict) -> list[_Interval]:
    """Compute the [start, end] intervals that count as "bookable" on this
    calendar day, per the supplied working_hours config."""
    is_weekend = day.weekday() >= 5
    key = "weekend" if is_weekend else "weekday"
    specs = working_hours.get(key) or []
    out: list[_Interval] = []
    for spec in specs:
        parsed = _parse_range_spec(str(spec))
        if not parsed:
            continue
        start_t, end_t = parsed
        start = day.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
        end = day.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
        if end > start:
            out.append(_Interval(start, end))
    return out


def _intersect_window(interval: _Interval, preferred: str) -> _Interval | None:
    if preferred not in WINDOW_RANGES:
        preferred = "any"
    start_t, end_t = WINDOW_RANGES[preferred]
    day = interval.start
    pref_start = day.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
    pref_end = day.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
    start = max(interval.start, pref_start)
    end = min(interval.end, pref_end)
    if end > start:
        return _Interval(start, end)
    return None


def _busy_from_events(events: list[dict], *, buffer_min: int) -> list[_Interval]:
    out: list[_Interval] = []
    pad = timedelta(minutes=buffer_min)
    for ev in events:
        s = ev.get("start_at")
        e = ev.get("end_at") or ev.get("start_at")
        if not s:
            continue
        try:
            start = _parse_iso(str(s))
            end = _parse_iso(str(e)) if e else start + timedelta(minutes=60)
        except ValueError:
            continue
        if end <= start:
            end = start + timedelta(minutes=60)
        out.append(_Interval(start - pad, end + pad))
    out.sort(key=lambda iv: iv.start)
    # Merge overlaps so the subtract loop is O(slots * busy) not O(slots * busy^2).
    merged: list[_Interval] = []
    for iv in out:
        if merged and iv.start <= merged[-1].end:
            merged[-1] = _Interval(merged[-1].start, max(merged[-1].end, iv.end))
        else:
            merged.append(iv)
    return merged


def _compute_slots(
    *, earliest: datetime, latest: datetime, duration_min: int,
    working_hours: dict, busy: list[_Interval], preferred_window: str,
    max_slots: int,
) -> tuple[list[_Interval], bool]:
    """Return (slots, exhausted_flag). exhausted=False means we stopped at
    max_slots and there might be more available."""
    slots: list[_Interval] = []
    day = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
    duration = timedelta(minutes=duration_min)
    while day < latest:
        for window in _day_window(day, working_hours):
            # Clip to overall earliest/latest first
            win = _Interval(
                max(window.start, earliest),
                min(window.end, latest),
            )
            if win.end <= win.start:
                continue
            # Intersect with preferred window
            preferred_win = _intersect_window(win, preferred_window)
            if preferred_win is None:
                continue
            # Subtract busy
            free_pieces: list[_Interval] = [preferred_win]
            for b in busy:
                next_pieces: list[_Interval] = []
                for piece in free_pieces:
                    next_pieces.extend(piece.subtract(b))
                free_pieces = next_pieces
            for piece in free_pieces:
                if piece.length_minutes() >= duration_min:
                    candidate_start = piece.start
                    while candidate_start + duration <= piece.end:
                        slots.append(_Interval(candidate_start, candidate_start + duration))
                        if len(slots) >= max_slots:
                            return slots, False
                        # Skip ahead by the full duration so we don't propose
                        # 19:00 and 19:01 — give the caller meaningfully
                        # different options.
                        candidate_start = candidate_start + duration
        day = day + timedelta(days=1)
    return slots, True  # exhausted the window


def _iso(d: datetime) -> str:
    return d.isoformat()


class SlotWorker:
    def __init__(self, *, name: str, kernel: KernelHandle,
                 default_working_hours: dict | None = None,
                 default_buffer_min: int = 15,
                 default_max_slots: int = 3) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="concierge_slot", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Propose free calendar slots honoring working_hours + buffer.",
        )
        self._kernel = kernel
        self._default_working_hours = default_working_hours or dict(DEFAULT_WORKING_HOURS)
        self._default_buffer_min = default_buffer_min
        self._default_max_slots = default_max_slots
        self._in_flight = 0
        self._last_error: str | None = None

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready", in_flight=self._in_flight, last_error=self._last_error)

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        self._in_flight += 1
        try:
            if req.capability != "concierge.propose_slot.v1":
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.NOT_IMPLEMENTED, req.capability))
            p = req.payload
            try:
                duration_min = int(p["duration_minutes"])
                earliest = _parse_iso(str(p["earliest"]))
                latest = _parse_iso(str(p["latest"]))
            except (KeyError, ValueError) as exc:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.PAYLOAD_INVALID, str(exc)))
            if duration_min <= 0 or duration_min > 24 * 60:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.PAYLOAD_INVALID,
                    f"duration_minutes must be in (0, 1440], got {duration_min}"))
            if latest <= earliest:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.PAYLOAD_INVALID, "latest must be > earliest"))

            working_hours = p.get("working_hours") or self._default_working_hours
            buffer_min = int(p.get("buffer_min", self._default_buffer_min))
            preferred_window = str(p.get("preferred_window") or "any").lower()
            max_slots = int(p.get("max_slots", self._default_max_slots))
            max_slots = max(1, min(max_slots, 10))

            # Pull busy events via kernel.dispatch — we use the request_chat-
            # style backchannel that KernelHandle exposes implicitly through
            # the global kernel singleton. The slot worker holds a ref to
            # KernelHandle but the handle doesn't expose a generic dispatch;
            # we go through get_kernel() to read calendar events.
            from llmcore.kernel import get_kernel
            cal_resp = get_kernel().dispatch(
                capability="calendar.query_events.v1",
                payload={"from": _iso(earliest), "to": _iso(latest)},
                deadline_ms=4_000,
            )
            if not cal_resp.ok:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.UPSTREAM_FAILURE,
                    f"calendar query failed: {(cal_resp.error or {}).get('message')}",
                    retryable=True))
            events = list((cal_resp.result or {}).get("events") or [])

            busy = _busy_from_events(events, buffer_min=buffer_min)
            slots, exhausted = _compute_slots(
                earliest=earliest, latest=latest,
                duration_min=duration_min,
                working_hours=working_hours, busy=busy,
                preferred_window=preferred_window, max_slots=max_slots,
            )
            result_slots = [
                {"start": _iso(s.start), "end": _iso(s.end), "rank": i + 1}
                for i, s in enumerate(slots)
            ]
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "slots": result_slots,
                "exhausted": bool(exhausted),
            })
        except Exception as exc:
            self._last_error = repr(exc)
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.INTERNAL, str(exc)))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        return


class SlotFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.concierge_slot",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "concierge_slot"
        return SlotWorker(
            name=name, kernel=kernel,
            default_working_hours=config.get("working_hours"),
            default_buffer_min=int(config.get("buffer_min", 15)),
            default_max_slots=int(config.get("max_slots", 3)),
        )
