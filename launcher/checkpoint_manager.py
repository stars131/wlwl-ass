"""Checkpoint manager for long-running agent tasks.

Use case: a self-driving research/coding task runs for 40 minutes, hits a
context limit or user interrupt, and loses everything. With checkpoints,
the agent (or the user) can:

  * ``save(task_id, state, note=...)`` periodically — atomic write, kept
    forever until ``clear`` or trimmed by ``max_per_task``.
  * ``load(task_id, checkpoint_id=None)`` returns the latest (or named)
    checkpoint dict.
  * ``list_checkpoints(task_id)`` enumerates them.
  * ``list_tasks()`` returns every task that has at least one checkpoint.
  * ``clear(task_id)`` deletes them all.

Layout on disk: ``temp/checkpoints/<task_id>/<ts>_<rand>.json``. Each file
is ``{id, task_id, saved_at, note, state}``. ``state`` is whatever JSON-
serialisable blob the caller provides — typically the agent's working
memory + cursor / iteration index.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from typing import Any

_DEFAULT_DIR = os.path.join("temp", "checkpoints")
_MAX_PER_TASK = 50  # ring-buffer cap; oldest dropped when exceeded


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_segment(s: str) -> str:
    """Restrict task_id to a filesystem-safe slug."""
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s))
    return out[:120] or "task"


class CheckpointManager:
    def __init__(self, base_dir: str, *, root: str | None = None, max_per_task: int = _MAX_PER_TASK):
        self.base_dir = base_dir
        self.root = root or os.path.join(base_dir, _DEFAULT_DIR)
        self.max_per_task = max(1, int(max_per_task))
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    # ── private ───────────────────────────────────────────────────────

    def _task_dir(self, task_id: str) -> str:
        return os.path.join(self.root, _safe_segment(task_id))

    def _ensure_task_dir(self, task_id: str) -> str:
        path = self._task_dir(task_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _gen_id(self) -> str:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"{ts}_{secrets.token_hex(3)}"

    def _files(self, task_id: str) -> list[str]:
        d = self._task_dir(task_id)
        if not os.path.isdir(d):
            return []
        return sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")),
        )

    # ── public API ────────────────────────────────────────────────────

    def save(self, task_id: str, state: Any, *, note: str = "") -> dict[str, Any]:
        """Persist ``state`` as a new checkpoint. Returns the metadata dict
        (without the state body, to avoid round-tripping huge payloads)."""
        if not task_id:
            raise ValueError("task_id is required")
        with self._lock:
            d = self._ensure_task_dir(task_id)
            cp_id = self._gen_id()
            entry = {
                "id": cp_id,
                "task_id": str(task_id),
                "saved_at": _now_iso(),
                "note": str(note or ""),
                "state": state,
            }
            tmp = os.path.join(d, f".{cp_id}.tmp")
            final = os.path.join(d, f"{cp_id}.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, final)
            self._trim(task_id)
        return {k: v for k, v in entry.items() if k != "state"}

    def _trim(self, task_id: str) -> None:
        files = self._files(task_id)
        excess = len(files) - self.max_per_task
        for path in files[:max(0, excess)]:
            try:
                os.remove(path)
            except OSError:
                pass

    def load(self, task_id: str, *, checkpoint_id: str | None = None) -> dict[str, Any] | None:
        """Return the named checkpoint, or the most recent one if id is None.

        Returns None if no checkpoint exists for this task / id."""
        files = self._files(task_id)
        if not files:
            return None
        if checkpoint_id:
            for f in files:
                if os.path.basename(f) == f"{checkpoint_id}.json":
                    return self._read(f)
            return None
        return self._read(files[-1])

    def _read(self, path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        out = []
        for f in self._files(task_id):
            data = self._read(f)
            if not data:
                continue
            # Strip ``state`` from list response — call ``load`` to fetch.
            out.append({k: v for k, v in data.items() if k != "state"})
        return out

    def list_tasks(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.root):
            return []
        out = []
        for name in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, name)
            if not os.path.isdir(d):
                continue
            files = self._files(name)
            if not files:
                continue
            latest = self._read(files[-1]) or {}
            out.append({
                "task_id": name,
                "count": len(files),
                "latest_saved_at": latest.get("saved_at"),
                "latest_note": latest.get("note", ""),
            })
        return out

    def clear(self, task_id: str) -> int:
        """Delete every checkpoint for ``task_id``. Returns count removed."""
        files = self._files(task_id)
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        d = self._task_dir(task_id)
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
        return len(files)


# ── singleton helper ──────────────────────────────────────────────────

_singleton: CheckpointManager | None = None
_singleton_lock = threading.Lock()


def get_manager(base_dir: str | None = None) -> CheckpointManager:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            root = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _singleton = CheckpointManager(root)
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
