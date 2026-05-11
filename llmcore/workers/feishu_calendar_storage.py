"""Feishu (Lark) Calendar storage backend for the calendar worker.

Implements the ``CalendarStorage`` Protocol declared in
``llmcore.workers.calendar_worker``. Events live in the user's primary
Feishu calendar (auto-discovered on first use and cached in
``bots.feishu.calendar_id``). Reuses the existing ``bots.feishu.app_id`` /
``bots.feishu.app_secret`` credentials shared with the IM frontend.

Required app permission scope (configure on the Feishu Open Platform and
re-publish):
    calendar:calendar      — full read/write of user's calendars

Wired in via ``CalendarFactory.build()`` when the worker config carries
``storage="feishu"``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


_TAGS_PREFIX = "[tags:"  # description-prefix marker used to round-trip tags


class FeishuCalendarStorage:
    """Feishu Calendar v4 implementation of CalendarStorage.

    Constructed via ``from_config_store()`` in normal use; direct instantiation
    is for tests.
    """

    def __init__(self, *, app_id: str, app_secret: str, calendar_id: str,
                 client: Any | None = None) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._calendar_id = calendar_id
        self._client = client  # injectable for tests

    # ── construction ──

    @classmethod
    def from_config_store(cls) -> "FeishuCalendarStorage":
        from launcher.config_store import default_store
        store = default_store()
        app_id = (store.get("bots.feishu.app_id") or "").strip()
        app_secret = (store.get("bots.feishu.app_secret") or "").strip()
        if not app_id or not app_secret:
            raise RuntimeError(
                "feishu calendar: bots.feishu.app_id / app_secret missing. "
                "Configure via `python -m launcher.config set bots.feishu.app_id ...` "
                "or the GUI Bots tab.")

        calendar_id = (store.get("bots.feishu.calendar_id") or "").strip()
        client = _build_client(app_id, app_secret)
        if not calendar_id:
            calendar_id = _discover_primary_calendar_id(client)
            try:
                store.set_bot("feishu", {"calendar_id": calendar_id})
                log.info("cached primary calendar_id=%s into bots.feishu", calendar_id)
            except Exception:
                log.exception("could not persist calendar_id; will re-discover next start")
        return cls(app_id=app_id, app_secret=app_secret,
                   calendar_id=calendar_id, client=client)

    @property
    def client(self):
        if self._client is None:
            self._client = _build_client(self._app_id, self._app_secret)
        return self._client

    # ── CalendarStorage Protocol ──

    def create(self, event: dict) -> dict:
        from lark_oapi.api.calendar.v4 import CreateCalendarEventRequest
        body = _event_to_lark(event)
        req = (CreateCalendarEventRequest.builder()
               .calendar_id(self._calendar_id)
               .request_body(body)
               .build())
        resp = self.client.calendar.v4.calendar_event.create(req)
        _raise_if_failed(resp, "create_event")
        lark_event = resp.data.event
        return _lark_to_event(lark_event,
                              source=event.get("source", "voice"),
                              source_session=event.get("source_session"))

    def update(self, match: dict, patch: dict) -> dict | None:
        from lark_oapi.api.calendar.v4 import PatchCalendarEventRequest
        before = self._resolve_one(match)
        if before is None:
            return None
        eid = before["id"]
        merged = {
            "title": patch.get("title", before["title"]),
            "start_at": patch.get("start_at", before["start_at"]),
            "end_at": patch.get("end_at", before["end_at"]),
            "location": patch.get("location", before["location"]),
            "notes": patch.get("notes", before["notes"]),
            "tags": patch["tags"] if "tags" in patch else before.get("tags") or [],
        }
        body = _event_to_lark(merged)
        req = (PatchCalendarEventRequest.builder()
               .calendar_id(self._calendar_id)
               .event_id(eid)
               .request_body(body)
               .build())
        resp = self.client.calendar.v4.calendar_event.patch(req)
        _raise_if_failed(resp, "patch_event")
        after = _lark_to_event(resp.data.event,
                               source=before.get("source"),
                               source_session=before.get("source_session"))
        return {"id": eid, "before": before, "after": after}

    def delete(self, match: dict, *, soft: bool = False) -> dict | None:
        before = self._resolve_one(match)
        if before is None:
            return None
        eid = before["id"]
        if soft:
            # Feishu has no soft-delete; mark by prefixing the title.
            from lark_oapi.api.calendar.v4 import PatchCalendarEventRequest
            patched = dict(before)
            if not patched["title"].startswith("[deleted] "):
                patched["title"] = "[deleted] " + (patched["title"] or "")
            body = _event_to_lark(patched)
            req = (PatchCalendarEventRequest.builder()
                   .calendar_id(self._calendar_id)
                   .event_id(eid)
                   .request_body(body)
                   .build())
            resp = self.client.calendar.v4.calendar_event.patch(req)
            _raise_if_failed(resp, "soft_delete_event")
        else:
            from lark_oapi.api.calendar.v4 import DeleteCalendarEventRequest
            req = (DeleteCalendarEventRequest.builder()
                   .calendar_id(self._calendar_id)
                   .event_id(eid)
                   .build())
            resp = self.client.calendar.v4.calendar_event.delete(req)
            _raise_if_failed(resp, "delete_event")
        return {"id": eid, "deleted": True}

    def query(self, *, from_iso: str | None = None, to_iso: str | None = None,
              q: str | None = None, tags: list[str] | None = None) -> list[dict]:
        from lark_oapi.api.calendar.v4 import ListCalendarEventRequest
        # Feishu's list-events endpoint **requires** start_time / end_time. The
        # SQLiteStorage contract permits both to be None ("give me everything"),
        # so when callers leave them empty we apply a generous default window
        # rather than letting the API return 400. Window = now-30d → now+1y,
        # which covers reasonable day-planning use without risking pagination
        # blow-up given our 4-page (200-event) ceiling below.
        from datetime import timedelta
        now = datetime.now()
        eff_from = from_iso or (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        eff_to = to_iso or (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
        builder = (ListCalendarEventRequest.builder()
                   .calendar_id(self._calendar_id)
                   .page_size(50)
                   .start_time(_iso_to_unix_str(eff_from))
                   .end_time(_iso_to_unix_str(eff_to)))
        # Feishu list API only paginates by default; we pull up to 4 pages
        # (200 events) to mirror SQLiteStorage's LIMIT 200 ceiling.
        events: list[dict] = []
        page_token: str | None = None
        for _ in range(4):
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = self.client.calendar.v4.calendar_event.list(req)
            _raise_if_failed(resp, "list_events")
            data = resp.data
            for it in (data.items or []):
                events.append(_lark_to_event(it))
            if not data.has_more:
                break
            page_token = data.page_token
            if not page_token:
                break
        if q:
            qq = q.lower()
            events = [e for e in events
                      if qq in (e.get("title") or "").lower()
                      or qq in (e.get("notes") or "").lower()]
        if tags:
            tag_set = set(tags)
            events = [e for e in events if tag_set.intersection(e.get("tags") or [])]
        events.sort(key=lambda e: e.get("start_at") or "")
        return events[:200]

    def close(self) -> None:
        # lark client has no close(); nothing to do.
        return

    # ── internals ──

    def _resolve_one(self, match: dict) -> dict | None:
        eid = (match or {}).get("by_id")
        if eid:
            return self._fetch_one(eid)
        title = (match or {}).get("by_title_substring")
        near = (match or {}).get("near_date")
        if not title and not near:
            return None
        # Bound the list window to ±3 days around `near` (else the whole year).
        from_iso, to_iso = _bracket_for_match(near)
        candidates = self.query(from_iso=from_iso, to_iso=to_iso, q=title)
        if near:
            day = near[:10]
            candidates = [e for e in candidates if (e.get("start_at") or "")[:10] == day]
        if title:
            tt = title.lower()
            candidates = [e for e in candidates
                          if tt in (e.get("title") or "").lower()]
        if len(candidates) == 1:
            return candidates[0]
        return None  # ambiguous or none — caller decides

    def _fetch_one(self, eid: str) -> dict | None:
        from lark_oapi.api.calendar.v4 import GetCalendarEventRequest
        req = (GetCalendarEventRequest.builder()
               .calendar_id(self._calendar_id)
               .event_id(eid)
               .build())
        resp = self.client.calendar.v4.calendar_event.get(req)
        if not resp.success():
            return None
        return _lark_to_event(resp.data.event)


# ── helpers ──


def _build_client(app_id: str, app_secret: str):
    import lark_oapi as lark
    return (lark.Client.builder()
            .app_id(app_id).app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build())


def _discover_primary_calendar_id(client) -> str:
    from lark_oapi.api.calendar.v4 import PrimaryCalendarRequest
    req = PrimaryCalendarRequest.builder().build()
    resp = client.calendar.v4.calendar.primary(req)
    _raise_if_failed(resp, "primary_calendar")
    cals = resp.data.calendars or []
    if not cals:
        raise RuntimeError("feishu calendar: primary() returned 0 calendars; "
                           "ensure the app has 'calendar:calendar' scope and "
                           "is published.")
    cal = cals[0].calendar
    if not cal or not cal.calendar_id:
        raise RuntimeError("feishu calendar: primary calendar has no calendar_id")
    return cal.calendar_id


def _raise_if_failed(resp, op: str) -> None:
    if resp.success():
        return
    code = getattr(resp, "code", "?")
    msg = getattr(resp, "msg", "")
    log_id = ""
    try:
        log_id = resp.get_log_id()
    except Exception:
        pass
    raise RuntimeError(f"feishu calendar {op} failed: code={code} msg={msg!r} log_id={log_id}")


def _event_to_lark(event: dict):
    from lark_oapi.api.calendar.v4 import CalendarEvent, TimeInfo, EventLocation
    tz = _resolve_timezone()
    notes = event.get("notes") or ""
    tags = event.get("tags") or []
    if tags:
        tag_line = _TAGS_PREFIX + ",".join(str(t) for t in tags) + "]"
        notes = f"{tag_line}\n{notes}" if notes else tag_line

    builder = (CalendarEvent.builder()
               .summary(event.get("title") or "")
               .description(notes))
    start_at = event.get("start_at")
    end_at = event.get("end_at") or start_at
    if start_at:
        builder = builder.start_time(TimeInfo.builder()
                                     .timestamp(_iso_to_unix_str(start_at))
                                     .timezone(tz).build())
    if end_at:
        builder = builder.end_time(TimeInfo.builder()
                                   .timestamp(_iso_to_unix_str(end_at))
                                   .timezone(tz).build())
    if event.get("location"):
        builder = builder.location(EventLocation.builder()
                                   .name(event["location"]).build())
    return builder.build()


def _lark_to_event(lark_event, *, source: str | None = None,
                   source_session: str | None = None) -> dict:
    desc = lark_event.description or ""
    tags: list[str] = []
    if desc.startswith(_TAGS_PREFIX):
        first_nl = desc.find("\n")
        head = desc if first_nl < 0 else desc[:first_nl]
        rest = "" if first_nl < 0 else desc[first_nl + 1:]
        if head.endswith("]"):
            tags = [t for t in head[len(_TAGS_PREFIX):-1].split(",") if t]
            desc = rest
    start_at = _time_info_to_iso(lark_event.start_time)
    end_at = _time_info_to_iso(lark_event.end_time)
    location = (lark_event.location.name
                if lark_event.location and lark_event.location.name else None)
    create_ms = getattr(lark_event, "create_time", None)
    created = _ms_to_iso(create_ms) if create_ms else _now_iso()
    return {
        "id": lark_event.event_id,
        "title": lark_event.summary or "",
        "start_at": start_at,
        "end_at": end_at,
        "location": location,
        "notes": desc or None,
        "tags": tags,
        "created_at": created,
        "updated_at": created,
        "source": source or "voice",
        "source_session": source_session,
    }


def _time_info_to_iso(ti) -> str | None:
    if ti is None:
        return None
    if ti.timestamp:
        try:
            return _unix_str_to_iso(ti.timestamp)
        except (TypeError, ValueError):
            return None
    if ti.date:
        return f"{ti.date}T00:00:00"
    return None


def _iso_to_unix_str(iso: str) -> str:
    s = (iso or "").strip()
    if not s:
        raise ValueError("empty iso datetime")
    # Accept "YYYY-MM-DDTHH:MM:SS[+ZZ:ZZ]" or with space; bare-date → midnight local.
    s = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        if len(s) == 10:  # bare date
            dt = datetime.fromisoformat(s + "T00:00:00")
        else:
            raise
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local timezone
    return str(int(dt.timestamp()))


def _unix_str_to_iso(ts: str | int) -> str:
    secs = int(ts)
    return datetime.fromtimestamp(secs).strftime("%Y-%m-%dT%H:%M:%S")


def _ms_to_iso(ms: str | int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%dT%H:%M:%S")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _local_tz_name() -> str:
    """Best-effort IANA timezone name. NEVER returns the platform-localized
    `tzname()` (e.g. '中国标准时间' on Windows-Chinese), which Feishu rejects.
    Resolution order: TZ env var → tzlocal package → Asia/Shanghai default."""
    tz = (os.environ.get("TZ") or "").strip()
    if tz and "/" in tz:  # crude IANA-shape check
        return tz
    try:
        import tzlocal  # type: ignore
        name = str(tzlocal.get_localzone_name() or "").strip()
        if name and "/" in name:
            return name
    except Exception:
        pass
    return "Asia/Shanghai"


def _resolve_timezone() -> str:
    """User-overridable. Reads bots.feishu.timezone from config_store first,
    falls back to platform IANA name (`_local_tz_name`)."""
    try:
        from launcher.config_store import default_store
        tz = (default_store().get("bots.feishu.timezone") or "").strip()
        if tz and "/" in tz:
            return tz
    except Exception:
        pass
    return _local_tz_name()


def _bracket_for_match(near_iso: str | None) -> tuple[str | None, str | None]:
    if not near_iso:
        return (None, None)
    try:
        d = datetime.fromisoformat(near_iso[:10])
    except ValueError:
        return (None, None)
    from datetime import timedelta
    return ((d - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"),
            (d + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"))
