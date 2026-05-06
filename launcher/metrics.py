"""Task & tool metrics aggregator.

Reads ``temp/activity/*.jsonl`` (produced by :mod:`launcher.activity_log`) and
computes the metrics that matter for product-quality decisions:

* **Task completion rate** — completed / (completed + failed + aborted)
* **Avg / p50 / p90 turns per task**
* **Avg / p50 / p90 elapsed per task**
* **Tool failure rate** (per-tool success / error / unknown counts)
* **Top failing tools** (sorted by error count)
* **Abort rate** (user-initiated stops as fraction of all task_end events)
* **LLM usage breakdown** — task counts per model

Why a separate aggregator? :mod:`launcher.activity_log` already had a
per-skill ``summarize_outcomes()`` but that's keyed on ``related_sop`` which
the agent only sets sometimes. For product metrics we need the task as the
universe — every user request, every model, every tool. Keeping it in a
sibling module avoids overloading activity_log with two different aggregation
contracts.

Entry points:
  * ``python -m launcher.metrics``                — pretty CLI report
  * ``python -m launcher.metrics --json``         — machine-readable
  * ``python -m launcher.metrics --since 7d``     — sliding window
  * Programmatic: :func:`aggregate`, :func:`render_text`
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from launcher import activity_log


# ─── Time window parsing ─────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^(\d+)([dhmw])$")


def _parse_since(s: str) -> _dt.datetime:
    """Accept '7d', '24h', '30m', '2w', or an ISO date. Returns a UTC
    cutoff timestamp (events with ``ts >= cutoff`` are kept)."""
    s = (s or "").strip().lower()
    if not s:
        return _dt.datetime.fromtimestamp(0, tz=_dt.timezone.utc)
    m = _DURATION_RE.match(s)
    now = _dt.datetime.now(_dt.timezone.utc)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "m": _dt.timedelta(minutes=n),
            "h": _dt.timedelta(hours=n),
            "d": _dt.timedelta(days=n),
            "w": _dt.timedelta(weeks=n),
        }[unit]
        return now - delta
    # Try ISO 8601 (e.g. '2026-04-01').
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
        return _dt.datetime.fromisoformat(s.replace("z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unrecognized --since {s!r}: use '7d' / '24h' / '2026-04-01'") from exc


def _ev_ts(ev: dict[str, Any]) -> Optional[_dt.datetime]:
    ts = ev.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Event loading ───────────────────────────────────────────────────────


def _iter_event_files(*, since: _dt.datetime) -> Iterable[str]:
    """Yield JSONL file paths whose **stem date** is on or after the cutoff
    day. We use day-level filtering to avoid opening files we'll discard.
    Per-event filtering in :func:`_iter_events` does the precise cutoff."""
    dir_ = activity_log.activity_dir()
    if not os.path.isdir(dir_):
        return
    cutoff_day = since.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d")
    for name in sorted(os.listdir(dir_)):
        if not name.endswith(".jsonl"):
            continue
        stem = name[:-6]  # strip '.jsonl'
        if stem >= cutoff_day:
            yield os.path.join(dir_, name)


def _iter_events(*, since: _dt.datetime) -> Iterable[dict[str, Any]]:
    """Stream events newer than ``since`` from the activity log directory."""
    for path in _iter_event_files(since=since):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _ev_ts(ev)
                    if ts is not None and ts < since:
                        continue
                    yield ev
        except OSError:
            continue


# ─── Aggregation core ────────────────────────────────────────────────────


@dataclass
class TaskRow:
    """Per-task summary built by walking task_start → task_end."""
    task_id: str
    source: str = ""
    llm: str = ""
    started_ts: str = ""
    ended_ts: str = ""
    elapsed_s: float = 0.0
    turns: int = 0
    outcome: str = "unknown"  # completed / failed / aborted / unknown
    tool_calls: int = 0
    tool_errors: int = 0
    tools_used: set[str] = field(default_factory=set)


@dataclass
class ToolRow:
    name: str
    calls: int = 0
    errors: int = 0
    elapsed_total_s: float = 0.0

    @property
    def error_rate(self) -> float:
        return (self.errors / self.calls) if self.calls else 0.0


@dataclass
class Metrics:
    window_start: str
    window_end: str
    tasks: list[TaskRow] = field(default_factory=list)
    tools: dict[str, ToolRow] = field(default_factory=dict)
    llm_task_counts: dict[str, int] = field(default_factory=dict)
    orphan_tool_events: int = 0  # tool_end without enclosing task (legacy logs)


def aggregate(*, since: _dt.datetime, until: Optional[_dt.datetime] = None) -> Metrics:
    """Walk events and produce a Metrics snapshot.

    Algorithm: stream events, maintain a per-task_id accumulator keyed on the
    first ``task_start``. ``tool_end`` events without a current task_id (e.g.
    legacy logs from before we instrumented task lifecycle) go into
    ``orphan_tool_events`` for visibility.
    """
    until = until or _dt.datetime.now(_dt.timezone.utc)
    metrics = Metrics(
        window_start=since.isoformat(timespec="seconds").replace("+00:00", "Z"),
        window_end=until.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    open_tasks: dict[str, TaskRow] = {}
    # We don't have task_id on tool_* events (they're emitted from the
    # WlwlAssHandler which doesn't track it), so attribute by *the most
    # recent open task* in this PID. Multi-agent processes would need
    # per-pid tracking; current architecture is single-agent-per-pid.
    last_open_task_id: Optional[str] = None

    for ev in _iter_events(since=since):
        ts = _ev_ts(ev)
        if ts is not None and ts > until:
            continue
        phase = ev.get("phase", "")
        if phase == "task_start":
            tid = str(ev.get("task_id") or "")
            if not tid:
                continue
            row = TaskRow(
                task_id=tid,
                source=str(ev.get("source") or ""),
                llm=str(ev.get("llm") or ""),
                started_ts=str(ev.get("ts") or ""),
            )
            open_tasks[tid] = row
            last_open_task_id = tid
        elif phase == "task_end":
            tid = str(ev.get("task_id") or "")
            row = open_tasks.pop(tid, None)
            if row is None:
                # Spurious task_end (or restarted process between start/end).
                # Skip rather than mis-attribute.
                continue
            row.outcome = str(ev.get("outcome") or "unknown")
            row.turns = int(ev.get("turns") or 0)
            row.elapsed_s = float(ev.get("elapsed_s") or 0.0)
            row.ended_ts = str(ev.get("ts") or "")
            metrics.tasks.append(row)
            metrics.llm_task_counts[row.llm] = metrics.llm_task_counts.get(row.llm, 0) + 1
            if last_open_task_id == tid:
                last_open_task_id = next(iter(open_tasks), None)
        elif phase == "tool_end":
            tool = str(ev.get("tool") or "")
            if not tool:
                continue
            tr = metrics.tools.setdefault(tool, ToolRow(name=tool))
            tr.calls += 1
            try:
                tr.elapsed_total_s += float(ev.get("elapsed_s") or 0.0)
            except (TypeError, ValueError):
                pass
            status = str(ev.get("status") or "").lower()
            if status == "error":
                tr.errors += 1
            # Attribute to most-recent open task in this stream.
            if last_open_task_id and last_open_task_id in open_tasks:
                trow = open_tasks[last_open_task_id]
                trow.tool_calls += 1
                trow.tools_used.add(tool)
                if status == "error":
                    trow.tool_errors += 1
            else:
                metrics.orphan_tool_events += 1

    # Tasks that started in-window but never ended (process killed mid-run).
    # Surface them as outcome='in_progress' so the user sees what happened.
    for row in open_tasks.values():
        row.outcome = "in_progress"
        metrics.tasks.append(row)

    return metrics


# ─── Derived statistics ──────────────────────────────────────────────────


def summarize(metrics: Metrics) -> dict[str, Any]:
    """Reduce a Metrics object to a single dict suitable for JSON / printing."""
    tasks = metrics.tasks
    n = len(tasks)
    by_outcome: dict[str, int] = defaultdict(int)
    for t in tasks:
        by_outcome[t.outcome] += 1
    completed = by_outcome.get("completed", 0)
    failed = by_outcome.get("failed", 0)
    aborted = by_outcome.get("aborted", 0)
    in_progress = by_outcome.get("in_progress", 0)
    terminal = completed + failed + aborted
    completion_rate = (completed / terminal) if terminal else 0.0
    abort_rate = (aborted / terminal) if terminal else 0.0

    turn_values = [t.turns for t in tasks if t.turns > 0]
    elapsed_values = [t.elapsed_s for t in tasks if t.elapsed_s > 0]

    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
        return s[k]

    tool_rows = sorted(metrics.tools.values(), key=lambda r: r.calls, reverse=True)
    failing_tools = sorted(
        (t for t in metrics.tools.values() if t.errors > 0),
        key=lambda r: (r.errors, r.error_rate),
        reverse=True,
    )

    return {
        "window": {"start": metrics.window_start, "end": metrics.window_end},
        "task_count": n,
        "by_outcome": dict(by_outcome),
        "completion_rate": round(completion_rate, 4),
        "abort_rate": round(abort_rate, 4),
        "in_progress": in_progress,
        "turns": {
            "mean": round(statistics.fmean(turn_values), 2) if turn_values else 0,
            "p50": _pct([float(v) for v in turn_values], 0.50),
            "p90": _pct([float(v) for v in turn_values], 0.90),
            "max": max(turn_values) if turn_values else 0,
        },
        "elapsed_s": {
            "mean": round(statistics.fmean(elapsed_values), 2) if elapsed_values else 0,
            "p50": round(_pct(elapsed_values, 0.50), 2),
            "p90": round(_pct(elapsed_values, 0.90), 2),
            "max": round(max(elapsed_values), 2) if elapsed_values else 0,
        },
        "tools": [
            {"name": t.name, "calls": t.calls, "errors": t.errors,
             "error_rate": round(t.error_rate, 4),
             "elapsed_total_s": round(t.elapsed_total_s, 2)}
            for t in tool_rows
        ],
        "top_failing_tools": [
            {"name": t.name, "errors": t.errors, "calls": t.calls,
             "error_rate": round(t.error_rate, 4)}
            for t in failing_tools[:5]
        ],
        "llm_task_counts": dict(sorted(metrics.llm_task_counts.items(),
                                        key=lambda kv: -kv[1])),
        "orphan_tool_events": metrics.orphan_tool_events,
    }


# ─── Rendering ───────────────────────────────────────────────────────────


def _isatty() -> bool:
    try: return bool(sys.stdout.isatty())
    except Exception: return False


_COLOR_ON = _isatty() and os.environ.get("NO_COLOR", "") == ""


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR_ON else s


def _bar(value: float, width: int = 20) -> str:
    """Inline bar from 0..1 for rate-style metrics."""
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "█" * filled + "·" * (width - filled)


def render_text(summary: dict[str, Any]) -> str:
    out: list[str] = []
    w = summary["window"]
    out.append(_c(f"wlwl-ass metrics — {w['start']} → {w['end']}", "1"))
    out.append("")
    n = summary["task_count"]
    out.append(_c("Tasks", "1") + f"   total={n}   in_progress={summary['in_progress']}")
    if n == 0:
        out.append(_c("  (no task_start events in this window)", "2"))
        out.append("")
        out.append(_c("Tip: ", "33") + "run a task first, then re-run this command. "
                   + "If you have legacy data without task_id, see orphan_tool_events below.")
    else:
        cr = summary["completion_rate"]
        ar = summary["abort_rate"]
        out.append(f"  completion_rate  {_bar(cr)}  {cr*100:5.1f}%   "
                   + f"({summary['by_outcome'].get('completed', 0)}/"
                   + f"{summary['by_outcome'].get('completed', 0)+summary['by_outcome'].get('failed', 0)+summary['by_outcome'].get('aborted', 0)})")
        out.append(f"  abort_rate       {_bar(ar)}  {ar*100:5.1f}%")
        out.append(f"  outcomes:        " + ", ".join(
            f"{k}={v}" for k, v in sorted(summary["by_outcome"].items())))
        t = summary["turns"]; e = summary["elapsed_s"]
        out.append(f"  turns/task       mean={t['mean']:>5}   p50={t['p50']}  p90={t['p90']}  max={t['max']}")
        out.append(f"  elapsed/task(s)  mean={e['mean']:>5}   p50={e['p50']}  p90={e['p90']}  max={e['max']}")

    out.append("")
    out.append(_c("Tools", "1"))
    if not summary["tools"]:
        out.append(_c("  (no tool_end events)", "2"))
    else:
        out.append(f"  {'tool':<24}  {'calls':>6}  {'errs':>5}  {'err_rate':>8}  {'sum_s':>8}")
        out.append("  " + "─" * 60)
        for t in summary["tools"][:15]:
            err = t["error_rate"]
            color = "31" if err > 0.2 else ("33" if err > 0.05 else "32")
            line = (f"  {t['name']:<24}  {t['calls']:>6}  {t['errors']:>5}  "
                    + _c(f"{err*100:>6.1f}%", color)
                    + f"  {t['elapsed_total_s']:>8.1f}")
            out.append(line)
        if len(summary["tools"]) > 15:
            out.append(_c(f"  …({len(summary['tools']) - 15} more, use --json for full list)", "2"))

    out.append("")
    out.append(_c("Top failing tools", "1"))
    if not summary["top_failing_tools"]:
        out.append(_c("  (no errors recorded)", "32"))
    else:
        for t in summary["top_failing_tools"]:
            out.append(f"  {t['name']:<24}  {t['errors']} errors / {t['calls']} calls "
                       + f"({t['error_rate']*100:.1f}%)")

    out.append("")
    out.append(_c("LLM task share", "1"))
    if not summary["llm_task_counts"]:
        out.append(_c("  (none)", "2"))
    else:
        total = sum(summary["llm_task_counts"].values())
        for name, c in summary["llm_task_counts"].items():
            pct = (c / total * 100) if total else 0
            out.append(f"  {name:<32}  {c:>4}  {pct:5.1f}%")

    if summary["orphan_tool_events"]:
        out.append("")
        out.append(_c(f"  {summary['orphan_tool_events']} tool events with no enclosing task — "
                     "likely from before task_id instrumentation.", "2"))
    return "\n".join(out)


# ─── CLI ─────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ga metrics",
        description="Aggregate task / tool metrics from temp/activity/*.jsonl",
    )
    parser.add_argument("--since", default="7d",
                        help="Sliding window start: '24h', '7d', '2w', or ISO date. Default: 7d.")
    parser.add_argument("--until", default=None,
                        help="Optional window end (default: now).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    try:
        since = _parse_since(args.since)
        until = _parse_since(args.until) if args.until else None
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    metrics = aggregate(since=since, until=until)
    summary = summarize(metrics)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        # Force UTF-8 on Windows so the bar glyph and any Chinese llm names
        # don't mojibake through cp936.
        try:
            enc = (sys.stdout.encoding or "").lower()
            if enc and enc not in ("utf-8", "utf8", "cp65001"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
        print(render_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
