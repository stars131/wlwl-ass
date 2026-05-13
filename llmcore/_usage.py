"""Token-usage counters — running totals + cache hit rate per ``api_mode``.

The agent loop streams thousands of LLM calls in a long-running session;
we accumulate per-api_mode totals here so the GUI can surface a running
tally + cache hit rate. Per-model attribution would need threading the
model name through the SSE/JSON parsers — deferred until requested. For
now ``api_mode`` is enough to separate Claude (``messages``) from OAI
variants (``chat_completions`` / ``responses``).

Concurrency: ``BaseSession`` instances stream sequentially within a
single agent loop, but the API server may serve multiple sessions.
A coarse lock keeps the snapshot consistent across the GIL boundary.

State lives in this module's globals — Python's module cache is a
natural singleton, so all callers see the same counters.
"""
import datetime as _dt
import json as _json
import os as _os
import threading as _threading
from pathlib import Path as _Path

from llmcore._pricing import estimate_cost_usd as _estimate_cost_usd
from llmcore._utils import safeprint as print

_USAGE_LOCK = _threading.Lock()
_USAGE_SINCE = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
_USAGE_LAST_TS = ""
_USAGE_BY_MODE: dict[str, dict[str, int]] = {}
_USAGE_BY_SOURCE: dict[str, dict[str, int]] = {}  # "agent" / "vision" / "llm_test" / etc.
_USAGE_RECENT: list[dict] = []  # bounded ring buffer
_USAGE_RECENT_CAP = 200


def _ledger_path() -> _Path:
    # llmcore/_usage.py → project root is one parent up
    return _Path(__file__).resolve().parents[1] / "temp" / "cost_ledger.jsonl"


def _append_ledger(row: dict) -> None:
    """Append a single row to temp/cost_ledger.jsonl. Best-effort: any IO
    failure is swallowed so a disk-full / permission error never breaks the
    agent loop. The file is opened in append-binary mode per line — coarse
    but safe under the module lock."""
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _record_usage(usage, api_mode, *, source: str = "agent", model: str = ""):
    """Translate a provider-specific usage dict into our normalized counters
    and accumulate. Each API has its own field names — that mapping is the
    only provider-aware logic in this module.

    ``source`` (added in the by-source rollup) lets callers tag where the
    spend came from: ``"agent"`` (default — main turn loop), ``"vision"``,
    ``"llm_test"``, ``"moa"``, etc. Lets the GUI separate "what's the
    agent eating" from "what side-tools cost".

    ``model`` is best-effort: extracted from the API response when available
    and threaded through here so the cost ledger can apply per-model pricing."""
    if not usage:
        return
    if api_mode == 'responses':
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        print(f"[Cache] input={inp} cached={cached}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out, cache_read=cached, source=source, model=model)
    elif api_mode == 'chat_completions':
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        inp = usage.get("prompt_tokens", 0)
        out = usage.get("completion_tokens", 0)
        print(f"[Cache] input={inp} cached={cached}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out, cache_read=cached, source=source, model=model)
    elif api_mode == 'messages':
        ci, cr, inp = usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0), usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        print(f"[Cache] input={inp} creation={ci} read={cr}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out,
                          cache_creation=ci, cache_read=cr, source=source, model=model)


def _accumulate_usage(api_mode: str, *, input_tokens: int = 0, output_tokens: int = 0,
                      cache_creation: int = 0, cache_read: int = 0,
                      source: str = "agent", model: str = "") -> None:
    """Add one streaming chunk's usage to the running totals. Best-effort —
    swallows errors so a malformed event never breaks the agent loop.

    Also appends a row to ``temp/cost_ledger.jsonl`` so the GUI can render a
    cost bar + anomaly list. One row per accumulate call (Anthropic streams
    produce two rows per LLM call — one input-side, one output-side); sum
    rows for true totals, dedupe by ``turn_id`` if you need per-call counts.

    The ledger append is done inside the module lock — without it, two
    threads can interleave bytes mid-row and break the JSONL parser, which
    silently drops cost rows from /api/cost/summary. Disk IO here is a few
    microseconds; not worth the integrity tradeoff."""
    global _USAGE_LAST_TS
    try:
        with _USAGE_LOCK:
            now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            entry = _USAGE_BY_MODE.setdefault(
                api_mode,
                {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0},
            )
            entry["input"] += int(input_tokens or 0)
            entry["output"] += int(output_tokens or 0)
            entry["cache_creation"] += int(cache_creation or 0)
            entry["cache_read"] += int(cache_read or 0)
            entry["calls"] += 1
            # by-source rollup parallels by_mode: same shape so the GUI
            # can render either dimension with one component.
            src_entry = _USAGE_BY_SOURCE.setdefault(
                str(source or "agent"),
                {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0},
            )
            src_entry["input"] += int(input_tokens or 0)
            src_entry["output"] += int(output_tokens or 0)
            src_entry["cache_creation"] += int(cache_creation or 0)
            src_entry["cache_read"] += int(cache_read or 0)
            src_entry["calls"] += 1
            _USAGE_LAST_TS = now
            cost = _estimate_cost_usd(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation=cache_creation,
                cache_read=cache_read,
            )
            recent_row = {
                "ts": now,
                "api_mode": api_mode,
                "source": str(source or "agent"),
                "model": str(model or ""),
                "input": int(input_tokens or 0),
                "output": int(output_tokens or 0),
                "cache_creation": int(cache_creation or 0),
                "cache_read": int(cache_read or 0),
                "cost_usd": cost,
            }
            _USAGE_RECENT.append(recent_row)
            if len(_USAGE_RECENT) > _USAGE_RECENT_CAP:
                # Drop oldest in chunks to amortize O(n) shift cost.
                del _USAGE_RECENT[: len(_USAGE_RECENT) - _USAGE_RECENT_CAP]
            _append_ledger(recent_row)
    except Exception:
        pass


def get_token_usage() -> dict:
    """Snapshot the running token-usage counters. Safe to call concurrently
    with active streams — returns a deep-enough copy that the caller can
    serialize without races."""
    with _USAGE_LOCK:
        by_mode = {k: dict(v) for k, v in _USAGE_BY_MODE.items()}
        by_source = {k: dict(v) for k, v in _USAGE_BY_SOURCE.items()}
        recent = list(_USAGE_RECENT)
        last_ts = _USAGE_LAST_TS
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "calls": 0}
    for v in by_mode.values():
        for k in totals:
            totals[k] += v.get(k, 0)
    # Cache hit rate must be computed per api_mode because providers count
    # cached tokens differently:
    #   * Anthropic ``messages``: ``input_tokens`` is *exclusive* of cache
    #     reads, so denom = input + cache_creation + cache_read.
    #   * OpenAI ``chat_completions`` / ``responses``: ``prompt_tokens``
    #     *includes* cached tokens, so denom = input (already the full
    #     billable input).
    # Mixing both into one denominator double-counted OpenAI cached input
    # and made cache_hit_rate falsely low when both providers contributed.
    cache_hit_rate_by_mode: dict[str, float | None] = {}
    weighted_num = 0.0
    weighted_denom = 0.0
    for mode, v in by_mode.items():
        c_read = int(v.get("cache_read", 0) or 0)
        c_create = int(v.get("cache_creation", 0) or 0)
        inp = int(v.get("input", 0) or 0)
        if mode == "messages":
            denom = inp + c_create + c_read
        else:
            # OpenAI-flavoured api_mode (chat_completions / responses).
            # cache_creation is always 0 here; input already includes cached.
            denom = inp
        rate = (c_read / denom) if denom > 0 else None
        cache_hit_rate_by_mode[mode] = rate
        if denom > 0:
            weighted_num += c_read
            weighted_denom += denom
    cache_hit_rate = (weighted_num / weighted_denom) if weighted_denom > 0 else None
    return {
        "totals": totals,
        "by_mode": by_mode,
        "by_source": by_source,
        "since": _USAGE_SINCE,
        "last_ts": last_ts,
        "cache_hit_rate": cache_hit_rate,
        "cache_hit_rate_by_mode": cache_hit_rate_by_mode,
        "recent": recent,
    }


def reset_token_usage() -> None:
    """Zero the counters. Used by the GUI's "reset" button and by tests."""
    global _USAGE_SINCE, _USAGE_LAST_TS
    with _USAGE_LOCK:
        _USAGE_BY_MODE.clear()
        _USAGE_BY_SOURCE.clear()
        _USAGE_RECENT.clear()
        _USAGE_SINCE = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        _USAGE_LAST_TS = ""
