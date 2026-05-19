"""Ecosystem Radar — start/stop/status helpers shared by CLI + API server.

The radar runs as a detached subprocess (``scripts/radar_runner.py``) and
both the user-facing launcher (``scripts/start_radar.py``) and the GUI
backend (``launcher/api_server.py`` REST endpoints) need to start/stop/
inspect it. Without this shared module the two would drift apart.

All functions are side-effect-only on the radar process itself — they
never touch the seen-set, buffer, or notification recipient.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "scripts" / "radar_runner.py"
PID_FILE = PROJECT_ROOT / "temp" / "radar_runner.pid"
LOG_FILE = PROJECT_ROOT / "temp" / "logs" / "radar_runner.log"


def is_alive(pid: int) -> bool:
    """Cross-platform liveness check. Falls back to OS calls when psutil isn't
    available — the radar deps are intentionally narrow."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
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
                    capture_output=True, text=True, timeout=3,
                )
                return f" {pid} " in (r.stdout or "")
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def read_pid() -> int:
    """Returns the recorded PID, or 0 if no/invalid pid file."""
    if not PID_FILE.is_file():
        return 0
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def tail_log(n: int = 20) -> list[str]:
    """Last ``n`` lines from the runner log. Empty list when log absent."""
    if not LOG_FILE.is_file():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-n:] if n > 0 else lines


def status(*, log_lines: int = 20) -> dict[str, Any]:
    """Snapshot for the GUI: alive flag, PID, log tail."""
    pid = read_pid()
    return {
        "alive": is_alive(pid) if pid else False,
        "pid": pid,
        "log_path": str(LOG_FILE),
        "log_tail": tail_log(log_lines),
    }


def _detached_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def start() -> dict[str, Any]:
    """Spawn the radar runner if not already alive. Idempotent.

    Returns: ``{ok, alive, pid, message}``. ``ok`` is True even if it was
    already running — that's the desired "you asked, it's running" outcome.
    """
    existing = read_pid()
    if existing and is_alive(existing):
        return {"ok": True, "alive": True, "pid": existing, "message": "already running"}
    if existing:
        # Stale pid file (process died without cleaning up). Clear it.
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    if not RUNNER.is_file():
        return {"ok": False, "alive": False, "pid": 0,
                "message": f"runner missing: {RUNNER}"}

    # Health pre-check — if the runner can't even parse its tasks, fail fast
    # with the actual error rather than spawning a process that immediately
    # crashes.
    try:
        pre = subprocess.run(
            [sys.executable, str(RUNNER), "--check-only"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "alive": False, "pid": 0,
                "message": f"health check spawn failed: {exc}"}
    if pre.returncode != 0:
        return {"ok": False, "alive": False, "pid": 0,
                "message": f"health check failed (rc={pre.returncode}): "
                           f"{(pre.stderr or pre.stdout or '').strip()[:400]}"}

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(
            [sys.executable, str(RUNNER)],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_detached_flags(),
            close_fds=True,
        )
    except Exception as exc:
        log.close()
        return {"ok": False, "alive": False, "pid": 0,
                "message": f"spawn failed: {exc}"}
    log.close()

    # Wait for the runner to write its own pid file (it does this on startup
    # — see scripts/radar_runner.py). 3s is enough on every machine we've
    # tested; on a heavily loaded box bump if needed.
    for _ in range(30):
        time.sleep(0.1)
        if PID_FILE.is_file() and is_alive(p.pid):
            return {"ok": True, "alive": True, "pid": p.pid, "message": "spawned"}

    # PID file didn't appear or process died — surface a useful error.
    alive = is_alive(p.pid)
    msg = "spawned but pid-file/liveness unconfirmed" if alive else "process exited immediately; see log"
    return {"ok": alive, "alive": alive, "pid": p.pid, "message": msg}


def stop(*, timeout_s: float = 5.0) -> dict[str, Any]:
    """Stop the runner. No-op when not running. Always clears the PID file."""
    pid = read_pid()
    if not pid:
        return {"ok": True, "killed": False, "pid": 0, "message": "not running (no pid file)"}
    if not is_alive(pid):
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": True, "killed": False, "pid": pid, "message": "stale pid cleaned"}

    try:
        if os.name == "nt":
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=timeout_s,
            )
            killed_msg = f"taskkill rc={r.returncode}"
        else:
            import signal as _sig
            os.kill(pid, _sig.SIGTERM)
            killed_msg = "SIGTERM sent"
    except Exception as exc:
        return {"ok": False, "killed": False, "pid": pid,
                "message": f"kill failed: {exc}"}

    # Brief wait for the process to actually exit, then verify.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_alive(pid):
            break
        time.sleep(0.1)

    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "killed": True, "pid": pid, "message": killed_msg}
