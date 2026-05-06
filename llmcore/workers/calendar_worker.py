"""calendar_worker — SQLite-backed event store.

Capabilities offered:
  - calendar.create_event.v1
  - calendar.update_event.v1
  - calendar.delete_event.v1
  - calendar.query_events.v1

Storage layer is a Protocol so a future GoogleCalendarStorage / NotionStorage
plugin can swap in without touching the worker.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)

CAPS = (
    "calendar.create_event.v1",
    "calendar.update_event.v1",
    "calendar.delete_event.v1",
    "calendar.query_events.v1",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    start_at      TEXT NOT NULL,
    end_at        TEXT,
    location      TEXT,
    notes         TEXT,
    tags          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    source        TEXT,
    source_session TEXT
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS events_title ON events(title);
"""


# ── Storage protocol ──


class CalendarStorage(Protocol):
    def create(self, event: dict) -> dict: ...
    def update(self, match: dict, patch: dict) -> dict | None: ...
    def delete(self, match: dict, *, soft: bool = False) -> dict | None: ...
    def query(self, *, from_iso: str | None = None, to_iso: str | None = None,
              q: str | None = None, tags: list[str] | None = None) -> list[dict]: ...
    def close(self) -> None: ...


class SQLiteCalendarStorage:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def create(self, event: dict) -> dict:
        eid = str(uuid.uuid4())
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(id,title,start_at,end_at,location,notes,tags,"
                "created_at,updated_at,source,source_session) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (eid, event["title"], event["start_at"], event.get("end_at"),
                 event.get("location"), event.get("notes"),
                 json.dumps(event.get("tags") or [], ensure_ascii=False),
                 now, now, event.get("source", "voice"),
                 event.get("source_session")),
            )
        return self._fetch_one(eid)

    def update(self, match: dict, patch: dict) -> dict | None:
        with self._lock:
            row = self._resolve(match)
            if row is None:
                return None
            keys, vals = [], []
            for k in ("title", "start_at", "end_at", "location", "notes"):
                if k in patch:
                    keys.append(f"{k}=?")
                    vals.append(patch[k])
            if "tags" in patch:
                keys.append("tags=?")
                vals.append(json.dumps(patch["tags"], ensure_ascii=False))
            keys.append("updated_at=?")
            vals.append(_now_iso())
            vals.append(row["id"])
            self._conn.execute(f"UPDATE events SET {','.join(keys)} WHERE id=?", vals)
            after = self._fetch_one(row["id"])
            return {"id": row["id"], "before": _row_to_event(row), "after": after}

    def delete(self, match: dict, *, soft: bool = False) -> dict | None:
        with self._lock:
            row = self._resolve(match)
            if row is None:
                return None
            if soft:
                self._conn.execute(
                    "UPDATE events SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps((_decode_tags(row["tags"]) + ["_deleted"]), ensure_ascii=False),
                     _now_iso(), row["id"]),
                )
            else:
                self._conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
            return {"id": row["id"], "deleted": True}

    def query(self, *, from_iso: str | None = None, to_iso: str | None = None,
              q: str | None = None, tags: list[str] | None = None) -> list[dict]:
        clauses, vals = [], []
        if from_iso:
            clauses.append("start_at >= ?")
            vals.append(from_iso)
        if to_iso:
            clauses.append("start_at <= ?")
            vals.append(to_iso)
        if q:
            clauses.append("(title LIKE ? OR notes LIKE ?)")
            vals.extend([f"%{q}%", f"%{q}%"])
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY start_at ASC LIMIT 200"
        with self._lock:
            rows = self._conn.execute(sql, vals).fetchall()
        events = [_row_to_event(r) for r in rows]
        if tags:
            tag_set = set(tags)
            events = [e for e in events if tag_set.intersection(e.get("tags", []))]
        return events

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── internals ──

    def _resolve(self, match: dict) -> sqlite3.Row | None:
        if match.get("by_id"):
            return self._conn.execute("SELECT * FROM events WHERE id=?", (match["by_id"],)).fetchone()
        title = match.get("by_title_substring")
        near = match.get("near_date")
        if not title and not near:
            return None
        clauses, vals = [], []
        if title:
            clauses.append("title LIKE ?")
            vals.append(f"%{title}%")
        if near:
            day = near[:10]
            clauses.append("substr(start_at, 1, 10) = ?")
            vals.append(day)
        sql = "SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY start_at ASC LIMIT 5"
        rows = self._conn.execute(sql, vals).fetchall()
        if len(rows) == 1:
            return rows[0]
        return None  # ambiguous or none — caller decides what to do

    def _fetch_one(self, eid: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return _row_to_event(row) if row else None


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "title": row["title"], "start_at": row["start_at"],
        "end_at": row["end_at"], "location": row["location"], "notes": row["notes"],
        "tags": _decode_tags(row["tags"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "source": row["source"], "source_session": row["source_session"],
    }


def _decode_tags(s: str | None) -> list[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()) or time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Worker ──


class CalendarWorker:
    def __init__(self, *, name: str, storage: CalendarStorage) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="calendar", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Local-SQLite calendar event store.",
        )
        self._storage = storage
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
            return self._dispatch(req)
        except Exception as exc:
            self._last_error = repr(exc)
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.INTERNAL, str(exc), retryable=False))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def _dispatch(self, req: InvokeRequest) -> InvokeResponse:
        cap = req.capability
        p = req.payload
        if cap == "calendar.create_event.v1":
            event = self._storage.create(p)
            return InvokeResponse(call_id=req.call_id, ok=True, result={"id": event["id"], "event": event})
        if cap == "calendar.update_event.v1":
            r = self._storage.update(p["match"], p["patch"])
            if r is None:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    "not_found", "no event matched", retryable=False))
            return InvokeResponse(call_id=req.call_id, ok=True, result=r)
        if cap == "calendar.delete_event.v1":
            r = self._storage.delete(p["match"], soft=p.get("soft", False))
            if r is None:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    "not_found", "no event matched", retryable=False))
            return InvokeResponse(call_id=req.call_id, ok=True, result=r)
        if cap == "calendar.query_events.v1":
            events = self._storage.query(
                from_iso=p.get("from"), to_iso=p.get("to"),
                q=p.get("q"), tags=p.get("tags"),
            )
            return InvokeResponse(call_id=req.call_id, ok=True, result={"events": events})
        return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
            ErrorCodes.NOT_IMPLEMENTED, f"unhandled capability {cap!r}"))

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        self._storage.close()


class CalendarFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.calendar",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "calendar"
        db_path = config.get("db_path") or os.path.join(_default_temp(), "calendar.db")
        storage = SQLiteCalendarStorage(db_path)
        return CalendarWorker(name=name, storage=storage)


def _default_temp() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "temp")
