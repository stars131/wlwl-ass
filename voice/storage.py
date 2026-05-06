"""Per-session voice storage — audio.opus + transcript.jsonl + manifest.json.

Storage layout (one dir per session under temp/voice_sessions/):

  <session_uuid>/
    audio.opus        # raw concatenation of inbound audio frames
    transcript.jsonl  # per-turn events
    manifest.json     # written on close

Retention: a one-shot `gc()` call wipes sessions older than `max_age_days`
and trims oldest-first when total bytes exceed `max_bytes`. Caller decides
when to gc — typically agent startup.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("wlwl_ass.voice.storage")


@dataclass
class TurnEvent:
    role: str                     # "user" | "agent" | "system"
    kind: str                     # "stt" | "tts" | "event"
    text: str = ""
    extra: dict = field(default_factory=dict)


def _default_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "temp", "voice_sessions")


class VoiceSession:
    def __init__(self, *, session_id: str | None = None, root: str | None = None,
                 forget_on_close: bool = False) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.root = root or _default_root()
        self.dir = os.path.join(self.root, self.session_id)
        os.makedirs(self.dir, exist_ok=True)
        self._audio = open(os.path.join(self.dir, "audio.opus"), "ab", buffering=0)
        self._transcript = open(os.path.join(self.dir, "transcript.jsonl"), "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._started = time.time()
        self._intents: list[str] = []
        self._workers: list[str] = []
        self._exit_reason = "unset"
        self._forget = forget_on_close

    # ── append paths ─────

    def append_audio(self, blob: bytes) -> None:
        with self._lock:
            self._audio.write(blob)

    def append_event(self, event: TurnEvent) -> None:
        rec = {"t": time.time(), "role": event.role, "kind": event.kind,
               "text": event.text, **event.extra}
        with self._lock:
            self._transcript.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._transcript.flush()
            if event.kind == "event" and event.extra.get("intent"):
                self._intents.append(event.extra["intent"])
            if event.kind == "event" and event.extra.get("worker"):
                self._workers.append(event.extra["worker"])

    def mark_forget(self) -> None:
        self._forget = True

    def set_exit_reason(self, reason: str) -> None:
        self._exit_reason = reason

    def close(self) -> None:
        with self._lock:
            try:
                self._audio.close()
                self._transcript.close()
            except Exception:
                log.exception("close failed")
            if self._forget:
                shutil.rmtree(self.dir, ignore_errors=True)
                return
            manifest = {
                "session_id": self.session_id,
                "start_at": self._started,
                "end_at": time.time(),
                "duration_s": time.time() - self._started,
                "exit_reason": self._exit_reason,
                "intents_detected": list(self._intents),
                "workers_invoked": list(self._workers),
            }
            try:
                with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
            except OSError:
                log.exception("manifest write failed")


def list_sessions(root: str | None = None) -> list[dict]:
    """List past sessions, newest first. Each entry includes the manifest if present."""
    root = root or _default_root()
    if not os.path.isdir(root):
        return []
    entries = []
    for name in os.listdir(root):
        sdir = os.path.join(root, name)
        if not os.path.isdir(sdir):
            continue
        m_path = os.path.join(sdir, "manifest.json")
        m = {}
        if os.path.isfile(m_path):
            try:
                with open(m_path, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        entries.append({"session_id": name, "dir": sdir,
                        "mtime": os.path.getmtime(sdir), "manifest": m})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def forget_session(session_id: str, *, root: str | None = None) -> bool:
    root = root or _default_root()
    sdir = os.path.join(root, session_id)
    if not os.path.isdir(sdir):
        return False
    shutil.rmtree(sdir, ignore_errors=True)
    return True


def gc(*, root: str | None = None, max_age_days: int = 30, max_bytes: int = 200 * 1024 * 1024) -> dict:
    """One-shot retention sweep. Returns ``{deleted: int, kept: int}``."""
    root = root or _default_root()
    if not os.path.isdir(root):
        return {"deleted": 0, "kept": 0}
    cutoff = time.time() - max_age_days * 86400
    sessions = list_sessions(root)
    deleted = 0
    for s in sessions[:]:
        if s["mtime"] < cutoff:
            forget_session(s["session_id"], root=root)
            sessions.remove(s)
            deleted += 1
    # size-cap
    total = sum(_dir_size(s["dir"]) for s in sessions)
    sessions_oldest_first = sorted(sessions, key=lambda x: x["mtime"])
    for s in sessions_oldest_first:
        if total <= max_bytes:
            break
        sz = _dir_size(s["dir"])
        forget_session(s["session_id"], root=root)
        total -= sz
        deleted += 1
    kept = max(0, len(sessions) - deleted)
    return {"deleted": deleted, "kept": kept}


def _dir_size(d: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
