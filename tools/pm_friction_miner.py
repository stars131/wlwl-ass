"""Track A: friction-signal mining from activity logs.

This module is intentionally *pre-LLM*: it scans daily activity jsonl files,
detects friction signals via deterministic rules, groups them by candidate
root cause, and returns structured JSON. The agent then reasons about the
output in its own turn using its own LLM — the tool itself never calls an
LLM.

Signal taxonomy (initial cut, expand as we learn what's noisy vs valuable):

* ``retry_burst`` — ≥3 ``tool_start`` events for the same tool inside one
  turn. Strongest "user is fighting the tool" signal.
* ``error_retry`` — a ``tool_end`` with ``status`` indicating error, followed
  within the same turn by another ``tool_start`` of the same tool.
* ``long_turn`` — a turn whose total tool calls cross ``LONG_TURN_THRESHOLD``
  (default 15). Proxy for "agent got stuck".
* ``bad_exit`` — ``turn_end`` whose ``exit_reason`` doesn't look like a
  normal completion (cancelled / aborted / errored / etc.).

Provenance is preserved on every signal — turn, run_id (derived from day
filename + position), tool name, ts. The agent must keep ≥3 distinct runs
of evidence before proposing.

Accept-rate tracking lives in ``temp/pm_proposal_log.jsonl``; see
``compute_accept_rate`` for the ``accepted / (accepted + rejected)`` formula
that drives PM's auto-throttle.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


# ── Tunables ───────────────────────────────────────────────────────────

RETRY_BURST_THRESHOLD = 3       # ≥N same-tool tool_starts in one turn
LONG_TURN_THRESHOLD = 15        # ≥N total tool calls in one turn
DEFAULT_WINDOW_DAYS = 7
MIN_DISTINCT_RUNS = 3           # PM-A's provenance gate

ACCEPT_RATE_FLOOR = 0.30
ACCEPT_RATE_WINDOW_DAYS = 30
NORMAL_INTERVAL_DAYS = 1        # how often Track A runs when accept rate is healthy
THROTTLED_INTERVAL_DAYS = 7     # when accept_rate < floor

# Patterns that suggest an error-style status string.
_ERROR_STATUS_HINTS = (
    "error", "fail", "exception", "denied", "cancel", "abort", "timeout",
)
# Exit reasons that count as "bad" (anything that isn't a clean completion).
_BAD_EXIT_HINTS = (
    "abort", "cancel", "error", "fail", "interrupt", "killed", "stop_user",
)


# ── Data shapes ────────────────────────────────────────────────────────


@dataclass
class FrictionSignal:
    signal_type: str
    run_id: str
    turn: int
    tool: str
    detail: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FrictionCluster:
    """Deterministic pre-LLM grouping. The agent does the smart clustering."""
    key: str  # e.g. "retry_burst::file_patch"
    signal_type: str
    tool: str
    count: int
    distinct_runs: list[str] = field(default_factory=list)
    distinct_turns: list[tuple[str, int]] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "signal_type": self.signal_type,
            "tool": self.tool,
            "count": self.count,
            "distinct_run_count": len(self.distinct_runs),
            "distinct_runs": self.distinct_runs[:10],
            "distinct_turns": self.distinct_turns[:20],
            "samples": self.samples[:5],
        }


# ── Path helpers ───────────────────────────────────────────────────────


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def activity_dir() -> Path:
    override = os.environ.get("WLWL_ACTIVITY_LOG_DIR")
    if override:
        return Path(override)
    return _project_root() / "temp" / "activity"


def proposal_log_path() -> Path:
    return _project_root() / "temp" / "pm_proposal_log.jsonl"


def proposals_path() -> Path:
    return _project_root() / "temp" / "pm_proposals.md"


def cadence_path() -> Path:
    return _project_root() / "temp" / "pm_cadence.json"


# ── Iteration ──────────────────────────────────────────────────────────


def _iter_day_files(
    *, days_window: int, activity_dir_path: Path | None = None,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(YYYY-MM-DD, path)`` for the last ``days_window`` days."""
    base = activity_dir_path or activity_dir()
    if not base.is_dir():
        return
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days_window)).date()
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".jsonl"):
            continue
        day = entry.name[:-len(".jsonl")]
        try:
            day_dt = _dt.datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day_dt < cutoff:
            continue
        yield day, entry


def _iter_events(day_files: Iterator[tuple[str, Path]]) -> Iterator[dict[str, Any]]:
    for day, path in day_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev.setdefault("_day", day)
                    yield ev
        except OSError:
            continue


# ── Signal extraction ──────────────────────────────────────────────────


def _looks_like_error(status: Any) -> bool:
    s = str(status or "").lower()
    return any(h in s for h in _ERROR_STATUS_HINTS)


def _exit_is_bad(exit_reason: Any) -> str | None:
    """Return a short bad-exit label or None for clean exits."""
    if not exit_reason:
        return None
    text = json.dumps(exit_reason, ensure_ascii=False).lower()
    for hint in _BAD_EXIT_HINTS:
        if hint in text:
            return hint
    return None


def _run_id_for(day: str, turn_runs: dict[int, str], turn: int) -> str:
    """Map a (day, turn) pair to a stable run_id. We boundary-cut runs on
    ``turn==1``: each turn 1 starts a new run within the day."""
    if turn in turn_runs:
        return turn_runs[turn]
    # If we've never seen this turn before, place it in the current run.
    # The caller maintains a per-day "active run id" via this dict.
    return turn_runs.get(0, f"{day}-r1")


def collect_signals(
    *,
    days_window: int = DEFAULT_WINDOW_DAYS,
    activity_dir_path: Path | None = None,
) -> list[FrictionSignal]:
    """Scan activity logs and return all friction signals.

    Order matters: events arrive chronologically per day, so we can detect
    retry bursts and error-retry pairs in a single forward pass.
    """
    signals: list[FrictionSignal] = []

    # Per-turn aggregators (reset whenever we cross day or turn boundary)
    current_day: str | None = None
    current_run_id: str = "init"
    run_counter: int = 0
    turn_tool_counts: dict[tuple[int, str], int] = defaultdict(int)
    turn_total_calls: dict[int, int] = defaultdict(int)
    turn_first_seen_ts: dict[int, str] = {}
    last_tool_end_in_turn: dict[int, tuple[str, str]] = {}  # turn → (tool, status)

    def _emit_long_turn_for(turn: int) -> None:
        count = turn_total_calls.get(turn, 0)
        if count >= LONG_TURN_THRESHOLD:
            signals.append(FrictionSignal(
                signal_type="long_turn",
                run_id=current_run_id,
                turn=turn,
                tool="*",
                detail={"tool_calls": count},
                ts=turn_first_seen_ts.get(turn, ""),
            ))

    def _emit_retry_bursts_for(turn: int) -> None:
        for (t, tool), cnt in list(turn_tool_counts.items()):
            if t != turn or cnt < RETRY_BURST_THRESHOLD:
                continue
            signals.append(FrictionSignal(
                signal_type="retry_burst",
                run_id=current_run_id,
                turn=turn,
                tool=tool,
                detail={"count": cnt},
                ts=turn_first_seen_ts.get(turn, ""),
            ))

    def _flush_turn(turn: int) -> None:
        _emit_long_turn_for(turn)
        _emit_retry_bursts_for(turn)
        # Drop accumulators for this turn
        for (t, tool) in [(t, tool) for (t, tool) in turn_tool_counts if t == turn]:
            del turn_tool_counts[(t, tool)]
        turn_total_calls.pop(turn, None)
        turn_first_seen_ts.pop(turn, None)
        last_tool_end_in_turn.pop(turn, None)

    def _flush_all_turns() -> None:
        for turn in list(set([t for t, _ in turn_tool_counts] + list(turn_total_calls))):
            _flush_turn(turn)

    for ev in _iter_events(_iter_day_files(
        days_window=days_window, activity_dir_path=activity_dir_path,
    )):
        day = ev["_day"]
        if day != current_day:
            _flush_all_turns()
            current_day = day
            run_counter = 0
            current_run_id = f"{day}-r1"

        turn = ev.get("turn")
        if not isinstance(turn, int):
            continue
        # turn==1 marks a new run within the day
        if turn == 1 and ev.get("phase") == "turn_end" and run_counter > 0:
            _flush_all_turns()
        phase = ev.get("phase") or ""
        ts = str(ev.get("ts") or "")

        if phase == "tool_start":
            tool = str(ev.get("tool") or "")
            if not tool:
                continue
            turn_first_seen_ts.setdefault(turn, ts)
            turn_total_calls[turn] += 1
            turn_tool_counts[(turn, tool)] += 1
            # Check error → same-tool retry
            prev = last_tool_end_in_turn.get(turn)
            if prev and prev[0] == tool and _looks_like_error(prev[1]):
                signals.append(FrictionSignal(
                    signal_type="error_retry",
                    run_id=current_run_id,
                    turn=turn,
                    tool=tool,
                    detail={"prev_status": prev[1]},
                    ts=ts,
                ))
                # Clear so we don't emit again for the same prev
                last_tool_end_in_turn.pop(turn, None)
        elif phase == "tool_end":
            tool = str(ev.get("tool") or "")
            status = str(ev.get("status") or "")
            if tool:
                last_tool_end_in_turn[turn] = (tool, status)
        elif phase == "turn_end":
            bad = _exit_is_bad(ev.get("exit_reason"))
            if bad:
                signals.append(FrictionSignal(
                    signal_type="bad_exit",
                    run_id=current_run_id,
                    turn=turn,
                    tool="*",
                    detail={"reason": bad, "summary": ev.get("summary", "")[:200]},
                    ts=ts,
                ))
            _flush_turn(turn)
            if turn == 1:
                run_counter += 1
                current_run_id = f"{day}-r{run_counter + 1}"

    _flush_all_turns()
    return signals


# ── Pre-LLM clustering ─────────────────────────────────────────────────


def cluster_signals(
    signals: list[FrictionSignal],
    *,
    min_distinct_runs: int = MIN_DISTINCT_RUNS,
    sample_limit: int = 5,
) -> list[FrictionCluster]:
    """Group signals by ``(signal_type, tool)``. Drop clusters that don't
    clear the provenance gate (``≥ min_distinct_runs`` distinct runs).
    """
    by_key: dict[str, FrictionCluster] = {}
    for sig in signals:
        key = f"{sig.signal_type}::{sig.tool}"
        cluster = by_key.get(key)
        if cluster is None:
            cluster = FrictionCluster(
                key=key, signal_type=sig.signal_type, tool=sig.tool, count=0,
            )
            by_key[key] = cluster
        cluster.count += 1
        if sig.run_id not in cluster.distinct_runs:
            cluster.distinct_runs.append(sig.run_id)
        turn_key = (sig.run_id, sig.turn)
        if turn_key not in cluster.distinct_turns:
            cluster.distinct_turns.append(turn_key)
        if len(cluster.samples) < sample_limit:
            cluster.samples.append(sig.to_dict())

    qualified = [
        c for c in by_key.values()
        if len(c.distinct_runs) >= min_distinct_runs
    ]
    qualified.sort(key=lambda c: (len(c.distinct_runs), c.count), reverse=True)
    return qualified


# ── Accept-rate tracking ───────────────────────────────────────────────


def log_proposal(proposal: dict[str, Any]) -> None:
    """Record a freshly-issued proposal (status=open). One JSON line."""
    entry = {
        "track": proposal.get("track", "A"),
        "id": proposal["id"],
        "title": proposal.get("title", "")[:200],
        "evidence_count": int(proposal.get("evidence_count", 0)),
        "status": "open",
        "created_at": _today_iso(),
    }
    _append_proposal_log(entry)


def record_decision(
    proposal_id: str, *, status: str, decided_by: str = "user", track: str = "A",
) -> None:
    if status not in {"accepted", "rejected", "deferred"}:
        raise ValueError(f"invalid status: {status!r}")
    entry = {
        "track": track,
        "id": proposal_id,
        "status": status,
        "decided_at": _today_iso(),
        "decided_by": decided_by,
    }
    _append_proposal_log(entry)


def _append_proposal_log(entry: dict[str, Any]) -> None:
    path = proposal_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _read_proposal_log() -> list[dict[str, Any]]:
    path = proposal_log_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def compute_accept_rate(
    *, track: str = "A", window_days: int = ACCEPT_RATE_WINDOW_DAYS,
) -> dict[str, Any]:
    """Compute the running accept rate. Returns ``{"rate", "accepted",
    "rejected", "open", "deferred"}``. ``rate`` is ``None`` when there are
    no decided entries in window (caller should treat as healthy by default).
    """
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=window_days)).date()
    by_id: dict[str, dict[str, Any]] = {}
    for entry in _read_proposal_log():
        if entry.get("track", "A") != track:
            continue
        eid = entry.get("id")
        if not eid:
            continue
        cur = by_id.get(eid, {})
        cur.update(entry)
        by_id[eid] = cur

    accepted = rejected = deferred = open_ = 0
    for entry in by_id.values():
        decided = entry.get("decided_at")
        if decided:
            try:
                decided_dt = _dt.datetime.fromisoformat(decided).date()
            except ValueError:
                decided_dt = None
            if decided_dt is None or decided_dt < cutoff:
                continue
        status = entry.get("status") or "open"
        if status == "accepted":
            accepted += 1
        elif status == "rejected":
            rejected += 1
        elif status == "deferred":
            deferred += 1
        else:
            open_ += 1

    decided = accepted + rejected
    rate = (accepted / decided) if decided else None
    return {
        "track": track,
        "rate": rate,
        "accepted": accepted,
        "rejected": rejected,
        "open": open_,
        "deferred": deferred,
        "window_days": window_days,
    }


# ── Cadence ────────────────────────────────────────────────────────────


def last_run_at(track: str = "A") -> str | None:
    path = cadence_path()
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(track)


def mark_run(track: str = "A", *, when: str | None = None) -> None:
    path = cadence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    data[track] = when or _today_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def check_cadence(
    *,
    track: str = "A",
    accept_rate_floor: float = ACCEPT_RATE_FLOOR,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Decide whether Track A is allowed to run right now.

    Returns ``{"allowed", "mode", "reason", "min_interval_days",
    "days_since_last_run", "accept_rate"}``. ``mode`` is ``"normal"`` or
    ``"reflection"`` when accept_rate is below floor.
    """
    now = now or _dt.datetime.now()
    last = last_run_at(track)
    days_since = None
    if last:
        try:
            last_dt = _dt.datetime.fromisoformat(last)
            days_since = (now - last_dt).total_seconds() / 86400.0
        except ValueError:
            days_since = None

    stats = compute_accept_rate(track=track)
    rate = stats["rate"]
    in_throttle = rate is not None and rate < accept_rate_floor
    interval = THROTTLED_INTERVAL_DAYS if in_throttle else NORMAL_INTERVAL_DAYS

    if days_since is not None and days_since < interval:
        return {
            "allowed": False,
            "mode": "blocked",
            "reason": f"last run {days_since:.1f}d ago < interval {interval}d",
            "min_interval_days": interval,
            "days_since_last_run": days_since,
            "accept_rate": rate,
        }
    return {
        "allowed": True,
        "mode": "reflection" if in_throttle else "normal",
        "reason": "throttled (low accept rate)" if in_throttle else "ok",
        "min_interval_days": interval,
        "days_since_last_run": days_since,
        "accept_rate": rate,
    }


# ── Top-level entry: scan + cluster ────────────────────────────────────


def scan(
    *,
    days_window: int = DEFAULT_WINDOW_DAYS,
    activity_dir_path: Path | None = None,
    min_distinct_runs: int = MIN_DISTINCT_RUNS,
) -> dict[str, Any]:
    """One-shot: collect signals, cluster, return both. Caller (the agent)
    does the LLM-based clustering on top of this."""
    signals = collect_signals(
        days_window=days_window, activity_dir_path=activity_dir_path,
    )
    clusters = cluster_signals(signals, min_distinct_runs=min_distinct_runs)
    return {
        "days_window": days_window,
        "signal_total": len(signals),
        "cluster_count_qualified": len(clusters),
        "min_distinct_runs": min_distinct_runs,
        "clusters": [c.to_dict() for c in clusters],
    }


# ── Helpers ────────────────────────────────────────────────────────────


def _today_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


PROPOSAL_ID_RE = re.compile(r"^\[?(PM-[A-Z])-(\d{4,})\]?$")


def next_proposal_id(track: str = "A") -> str:
    """Look at existing proposals + log to compute the next id."""
    seen: set[int] = set()
    for entry in _read_proposal_log():
        eid = str(entry.get("id") or "")
        m = PROPOSAL_ID_RE.match(eid)
        if m and m.group(1) == f"PM-{track}":
            try:
                seen.add(int(m.group(2)))
            except ValueError:
                continue
    md = proposals_path()
    if md.is_file():
        for m in re.finditer(rf"\[PM-{track}-(\d{{4,}})\]", md.read_text(encoding="utf-8")):
            try:
                seen.add(int(m.group(1)))
            except ValueError:
                continue
    next_n = (max(seen) + 1) if seen else 1
    return f"PM-{track}-{next_n:04d}"
