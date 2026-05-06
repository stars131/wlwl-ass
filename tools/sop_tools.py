"""LLM-callable wrappers around memory.skill_search (Sophub SOP API).

Both functions return plain strings so they slot directly into LLM tool-result
history. Errors are surfaced as `[sop_* error] ...` rather than raised, so a
failed Sophub query never aborts the agent loop.
"""
from __future__ import annotations

import os
import sys

_SKILL_SEARCH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "memory",
    "skill_search",
)
if _SKILL_SEARCH_PATH not in sys.path:
    sys.path.insert(0, _SKILL_SEARCH_PATH)


def _import_engine():
    from skill_search import SkillSearchError, get_stats, read_sop, search

    return SkillSearchError, search, read_sop, get_stats


def sop_search(query: str, top_k: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "[sop_search error] query is required"
    try:
        SkillSearchError, search, _, _ = _import_engine()
    except Exception as exc:
        return f"[sop_search error] cannot import skill_search: {exc}"
    try:
        results = search(query, top_k=max(1, min(int(top_k or 5), 25)))
    except SkillSearchError as exc:
        return f"[sop_search error] {exc}"
    except Exception as exc:
        return f"[sop_search error] {type(exc).__name__}: {exc}"
    if not results:
        return f"No SOPs found for: {query}"
    lines = [f"Found {len(results)} SOPs for '{query}':"]
    for r in results:
        s = r.skill
        preview = (s.one_line_summary or s.description or "").strip().splitlines()
        snippet = preview[0][:140] if preview else ""
        score = r.quality if r.quality else r.final_score
        lines.append(f"- `{s.key}` · {s.name} · ⭐ {score:.1f}")
        if snippet:
            lines.append(f"  {snippet}")
    lines.append("")
    lines.append("Use sop_read(sop_id) to fetch full content before applying.")
    return "\n".join(lines)


def sop_read(sop_id: str) -> str:
    sop_id = (sop_id or "").strip()
    if not sop_id:
        return "[sop_read error] sop_id is required"
    try:
        SkillSearchError, _, read_sop, _ = _import_engine()
    except Exception as exc:
        return f"[sop_read error] cannot import skill_search: {exc}"
    try:
        sop = read_sop(sop_id)
    except SkillSearchError as exc:
        return f"[sop_read error] {exc}"
    except Exception as exc:
        return f"[sop_read error] {type(exc).__name__}: {exc}"
    body = sop.content or sop.preview or "(empty SOP)"

    # Parse YAML frontmatter (if present) and run prompt-injection guard.
    # Both are stdlib-only and best-effort: malformed metadata falls back
    # to the raw body unchanged; guard ``warn``-level findings are surfaced
    # but content still rendered. Only ``fail``-level findings (override /
    # role hijack / inline system prompt) suppress the body — those are
    # almost certainly attacks.
    from tools.skill_frontmatter import parse_frontmatter, format_metadata_summary
    from tools.skills_guard import scan, render_findings

    meta, body = parse_frontmatter(body)
    guard = scan(body)

    header = f"# {sop.title} (id={sop.id})"
    if sop.author:
        header += f"\n_author: {sop.author}_"
    meta_summary = format_metadata_summary(meta)
    if meta_summary:
        header += f"\n_{meta_summary}_"

    parts = [header]
    banner = render_findings(guard)
    if banner:
        parts.append(banner)
    if guard.safe:
        parts.append(body)
    else:
        parts.append("(SOP body suppressed by skills_guard — see warnings above.)")
    return "\n\n".join(parts)


def sop_stats() -> str:
    try:
        SkillSearchError, _, _, get_stats = _import_engine()
    except Exception as exc:
        return f"[sop_stats error] cannot import skill_search: {exc}"
    try:
        stats = get_stats()
    except SkillSearchError as exc:
        return f"[sop_stats error] {exc}"
    except Exception as exc:
        return f"[sop_stats error] {type(exc).__name__}: {exc}"
    return f"Sophub: {stats.get('total', '?')} SOPs at {stats.get('api_url', '?')}"
