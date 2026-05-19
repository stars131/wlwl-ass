"""``wlwl tokens`` subcommand — aggregate temp/cost_ledger.jsonl for the terminal.

Mirrors the data returned by the GUI's `/api/cost/summary`:

    wlwl tokens                  totals + last 7 days + top models
    wlwl tokens --days 30        last N days
    wlwl tokens --by-model       only the per-model table
    wlwl tokens --recent N       last N rows
    wlwl tokens --json           machine-readable JSON dump

The ledger lives at ``temp/cost_ledger.jsonl`` — one JSON object per LLM
call appended by the cost-tracking middleware in llmcore. We intentionally
re-implement the aggregation here (instead of importing the api_server
route) so the CLI works without spinning up the HTTP server.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from collections import defaultdict


def _project_root() -> str:
    return os.environ.get("WLWL_PROJECT_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


def _ledger_path() -> str:
    return os.path.join(_project_root(), "temp", "cost_ledger.jsonl")


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_tok(n: int) -> str:
    """Compact token formatter: 1.2k / 4.7M / 813."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_usd(x: float) -> str:
    return f"${x:.4f}" if x < 10 else f"${x:.2f}"


def _print_table(rows: list[list[str]], header: list[str]) -> None:
    cols = list(zip(header, *rows)) if rows else [[h] for h in header]
    widths = [max(len(str(cell)) for cell in col) for col in cols]
    sep = "  "
    def fmt(line):
        return sep.join(str(c).ljust(w) for c, w in zip(line, widths))
    print(fmt(header))
    print(sep.join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def _scan_ledger():
    """Yield parsed rows from temp/cost_ledger.jsonl, skipping bad lines."""
    path = _ledger_path()
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _aggregate():
    """Return the full aggregation dict (no truncation)."""
    today_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    total = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    today = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    by_model: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    )
    by_day: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    )
    by_source: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    )
    recent: list[dict] = []
    keep_recent = 100  # ring buffer of last N rows
    for row in _scan_ledger():
        ts = str(row.get("ts") or "")
        day = ts[:10] if len(ts) >= 10 else "(no-ts)"
        model = str(row.get("model") or "(unknown)")
        source = str(row.get("source") or "agent")
        in_t = int(row.get("input") or 0)
        out_t = int(row.get("output") or 0)
        cc_t = int(row.get("cache_creation") or 0)
        cr_t = int(row.get("cache_read") or 0)
        cost = float(row.get("cost_usd") or 0.0)
        for bucket in (total, by_model[model], by_day[day], by_source[source]):
            bucket["input"] += in_t
            bucket["output"] += out_t
            bucket["cache_creation"] += cc_t
            bucket["cache_read"] += cr_t
            bucket["calls"] += 1
            bucket["cost_usd"] += cost
        if ts.startswith(today_iso):
            today["input"] += in_t
            today["output"] += out_t
            today["cache_creation"] += cc_t
            today["cache_read"] += cr_t
            today["calls"] += 1
            today["cost_usd"] += cost
        recent.append({"ts": ts, "model": model, "source": source,
                       "input": in_t, "output": out_t, "cache_read": cr_t,
                       "cost_usd": cost})
        if len(recent) > keep_recent:
            recent.pop(0)
    return {
        "total": total,
        "today": today,
        "by_model": dict(by_model),
        "by_day": dict(by_day),
        "by_source": dict(by_source),
        "recent": recent,
    }


def _print_overview(agg: dict) -> None:
    total = agg["total"]
    today = agg["today"]
    if total["calls"] == 0:
        print("(no rows in temp/cost_ledger.jsonl — agent has not made billed calls yet)")
        return
    print("总览（自首条记录）")
    rows = [
        ["calls",            _fmt_int(total["calls"]),            _fmt_int(today["calls"])],
        ["input tokens",     _fmt_tok(total["input"]),            _fmt_tok(today["input"])],
        ["output tokens",    _fmt_tok(total["output"]),           _fmt_tok(today["output"])],
        ["cache_creation",   _fmt_tok(total["cache_creation"]),   _fmt_tok(today["cache_creation"])],
        ["cache_read",       _fmt_tok(total["cache_read"]),       _fmt_tok(today["cache_read"])],
        ["cost (USD)",       _fmt_usd(total["cost_usd"]),         _fmt_usd(today["cost_usd"])],
    ]
    _print_table(rows, ["metric", "all-time", "today (UTC)"])


def _print_by_model(agg: dict, limit: int) -> None:
    items = sorted(
        agg["by_model"].items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
    )[:limit]
    if not items:
        return
    print("\n按模型（按 USD 倒序）")
    rows = []
    for model, v in items:
        rows.append([
            model,
            _fmt_int(v["calls"]),
            _fmt_tok(v["input"]),
            _fmt_tok(v["output"]),
            _fmt_tok(v["cache_read"]),
            _fmt_usd(v["cost_usd"]),
        ])
    _print_table(rows, ["model", "calls", "input", "output", "cache_read", "cost"])


def _print_by_day(agg: dict, days: int) -> None:
    items = sorted(agg["by_day"].items(), key=lambda kv: kv[0], reverse=True)[:days]
    if not items:
        return
    print(f"\n最近 {len(items)} 天")
    rows = []
    for day, v in items:
        rows.append([
            day,
            _fmt_int(v["calls"]),
            _fmt_tok(v["input"]),
            _fmt_tok(v["output"]),
            _fmt_tok(v["cache_read"]),
            _fmt_usd(v["cost_usd"]),
        ])
    _print_table(rows, ["day", "calls", "input", "output", "cache_read", "cost"])


def _print_by_source(agg: dict) -> None:
    items = sorted(
        agg["by_source"].items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
    )
    if not items:
        return
    print("\n按来源")
    rows = []
    for source, v in items:
        rows.append([
            source,
            _fmt_int(v["calls"]),
            _fmt_tok(v["input"] + v["output"]),
            _fmt_usd(v["cost_usd"]),
        ])
    _print_table(rows, ["source", "calls", "io_tokens", "cost"])


def _print_recent(agg: dict, n: int) -> None:
    items = agg["recent"][-n:]
    if not items:
        return
    print(f"\n最近 {len(items)} 条")
    rows = []
    for r in items:
        rows.append([
            (r["ts"] or "")[:19],
            r["model"][:32],
            r["source"][:14],
            _fmt_tok(r["input"]),
            _fmt_tok(r["output"]),
            _fmt_usd(r["cost_usd"]),
        ])
    _print_table(rows, ["ts(UTC)", "model", "source", "in", "out", "cost"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wlwl tokens")
    p.add_argument("--days", type=int, default=7, help="show last N days (default 7)")
    p.add_argument("--top-models", type=int, default=10,
                   help="show top N models (default 10)")
    p.add_argument("--by-model", action="store_true",
                   help="only print per-model table")
    p.add_argument("--by-day", action="store_true",
                   help="only print per-day table")
    p.add_argument("--by-source", action="store_true",
                   help="only print per-source table")
    p.add_argument("--recent", type=int, default=0,
                   help="also print last N ledger rows")
    p.add_argument("--json", action="store_true",
                   help="dump aggregation as JSON to stdout (machine-readable)")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    agg = _aggregate()

    if args.json:
        # Slim defaultdicts → plain dicts for json
        out = {
            "total": agg["total"],
            "today": agg["today"],
            "by_model": agg["by_model"],
            "by_day": agg["by_day"],
            "by_source": agg["by_source"],
            "recent": agg["recent"],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.by_model:
        _print_by_model(agg, args.top_models)
        return 0
    if args.by_day:
        _print_by_day(agg, args.days)
        return 0
    if args.by_source:
        _print_by_source(agg)
        return 0

    _print_overview(agg)
    _print_by_model(agg, args.top_models)
    _print_by_day(agg, args.days)
    _print_by_source(agg)
    if args.recent:
        _print_recent(agg, args.recent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
