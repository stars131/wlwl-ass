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
import json
import os
import sys
from dataclasses import dataclass, field
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
        if ev.get("phase") != "turn_end":
            continue
        turn = ev.get("turn")
        if not isinstance(turn, int) or turn < 1:
            continue
        # turn==1 closes the previous run (if any) and forces a new one.
        # A non-1 turn before any turn==1 (e.g. crash before first turn_end
        # of a fresh run) falls through and seeds a synthesized run rather
        # than dropping the data.
        if turn == 1 and current is not None:
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
    run.n_turns = max(run.n_turns, int(ev.get("turn", 0) or 0))
    run.end_ts = str(ev.get("ts", "") or run.end_ts)
    skill = activity_log.normalize_related_sop(ev.get("related_sop", ""))
    if skill and skill not in run.skills_used:
        run.skills_used.append(skill)
    outcome = activity_log.classify_outcome(ev.get("exit_reason"))
    if outcome:
        run.outcomes[outcome] = run.outcomes.get(outcome, 0) + 1
    compact_ev: dict[str, Any] = {
        "turn": ev.get("turn"),
        "ts": ev.get("ts"),
        "summary": ev.get("summary", ""),
        "related_sop": skill,
    }
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
