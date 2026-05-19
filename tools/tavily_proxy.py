"""``tavily_proxy`` — Tavily-style HTTP API via the ivanli.cc relay.

This wraps the proxy at ``https://tavily.ivanli.cc/api/tavily`` which
exposes five endpoints (``/search``, ``/extract``, ``/crawl``, ``/map``,
``/research``). The relay's wire protocol is Tavily-compatible, but it
adds ``/crawl``, ``/map`` and ``/research`` on top of the stock Tavily
REST surface — meaning this module is *not* a drop-in replacement for
:mod:`tools.web_search`'s ``api.tavily.com`` path; it's a strict superset
that points at a different host.

Authentication
~~~~~~~~~~~~~~

Credential resolution order (first non-empty wins):

  1. ``TAVILY_PROXY_TOKEN`` env var
  2. ``settings.tavily_proxy.token`` in :mod:`launcher.config_store`
     (lives in ``~/.wlwl-ass/config.json`` — gitignored)

The token is sent as ``Authorization: Bearer <token>`` by default. Some
clients can't customise headers (e.g. embedded webhook callers); for
those, set ``WLWL_TAVILY_PROXY_BODY_AUTH=1`` and the token is added to
the JSON body as ``api_key`` instead. Both modes use the same token
value — the proxy accepts either form.

Base URL override
~~~~~~~~~~~~~~~~~

The base URL is fixed to the ivanli.cc relay unless overridden via
``TAVILY_PROXY_BASE_URL`` or ``settings.tavily_proxy.base_url``. Useful
for self-hosted proxies or staging environments.

Public surface
~~~~~~~~~~~~~~

Each endpoint returns a plain ``dict`` (success) or ``{"error": str}``
(failure). Errors never raise — agent loop integration treats every
tool return value as data. The agent calls these via the ``mcp_call``
or ``code_run`` paths, so we don't register them in
``assets/tools_schema.json`` directly.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import requests

DEFAULT_BASE_URL = "https://tavily.ivanli.cc/api/tavily"
DEFAULT_TIMEOUT_S = 45.0


# ── credential & base-url resolution ─────────────────────────────────────


def _get_token() -> str:
    v = (os.environ.get("TAVILY_PROXY_TOKEN") or "").strip()
    if v:
        return v
    try:
        from launcher.config_store import default_store
        cfg = default_store().get("settings.tavily_proxy.token")
    except Exception:
        cfg = None
    return str(cfg or "").strip()


def _get_base_url() -> str:
    v = (os.environ.get("TAVILY_PROXY_BASE_URL") or "").strip()
    if v:
        return v.rstrip("/")
    try:
        from launcher.config_store import default_store
        cfg = default_store().get("settings.tavily_proxy.base_url")
    except Exception:
        cfg = None
    return str(cfg or DEFAULT_BASE_URL).rstrip("/")


def _use_body_auth() -> bool:
    """Switch from Bearer header to ``api_key`` body field.

    Honours ``WLWL_TAVILY_PROXY_BODY_AUTH`` (any of ``1``/``true``/``yes``)
    and ``settings.tavily_proxy.body_auth`` in the config store."""
    env = (os.environ.get("WLWL_TAVILY_PROXY_BODY_AUTH") or "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    try:
        from launcher.config_store import default_store
        cfg = default_store().get("settings.tavily_proxy.body_auth")
    except Exception:
        cfg = None
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, str):
        return cfg.strip().lower() in {"1", "true", "yes", "on"}
    return False


# ── HTTP plumbing ─────────────────────────────────────────────────────────


def _post(endpoint: str, payload: dict[str, Any],
          *, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """POST to ``<base>/{endpoint}`` and return parsed JSON (or an error
    envelope). Used by every public function in this module so error
    handling stays uniform."""
    token = _get_token()
    if not token:
        return {
            "error": (
                "tavily_proxy token missing — set TAVILY_PROXY_TOKEN env var "
                "or run `python -m launcher.config set settings.tavily_proxy "
                "'{\"token\":\"<your-token>\"}'`"
            )
        }
    url = f"{_get_base_url()}/{endpoint.lstrip('/')}"
    body = dict(payload)
    headers = {"Content-Type": "application/json"}
    if _use_body_auth():
        body["api_key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return {"error": f"{endpoint} request failed: {type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {
            "error": f"{endpoint} HTTP {r.status_code}: {r.text[:400]}",
            "status_code": r.status_code,
        }
    try:
        return r.json()
    except ValueError:
        return {"error": f"{endpoint} returned non-JSON: {r.text[:200]}"}


# ── public endpoints ──────────────────────────────────────────────────────


def search(
    query: str,
    *,
    topic: str = "general",
    search_depth: str = "basic",
    max_results: int = 5,
    include_answer: bool = True,
    include_raw_content: bool = False,
    include_domains: Iterable[str] | None = None,
    exclude_domains: Iterable[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST /api/tavily/search — Tavily-compatible search.

    See `<https://docs.tavily.com/docs/rest-api/api-reference>`_ for the
    full parameter surface. The relay forwards most options verbatim."""
    payload: dict[str, Any] = {
        "query": (query or "").strip(),
        "topic": topic,
        "search_depth": "advanced" if search_depth == "advanced" else "basic",
        "max_results": max(1, min(int(max_results), 20)),
        "include_answer": bool(include_answer),
        "include_raw_content": bool(include_raw_content),
    }
    if include_domains:
        payload["include_domains"] = list(include_domains)
    if exclude_domains:
        payload["exclude_domains"] = list(exclude_domains)
    if not payload["query"]:
        return {"error": "query is required"}
    return _post("search", payload, timeout=timeout)


def extract(
    urls: str | Iterable[str],
    *,
    include_images: bool = False,
    extract_depth: str = "basic",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST /api/tavily/extract — pull clean article content from URLs."""
    if isinstance(urls, str):
        url_list = [urls]
    else:
        url_list = [str(u).strip() for u in urls if str(u).strip()]
    if not url_list:
        return {"error": "at least one URL is required"}
    payload = {
        "urls": url_list,
        "include_images": bool(include_images),
        "extract_depth": "advanced" if extract_depth == "advanced" else "basic",
    }
    return _post("extract", payload, timeout=timeout)


def crawl(
    url: str,
    *,
    max_depth: int = 1,
    max_pages: int = 10,
    include_images: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST /api/tavily/crawl — multi-page crawl from a seed URL.

    Proxy-specific endpoint (not in stock Tavily REST). The relay caps
    ``max_pages`` server-side; values much above ~50 are rejected."""
    if not (url or "").strip():
        return {"error": "url is required"}
    payload = {
        "url": url.strip(),
        "max_depth": max(1, int(max_depth)),
        "max_pages": max(1, int(max_pages)),
        "include_images": bool(include_images),
    }
    return _post("crawl", payload, timeout=timeout)


def site_map(
    url: str,
    *,
    max_pages: int = 50,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST /api/tavily/map — enumerate the link graph of a site.

    Named ``site_map`` to avoid shadowing the builtin ``map``."""
    if not (url or "").strip():
        return {"error": "url is required"}
    payload = {"url": url.strip(), "max_pages": max(1, int(max_pages))}
    return _post("map", payload, timeout=timeout)


def research(
    query: str,
    *,
    breadth: int = 3,
    depth: int = 2,
    timeout: float = 90.0,  # research is slower; default override
) -> dict[str, Any]:
    """POST /api/tavily/research — multi-step research pipeline.

    The relay runs an agentic loop server-side; expect 30–90 s latency.
    Returns ``{"answer": ..., "steps": [...], "sources": [...]}`` (shape
    set by the relay, not stock Tavily)."""
    if not (query or "").strip():
        return {"error": "query is required"}
    payload = {
        "query": query.strip(),
        "breadth": max(1, int(breadth)),
        "depth": max(1, int(depth)),
    }
    return _post("research", payload, timeout=timeout)
