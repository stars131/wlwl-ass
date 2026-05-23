"""Local playbook for reviewed execution experience.

This is the lightweight wlwl-ass version of Sico's playbook idea: successful
execution lessons become durable, reviewed strategies that can be injected into
future agent runs. It intentionally avoids Sico's service stack, database, and
LLM curator pipeline; the local contract is append/propose, review, then prompt
injection of accepted entries only.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Status = Literal["pending", "active", "rejected"]

_LOCK = threading.Lock()
_MAX_CONTENT = 800
_MAX_RATIONALE = 800
_VALID_STATUS = {"pending", "active", "rejected"}


@dataclass
class PlaybookEntry:
    id: str
    category: str
    content: str
    status: Status = "pending"
    rationale: str = ""
    source: str = ""
    source_turn: int | None = None
    source_session: str = ""
    tags: list[str] = field(default_factory=list)
    helpful: int = 0
    harmful: int = 0
    created_at: str = field(default_factory=lambda: _now_iso())
    decided_at: str | None = None
    decision_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlaybookEntry":
        data = dict(payload)
        data.setdefault("status", "pending")
        data.setdefault("rationale", "")
        data.setdefault("source", "")
        data.setdefault("source_turn", None)
        data.setdefault("source_session", "")
        data.setdefault("tags", [])
        data.setdefault("helpful", 0)
        data.setdefault("harmful", 0)
        data.setdefault("created_at", _now_iso())
        data.setdefault("decided_at", None)
        data.setdefault("decision_note", None)
        if data["status"] not in _VALID_STATUS:
            data["status"] = "pending"
        if not isinstance(data["tags"], list):
            data["tags"] = []
        return cls(**data)


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def playbook_path() -> str:
    override = os.environ.get("WLWL_PLAYBOOK_PATH")
    if override:
        return override
    return os.path.join(_project_root(), "memory", "playbook.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _gen_id() -> str:
    return f"pb_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"


def _clean_text(value: Any, *, max_len: int) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:max_len].rstrip()


def _read_all_unlocked() -> list[PlaybookEntry]:
    path = playbook_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("entries", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[PlaybookEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(PlaybookEntry.from_dict(row))
        except TypeError:
            continue
    return out


def _write_all_unlocked(entries: list[PlaybookEntry]) -> None:
    path = playbook_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "entries": [entry.to_dict() for entry in entries],
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_entries(*, status: Status | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Return playbook entries, newest first."""
    with _LOCK:
        entries = _read_all_unlocked()
    if status:
        entries = [entry for entry in entries if entry.status == status]
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    if limit is not None:
        entries = entries[: max(0, int(limit))]
    return [entry.to_dict() for entry in entries]


def propose(
    content: str,
    *,
    category: str = "lesson",
    rationale: str = "",
    source: str = "",
    source_turn: int | None = None,
    source_session: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a playbook entry for user review.

    Accepted entries are injected into future prompts. Pending entries are only
    visible through the review/list APIs.
    """
    content = _clean_text(content, max_len=_MAX_CONTENT)
    if not content:
        raise ValueError("content is required")
    category = _clean_text(category or "lesson", max_len=80) or "lesson"
    rationale = _clean_text(rationale, max_len=_MAX_RATIONALE)
    source = _clean_text(source, max_len=120)
    source_session = _clean_text(source_session, max_len=200)
    clean_tags = [_clean_text(tag, max_len=40) for tag in (tags or [])]
    clean_tags = [tag for tag in clean_tags if tag][:8]

    with _LOCK:
        entries = _read_all_unlocked()
        for entry in entries:
            if entry.status in {"pending", "active"} and entry.category == category and entry.content == content:
                return entry.to_dict()
        entry = PlaybookEntry(
            id=_gen_id(),
            category=category,
            content=content,
            rationale=rationale,
            source=source,
            source_turn=source_turn,
            source_session=source_session,
            tags=clean_tags,
        )
        entries.append(entry)
        _write_all_unlocked(entries)
    return entry.to_dict()


def accept(entry_id: str, *, note: str = "") -> tuple[bool, str]:
    return _decide(entry_id, status="active", note=note or "accepted by user")


def reject(entry_id: str, *, note: str = "") -> tuple[bool, str]:
    return _decide(entry_id, status="rejected", note=note or "rejected by user")


def _decide(entry_id: str, *, status: Status, note: str) -> tuple[bool, str]:
    if status not in {"active", "rejected"}:
        raise ValueError("status must be active or rejected")
    with _LOCK:
        entries = _read_all_unlocked()
        entry = next((item for item in entries if item.id == entry_id), None)
        if entry is None:
            return False, f"playbook entry {entry_id!r} not found"
        if entry.status != "pending":
            return False, f"playbook entry already {entry.status}"
        entry.status = status
        entry.decided_at = _now_iso()
        entry.decision_note = _clean_text(note, max_len=300)
        _write_all_unlocked(entries)
    return True, entry.decision_note or status


def render_prompt(*, limit: int = 12, max_chars: int = 2400) -> str:
    """Render accepted playbook entries for system prompt injection."""
    with _LOCK:
        entries = [entry for entry in _read_all_unlocked() if entry.status == "active"]
    if not entries:
        return ""
    entries.sort(key=lambda entry: (entry.helpful - entry.harmful, entry.created_at), reverse=True)
    lines = ["\n[Playbook] Reviewed execution lessons:"]
    for entry in entries[: max(1, int(limit))]:
        score = ""
        if entry.helpful or entry.harmful:
            score = f" (helpful={entry.helpful}, harmful={entry.harmful})"
        lines.append(f"- [{entry.category}] {entry.content}{score}")
    rendered = "\n".join(lines) + "\n"
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars].rstrip() + "\n[Playbook truncated]\n"


def stats() -> dict[str, Any]:
    with _LOCK:
        entries = _read_all_unlocked()
    counts = {status: 0 for status in sorted(_VALID_STATUS)}
    categories: dict[str, int] = {}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
        if entry.status == "active":
            categories[entry.category] = categories.get(entry.category, 0) + 1
    return {
        "path": playbook_path(),
        "total": len(entries),
        "status": counts,
        "active_categories": categories,
    }
