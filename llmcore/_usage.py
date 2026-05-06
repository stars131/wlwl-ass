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
import threading as _threading

from llmcore._utils import safeprint as print

_USAGE_LOCK = _threading.Lock()
_USAGE_SINCE = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
_USAGE_LAST_TS = ""
_USAGE_BY_MODE: dict[str, dict[str, int]] = {}
_USAGE_BY_SOURCE: dict[str, dict[str, int]] = {}  # "agent" / "vision" / "llm_test" / etc.
_USAGE_RECENT: list[dict] = []  # bounded ring buffer
_USAGE_RECENT_CAP = 200


def _record_usage(usage, api_mode, *, source: str = "agent"):
    """Translate a provider-specific usage dict into our normalized counters
    and accumulate. Each API has its own field names — that mapping is the
    only provider-aware logic in this module.

    ``source`` (added in the by-source rollup) lets callers tag where the
    spend came from: ``"agent"`` (default — main turn loop), ``"vision"``,
    ``"llm_test"``, ``"moa"``, etc. Lets the GUI separate "what's the
    agent eating" from "what side-tools cost"."""
    if not usage:
        return
    if api_mode == 'responses':
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        print(f"[Cache] input={inp} cached={cached}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out, cache_read=cached, source=source)
    elif api_mode == 'chat_completions':
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        inp = usage.get("prompt_tokens", 0)
        out = usage.get("completion_tokens", 0)
        print(f"[Cache] input={inp} cached={cached}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out, cache_read=cached, source=source)
    elif api_mode == 'messages':
        ci, cr, inp = usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0), usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        print(f"[Cache] input={inp} creation={ci} read={cr}")
        _accumulate_usage(api_mode, input_tokens=inp, output_tokens=out,
                          cache_creation=ci, cache_read=cr, source=source)


def _accumulate_usage(api_mode: str, *, input_tokens: int = 0, output_tokens: int = 0,
                      cache_creation: int = 0, cache_read: int = 0,
                      source: str = "agent") -> None:
    """Add one streaming chunk's usage to the running totals. Best-effort —
    swallows errors so a malformed event never breaks the agent loop."""
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
            _USAGE_RECENT.append({
                "ts": now,
                "api_mode": api_mode,
                "source": str(source or "agent"),
                "input": int(input_tokens or 0),
                "output": int(output_tokens or 0),
                "cache_creation": int(cache_creation or 0),
                "cache_read": int(cache_read or 0),
            })
            if len(_USAGE_RECENT) > _USAGE_RECENT_CAP:
                # Drop oldest in chunks to amortize O(n) shift cost.
                del _USAGE_RECENT[: len(_USAGE_RECENT) - _USAGE_RECENT_CAP]
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
    # Cache hit rate vs *total billable input* (i.e. input that wasn't a
    # cache read). Anthropic counts input_tokens *exclusive* of cache reads,
    # so the denominator is input + cache_creation + cache_read.
    denom = totals["input"] + totals["cache_creation"] + totals["cache_read"]
    cache_hit_rate = (totals["cache_read"] / denom) if denom > 0 else None
    return {
        "totals": totals,
        "by_mode": by_mode,
        "by_source": by_source,
        "since": _USAGE_SINCE,
        "last_ts": last_ts,
        "cache_hit_rate": cache_hit_rate,
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
