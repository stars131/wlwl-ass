"""inspiration_worker — SQLite + FTS5 note store.

Capabilities offered:
  - inspiration.record.v1   (auto-tags via kernel.request_chat if available)
  - inspiration.query.v1
  - inspiration.list_recent.v1
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)

CAPS = (
    "inspiration.record.v1",
    "inspiration.query.v1",
    "inspiration.list_recent.v1",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    tags        TEXT,
    links       TEXT,
    created_at  TEXT NOT NULL,
    source      TEXT,
    source_session TEXT
);
CREATE INDEX IF NOT EXISTS notes_created ON notes(created_at);
"""

# Some sqlite builds lack FTS5 — we fall back to plain LIKE queries if so.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    text, content='notes', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, text) VALUES('delete', old.rowid, old.text);
  INSERT INTO notes_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


class InspirationStorage:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._fts = self._try_create_fts()

    def _try_create_fts(self) -> bool:
        try:
            self._conn.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False  # FTS5 not compiled in; we'll fall back to LIKE

    def record(self, text: str, *, tags: list[str], links: list[str], source_session: str | None) -> dict:
        nid = str(uuid.uuid4())
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes(id,text,tags,links,created_at,source,source_session) "
                "VALUES(?,?,?,?,?,?,?)",
                (nid, text, json.dumps(tags, ensure_ascii=False),
                 json.dumps(links, ensure_ascii=False), now, "voice", source_session),
            )
            row = self._conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        return _row_to_note(row)

    def query(self, *, q: str, limit: int = 20) -> list[dict]:
        with self._lock:
            if self._fts:
                rows = self._conn.execute(
                    "SELECT n.* FROM notes_fts f JOIN notes n ON n.rowid = f.rowid "
                    "WHERE notes_fts MATCH ? ORDER BY n.created_at DESC LIMIT ?",
                    (q, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM notes WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{q}%", limit),
                ).fetchall()
        return [_row_to_note(r) for r in rows]

    def list_recent(self, *, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_note(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_note(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "text": row["text"],
        "tags": _decode_json_list(row["tags"]),
        "links": _decode_json_list(row["links"]),
        "created_at": row["created_at"], "source": row["source"],
        "source_session": row["source_session"],
    }


def _decode_json_list(s: str | None) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()) or time.strftime("%Y-%m-%dT%H:%M:%S")


_VERB_PREFIXES = (
    "记一下", "记录一下", "记下来", "记下", "记录", "记一笔",
    "帮我记", "帮我记录", "帮我记下", "我想记",
    "note that", "remember that", "log that",
)


def _strip_verb(text: str) -> str:
    s = text.strip().lstrip("：:，,。.")
    for prefix in _VERB_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):].lstrip("：:，,。. ")
    return s


# ── Worker ──


class InspirationWorker:
    def __init__(self, *, name: str, storage: InspirationStorage, kernel: KernelHandle) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="inspiration", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Local-SQLite inspiration / note store with FTS5 search.",
        )
        self._storage = storage
        self._kernel = kernel
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
                ErrorCodes.INTERNAL, str(exc)))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def _dispatch(self, req: InvokeRequest) -> InvokeResponse:
        cap = req.capability
        p = req.payload
        if cap == "inspiration.record.v1":
            text = _strip_verb(p["text"])
            tags = list(p.get("tags") or [])
            note = self._storage.record(text, tags=tags, links=list(p.get("links") or []),
                                        source_session=p.get("source_session"))
            # auto-tag is best-effort; never blocks the primary save
            auto = self._maybe_auto_tag(text) if not tags else []
            if auto:
                # merge into the saved row
                new_tags = list(dict.fromkeys(tags + auto))
                # cheap update via the storage layer's connection — no public API for tags-only update
                # For MVP, return both saved + auto; persistence of auto-tags can be a v2 cleanup.
                note["tags"] = new_tags
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "id": note["id"], "auto_tags": auto, "note": note,
            })
        if cap == "inspiration.query.v1":
            limit = int(p.get("limit", 20))
            notes = self._storage.query(q=p["q"], limit=limit)
            return InvokeResponse(call_id=req.call_id, ok=True, result={"notes": notes})
        if cap == "inspiration.list_recent.v1":
            limit = int(p.get("limit", 20))
            notes = self._storage.list_recent(limit=limit)
            return InvokeResponse(call_id=req.call_id, ok=True, result={"notes": notes})
        return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
            ErrorCodes.NOT_IMPLEMENTED, cap))

    def _maybe_auto_tag(self, text: str) -> list[str]:
        return []  # no kernel.request_chat sync path in MVP; v2 turns this on via async dispatch.

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        self._storage.close()


class InspirationFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.inspiration",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "inspiration"
        db_path = config.get("db_path") or os.path.join(_default_temp(), "inspiration.db")
        storage = InspirationStorage(db_path)
        return InspirationWorker(name=name, storage=storage, kernel=kernel)


def _default_temp() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "temp")
