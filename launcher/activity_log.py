"""Lightweight observability for the agent loop.

Every tool dispatch and turn boundary is appended as a JSON line to a daily
file under ``<project_root>/temp/activity/YYYY-MM-DD.jsonl``. The GUI reads
recent entries and tails the current file via SSE so the user can see what
the agent is doing in real time.

Design goals:
  * Zero new dependencies — stdlib json + os only.
  * Zero cost when disabled (env ``WLWL_ACTIVITY_LOG_OFF=1``) — single bool check.
  * Append-only, line-oriented — safe to tail concurrently from another process.
  * Bounded retention — old day files trimmed to the last ``RETENTION_DAYS``.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from typing import Any, Iterable

RETENTION_DAYS = 7
_MAX_ARG_CHARS = 400  # truncate huge args (paste of 5MB file etc.) before persisting.

_LOCK = threading.Lock()
_LAST_TRIM_DAY: str | None = None


def _disabled() -> bool:
    return os.environ.get("WLWL_ACTIVITY_LOG_OFF", "") in ("1", "true", "yes")


def _project_root() -> str:
    # launcher/ sits one level under the repo root.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def activity_dir() -> str:
    override = os.environ.get("WLWL_ACTIVITY_LOG_DIR")
    if override:
        return override
    return os.path.join(_project_root(), "temp", "activity")


def latest_path(day: str | None = None) -> str:
    if day is None:
        day = _dt.datetime.now().strftime("%Y-%m-%d")
    return os.path.join(activity_dir(), f"{day}.jsonl")


def compact_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of args with internals stripped and big strings clipped."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str) and len(v) > _MAX_ARG_CHARS:
            out[k] = v[:_MAX_ARG_CHARS] + f"…(+{len(v) - _MAX_ARG_CHARS} chars)"
        else:
            out[k] = v
    return out


def record(event: dict[str, Any]) -> None:
    """Append one event line. Failures are swallowed — observability must
    never break the agent loop."""
    if _disabled():
        return
    try:
        now_local = _dt.datetime.now()
        now_utc = now_local.astimezone(_dt.timezone.utc)
        enriched = {
            "ts": now_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "pid": os.getpid(),
            **event,
        }
        path = latest_path(now_local.strftime("%Y-%m-%d"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(enriched, ensure_ascii=False, default=str)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_trim_old_days(now_local)
    except Exception:
        # Never raise — this is best-effort instrumentation.
        pass


def _maybe_trim_old_days(now: _dt.datetime) -> None:
    global _LAST_TRIM_DAY
    today = now.strftime("%Y-%m-%d")
    if _LAST_TRIM_DAY == today:
        return
    _LAST_TRIM_DAY = today
    try:
        dir_ = activity_dir()
        if not os.path.isdir(dir_):
            return
        cutoff = (now - _dt.timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        for name in os.listdir(dir_):
            if not name.endswith(".jsonl"):
                continue
            stem = name[:-6]
            if stem < cutoff:
                try:
                    os.remove(os.path.join(dir_, name))
                except OSError:
                    pass
    except Exception:
        pass


def iter_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Return the latest ``limit`` events, newest last (chronological order).

    Reads from today's file first; if that has fewer than ``limit`` lines,
    keeps walking backward through prior days until satisfied or files run out.
    """
    if limit <= 0:
        return []
    dir_ = activity_dir()
    if not os.path.isdir(dir_):
        return []
    files = sorted(
        (n for n in os.listdir(dir_) if n.endswith(".jsonl")),
        reverse=True,
    )
    collected: list[dict[str, Any]] = []
    for name in files:
        path = os.path.join(dir_, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                day_lines = f.readlines()
        except OSError:
            continue
        for line in reversed(day_lines):
            line = line.strip()
            if not line:
                continue
            try:
                collected.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
    collected.reverse()
    return collected


def parse_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Helper for tests / SSE parsing."""
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Skill outcome aggregation.
#
# A "skill" here is a SOP file referenced via working['related_sop'] (set by
# the agent itself when it adopts an SOP). Every turn_end event carries the
# active skill (if any) plus an exit_reason. We classify outcomes:
#
#   ok          — exit_reason.result == 'CURRENT_TASK_DONE'
#   max_turns   — exit_reason.result == 'MAX_TURNS_EXCEEDED'
#   exited      — exit_reason.result == 'EXITED' (e.g. ask_user yielded)
#   other       — any other / unknown classification
#
# Aggregation walks all retained day files (cheap: a few MB at most under
# RETENTION_DAYS). For larger volumes we'd push this into a sqlite index.
# ---------------------------------------------------------------------------

_OUTCOME_CATEGORIES = ("ok", "max_turns", "exited", "other")


def normalize_related_sop(value: str) -> str:
    """Reduce an arbitrary ``related_sop`` string to a single SOP stem.

    Agents may write any of these forms (per tools_schema):

      * "web_setup_sop"                  ← bare stem
      * "memory/web_setup_sop.md"        ← repo-relative path
      * "web_setup_sop, plan_sop"        ← multiple, comma-joined
      * ""                               ← unset

    We pick the *first* token, strip directory + ``.md`` suffix, and return
    the bare stem. Empty input → empty string (caller decides bucket name).
    """
    if not value:
        return ""
    s = str(value).strip()
    for sep in (",", ";", "\n", "\t", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = s.strip().replace("\\", "/")
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    if s.endswith(".md"):
        s = s[:-3]
    return s


def classify_outcome(exit_reason: dict[str, Any] | None) -> str | None:
    """Map a turn_end ``exit_reason`` dict to one of the categories in
    :data:`_OUTCOME_CATEGORIES`, or ``None`` if the turn is mid-run.

    Public so downstream consumers (``launcher.trajectory`` etc.) can apply
    the same buckets without duplicating logic — the categorical naming is
    a stable contract, not an internal detail.
    """
    if not exit_reason:
        return None
    result = str(exit_reason.get("result", "")).upper()
    if result == "CURRENT_TASK_DONE":
        return "ok"
    if result == "MAX_TURNS_EXCEEDED":
        return "max_turns"
    if result == "EXITED":
        return "exited"
    return "other"


# Underscore alias kept to avoid breaking existing internal callers that
# imported the private name. New code should use ``classify_outcome``.
_classify_outcome = classify_outcome


def summarize_outcomes() -> dict[str, dict[str, Any]]:
    """Group turn_end events by ``related_sop`` and count outcome categories.

    Returns a mapping ``{skill_name: {ok, max_turns, exited, other, total,
    last_seen, success_rate}}``. Skill name "_unattributed" collects events
    where no related_sop was set when the turn ended.
    """
    dir_ = activity_dir()
    skills: dict[str, dict[str, Any]] = {}
    if not os.path.isdir(dir_):
        return skills
    for name in sorted(n for n in os.listdir(dir_) if n.endswith(".jsonl")):
        path = os.path.join(dir_, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("phase") != "turn_end":
                continue
            outcome = _classify_outcome(ev.get("exit_reason"))
            if outcome is None:
                continue
            skill = normalize_related_sop(ev.get("related_sop", "")) or "_unattributed"
            entry = skills.setdefault(
                skill,
                {cat: 0 for cat in _OUTCOME_CATEGORIES} | {"total": 0, "last_seen": ""},
            )
            entry[outcome] += 1
            entry["total"] += 1
            ts = ev.get("ts", "")
            if ts > entry["last_seen"]:
                entry["last_seen"] = ts
    for entry in skills.values():
        terminal = entry["ok"] + entry["max_turns"] + entry["other"]
        entry["success_rate"] = (entry["ok"] / terminal) if terminal > 0 else None
    return skills
