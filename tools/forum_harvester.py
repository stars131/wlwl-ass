"""linux.do (Discourse) free-API harvester — pure parsing layer.

The agent layer (``wlwl_ass.do_forum_harvest``) fetches Discourse JSON via
``web_execute_js`` against the user's already-logged-in Chrome session.
This module is the **pure** extractor: feed it topic JSON, get candidates.
Keep network IO out of here so it stays unit-testable.

Candidate extraction is heuristic-first (regex). A post is "interesting" if
it mentions an API endpoint URL **and** an OpenAI-style key in the same
post. Model names are best-effort guesses from text near those tokens.

Limitations to be aware of:
- Discourse ``cooked`` HTML escapes code blocks; ``raw`` markdown is cleaner
  but only visible to logged-in users with trust level ≥ 1.
- Some posters paste credentials in screenshots or behind a "回复可见"
  paywall — those we skip silently. Better to miss than to false-positive.
- A single post can advertise multiple endpoints; we emit one Candidate
  per detected ``(base_url, key)`` pair.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# Discourse usually exposes a JSON view by appending ``.json`` to any HTML
# route. Latest topics:           /latest.json
# Topic detail (with posts):      /t/{topic_id}.json   or  /t/{slug}/{topic_id}.json
# Category:                       /c/{slug}/{id}.json
# Search:                         /search.json?q=...
LATEST_URL = "https://linux.do/latest.json"
TOPIC_URL_FMT = "https://linux.do/t/{topic_id}.json"


# Forum tags / category fragments that hint at "free API" content. We don't
# require these — posts in any category can match — but they bias the
# harvester to look at the right places first.
INTERESTING_TAG_HINTS: tuple[str, ...] = (
    "api", "免费", "中转", "key", "llm", "claude", "gpt", "gemini",
    "deepseek", "qwen", "openai", "chatgpt", "ai",
)


# Regexes — keep them strict enough to avoid grabbing every URL on the page
# but loose enough for common Discourse formatting variations.

_URL_RE = re.compile(
    r"https?://[A-Za-z0-9.\-_:]+(?::\d{2,5})?(?:/[A-Za-z0-9._\-/]*)?",
    flags=re.IGNORECASE,
)

# Match "looks like an API endpoint" — must contain /v1 or end in /api or
# /api/v1 etc. Trims trailing punctuation.
_API_ENDPOINT_RE = re.compile(
    r"(?P<url>https?://[A-Za-z0-9.\-_:]+(?::\d{2,5})?"
    r"(?:/[A-Za-z0-9._\-]+)*"
    r"/(?:api(?:/v\d+)?|v\d+))",
    flags=re.IGNORECASE,
)

# OpenAI-style keys: ``sk-...`` long token (covers Anthropic ``sk-ant-...``
# and most OpenAI-compat relays). We require ≥ 20 chars of payload to avoid
# matching code samples like ``sk-test``.
_OPENAI_KEY_RE = re.compile(
    r"\bsk-(?:[A-Za-z0-9\-_]{20,200})\b",
)

# Model name guesses near the endpoint/key.
_MODEL_NAME_RE = re.compile(
    r"\b("
    r"claude[-\w.]*|gpt[-\w.]*|chatgpt[-\w.]*|gemini[-\w.]*|"
    r"deepseek[-\w.]*|qwen[-\w.]*|glm[-\w.]*|kimi[-\w.]*|"
    r"moonshot[-\w.]*|doubao[-\w.]*|yi[-\w.]*|llama[-\w.]*|"
    r"mistral[-\w.]*|mixtral[-\w.]*|o[1-4][-\w.]*"
    r")\b",
    flags=re.IGNORECASE,
)


@dataclass
class Candidate:
    base_url: str
    key: str
    claimed_model: str = ""
    post_url: str = ""
    topic_id: int | None = None
    post_id: int | None = None
    author: str = ""
    title: str = ""
    category_id: int | None = None
    tags: list[str] = field(default_factory=list)
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "key": self.key,
            "claimed_model": self.claimed_model,
            "source": {
                "forum": "linux.do",
                "post_url": self.post_url,
                "topic_id": self.topic_id,
                "post_id": self.post_id,
                "author": self.author,
                "title": self.title,
                "category_id": self.category_id,
                "tags": self.tags,
            },
            "snippet": self.snippet,
        }


def _strip_html(cooked: str) -> str:
    """Discourse ``cooked`` is HTML. We only care about visible text + URLs
    inside ``<a href=...>`` and code blocks. Strip tags but keep hrefs and
    code content."""
    if not cooked:
        return ""
    # Pull href= URLs out so the regex sees them as plain text.
    text = re.sub(
        r"<a[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>",
        r" \1 \2 ",
        cooked,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _html.unescape(text)


def _normalize_base_url(url: str) -> str:
    """Turn ``https://x.com/v1/chat/completions`` into ``https://x.com/v1``.
    Keeps the API root, drops the resource path."""
    if not url:
        return ""
    url = url.rstrip("/.,;:!)]\"'>")
    m = re.match(
        r"^(https?://[^/]+(?:/[A-Za-z0-9._\-]+)*?/(?:api(?:/v\d+)?|v\d+))",
        url,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else url


def _guess_model_near(text: str, anchor_idx: int, *, window: int = 240) -> str:
    """Find the most likely model name within ``window`` chars of ``anchor_idx``."""
    start = max(0, anchor_idx - window)
    end = min(len(text), anchor_idx + window)
    region = text[start:end]
    matches = list(_MODEL_NAME_RE.finditer(region))
    if not matches:
        return ""
    # Closest to the anchor wins.
    rel_anchor = anchor_idx - start
    best = min(matches, key=lambda m: abs(((m.start() + m.end()) // 2) - rel_anchor))
    return best.group(1).strip().lower()


def parse_post_text(
    text: str,
    *,
    post_url: str = "",
    topic_id: int | None = None,
    post_id: int | None = None,
    author: str = "",
    title: str = "",
    category_id: int | None = None,
    tags: Iterable[str] | None = None,
) -> list[Candidate]:
    """Extract candidates from a single post's text (Discourse ``raw`` or
    HTML-stripped ``cooked``). Returns 0..N candidates.

    Pairing logic: every key is paired with the **nearest** endpoint URL by
    character index. If a post mentions one URL and three keys, you get
    three candidates sharing the same ``base_url``. If a post has no key in
    sight at all, we emit nothing — the URL alone is not actionable.
    """
    if not text or not text.strip():
        return []
    urls = list(_API_ENDPOINT_RE.finditer(text))
    keys = list(_OPENAI_KEY_RE.finditer(text))
    if not urls or not keys:
        return []
    candidates: list[Candidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for km in keys:
        key_pos = (km.start() + km.end()) // 2
        nearest_url = min(
            urls,
            key=lambda um: abs(((um.start() + um.end()) // 2) - key_pos),
        )
        base_url = _normalize_base_url(nearest_url.group("url"))
        key = km.group(0)
        pair = (base_url, key)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        model = _guess_model_near(text, key_pos)
        # Snippet: a small text window around the key, for the report.
        snippet_start = max(0, km.start() - 80)
        snippet_end = min(len(text), km.end() + 80)
        snippet = re.sub(r"\s+", " ", text[snippet_start:snippet_end]).strip()
        candidates.append(Candidate(
            base_url=base_url,
            key=key,
            claimed_model=model,
            post_url=post_url,
            topic_id=topic_id,
            post_id=post_id,
            author=author,
            title=title,
            category_id=category_id,
            tags=list(tags or []),
            snippet=snippet,
        ))
    return candidates


def extract_from_topic_json(topic_json: dict[str, Any]) -> list[Candidate]:
    """Walk a ``/t/<id>.json`` payload, calling ``parse_post_text`` on each post."""
    if not isinstance(topic_json, dict):
        return []
    topic_id = topic_json.get("id")
    title = topic_json.get("title") or topic_json.get("fancy_title") or ""
    category_id = topic_json.get("category_id")
    tags = topic_json.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    post_stream = topic_json.get("post_stream") or {}
    posts = post_stream.get("posts") if isinstance(post_stream, dict) else None
    if not isinstance(posts, list):
        return []

    out: list[Candidate] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        post_id = post.get("id")
        author = post.get("username") or ""
        post_url = (
            f"https://linux.do/t/{topic_id}/{post.get('post_number', '')}".rstrip("/")
            if topic_id is not None else ""
        )
        # Prefer ``raw`` when present (cleaner). Fall back to ``cooked`` stripped.
        raw = (post.get("raw") or "").strip()
        text = raw or _strip_html(post.get("cooked") or "")
        if not text:
            continue
        out.extend(parse_post_text(
            text,
            post_url=post_url,
            topic_id=topic_id if isinstance(topic_id, int) else None,
            post_id=post_id if isinstance(post_id, int) else None,
            author=author,
            title=title,
            category_id=category_id if isinstance(category_id, int) else None,
            tags=tags,
        ))
    return out


def select_topic_ids_from_latest(
    latest_json: dict[str, Any],
    *,
    limit: int = 30,
    require_keywords: bool = True,
) -> list[int]:
    """Pick which topic IDs from ``/latest.json`` to actually fetch.

    When ``require_keywords`` is True, only topics whose title or tags hint
    at API content are returned. Set False to scan everything (slower).
    """
    if not isinstance(latest_json, dict):
        return []
    topic_list = latest_json.get("topic_list") or {}
    topics = topic_list.get("topics") if isinstance(topic_list, dict) else None
    if not isinstance(topics, list):
        return []
    out: list[int] = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if not isinstance(tid, int):
            continue
        title = (t.get("title") or "").lower()
        tags = t.get("tags") or []
        tag_text = " ".join(tags).lower() if isinstance(tags, list) else ""
        haystack = f"{title} {tag_text}"
        if require_keywords and not any(h in haystack for h in INTERESTING_TAG_HINTS):
            continue
        out.append(tid)
        if len(out) >= limit:
            break
    return out
