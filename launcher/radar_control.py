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
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "scripts" / "radar_runner.py"
PID_FILE = PROJECT_ROOT / "temp" / "radar_runner.pid"
LOG_FILE = PROJECT_ROOT / "temp" / "logs" / "radar_runner.log"
LOCK_PORT = 45765


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
    if n <= 0:
        return []
    if not LOG_FILE.is_file():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-n:]


def _lock_owner_pid() -> int:
    """Best-effort PID lookup for the runner's single-instance lock port."""
    try:
        import psutil  # type: ignore

        for conn in psutil.net_connections(kind="tcp"):
            laddr = getattr(conn, "laddr", None)
            if not laddr:
                continue
            port = getattr(laddr, "port", None)
            if port is None and len(laddr) >= 2:
                port = laddr[1]
            if port == LOCK_PORT and getattr(conn, "status", "") == psutil.CONN_LISTEN:
                return int(conn.pid or 0)
    except Exception:
        pass
    return 0


def _lock_port_in_use() -> bool:
    """Return True when another runner appears to hold the singleton lock."""
    if _lock_owner_pid():
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", LOCK_PORT))
            return False
    except OSError:
        return True


def _store_get(path: str, default: Any = "") -> Any:
    try:
        from launcher.config_store import default_store

        return default_store().get(path, default)
    except Exception:
        return default


def _env_get(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        from launcher.dotenv_shim import parse_env_file

        env = parse_env_file(str(PROJECT_ROOT / ".env"))
        return str(env.get(name) or "").strip()
    except Exception:
        return ""


def _mykey_get(name: str) -> str:
    try:
        import llmcore

        merged = llmcore.reload_mykeys(project_root=str(PROJECT_ROOT))[0]
        return str(merged.get(name) or "").strip()
    except Exception:
        return ""


def _split_watchlist(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = [str(v).strip() for v in value]
    else:
        raw = [s.strip() for s in str(value or "").replace("\n", ",").split(",")]
    return [s for s in raw if s and "/" in s][:20]


def _default_watchlist() -> list[str]:
    try:
        from tools.ecosystem_sources import DEFAULT_WATCHLIST

        return list(DEFAULT_WATCHLIST)
    except Exception:
        return []


def config_status() -> dict[str, Any]:
    """Non-secret radar dependency snapshot for GUI status rendering."""
    radar_settings = _store_get("settings.radar", {})
    if not isinstance(radar_settings, dict):
        radar_settings = {}

    fs_app_id = (
        _env_get("FS_APP_ID") or str(_store_get("bots.feishu.app_id", "")) or _mykey_get("fs_app_id")
    ).strip()
    fs_app_secret = (
        _env_get("FS_APP_SECRET")
        or str(_store_get("bots.feishu.app_secret", ""))
        or _mykey_get("fs_app_secret")
    ).strip()

    notify_to_explicit = (
        _env_get("WLWL_RADAR_NOTIFY_TO")
        or str(radar_settings.get("notify_to") or "")
        or _mykey_get("radar_notify_to")
    ).strip()
    notify_to = notify_to_explicit or "owner"

    grok_key = (
        _env_get("XAI_API_KEY") or str(_store_get("providers.grok.api_key", ""))
    ).strip()
    grok_model = (
        str(_store_get("providers.grok.model", "") or "").strip()
        or "grok-4-fast-reasoning"
    )
    tavily_key = (
        _env_get("TAVILY_API_KEY") or str(_store_get("settings.tavily.api_key", ""))
    ).strip()
    quiet_hours = (
        _env_get("WLWL_RADAR_QUIET_HOURS")
        or str(radar_settings.get("quiet_hours") or "22-8")
    ).strip()
    env_watchlist = _env_get("WLWL_RADAR_WATCHLIST")
    if env_watchlist:
        watchlist = _split_watchlist(env_watchlist)
    elif "watchlist" in radar_settings:
        watchlist = _split_watchlist(radar_settings.get("watchlist"))
    else:
        watchlist = _default_watchlist()

    missing: list[str] = []
    warnings: list[str] = []
    if not fs_app_id:
        missing.append("飞书 App ID")
    if not fs_app_secret:
        missing.append("飞书 App Secret")
    if not notify_to_explicit:
        missing.append("通知接收人")
    if not grok_key:
        warnings.append("Grok 实时源未配置；GitHub/HN 源仍可运行")

    return {
        "ready": not missing,
        "missing": missing,
        "warnings": warnings,
        "feishu": {
            "app_id_configured": bool(fs_app_id),
            "app_secret_configured": bool(fs_app_secret),
            "notify_to": notify_to,
            "notify_to_configured": bool(notify_to_explicit),
        },
        "sources": {
            "github": True,
            "hn": True,
            "grok": bool(grok_key),
            "grok_model": grok_model,
            "tavily": bool(tavily_key),
        },
        "quiet_hours": quiet_hours,
        "watchlist_count": len(watchlist),
    }


def status(*, log_lines: int = 20) -> dict[str, Any]:
    """Snapshot for the GUI: alive flag, PID, config health, and log tail."""
    pid_from_file = read_pid()
    pid = pid_from_file
    alive = is_alive(pid) if pid else False
    pid_source = "pid_file" if alive else "none"
    if not alive:
        lock_pid = _lock_owner_pid()
        if lock_pid and is_alive(lock_pid):
            pid = lock_pid
            alive = True
            pid_source = "lock_port"
        elif _lock_port_in_use():
            pid = 0
            alive = True
            pid_source = "lock_port"
    return {
        "alive": alive,
        "pid": pid,
        "pid_source": pid_source,
        "lock_port": LOCK_PORT,
        "config": config_status(),
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

    lock_pid = _lock_owner_pid()
    if lock_pid and is_alive(lock_pid):
        return {"ok": True, "alive": True, "pid": lock_pid, "message": "already running (lock port)"}
    if _lock_port_in_use():
        return {"ok": True, "alive": True, "pid": 0, "message": "already running (lock port)"}

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
        lock_pid = _lock_owner_pid()
        if lock_pid and is_alive(lock_pid):
            pid = lock_pid
        elif _lock_port_in_use():
            return {
                "ok": False,
                "killed": False,
                "pid": 0,
                "message": "running on lock port but pid unavailable",
            }
        else:
            return {"ok": True, "killed": False, "pid": 0, "message": "not running (no pid file)"}
    if not is_alive(pid):
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        lock_pid = _lock_owner_pid()
        if lock_pid and is_alive(lock_pid):
            pid = lock_pid
        else:
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

    if is_alive(pid):
        return {
            "ok": False,
            "killed": False,
            "pid": pid,
            "message": f"{killed_msg}; process still alive after {timeout_s:.1f}s",
        }

    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "killed": True, "pid": pid, "message": killed_msg}
