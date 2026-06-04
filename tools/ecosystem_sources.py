"""Ecosystem radar — fetch raw signals from AI-ecosystem sources.

Four collectors, all stdlib + ``requests`` + ``beautifulsoup4`` only:

  * github_releases — Atom feed per repo (no PAT needed). Default watchlist
    targets AI-coding agents / CLIs / dev-tooling.
  * github_trending — HTML scrape of ``github.com/trending`` with keyword
    filtering; the LLM step downstream does the real triage.
  * hn_ai — Hacker News front-page hits via the Algolia public API.
  * grok_live — reuses ``tools.web_search._grok_live`` if XAI_API_KEY is set,
    otherwise returns an empty list silently.

Every collector returns ``list[RawItem]`` with stable ``id`` so the scorer
can dedupe across runs. Failures degrade gracefully — a single source going
down never blocks the others (the orchestrator calls each in its own
thread).

CLI smoke (each source is independently runnable):

    python -m tools.ecosystem_sources --source github_releases \\
        --watchlist openai/codex,anthropics/claude-code
    python -m tools.ecosystem_sources --source github_trending
    python -m tools.ecosystem_sources --source hn_ai
    python -m tools.ecosystem_sources --source grok_live
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

# ── shared types ──────────────────────────────────────────────────────


@dataclass
class RawItem:
    """One raw signal from any source. Keep ``id`` stable across runs so the
    scorer's dedupe set stays meaningful — collectors must derive ``id`` from
    immutable fields (release tag, repo name, HN story_id), never from a
    timestamp or rank."""

    source: str
    id: str
    title: str
    url: str
    repo: str | None
    summary: str
    signal_at: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Default watchlist — covers the user's stated examples (Codex, Claude Code,
# Gemini CLI, Lark/Feishu CLI) plus high-signal sibling projects. Override
# via ``WLWL_RADAR_WATCHLIST=owner/repo,owner/repo,...``.
DEFAULT_WATCHLIST: tuple[str, ...] = (
    "lsdefine/GenericAgent",  # upstream — track own origin
    "openai/codex",
    "anthropics/claude-code",
    "google-gemini/gemini-cli",
    "larksuite/lark-cli",
    "sst/opencode",
    "block/goose",
    "microsoft/markitdown",
    "All-Hands-AI/OpenHands",
    "QwenLM/Qwen-Agent",
    "modelcontextprotocol/servers",
    "stanfordnlp/dspy",
    "huggingface/smolagents",
)

# Topic / keyword tokens used by the trending filter and the HN query
# expansion. Kept narrow on purpose — broader nets ("AI") drown the signal.
TRENDING_KEYWORDS: tuple[str, ...] = (
    "ai", "agent", "agents", "llm", "mcp", "cli", "terminal", "tts",
    "voice", "rag", "chatgpt", "claude", "gemini", "codex",
    "coding-agent", "developer-tools",
)

HN_KEYWORDS: tuple[str, ...] = (
    "claude", "gemini", "codex", "mcp", "agent", "llm",
    "anthropic", "openai", "cli",
)

# Default request budget per source. Orchestrator enforces a hard 90s total.
DEFAULT_TIMEOUT_S = 25.0

# Polite UA — GitHub HTML scraping has been known to soft-block default
# requests UAs. Keep it identifiable without pretending to be a browser.
_UA = "wlwl-ass-ecosystem-radar/0.1 (+https://github.com/)"


def _http_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"User-Agent": _UA, "Accept": "*/*"}
    pat = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if pat:
        h["Authorization"] = f"token {pat}"
    if extra:
        h.update(extra)
    return h


# ── github_releases ───────────────────────────────────────────────────


_ATOM_RELEASES_URL = "https://github.com/{repo}/releases.atom"


def fetch_github_releases(
    watchlist: list[str] | None = None,
    *,
    per_repo_limit: int = 3,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RawItem]:
    """Pull the latest releases for each watchlist repo via Atom feed.

    Atom is the only zero-auth, zero-rate-limit-friendly path — the REST
    ``/releases`` endpoint counts against the 60 req/h anonymous quota and
    fails for repos that only tag (don't ``release``). Atom catches both
    tags and releases.
    """
    repos = _resolve_watchlist(watchlist)
    out: list[RawItem] = []
    for repo in repos:
        url = _ATOM_RELEASES_URL.format(repo=repo)
        try:
            r = requests.get(url, headers=_http_headers(), timeout=timeout)
        except requests.RequestException:
            continue
        if r.status_code != 200 or not r.text:
            continue
        out.extend(_parse_releases_atom(r.text, repo=repo, limit=per_repo_limit))
    return out


def _resolve_watchlist(override: list[str] | None) -> list[str]:
    if override:
        items = override
    else:
        env = (os.environ.get("WLWL_RADAR_WATCHLIST") or "").strip()
        items = [s.strip() for s in env.split(",")] if env else list(DEFAULT_WATCHLIST)
    # Truncate to 20 to stay under anonymous GitHub rate limit (60 req/h),
    # leaving headroom for trending + other endpoints.
    cleaned = [s for s in items if "/" in s and not s.startswith("#")]
    return cleaned[:20]


def _parse_releases_atom(xml_text: str, *, repo: str, limit: int) -> list[RawItem]:
    """Atom is XML but a tolerant regex extractor is enough — we only need
    title / link / updated / id per entry. Pulling in lxml/feedparser just
    for 3 fields would be overkill given the dep budget."""
    out: list[RawItem] = []
    entries = re.findall(r"<entry>(.*?)</entry>", xml_text, flags=re.DOTALL)
    for block in entries[:limit]:
        eid = _atom_field(block, "id") or ""
        title = _atom_field(block, "title") or ""
        updated = _atom_field(block, "updated") or ""
        # <link href="..."/>
        m = re.search(r'<link[^>]*href="([^"]+)"', block)
        link = m.group(1) if m else f"https://github.com/{repo}"
        content = _atom_field(block, "content") or ""
        # Reasonable summary cap; the LLM scorer will see this as context.
        summary = re.sub(r"<[^>]+>", " ", content)
        summary = html.unescape(summary)
        summary = re.sub(r"\s+", " ", summary).strip()[:800]
        tag = eid.rsplit("/", 1)[-1] if eid else title
        out.append(RawItem(
            source="github_release",
            id=f"github:{repo}:{tag}",
            title=html.unescape(title.strip() or tag),
            url=link,
            repo=repo,
            summary=summary,
            signal_at=updated or _now_iso(),
            raw={"atom_id": eid},
        ))
    return out


def _atom_field(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, flags=re.DOTALL)
    return m.group(1).strip() if m else None


# ── github_trending ───────────────────────────────────────────────────


_TRENDING_URL = "https://github.com/trending?since={since}"


def fetch_github_trending(
    *,
    since: str = "daily",
    limit: int = 25,
    keyword_filter: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RawItem]:
    """Scrape GitHub's public trending HTML page. There is no official API.

    The page layout has been stable for years (``article.Box-row`` per repo)
    but if GitHub ships a redesign, this should degrade to an empty list
    silently rather than throw — upstream callers rely on best-effort.
    """
    url = _TRENDING_URL.format(since=since)
    try:
        r = requests.get(url, headers=_http_headers(), timeout=timeout)
    except requests.RequestException:
        return []
    if r.status_code != 200 or not r.text:
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    rows = soup.select("article.Box-row")
    out: list[RawItem] = []
    for row in rows[:limit]:
        link = row.select_one("h2 a")
        if not link:
            continue
        repo = (link.get("href") or "").strip("/")
        if "/" not in repo:
            continue
        desc_el = row.select_one("p")
        desc = (desc_el.text or "").strip() if desc_el else ""
        # Star count appears as text in the second a[href$="/stargazers"];
        # parsing is best-effort.
        star_el = row.select_one('a[href$="/stargazers"]')
        stars = (star_el.text or "").strip() if star_el else ""
        haystack = f"{repo} {desc}".lower()
        if keyword_filter and not any(k in haystack for k in TRENDING_KEYWORDS):
            continue
        out.append(RawItem(
            source="github_trending",
            id=f"trending:{since}:{repo}",
            title=repo,
            url=f"https://github.com/{repo}",
            repo=repo,
            summary=desc,
            signal_at=_now_iso(),
            raw={"stars_text": stars, "since": since},
        ))
    return out


# ── hn_ai ─────────────────────────────────────────────────────────────


_HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def fetch_hn_ai(
    *,
    keywords: list[str] | None = None,
    per_keyword_limit: int = 5,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RawItem]:
    """Query Algolia's free HN index for front-page stories matching AI-tool
    keywords. Algolia is the canonical HN search backend — no API key, no
    rate limit headache for the volumes here."""
    kws = keywords or list(HN_KEYWORDS)
    seen_ids: set[str] = set()
    out: list[RawItem] = []
    for kw in kws:
        params = {
            "tags": "front_page",
            "query": kw,
            "hitsPerPage": per_keyword_limit,
        }
        try:
            r = requests.get(_HN_SEARCH, params=params, headers=_http_headers(), timeout=timeout)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        for hit in (data.get("hits") or []):
            story_id = str(hit.get("objectID") or "")
            if not story_id or story_id in seen_ids:
                continue
            seen_ids.add(story_id)
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            url_field = (hit.get("url") or "").strip()
            hn_url = f"https://news.ycombinator.com/item?id={story_id}"
            out.append(RawItem(
                source="hn",
                id=f"hn:{story_id}",
                title=title,
                url=url_field or hn_url,
                repo=_guess_repo_from_url(url_field),
                summary=(hit.get("story_text") or "")[:500],
                signal_at=hit.get("created_at") or _now_iso(),
                raw={
                    "points": hit.get("points"),
                    "num_comments": hit.get("num_comments"),
                    "hn_url": hn_url,
                    "matched_keyword": kw,
                },
            ))
    return out


def _guess_repo_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


# ── grok_live ─────────────────────────────────────────────────────────


def fetch_grok_live(
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[RawItem]:
    """Ask Grok's live_search for top AI-tooling news. Only fires if the
    user has configured an xAI key — otherwise silent no-op, since this
    source is opt-in (quota-bearing)."""
    try:
        from tools.web_search import _get_grok_key, _get_grok_model, _grok_live
    except Exception:
        return []
    if not _get_grok_key():
        return []
    prompt = (
        "List the 5 most important news items from the past 24 hours about "
        "AI coding agents, terminal CLIs, LLM tooling, and developer "
        "productivity. For each: a one-line headline, the primary URL, and "
        "(if applicable) the github owner/repo. Plain text, one item per "
        "line, format:\n"
        "  HEADLINE | URL | repo-or-empty"
    )
    out_payload = _grok_live(prompt, ["web", "x", "news"], _get_grok_model(), timeout)
    if out_payload.get("error"):
        return []
    answer = (out_payload.get("answer") or "").strip()
    if not answer:
        return []
    out: list[RawItem] = []
    for line in answer.splitlines():
        line = line.strip().lstrip("-*•0123456789. ").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        headline = parts[0]
        url = parts[1]
        repo = parts[2] if len(parts) >= 3 and "/" in parts[2] else None
        if not headline or not url.startswith("http"):
            continue
        # Stable id from headline+url hash-ish prefix; Grok's output isn't
        # guaranteed stable so we accept some dedupe noise here.
        norm = re.sub(r"\W+", "_", (headline + url).lower())[:80]
        out.append(RawItem(
            source="grok",
            id=f"grok:{norm}",
            title=headline,
            url=url,
            repo=repo or _guess_repo_from_url(url),
            summary="",
            signal_at=_now_iso(),
            raw={"citations": out_payload.get("citations", [])},
        ))
    return out


# ── helpers ───────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── dispatcher ────────────────────────────────────────────────────────


COLLECTORS = {
    "github_releases": fetch_github_releases,
    "github_trending": fetch_github_trending,
    "hn_ai": fetch_hn_ai,
    "grok_live": fetch_grok_live,
}


def collect_all(
    *,
    watchlist: list[str] | None = None,
    timeout_per_source: float = DEFAULT_TIMEOUT_S,
) -> tuple[list[RawItem], dict[str, str]]:
    """Run every collector concurrently. Returns (items, errors_by_source).

    Errors are captured per source so a single dead endpoint never blocks
    the others — the orchestrator's contract is "best effort across all
    sources within ~90s".
    """
    import threading

    results: dict[str, list[RawItem]] = {k: [] for k in COLLECTORS}
    errors: dict[str, str] = {}

    def _run(name: str, fn):
        try:
            if name == "github_releases":
                results[name] = fn(watchlist=watchlist, timeout=timeout_per_source)
            else:
                results[name] = fn(timeout=timeout_per_source)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=_run, args=(n, f), daemon=True) for n, f in COLLECTORS.items()]
    for t in threads:
        t.start()
    deadline = timeout_per_source + 5
    for t in threads:
        t.join(timeout=deadline)
    for name, t in zip(COLLECTORS.keys(), threads):
        if t.is_alive() and name not in errors:
            errors[name] = f"timed out after {deadline:.0f}s"

    flat: list[RawItem] = []
    for v in results.values():
        flat.extend(v)
    return flat, errors


# ── CLI smoke ─────────────────────────────────────────────────────────


def _cli():
    import argparse

    p = argparse.ArgumentParser(description="Ecosystem radar source smoke runner")
    p.add_argument("--source", required=True, choices=list(COLLECTORS.keys()) + ["all"])
    p.add_argument("--watchlist", default="")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = p.parse_args()

    wl = [s.strip() for s in args.watchlist.split(",") if s.strip()] or None

    if args.source == "all":
        items, errors = collect_all(watchlist=wl, timeout_per_source=args.timeout)
        print(f"# total: {len(items)} items, errors: {errors}")
    elif args.source == "github_releases":
        items = fetch_github_releases(watchlist=wl, timeout=args.timeout)
        print(f"# github_releases: {len(items)} items")
    elif args.source == "github_trending":
        items = fetch_github_trending(limit=args.limit, timeout=args.timeout)
        print(f"# github_trending: {len(items)} items")
    elif args.source == "hn_ai":
        items = fetch_hn_ai(timeout=args.timeout)
        print(f"# hn_ai: {len(items)} items")
    elif args.source == "grok_live":
        items = fetch_grok_live(timeout=args.timeout)
        print(f"# grok_live: {len(items)} items (empty if XAI_API_KEY unset)")
    else:
        items = []

    for it in items[:args.limit]:
        d = it.to_dict()
        d.pop("raw", None)
        print(json.dumps(d, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
