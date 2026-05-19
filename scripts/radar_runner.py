"""Standalone ecosystem-radar scheduler.

Why this exists (and not reusing ``agentmain.py --reflect reflect/scheduler.py``):

  * ``agentmain.py`` requires at least one configured LLM provider to boot.
    When the user only wants the radar (Feishu watcher + digest), forcing
    them to configure an Anthropic / OpenAI key is overkill — the radar
    can run with just the free-pool vault and the feishu app.
  * The radar tasks under ``sche_tasks/ecosystem_*.json`` don't need to be
    dispatched through the agent loop — they're pure function calls into
    ``memory.ecosystem_radar.orchestrate``. So we skip the heavyweight
    path and directly invoke them here.

Schedule semantics match ``reflect/scheduler.py`` byte-for-byte (so the
JSON files are interchangeable): ``schedule`` (HH:MM), ``repeat``
(daily/weekday/weekly/every_Nh/every_Nd/once), ``enabled``,
``max_delay_hours``. Cooldown is determined by the most recent file in
``sche_tasks/done/`` matching ``*_<tid>.md`` — same convention.

This loop only watches files matching ``ecosystem_*.json`` so other
sche_tasks (if any later) keep working through the normal agent path.

Usage:
    python scripts/radar_runner.py                  # foreground
    python scripts/radar_runner.py --once           # tick once, exit
    python scripts/radar_runner.py --check-only     # print health, exit
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Side-effect: load .env into os.environ so WLWL_RADAR_NOTIFY_TO etc.
# reach the orchestrator on a fresh Windows process that wasn't launched
# with env vars pre-set.
_envfile = PROJECT_ROOT / ".env"
if _envfile.is_file():
    for _line in _envfile.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from memory import ecosystem_radar  # noqa: E402

TASKS_DIR = PROJECT_ROOT / "sche_tasks"
DONE_DIR = TASKS_DIR / "done"
LOG_PATH = PROJECT_ROOT / "temp" / "logs" / "radar_runner.log"
DEFAULT_INTERVAL_S = 120
DEFAULT_MAX_DELAY_H = 6

# Single-instance lock — same idea as reflect/scheduler.py:8 but on a
# different port so the two can coexist if the user ever runs both.
_LOCK_PORT = 45765


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("radar_runner")
    if not log.handlers:
        log.setLevel(logging.INFO)
        h = logging.FileHandler(LOG_PATH, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                         datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        # Also mirror to stdout for foreground runs / nohup tail.
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s",
                                          datefmt="%H:%M:%S"))
        log.addHandler(sh)
    return log


def _acquire_lock(logger: logging.Logger) -> socket.socket | None:
    """Bind a TCP port as a single-instance lock. None on conflict."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", _LOCK_PORT))
        s.listen(1)
        return s
    except OSError as exc:
        logger.error(f"another radar_runner is already running (port {_LOCK_PORT} bound): {exc}")
        return None


# ── schedule logic (matches reflect/scheduler.py semantics) ───────────


def _parse_cooldown(repeat: str) -> timedelta:
    if repeat == "once":
        return timedelta(days=999999)
    if repeat in ("daily", "weekday"):
        return timedelta(hours=20)
    if repeat == "weekly":
        return timedelta(days=6)
    if repeat == "monthly":
        return timedelta(days=27)
    if repeat.startswith("every_"):
        try:
            tag = repeat.split("_", 1)[1]
            n = int(tag.rstrip("hdm"))
            u = tag[-1]
            if u == "h":
                return timedelta(hours=n)
            if u == "m":
                return timedelta(minutes=n)
            if u == "d":
                return timedelta(days=n)
        except (ValueError, IndexError):
            pass
    return timedelta(hours=20)


def _last_run(tid: str, done_files: set[str]) -> datetime | None:
    latest: datetime | None = None
    for df in done_files:
        if not df.endswith(f"_{tid}.md"):
            continue
        try:
            t = datetime.strptime(df[:15], "%Y-%m-%d_%H%M")
        except ValueError:
            continue
        if latest is None or t > latest:
            latest = t
    return latest


def _mode_from_task_prompt(prompt: str) -> str | None:
    """Map the task prompt to an orchestrator mode. The prompts encode the
    intent in plain text (mode='watch' / 'poc' / 'digest') so we don't need
    a separate field; cheaper than re-defining the schema."""
    p = (prompt or "").lower()
    for mode in ("watch", "poc", "digest"):
        if f"mode='{mode}'" in p or f'mode="{mode}"' in p:
            return mode
    return None


def _stamp_done(tid: str, dt: datetime, summary: str) -> Path:
    """Write a stub done file so cooldown tracking works. Same name
    convention as reflect/scheduler.py uses."""
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{dt.strftime('%Y-%m-%d_%H%M')}_{tid}.md"
    p = DONE_DIR / name
    p.write_text(f"# {tid} @ {dt:%Y-%m-%d %H:%M}\n\n{summary}\n", encoding="utf-8")
    return p


def _tick(logger: logging.Logger) -> int:
    """One scheduler pass. Returns the number of tasks fired."""
    if not TASKS_DIR.is_dir():
        return 0
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    done_files = {f.name for f in DONE_DIR.glob("*.md")}
    now = datetime.now()
    fired = 0
    for tf in sorted(TASKS_DIR.glob("ecosystem_*.json")):
        tid = tf.stem
        try:
            task = json.loads(tf.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"{tid}: json parse error: {exc}")
            continue
        if not task.get("enabled", False):
            continue
        repeat = task.get("repeat", "daily")
        sched = task.get("schedule", "00:00")
        try:
            h, m = (int(x) for x in sched.split(":"))
        except Exception:
            logger.error(f"{tid}: bad schedule {sched!r}")
            continue
        if repeat == "weekday" and now.weekday() >= 5:
            continue
        if now.hour < h or (now.hour == h and now.minute < m):
            continue
        max_delay = int(task.get("max_delay_hours") or DEFAULT_MAX_DELAY_H)
        sched_mins = h * 60 + m
        now_mins = now.hour * 60 + now.minute
        if now_mins - sched_mins > max_delay * 60:
            continue
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        if last and (now - last) < cooldown:
            continue

        mode = _mode_from_task_prompt(task.get("prompt", ""))
        if mode is None:
            logger.error(f"{tid}: could not detect mode= from prompt — skipping")
            continue
        logger.info(f"TRIGGER {tid} mode={mode} (last_run={last}, cooldown={cooldown})")
        try:
            result = ecosystem_radar.orchestrate(mode)
        except Exception as exc:
            logger.exception(f"{tid}: orchestrate({mode}) crashed: {exc}")
            _stamp_done(tid, now, f"crashed: {exc}")
            done_files.add(f"{now.strftime('%Y-%m-%d_%H%M')}_{tid}.md")
            continue

        # For poc mode, the orchestrator returns a prompt the agent should
        # execute next. Without an agent online, we can't actually run the
        # PoC — so we Feishu-notify the user that there's a candidate
        # waiting and stash the prompt to a staging file. The cooldown
        # still stamps so we don't retry every minute.
        if mode == "poc" and result.get("prompt"):
            sel = result.get("selected") or {}
            staging = PROJECT_ROOT / "temp" / "radar_pending_poc.md"
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(result["prompt"], encoding="utf-8")
            try:
                from tools.feishu import feishu_send
                from memory.ecosystem_radar import _notify_recipient
                feishu_send(
                    _notify_recipient(),
                    f"🧪 [雷达·PoC 候选] {sel.get('repo')} (tier={sel.get('tier')}, "
                    f"score={sel.get('score')})\n"
                    f"已写入 temp/radar_pending_poc.md。\n"
                    f"在 wlwl REPL 里跑：ecosystem_radar mode=poc force_repo={sel.get('repo')}"
                )
            except Exception as exc:
                logger.error(f"feishu poc-notify failed: {exc}")

        _stamp_done(tid, now, json.dumps(result, ensure_ascii=False, indent=2))
        done_files.add(f"{now.strftime('%Y-%m-%d_%H%M')}_{tid}.md")
        fired += 1

    return fired


def _health(logger: logging.Logger) -> dict:
    """Print all task status — useful for --check-only."""
    out = {"tasks": [], "now": datetime.now().isoformat(timespec="seconds")}
    if not TASKS_DIR.is_dir():
        out["error"] = f"no {TASKS_DIR}"
        return out
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    done_files = {f.name for f in DONE_DIR.glob("*.md")}
    for tf in sorted(TASKS_DIR.glob("ecosystem_*.json")):
        tid = tf.stem
        try:
            task = json.loads(tf.read_text(encoding="utf-8"))
        except Exception as exc:
            out["tasks"].append({"id": tid, "status": "PARSE_ERROR", "error": str(exc)})
            continue
        last = _last_run(tid, done_files)
        out["tasks"].append({
            "id": tid,
            "enabled": task.get("enabled", False),
            "repeat": task.get("repeat"),
            "schedule": task.get("schedule"),
            "last_run": last.isoformat() if last else None,
            "next_eligible_at": (last + _parse_cooldown(task.get("repeat", "daily"))).isoformat() if last else "anytime",
        })
    return out


# ── main ─────────────────────────────────────────────────────────────


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Ecosystem radar standalone scheduler")
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    p.add_argument("--check-only", action="store_true", help="print task health and exit")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S, help="seconds between ticks (default 120)")
    args = p.parse_args()

    logger = _setup_logger()

    if args.check_only:
        print(json.dumps(_health(logger), ensure_ascii=False, indent=2))
        return 0

    if not args.once:
        lock = _acquire_lock(logger)
        if lock is None:
            return 2

    logger.info(f"radar_runner started (interval={args.interval}s, cwd={PROJECT_ROOT}, pid={os.getpid()})")
    pid_file = PROJECT_ROOT / "temp" / "radar_runner.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"could not write pid file: {exc}")

    try:
        while True:
            try:
                fired = _tick(logger)
                if fired:
                    logger.info(f"tick fired {fired} task(s)")
            except Exception as exc:
                logger.exception(f"tick crashed: {exc}")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("radar_runner interrupted, exiting")
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
