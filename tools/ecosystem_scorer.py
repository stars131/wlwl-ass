"""Ecosystem radar — dedupe + tiered scoring for RawItems.

Two-stage pipeline:

  1. ``dedupe(items)`` — drops anything we've already pushed in the last
     30 days. Stable ids from collectors are required.
  2. ``score(items)`` — tries the free-pool LLM first (one batched call),
     falls back to a deterministic heuristic if the LLM is unavailable or
     returns garbage. Every item is tagged with a ``tier`` so the
     orchestrator can decide push-now / digest / discard.

Persistence:
  * ``temp/ecosystem_radar_seen.json`` — ``{id: first_seen_iso}`` map.
    TTL 30 days; expired entries are pruned at every dedupe call so the
    file doesn't grow unbounded.

The scorer deliberately tolerates a missing LLM — the radar must still
push critical signals (matched watchlist release, top-3 trending) when
the user is offline or rate-limited. Heuristic tier assignment is the
fallback path; the LLM step refines (and can demote) when available.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from tools.ecosystem_sources import RawItem, DEFAULT_WATCHLIST, TRENDING_KEYWORDS

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_PATH = os.path.join(PROJECT_ROOT, "temp", "ecosystem_radar_seen.json")
SEEN_TTL_DAYS = 30

_seen_lock = threading.Lock()

# Tier ordering — used when sorting / comparing thresholds. Higher = louder.
TIER_ORDER = {"skip": 0, "news": 1, "trending": 2, "notable": 3, "critical": 4}


@dataclass
class ScoredItem:
    item: RawItem
    tier: str
    score: float
    rationale: str
    capability_gap: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # RawItem.raw can contain non-JSON-friendly bits if a source returns
        # surprises; toss it for the on-disk record.
        d["item"].pop("raw", None)
        return d


# ── dedupe ────────────────────────────────────────────────────────────


def _load_seen() -> dict[str, str]:
    if not os.path.isfile(SEEN_PATH):
        return {}
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_seen(data: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SEEN_PATH)


def _prune_expired(seen: dict[str, str]) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
    out = {}
    for k, v in seen.items():
        try:
            t = datetime.fromisoformat(v)
        except Exception:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t >= cutoff:
            out[k] = v
    return out


def dedupe(items: Iterable[RawItem]) -> tuple[list[RawItem], list[str]]:
    """Returns (new_items, already_seen_ids). Stamps new items into the
    seen file so the next run won't re-emit them."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _seen_lock:
        seen = _prune_expired(_load_seen())
        fresh: list[RawItem] = []
        already: list[str] = []
        for it in items:
            if not it.id:
                continue
            if it.id in seen:
                already.append(it.id)
                continue
            fresh.append(it)
            seen[it.id] = now
        _save_seen(seen)
    return fresh, already


# ── LLM scoring ───────────────────────────────────────────────────────


_SCORER_PROMPT = """你是一个 AI 生态雷达评分器。下面是一批刚刚从 GitHub releases、\
GitHub trending、Hacker News 前页和 Grok 实时搜索抓到的原始信号。请把每一条按\
"用户应该多快被打扰"分级。

用户的关注点：AI 编码 agent、终端 CLI 工具、LLM 工具链、开发者效率。\
他特别在意：Codex、Claude Code、Gemini CLI、飞书/Lark CLI、MCP 生态、新出现的\
能在本地跑通的工具。

watchlist（这些仓库的新 release 几乎肯定是 critical）：
{watchlist}

分级标准：
- critical：用户级即时事件——watchlist 仓库的重大 release、影响整个生态的发布\
（如新 Anthropic 模型、新 MCP spec）。这一档直接推飞书。
- notable：值得知道但不紧急——非 watchlist 但同类高质量项目的发布、被多源同时\
报道的事件。进 buffer 等 digest 推送。
- trending：单纯的"热度信号"——GitHub trending 命中、HN 前页但内容是泛 AI 新闻。\
仅 buffer，不单推。
- news：噪音/边缘——重复信息、广告软文、与 AI 工具关系弱的话题。
- skip：完全不相关，下次直接丢弃。

输出 **严格 JSON 数组**，每个元素：
{{"id": "<原 id>", "tier": "<档位>", "score": 0-10 数字, "rationale": "一句中文理由", "capability_gap": "如果用户装上能补什么能力（一句中文，或空字符串）"}}

如果某条你完全看不懂或来源可疑，给 skip。
**不要**输出 JSON 数组以外的任何字符（不要 markdown 围栏、不要解释段）。

待评分信号：
{items_json}
"""


def _build_scorer_input(items: list[RawItem], watchlist: list[str]) -> tuple[str, str]:
    lite = []
    for it in items:
        lite.append({
            "id": it.id,
            "source": it.source,
            "title": it.title[:160],
            "url": it.url,
            "repo": it.repo or "",
            "summary": (it.summary or "")[:300],
            "signal_at": it.signal_at,
        })
    return json.dumps(lite, ensure_ascii=False), ", ".join(watchlist)


def _call_llm_for_score(prompt: str, *, timeout: float = 60.0) -> str | None:
    """Try free-pool first (cheap, public sensitivity OK for trending titles),
    fall back to any configured paid session. Returns raw text answer or
    None on total failure."""
    # Path A: free pool router. This is what task_planning.md tells the
    # autonomous agent to try first; we follow the same rule for radar
    # scoring since the input is all public-source text.
    try:
        from launcher import free_pool_router
        result = free_pool_router.ask(
            prompt,
            sensitivity="public",
            max_attempts=2,
            max_tokens=4096,
            timeout_per_call=timeout,
            autonomous=True,
        )
        if result.get("used") == "free-pool" and result.get("answer"):
            return str(result["answer"])
    except Exception:
        pass

    # Path B: a configured paid LLM session. We pick the first usable one
    # from mykeys — same shape as launcher/llm_binding picks for owner bots.
    try:
        from llmcore import mykeys, LLMSession, ClaudeSession, NativeClaudeSession, NativeOAISession
        candidates_order = ("WLWL_OPENAI_API_KEY", "OPENAI_API_KEY", "WLWL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        cfg = None
        for k, v in mykeys.items() if hasattr(mykeys, "items") else []:
            if isinstance(v, dict) and v.get("apikey") and v.get("apibase"):
                cfg = v
                break
        if not cfg:
            return None
        kind = (cfg.get("kind") or "oai").lower()
        try:
            if "native" in kind and "claude" in kind:
                sess = NativeClaudeSession(cfg=cfg)
            elif "native" in kind:
                sess = NativeOAISession(cfg=cfg)
            elif "claude" in kind:
                sess = ClaudeSession(cfg=cfg)
            else:
                sess = LLMSession(cfg=cfg)
        except Exception:
            return None
        try:
            answer = sess.ask(prompt, stream=False)
        except Exception:
            return None
        if isinstance(answer, str):
            return answer
        return str(answer)
    except Exception:
        return None


def _parse_scorer_output(text: str) -> dict[str, dict[str, Any]]:
    """Tolerate LLMs wrapping JSON in fences / chatter. Returns {id: row}."""
    if not text:
        return {}
    # Strip code fences if present.
    text = text.strip()
    m = re.search(r"\[\s*\{.*?\}\s*\]", text, flags=re.DOTALL)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        tier = str(r.get("tier") or "news").strip().lower()
        if tier not in TIER_ORDER:
            tier = "news"
        try:
            score = float(r.get("score") or 0)
        except Exception:
            score = 0.0
        out[rid] = {
            "tier": tier,
            "score": max(0.0, min(10.0, score)),
            "rationale": str(r.get("rationale") or "").strip()[:200],
            "capability_gap": str(r.get("capability_gap") or "").strip()[:200],
        }
    return out


# ── heuristic fallback ────────────────────────────────────────────────


_HOT_VERBS_RE = re.compile(r"\b(release|launch|update|announce|introduc|ship|version|v?\d+\.\d+)", re.IGNORECASE)


def _heuristic_score(item: RawItem, watchlist_set: set[str]) -> dict[str, Any]:
    """Deterministic fallback tiering. Used when the LLM scorer is down,
    times out, or returns unparseable output. Errs on the side of
    'notable' for watchlist sources so the user doesn't miss a real
    release just because we lost a network round-trip."""
    text = f"{item.title} {item.summary}".lower()
    if item.source == "github_release":
        # Watchlist release → critical; release from other repo → notable.
        if item.repo and item.repo in watchlist_set:
            return {"tier": "critical", "score": 9.0, "rationale": "watchlist 仓库新 release（启发式）", "capability_gap": ""}
        return {"tier": "notable", "score": 7.0, "rationale": "非 watchlist 仓库 release（启发式）", "capability_gap": ""}
    if item.source == "github_trending":
        # First 3 trending entries treated as notable, rest as trending.
        return {"tier": "trending", "score": 5.0, "rationale": "GitHub trending 命中关键词（启发式）", "capability_gap": ""}
    if item.source == "hn":
        if _HOT_VERBS_RE.search(text):
            return {"tier": "notable", "score": 6.5, "rationale": "HN 前页 + 发布/更新动词（启发式）", "capability_gap": ""}
        return {"tier": "news", "score": 4.0, "rationale": "HN 前页（启发式）", "capability_gap": ""}
    if item.source == "grok":
        return {"tier": "news", "score": 4.0, "rationale": "Grok 实时摘要（启发式）", "capability_gap": ""}
    return {"tier": "skip", "score": 0.0, "rationale": "未知来源（启发式跳过）", "capability_gap": ""}


# ── public API ────────────────────────────────────────────────────────


def score(
    items: list[RawItem],
    *,
    watchlist: list[str] | None = None,
    batch_size: int = 20,
    llm_timeout: float = 60.0,
) -> list[ScoredItem]:
    """Score a list of RawItems. Returns one ScoredItem per input, in
    input order. LLM batches at ``batch_size`` to keep prompts bounded.
    Anything the LLM doesn't return falls back to heuristic.
    """
    if not items:
        return []
    wl = list(watchlist) if watchlist else list(DEFAULT_WATCHLIST)
    wl_set = set(wl)

    # 1) collect LLM verdicts across batches
    llm_verdicts: dict[str, dict[str, Any]] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        items_json, watchlist_text = _build_scorer_input(batch, wl)
        prompt = _SCORER_PROMPT.format(watchlist=watchlist_text, items_json=items_json)
        text = _call_llm_for_score(prompt, timeout=llm_timeout)
        if text:
            llm_verdicts.update(_parse_scorer_output(text))

    # 2) merge LLM + heuristic, LLM wins
    out: list[ScoredItem] = []
    for it in items:
        v = llm_verdicts.get(it.id)
        if v is None:
            v = _heuristic_score(it, wl_set)
        out.append(ScoredItem(
            item=it,
            tier=v["tier"],
            score=float(v["score"]),
            rationale=v["rationale"],
            capability_gap=v.get("capability_gap", ""),
        ))
    return out


def tier_at_or_above(scored: ScoredItem, threshold: str) -> bool:
    """Convenience: returns True if scored.tier ≥ threshold in TIER_ORDER."""
    return TIER_ORDER.get(scored.tier, 0) >= TIER_ORDER.get(threshold, 0)


# ── CLI smoke ─────────────────────────────────────────────────────────


def _cli():
    import argparse
    from tools.ecosystem_sources import collect_all

    p = argparse.ArgumentParser(description="Ecosystem radar scorer smoke runner")
    p.add_argument("--no-llm", action="store_true", help="skip LLM, force heuristic")
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()

    items, errors = collect_all()
    if errors:
        print(f"# collector errors: {errors}")
    fresh, already = dedupe(items)
    print(f"# collected={len(items)} fresh={len(fresh)} already_seen={len(already)}")

    if not fresh:
        return
    if args.no_llm:
        wl_set = set(DEFAULT_WATCHLIST)
        scored = [
            ScoredItem(item=it, **_heuristic_score(it, wl_set))
            for it in fresh[:args.limit]
        ]
    else:
        scored = score(fresh[:args.limit])

    for s in scored:
        print(json.dumps({
            "tier": s.tier,
            "score": s.score,
            "id": s.item.id,
            "title": s.item.title[:80],
            "rationale": s.rationale,
        }, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
