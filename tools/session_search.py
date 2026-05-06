"""``session_search`` — full-text search across compressed L4 session archives.

L4 session logs (``memory/L4_raw_sessions/MMDD_HHMM-MMDD_HHMM.txt``) are
the long-tail memory of the agent: every prompt + response from a turn
that's old enough to have rolled out of the working window. They're
stored as plain text — fine for archival, useless for "what did we
discuss two weeks ago about X" without grep.

Implementation:
  * stdlib only (``sqlite3``); zero new pip dependencies. The ``trigram``
    FTS5 tokenizer ships with CPython's bundled SQLite ≥ 3.34, which
    Python 3.10+ on Windows / macOS / Linux satisfies. trigram works
    well for both English and CJK without language-specific segmentation.
  * Index lives at ``temp/.session_search.db``. Incremental: re-indexes
    only files whose mtime changed since last run (cheap on every call).
  * Search returns top-K hits with a short snippet around each match.

Public API for the agent loop:
  * ``session_search(query, top_k=5)`` — formatted string, ready to drop
    into a ``tool_result``.
  * ``rebuild_index()`` — force full reindex (debug / after manual edit).

Don't pull personally-identifiable data into telemetry — the index DB is
local only. ``temp/`` is gitignored.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any


# Project root = parent of this file's parent (tools/ lives at root level).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_L4_DIR = os.path.join(_PROJECT_ROOT, "memory", "L4_raw_sessions")
_INDEX_PATH = os.path.join(_PROJECT_ROOT, "temp", ".session_search.db")


@dataclass
class SearchHit:
    filename: str
    snippet: str
    rank: float
    mtime: float


# ─── Index lifecycle ─────────────────────────────────────────────────────


def _ensure_dir(path: str) -> None:
    """Make parent dir without complaining if it exists."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open the index DB, creating schema if missing."""
    db_path = db_path or _INDEX_PATH
    _ensure_dir(db_path)
    conn = sqlite3.connect(db_path)
    # FTS5 is part of stdlib sqlite3 since 3.7+, but a tiny minority of
    # exotic builds disable it. Probe explicitly so the failure mode is a
    # readable message rather than a generic OperationalError mid-search.
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(filename, body, tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "session_search needs sqlite3 with FTS5 + trigram tokenizer. "
            "Your Python's bundled sqlite reports: " + str(exc),
        ) from exc
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_meta ("
        "filename TEXT PRIMARY KEY, mtime REAL NOT NULL, rowid_fts INTEGER NOT NULL)"
    )
    return conn


def _list_session_files(l4_dir: str | None = None) -> list[str]:
    """Enumerate L4 archive files. Returns absolute paths sorted by mtime
    (oldest first) so the most recent indexing is always the freshest."""
    d = l4_dir or _L4_DIR
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".txt"):
            continue
        full = os.path.join(d, name)
        if os.path.isfile(full):
            out.append(full)
    out.sort(key=os.path.getmtime)
    return out


def _index_one(conn: sqlite3.Connection, path: str) -> None:
    """Re-index a single session file. ``REPLACE`` semantics — caller has
    already determined this file is new or stale."""
    fname = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        return
    # Drop any existing row for this filename, then re-insert.
    cur = conn.cursor()
    row = cur.execute("SELECT rowid_fts FROM session_meta WHERE filename = ?", (fname,)).fetchone()
    if row is not None:
        cur.execute("DELETE FROM sessions_fts WHERE rowid = ?", (row[0],))
        cur.execute("DELETE FROM session_meta WHERE filename = ?", (fname,))
    cur.execute("INSERT INTO sessions_fts (filename, body) VALUES (?, ?)", (fname, body))
    rowid = cur.lastrowid
    cur.execute(
        "INSERT INTO session_meta (filename, mtime, rowid_fts) VALUES (?, ?, ?)",
        (fname, os.path.getmtime(path), rowid),
    )


def index_sessions(*, l4_dir: str | None = None, db_path: str | None = None) -> dict[str, int]:
    """Incremental reindex. Returns ``{added: int, updated: int, removed: int}``.

    Only files whose mtime changed (or that are missing from the index)
    are touched — repeated calls are cheap (a single SELECT per file)."""
    files = _list_session_files(l4_dir)
    conn = _connect(db_path)
    added = updated = removed = 0
    try:
        with conn:
            existing = {r[0]: r[1] for r in conn.execute("SELECT filename, mtime FROM session_meta")}
            seen: set[str] = set()
            for path in files:
                fname = os.path.basename(path)
                seen.add(fname)
                cur_mtime = os.path.getmtime(path)
                old_mtime = existing.get(fname)
                if old_mtime is None:
                    _index_one(conn, path)
                    added += 1
                elif cur_mtime > old_mtime + 0.01:  # nudge for filesystems with coarse mtime
                    _index_one(conn, path)
                    updated += 1
            # Drop entries for files that disappeared.
            stale = set(existing) - seen
            for fname in stale:
                row = conn.execute("SELECT rowid_fts FROM session_meta WHERE filename = ?", (fname,)).fetchone()
                if row:
                    conn.execute("DELETE FROM sessions_fts WHERE rowid = ?", (row[0],))
                conn.execute("DELETE FROM session_meta WHERE filename = ?", (fname,))
                removed += 1
    finally:
        conn.close()
    return {"added": added, "updated": updated, "removed": removed}


def rebuild_index(*, l4_dir: str | None = None, db_path: str | None = None) -> dict[str, int]:
    """Drop the entire index and rebuild from scratch. Use after a manual
    edit to the L4 archive or when adding a new session log format."""
    db = db_path or _INDEX_PATH
    if os.path.exists(db):
        os.remove(db)
    return index_sessions(l4_dir=l4_dir, db_path=db)


# ─── Search ──────────────────────────────────────────────────────────────


_FTS5_SPECIAL = re.compile(r'[^\w\s\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')


def _sanitize_query(q: str) -> str:
    """Strip FTS5 syntax operators that an end-user / agent likely didn't
    intend (``"``, ``*``, ``NEAR``, ``-``, etc), then suffix each token with
    ``*`` for prefix matching.

    Why prefix-match by default: the trigram tokenizer indexes 3-character
    overlapping windows. A 2-character CJK query like ``"飞书"`` doesn't
    match any pure trigram on its own, but ``"飞书*"`` matches any indexed
    trigram that starts with those two characters — so ``"飞书"`` can find
    a session containing ``"飞书机器人"``. The same trick is harmlessly
    permissive for English (``"Feishu"`` → also matches ``"Feishun"`` if
    that ever appeared, which is a non-issue in practice).
    """
    # Replace special chars with space, then collapse whitespace.
    cleaned = _FTS5_SPECIAL.sub(" ", q)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    return " ".join(tok + "*" for tok in cleaned.split())


def search(query: str, *, top_k: int = 5, db_path: str | None = None,
           auto_reindex: bool = True, l4_dir: str | None = None) -> list[SearchHit]:
    """Full-text search across the L4 archive.

    Default ``auto_reindex=True`` runs the cheap incremental pass before
    every search so a freshly-rolled session is searchable immediately
    without an explicit reindex call. Disable for tests that want to
    measure search alone."""
    if auto_reindex:
        try:
            index_sessions(l4_dir=l4_dir, db_path=db_path)
        except Exception:
            # Don't let an indexing transient kill a search — the existing
            # index is still queryable.
            pass
    q = _sanitize_query(query)
    if not q:
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT s.filename, snippet(sessions_fts, 1, '«', '»', ' … ', 24), m.mtime, bm25(sessions_fts) "
            "FROM sessions_fts s JOIN session_meta m ON m.filename = s.filename "
            "WHERE sessions_fts MATCH ? "
            "ORDER BY bm25(sessions_fts) "
            "LIMIT ?",
            (q, max(1, int(top_k))),
        ).fetchall()
    except sqlite3.OperationalError:
        # Empty index, malformed query, etc. Return no hits rather than a stack trace.
        return []
    finally:
        conn.close()
    return [SearchHit(filename=r[0], snippet=r[1] or "", rank=float(r[3] or 0.0), mtime=float(r[2] or 0.0))
            for r in rows]


# ─── Agent-loop entry point ──────────────────────────────────────────────


def session_search(query: str, top_k: int = 5) -> str:
    """LLM-callable wrapper. Returns a plain-text summary safe to drop
    directly into a tool_result block.

    Failures (no L4 dir / FTS5 unavailable / empty query) come back as
    ``[session_search …]`` lines rather than raising — matches the rest
    of ``tools/sop_tools.py``'s contract.

    The trigram tokenizer needs a 3-character minimum query length (it
    indexes 3-char windows). Shorter queries get an explicit hint instead
    of silently returning no results — easier to debug for the agent."""
    q = (query or "").strip()
    if not q:
        return "[session_search error] query is required"
    sanitized = _sanitize_query(q)
    # Strip the trailing ``*`` we add and check length of any remaining token.
    longest_token = max((len(t.rstrip('*')) for t in sanitized.split()), default=0)
    if longest_token < 3:
        return (
            "[session_search hint] trigram index needs ≥ 3 characters per token. "
            f"Got {q!r} (longest token {longest_token} char). "
            "Try a longer phrase, e.g. an N-gram from the topic."
        )
    try:
        hits = search(q, top_k=max(1, min(int(top_k or 5), 25)))
    except RuntimeError as exc:
        return f"[session_search error] {exc}"
    except Exception as exc:
        return f"[session_search error] {type(exc).__name__}: {exc}"
    if not hits:
        return f"No archived sessions matched '{query}'."
    lines = [f"Found {len(hits)} matches for '{query}':"]
    for h in hits:
        # filename already encodes timestamp range (MMDD_HHMM-MMDD_HHMM.txt).
        lines.append(f"- `{h.filename}`")
        if h.snippet:
            # Snippets can be long; clip to keep tool_result tight.
            s = h.snippet.strip().replace("\n", " ")
            if len(s) > 200:
                s = s[:200] + "…"
            lines.append(f"  {s}")
    return "\n".join(lines)
