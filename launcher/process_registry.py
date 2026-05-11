"""Process registry for agent-side background processes.

Use case: an agent spawns shells, browsers, long Python scripts, and tools
that run for minutes. Without a registry the agent has no way to enumerate
them later — PIDs scroll off chat history, ``ps`` is platform-specific, and
zombies pile up.

This module gives a single API:

  * ``register(label, pid, *, kind="proc", cmd=None, meta=None)``
  * ``unregister(pid)`` / ``unregister_label(label)``
  * ``list()`` → ``[{label, pid, kind, cmd, started_at, alive}]``
  * ``kill(pid_or_label, *, force=False)`` → ``(ok, message)``
  * ``cleanup_dead()`` — drops entries whose PIDs are no longer alive.

State is held in ``temp/process_registry.json`` so a restart can still see
processes the agent spawned last session (and reap them if dead).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DEFAULT_PATH = "temp/process_registry.json"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_alive(pid: int) -> bool:
    """Cross-platform liveness check. Returns False if pid is None/0/<=0."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    except ImportError:
        if os.name == "nt":
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=3,
                    creationflags=CREATE_NO_WINDOW,
                )
                return f" {pid} " in r.stdout
            except Exception:
                return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                return False


class ProcessRegistry:
    """Persistent process registry. Thread-safe."""

    def __init__(self, base_dir: str, *, path: str | None = None):
        self.base_dir = base_dir
        self.path = path or os.path.join(base_dir, _DEFAULT_PATH)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._entries: list[dict[str, Any]] = []
        self._load()

    # ── persistence ───────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            self._entries = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._entries = raw.get("entries", []) if isinstance(raw, dict) else []
        except Exception:
            self._entries = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": self._entries}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── core API ──────────────────────────────────────────────────────

    def register(
        self,
        label: str,
        pid: int,
        *,
        kind: str = "proc",
        cmd: list[str] | str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a running process. Returns the stored entry.

        ``label`` is a human-readable handle (e.g. "scrape-twitter-1") that
        callers can use in place of the OS pid for kill/lookup. Labels are
        not unique — the registry uses pid as the primary key, which IS
        unique for the lifetime of an entry.
        """
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        entry = {
            "label": str(label or f"proc-{pid}"),
            "pid": pid,
            "kind": str(kind or "proc"),
            "cmd": cmd if isinstance(cmd, str) else (" ".join(cmd) if cmd else ""),
            "started_at": _now_iso(),
            "meta": meta or {},
        }
        with self._lock:
            self._entries = [e for e in self._entries if e.get("pid") != pid]
            self._entries.append(entry)
            self._save()
        return dict(entry)

    def unregister(self, pid: int) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("pid") != pid]
            changed = len(self._entries) != before
            if changed:
                self._save()
            return changed

    def unregister_label(self, label: str) -> int:
        """Drop ALL entries with this label. Returns count removed."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("label") != label]
            removed = before - len(self._entries)
            if removed:
                self._save()
            return removed

    def list(self) -> list[dict[str, Any]]:
        """Return all entries with a fresh ``alive`` flag injected."""
        with self._lock:
            out = []
            for e in self._entries:
                row = dict(e)
                row["alive"] = _is_alive(int(e.get("pid", 0)))
                out.append(row)
            return out

    def get_by_label(self, label: str) -> list[dict[str, Any]]:
        return [e for e in self.list() if e.get("label") == label]

    def kill(self, pid_or_label: int | str, *, force: bool = False, timeout: float = 5.0) -> tuple[bool, str]:
        """Kill the process. Accepts pid (int) or label (str — kills all matching).

        Tries graceful terminate first; if ``force`` is True or terminate
        times out, escalates to taskkill /F (Windows) / SIGKILL (POSIX).
        Returns (ok, message). Always unregisters the entry.
        """
        # Resolve to a list of pids.
        targets: list[dict[str, Any]] = []
        if isinstance(pid_or_label, int):
            for e in self.list():
                if e.get("pid") == pid_or_label:
                    targets.append(e)
                    break
        else:
            targets = [e for e in self.list() if e.get("label") == pid_or_label]
        if not targets:
            return False, f"no process registered for {pid_or_label!r}"

        msgs: list[str] = []
        for entry in targets:
            pid = int(entry["pid"])
            if not _is_alive(pid):
                self.unregister(pid)
                msgs.append(f"pid={pid} already dead, unregistered")
                continue
            if os.name == "nt":
                args = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    args.append("/F")
                try:
                    r = subprocess.run(
                        args,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    msgs.append(f"pid={pid} taskkill rc={r.returncode}")
                except Exception as exc:
                    msgs.append(f"pid={pid} taskkill failed: {exc}")
            else:
                try:
                    import signal
                    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
                    msgs.append(f"pid={pid} signaled")
                except Exception as exc:
                    msgs.append(f"pid={pid} kill failed: {exc}")
            # Wait for it to actually die before unregistering.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not _is_alive(pid):
                    break
                time.sleep(0.1)
            self.unregister(pid)
        return True, "; ".join(msgs)

    def cleanup_dead(self) -> int:
        """Drop entries whose pids are no longer alive. Returns count removed."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if _is_alive(int(e.get("pid", 0)))]
            removed = before - len(self._entries)
            if removed:
                self._save()
            return removed


# ── module-level singleton helpers (for ergonomic ``from ... import`` use) ──

_singleton: ProcessRegistry | None = None
_singleton_lock = threading.Lock()


def get_registry(base_dir: str | None = None) -> ProcessRegistry:
    """Return a process-wide singleton. ``base_dir`` is honored only on first
    call; subsequent calls reuse the existing instance regardless."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            root = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _singleton = ProcessRegistry(root)
        return _singleton


def reset_singleton_for_tests() -> None:
    """Tests use this between cases to avoid carrying state across files."""
    global _singleton
    with _singleton_lock:
        _singleton = None
