"""Real-time web search tool.

Two parallel sources:
  * Grok native ``live_search`` via xAI Chat Completions API (web/x/news)
  * Tavily REST search

Both fire concurrently; one path's failure never blocks the other. Returns a
single LLM-friendly markdown string. No third-party SDKs — direct REST via
``requests`` so the project's optional-deps surface stays untouched.

Keys (any one source resolves):
  * Grok:   ``XAI_API_KEY`` env var, OR ``providers.grok.api_key`` in config_store
  * Tavily: ``TAVILY_API_KEY`` env var, OR ``settings.tavily.api_key`` in config_store

Configuration (one-time):
    python -m launcher.config set providers.grok \\
      '{"api_key":"xai-...","base_url":"https://api.x.ai/v1",\\
        "model":"grok-4-fast-reasoning","kind":"oai"}'
    python -m launcher.config set settings.tavily \
      '{"api_key":"tvly-...","url":"https://api.tavily.com/search"}'
"""
from __future__ import annotations

import os
import threading
from typing import Any

import requests

GROK_BASE = "https://api.x.ai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
GROK_DEFAULT_MODEL = "grok-4-fast-reasoning"
GROK_DEFAULT_SOURCES = ["web", "x", "news"]


# ── credential resolution ────────────────────────────────────────────


def _get_grok_key() -> str:
    v = (os.environ.get("XAI_API_KEY") or "").strip()
    if v:
        return v
    try:
        from launcher.config_store import default_store
        cfg = default_store().get("providers.grok.api_key")
    except Exception:
        cfg = None
    return str(cfg or "").strip()


def _get_grok_model() -> str:
    try:
        from launcher.config_store import default_store
        m = default_store().get("providers.grok.model")
    except Exception:
        m = None
    return str(m or "").strip() or GROK_DEFAULT_MODEL


def _get_grok_url() -> str:
    v = (
        os.environ.get("XAI_API_URL")
        or os.environ.get("XAI_BASE_URL")
        or os.environ.get("XAI_API_BASE_URL")
        or os.environ.get("GROK_API_URL")
        or os.environ.get("GROK_BASE_URL")
        or os.environ.get("GROK_API_BASE_URL")
        or ""
    ).strip()
    if not v:
        try:
            from launcher.config_store import default_store
            cfg = (
                default_store().get("providers.grok.url")
                or default_store().get("providers.grok.base_url")
                or default_store().get("providers.grok.apibase")
            )
        except Exception:
            cfg = None
        v = str(cfg or "").strip()
    if not v:
        return GROK_BASE
    v = v.rstrip("/")
    return v if v.endswith("/chat/completions") else f"{v}/chat/completions"


def _get_tavily_key() -> str:
    v = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if v:
        return v
    try:
        from launcher.config_store import default_store
        cfg = default_store().get("settings.tavily.api_key")
    except Exception:
        cfg = None
    return str(cfg or "").strip()


def _get_tavily_url() -> str:
    v = (os.environ.get("TAVILY_URL") or os.environ.get("TAVILY_BASE_URL") or "").strip()
    if not v:
        try:
            from launcher.config_store import default_store
            cfg = (
                default_store().get("settings.tavily.url")
                or default_store().get("settings.tavily.base_url")
            )
        except Exception:
            cfg = None
        v = str(cfg or "").strip()
    if not v:
        return TAVILY_URL
    v = v.rstrip("/")
    return v if v.endswith("/search") else f"{v}/search"


# ── single-source workers ────────────────────────────────────────────


def _grok_live(query: str, sources: list[str], model: str, timeout: float) -> dict[str, Any]:
    key = _get_grok_key()
    if not key:
        return {"error": "grok api key missing (set XAI_API_KEY or providers.grok.api_key)"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "search_parameters": {
            "mode": "on",
            "sources": [{"type": s} for s in (sources or GROK_DEFAULT_SOURCES)],
            "max_search_results": 8,
        },
    }
    try:
        r = requests.post(
            _get_grok_url(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"error": f"grok request failed: {type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {"error": f"grok HTTP {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except ValueError:
        return {"error": "grok returned non-JSON"}
    try:
        answer = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return {"error": f"grok response shape unexpected: {str(data)[:200]}"}
    citations = data.get("citations") or []
    return {"answer": str(answer), "citations": [str(c) for c in citations if c]}


def _tavily(query: str, max_results: int, depth: str, timeout: float) -> dict[str, Any]:
    key = _get_tavily_key()
    if not key:
        return {"error": "tavily api key missing (set TAVILY_API_KEY or settings.tavily.api_key)"}
    payload = {
        "api_key": key,
        "query": query,
        "max_results": max(1, min(int(max_results or 5), 20)),
        "search_depth": "advanced" if depth == "advanced" else "basic",
    }
    try:
        r = requests.post(_get_tavily_url(), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        return {"error": f"tavily request failed: {type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {"error": f"tavily HTTP {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except ValueError:
        return {"error": "tavily returned non-JSON"}
    return {
        "answer": str(data.get("answer") or ""),
        "results": [
            {
                "title": str(r.get("title", "") or ""),
                "url": str(r.get("url", "") or ""),
                "content": str(r.get("content", "") or ""),
                "score": r.get("score"),
            }
            for r in (data.get("results") or [])
            if isinstance(r, dict)
        ],
    }


# ── formatting ───────────────────────────────────────────────────────


def _format(query: str, grok: dict[str, Any], tavily: dict[str, Any]) -> str:
    parts = [f"# Web search: {query}"]

    if grok.get("error"):
        parts.append(f"## Grok native search\n[error] {grok['error']}")
    else:
        ans = (grok.get("answer") or "").strip()
        parts.append(f"## Grok answer\n{ans or '(empty)'}")
        cites = grok.get("citations") or []
        if cites:
            parts.append("## Grok sources\n" + "\n".join(f"- {c}" for c in cites[:10]))

    if tavily.get("error"):
        parts.append(f"## Tavily\n[error] {tavily['error']}")
    else:
        if (a := (tavily.get("answer") or "").strip()):
            parts.append(f"## Tavily summary\n{a}")
        results = tavily.get("results") or []
        if results:
            lines = ["## Tavily results"]
            for i, r in enumerate(results, 1):
                title = r.get("title") or "?"
                url = r.get("url") or ""
                content = (r.get("content") or "")[:300].replace("\n", " ")
                lines.append(f"{i}. [{title}]({url})\n   {content}")
            parts.append("\n".join(lines))

    if grok.get("error") and tavily.get("error"):
        parts.append("## Note\n双路均失败。检查 API key + 网络。")

    return "\n\n".join(parts)


# ── public API ───────────────────────────────────────────────────────


def web_search(
    query: str,
    *,
    sources: list[str] | None = None,
    max_results: int = 5,
    depth: str = "basic",
    grok_model: str | None = None,
    timeout: float = 45.0,
) -> str:
    """Run Grok live_search + Tavily concurrently, return merged markdown.

    ``sources`` only affects Grok native search ("web" / "x" / "news").
    ``max_results`` and ``depth`` only affect Tavily (depth: basic / advanced).
    """
    query = (query or "").strip()
    if not query:
        return "[web_search error] query is required"
    sources = sources or GROK_DEFAULT_SOURCES
    model = grok_model or _get_grok_model()

    grok_out: dict[str, Any] = {}
    tavily_out: dict[str, Any] = {}

    def _g():
        try:
            grok_out.update(_grok_live(query, sources, model, timeout))
        except Exception as exc:
            grok_out["error"] = f"grok crashed: {type(exc).__name__}: {exc}"

    def _t():
        try:
            tavily_out.update(_tavily(query, max_results, depth, timeout))
        except Exception as exc:
            tavily_out["error"] = f"tavily crashed: {type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=_g, daemon=True), threading.Thread(target=_t, daemon=True)]
    for t in threads:
        t.start()
    deadline = timeout + 5
    for t in threads:
        t.join(timeout=deadline)
    for t, label in zip(threads, ("grok", "tavily")):
        if t.is_alive():
            (grok_out if label == "grok" else tavily_out).setdefault(
                "error", f"{label} timed out after {deadline:.0f}s"
            )

    return _format(query, grok_out, tavily_out)
