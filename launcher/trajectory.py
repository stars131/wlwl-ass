"""``trajectory`` — compress and export agent runs from the activity log.

Closes the gap between ``launcher.activity_log`` (raw JSONL event stream)
and the training-data shape expected by RL / SFT pipelines downstream
(Atropos / Tinker / SWE-bench replays). Inspired by Hermes'
``trajectory_compressor.py`` + ``batch_runner.py`` but simpler — no
multi-agent orchestration, just a clean export pipeline.

Run boundary detection:
  A new run begins when a ``turn_end`` event with ``turn == 1`` is seen.
  This matches wlwl-ass's actual loop semantics — every fresh ``wlwl_ass.py`` invocation
  resets the turn counter. We don't try to be clever about cross-day
  splices; a run that spans midnight just produces a single record with
  events from both day-files.

Compression strategy:
  Default is "summarize" mode — keep timestamps + turn + summary +
  exit_reason + related_sop, drop the heavy stuff (full prompts,
  tool args). Pass ``include_args=True`` to keep ``compact_args`` for
  fine-grained replay.

Output format:
  JSONL — one trajectory per line. Easy to grep, append, stream into
  a downstream training pipeline. Filename embeds the run's UTC start
  for natural sort.

Public CLI:
  ``python -m launcher.trajectory export [--output DIR] [--include-args]``
  ``python -m launcher.trajectory list``  — show detected runs

LLM-agnostic; never imports llmcore. This is a post-hoc analysis tool
that should run without any of the LLM-dependent code paths.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from launcher import activity_log


# ─── Run boundary detection ──────────────────────────────────────────────


@dataclass
class Run:
    """One agent invocation, derived from a contiguous ``turn_end`` sequence
    starting at ``turn == 1``."""
    run_id: str = ""           # ``YYYYMMDDTHHMMSSZ_<n_turns>``
    start_ts: str = ""
    end_ts: str = ""
    n_turns: int = 0
    skills_used: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=lambda: {"ok": 0, "max_turns": 0, "exited": 0, "other": 0})
    events: list[dict[str, Any]] = field(default_factory=list)


def _ts_to_run_id(start_ts: str, n_turns: int) -> str:
    """Stable run id from start timestamp + turn count.

    ``start_ts`` is ISO 8601 from ``activity_log.record`` — slice the
    digits, drop the colons + microseconds, and append ``_<n_turns>``
    so two runs that started in the same second still hash differently.
    """
    if not start_ts:
        return f"unknown_{n_turns}"
    # ``2026-05-04T01:23:45.678Z`` → ``20260504T012345Z``
    digits = "".join(ch for ch in start_ts if ch.isdigit())
    truncated = digits[:14]  # YYYYMMDDhhmmss
    return f"{truncated}Z_{n_turns}t"


def _iter_event_lines(activity_dir: str) -> Iterator[dict[str, Any]]:
    """Stream events from every retained day-file in chronological order.

    Defensive on every error — a corrupt line in one file shouldn't
    abort the whole export."""
    if not os.path.isdir(activity_dir):
        return
    files = sorted(n for n in os.listdir(activity_dir) if n.endswith(".jsonl"))
    for name in files:
        path = os.path.join(activity_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def iter_runs(*, activity_dir: str | None = None, include_args: bool = False) -> list[Run]:
    """Detect runs in the activity log and return them in chronological
    order. Each ``Run`` is fully populated.

    The "turn==1 starts a new run" heuristic occasionally lumps a stray
    event into the wrong run if the agent crashes before the first
    turn_end. That's intentional — the cost of being wrong here is only
    "training data has a 1-event preamble"; the cost of being more
    aggressive is missing real run boundaries.
    """
    activity_dir = activity_dir or activity_log.activity_dir()
    runs: list[Run] = []
    current: Run | None = None
    for ev in _iter_event_lines(activity_dir):
        phase = ev.get("phase")
        if phase not in {"turn_end", "gui_step"}:
            continue
        turn = ev.get("turn")
        if phase == "turn_end" and (not isinstance(turn, int) or turn < 1):
            continue
        # turn==1 closes the previous run (if any) and forces a new one.
        # A non-1 turn before any turn==1 (e.g. crash before first turn_end
        # of a fresh run) falls through and seeds a synthesized run rather
        # than dropping the data.
        if phase == "turn_end" and turn == 1 and current is not None:
            runs.append(current)
            current = None
        if current is None:
            current = Run(start_ts=str(ev.get("ts", "")))
        _absorb_event(current, ev, include_args=include_args)
    if current is not None:
        runs.append(current)
    for r in runs:
        r.run_id = _ts_to_run_id(r.start_ts, r.n_turns)
    return runs


def _absorb_event(run: Run, ev: dict[str, Any], *, include_args: bool) -> None:
    phase = str(ev.get("phase") or "")
    run.n_turns = max(run.n_turns, int(ev.get("turn", 0) or 0))
    run.end_ts = str(ev.get("ts", "") or run.end_ts)
    skill = activity_log.normalize_related_sop(ev.get("related_sop", ""))
    if skill and skill not in run.skills_used:
        run.skills_used.append(skill)
    outcome = activity_log.classify_outcome(ev.get("exit_reason"))
    if outcome:
        run.outcomes[outcome] = run.outcomes.get(outcome, 0) + 1
    compact_ev: dict[str, Any] = {"phase": phase, "turn": ev.get("turn"), "ts": ev.get("ts")}
    if phase == "gui_step":
        compact_ev.update({
            "run_id": ev.get("run_id", ""),
            "step": ev.get("step"),
            "target": ev.get("target", ""),
            "action": ev.get("action", ""),
            "status": ev.get("status", ""),
            "screenshot_path": ev.get("screenshot_path", ""),
            "width": ev.get("width"),
            "height": ev.get("height"),
            "scale_factor": ev.get("scale_factor"),
            "elapsed_s": ev.get("elapsed_s"),
        })
        if ev.get("prediction") is not None:
            compact_ev["prediction"] = ev.get("prediction")
        if ev.get("detail") is not None:
            compact_ev["detail"] = ev.get("detail")
        if include_args and ev.get("parsed") is not None:
            compact_ev["parsed"] = ev.get("parsed")
    else:
        compact_ev.update({
            "summary": ev.get("summary", ""),
            "related_sop": skill,
        })
    if ev.get("exit_reason"):
        compact_ev["exit_reason"] = ev["exit_reason"]
    if include_args and ev.get("args") is not None:
        compact_ev["args"] = ev["args"]
    run.events.append(compact_ev)


# ─── Export ──────────────────────────────────────────────────────────────


def export_runs(
    runs: Iterable[Run],
    output_path: str,
) -> int:
    """Write all runs as JSONL to ``output_path``. Returns count.

    Format: one ``Run`` per line, fields = dataclass field names.
    Atomic write: writes to ``output_path.tmp`` then ``os.replace``.
    """
    runs = list(runs)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in runs:
            row = {
                "run_id": r.run_id,
                "start_ts": r.start_ts,
                "end_ts": r.end_ts,
                "n_turns": r.n_turns,
                "skills_used": r.skills_used,
                "outcomes": r.outcomes,
                "events": r.events,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, output_path)
    return len(runs)


def _truncate_blobs(events: list[dict[str, Any]], max_blob_chars: int) -> None:
    """In-place: cap any string field in each event at ``max_blob_chars`` and
    annotate the truncation. Skips ``ts`` and other small metadata."""
    if max_blob_chars <= 0:
        return
    for ev in events:
        for k, v in list(ev.items()):
            if k in ("ts", "turn"):
                continue
            if isinstance(v, str) and len(v) > max_blob_chars:
                ev[k] = v[:max_blob_chars] + f"…[+{len(v) - max_blob_chars}c]"
            elif isinstance(v, dict):
                # ``args`` block — recurse one level (deep recursion costs more
                # than it gains for this dataset shape).
                for k2, v2 in list(v.items()):
                    if isinstance(v2, str) and len(v2) > max_blob_chars:
                        v[k2] = v2[:max_blob_chars] + f"…[+{len(v2) - max_blob_chars}c]"


def export_compressed(
    *,
    output_path: str | None = None,
    activity_dir: str | None = None,
    include_args: bool = True,
    max_blob_chars: int = 200,
) -> dict[str, Any]:
    """One-shot export used by /api/trajectory/export.

    Returns ``{"path": str, "count": int, "runs": [{run_id, n_turns, skills_used, outcomes}, ...]}``
    so the GUI can show a summary without re-reading the JSONL file.
    """
    runs = iter_runs(activity_dir=activity_dir, include_args=include_args)
    if max_blob_chars > 0:
        for r in runs:
            _truncate_blobs(r.events, max_blob_chars)
    if output_path is None:
        output_path = os.path.join(
            os.path.dirname(activity_log.activity_dir()), "trajectories.jsonl"
        )
    n = export_runs(runs, output_path)
    return {
        "path": output_path,
        "count": n,
        "runs": [
            {
                "run_id": r.run_id,
                "n_turns": r.n_turns,
                "skills_used": r.skills_used,
                "outcomes": r.outcomes,
            }
            for r in runs
        ],
    }


def export_html(
    *,
    output_path: str | None = None,
    activity_dir: str | None = None,
    include_args: bool = True,
    max_blob_chars: int = 300,
) -> dict[str, Any]:
    runs = iter_runs(activity_dir=activity_dir, include_args=include_args)
    if max_blob_chars > 0:
        for r in runs:
            _truncate_blobs(r.events, max_blob_chars)
    if output_path is None:
        output_path = os.path.join(
            os.path.dirname(activity_log.activity_dir()), "trajectories.html"
        )
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    body = _render_html(runs, output_path=output_path)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, output_path)
    return {
        "path": output_path,
        "count": len(runs),
        "runs": [
            {
                "run_id": r.run_id,
                "n_turns": r.n_turns,
                "skills_used": r.skills_used,
                "outcomes": r.outcomes,
            }
            for r in runs
        ],
    }


def _render_html(runs: list[Run], *, output_path: str) -> str:
    rows: list[str] = []
    for run in runs:
        rows.append(
            "<section class='run'>"
            f"<h2>{html.escape(run.run_id)}</h2>"
            f"<p class='meta'>{html.escape(run.start_ts)} -> {html.escape(run.end_ts)} | turns {run.n_turns}</p>"
        )
        for ev in run.events:
            rows.append(_render_event(ev, output_path=output_path))
        rows.append("</section>")
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>wlwl-ass GUI Trajectory Replay</title>
  <style>
    body { margin: 0; font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; color: #17202a; background: #f7f8fb; }
    header { position: sticky; top: 0; background: #ffffff; border-bottom: 1px solid #d8dee8; padding: 14px 18px; z-index: 1; }
    h1 { margin: 0; font-size: 18px; }
    main { max-width: 1180px; margin: 0 auto; padding: 18px; }
    .run { background: #fff; border: 1px solid #d8dee8; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }
    .run h2 { font-size: 15px; margin: 0; padding: 12px 14px 0; }
    .meta { color: #667085; margin: 3px 14px 12px; font-size: 12px; }
    .event { border-top: 1px solid #edf0f5; padding: 12px 14px; display: grid; grid-template-columns: 260px 1fr; gap: 14px; }
    .tag { display: inline-block; min-width: 58px; color: #7a3e00; font-weight: 700; }
    .small { color: #667085; font-size: 12px; }
    img { max-width: 100%; border: 1px solid #d8dee8; border-radius: 6px; background: #eef1f6; }
    pre { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; background: #f3f5f9; border: 1px solid #d8dee8; border-radius: 6px; padding: 8px; font-size: 12px; }
  </style>
</head>
<body>
  <header><h1>wlwl-ass GUI Trajectory Replay</h1></header>
  <main>
""" + "\n".join(rows) + """
  </main>
</body>
</html>
"""


def _render_event(ev: dict[str, Any], *, output_path: str) -> str:
    phase = html.escape(str(ev.get("phase") or ""))
    ts = html.escape(str(ev.get("ts") or ""))
    action = html.escape(str(ev.get("action") or ev.get("summary") or ""))
    status = html.escape(str(ev.get("status") or ""))
    screenshot = str(ev.get("screenshot_path") or "")
    img = ""
    if screenshot and os.path.exists(screenshot):
        # Browsers can't render Windows backslash paths in `src=...`, and a
        # raw `D:\path` is ambiguous. Prefer a POSIX-style relative path when
        # the screenshot is under the HTML's parent directory; otherwise use
        # an absolute `file://` URI which is unambiguous on every platform.
        try:
            shot = Path(screenshot).resolve(strict=False)
            base = Path(output_path).resolve(strict=False).parent
            try:
                src = shot.relative_to(base).as_posix()
            except ValueError:
                src = shot.as_uri()
        except (OSError, ValueError):
            src = Path(screenshot).as_posix()
        img = f"<img src='{html.escape(src)}' alt='screenshot' />"
    detail = {
        k: v
        for k, v in ev.items()
        if k not in {"phase", "ts", "screenshot_path"}
    }
    blob = html.escape(json.dumps(detail, ensure_ascii=False, indent=2, default=str))
    return (
        "<div class='event'>"
        f"<div><span class='tag'>{phase}</span><div class='small'>{ts}</div>"
        f"<div>{action} {status}</div></div>"
        f"<div>{img}<pre>{blob}</pre></div>"
        "</div>"
    )


# ─── CLI ─────────────────────────────────────────────────────────────────


def _cmd_list(args: argparse.Namespace) -> int:
    runs = iter_runs(activity_dir=args.activity_dir, include_args=False)
    if not runs:
        print("(no runs detected — activity log is empty or only contains non-turn_end events)")
        return 0
    print(f"{len(runs)} run(s) detected:")
    for r in runs:
        outcomes_str = " ".join(f"{k}:{v}" for k, v in r.outcomes.items() if v)
        skills_str = ", ".join(r.skills_used[:5]) or "—"
        print(f"  {r.run_id}  turns={r.n_turns}  {outcomes_str or '(no outcomes)'}  skills={skills_str}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    runs = iter_runs(activity_dir=args.activity_dir, include_args=args.include_args)
    if not runs:
        print("(no runs to export)")
        return 0
    out_path = args.output or os.path.join(
        os.path.dirname(activity_log.activity_dir()), "trajectories.jsonl"
    )
    n = export_runs(runs, out_path)
    print(f"Exported {n} run(s) to {out_path}")
    return 0


def _cmd_export_html(args: argparse.Namespace) -> int:
    out = export_html(
        output_path=args.output,
        activity_dir=args.activity_dir,
        include_args=args.include_args,
    )
    print(f"Exported {out['count']} run(s) to {out['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trajectory", description=__doc__)
    parser.add_argument("--activity-dir", default=None,
                        help="Override activity_log directory (defaults to project temp/activity).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub_list = sub.add_parser("list", help="List detected runs")
    sub_list.set_defaults(func=_cmd_list)
    sub_export = sub.add_parser("export", help="Export all runs as JSONL")
    sub_export.add_argument("--output", default=None, help="Output JSONL path (default: <project>/temp/trajectories.jsonl)")
    sub_export.add_argument("--include-args", action="store_true",
                            help="Keep tool args in events (larger output, full replay).")
    sub_export.set_defaults(func=_cmd_export)
    sub_html = sub.add_parser("export-html", help="Export all runs as a local HTML replay")
    sub_html.add_argument("--output", default=None, help="Output HTML path (default: <project>/temp/trajectories.html)")
    sub_html.add_argument("--include-args", action="store_true",
                          help="Keep tool args in events (larger output, full replay).")
    sub_html.set_defaults(func=_cmd_export_html)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
