"""Tests for tools/tavily_proxy.py — credential resolution, header vs
body auth, all five endpoints hit the right URL.

Crucially: we never touch the network. `requests.post` is monkeypatched
to a stub that records arguments and returns canned JSON.

The user's real token (saved to ``~/.wlwl-ass/config.json`` by the SOP's
setup step) is **never** referenced here; tests use synthetic tokens.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@dataclass
class _StubResponse:
    """Drop-in for requests.Response."""
    status_code: int = 200
    payload: object = None
    text: str = ""

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@dataclass
class _Capture:
    """Records what the module sent to requests.post."""
    url: str = ""
    json_body: dict | None = None
    headers: dict | None = None
    timeout: float | None = None


def _install_stub(monkeypatch, capture: _Capture,
                  response: _StubResponse) -> None:
    """Replace requests.post on the tavily_proxy module."""
    import requests as _req  # ensure module imported before patch
    from tools import tavily_proxy

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        capture.url = url
        capture.json_body = dict(json or {})
        capture.headers = dict(headers or {})
        capture.timeout = timeout
        return response

    monkeypatch.setattr(tavily_proxy.requests, "post", fake_post)


# ── credential resolution ────────────────────────────────────────────────


def test_token_from_env(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "env-token-123")
    # Disable config store fallback to avoid relying on user state.
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    assert tavily_proxy._get_token() == "env-token-123"


def test_token_missing_returns_error(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.delenv("TAVILY_PROXY_TOKEN", raising=False)
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    out = tavily_proxy.search("anything")
    assert "error" in out
    assert "token missing" in out["error"]


def test_base_url_env_overrides(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_BASE_URL", "https://other.example/api")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    assert tavily_proxy._get_base_url() == "https://other.example/api"


def test_base_url_default_when_unset(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.delenv("TAVILY_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    assert tavily_proxy._get_base_url() == tavily_proxy.DEFAULT_BASE_URL


class _BadStore:
    """Pretend the config store has nothing for this setting."""
    def get(self, path, default=None):
        return default


# ── search ────────────────────────────────────────────────────────────────


def test_search_sends_bearer_header(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t-bearer")
    monkeypatch.delenv("WLWL_TAVILY_PROXY_BODY_AUTH", raising=False)
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={"results": []}))
    out = tavily_proxy.search("ping")
    assert "error" not in out
    assert cap.url == "https://tavily.ivanli.cc/api/tavily/search"
    assert cap.headers["Authorization"] == "Bearer t-bearer"
    # Token must NOT have leaked into the body when header auth is used.
    assert "api_key" not in cap.json_body
    assert cap.json_body["query"] == "ping"


def test_search_body_auth_mode(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t-body")
    monkeypatch.setenv("WLWL_TAVILY_PROXY_BODY_AUTH", "1")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={"results": []}))
    tavily_proxy.search("q")
    # In body-auth mode: NO Authorization header, token in body.
    assert "Authorization" not in cap.headers
    assert cap.json_body["api_key"] == "t-body"


def test_search_clamps_max_results(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={}))
    tavily_proxy.search("q", max_results=999)
    assert cap.json_body["max_results"] == 20  # clamp ceiling
    tavily_proxy.search("q", max_results=0)
    assert cap.json_body["max_results"] == 1   # clamp floor


def test_search_empty_query_short_circuits(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    out = tavily_proxy.search("   ")
    assert out == {"error": "query is required"}


# ── extract / crawl / map / research ─────────────────────────────────────


def test_extract_accepts_string_and_list(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={"results": []}))
    tavily_proxy.extract("https://a.example/x")
    assert cap.json_body["urls"] == ["https://a.example/x"]
    tavily_proxy.extract(["https://a/", " https://b/  "])
    assert cap.json_body["urls"] == ["https://a/", "https://b/"]


def test_extract_empty_urls_short_circuits(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    assert tavily_proxy.extract([]) == {"error": "at least one URL is required"}
    assert tavily_proxy.extract([""]) == {"error": "at least one URL is required"}


def test_crawl_hits_crawl_endpoint(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={}))
    tavily_proxy.crawl("https://example.com", max_depth=3, max_pages=5)
    assert cap.url.endswith("/crawl")
    assert cap.json_body == {
        "url": "https://example.com",
        "max_depth": 3,
        "max_pages": 5,
        "include_images": False,
    }


def test_site_map_hits_map_endpoint(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={}))
    tavily_proxy.site_map("https://example.com", max_pages=20)
    assert cap.url.endswith("/map")
    assert cap.json_body == {"url": "https://example.com", "max_pages": 20}


def test_research_hits_research_endpoint(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap, _StubResponse(payload={"answer": "x"}))
    out = tavily_proxy.research("What is X?")
    assert cap.url.endswith("/research")
    assert cap.json_body["query"] == "What is X?"
    assert out == {"answer": "x"}


# ── error handling ───────────────────────────────────────────────────────


def test_http_non_200_returns_error(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap,
                  _StubResponse(status_code=429, text="rate limited"))
    out = tavily_proxy.search("q")
    assert "error" in out
    assert out["status_code"] == 429
    assert "429" in out["error"]


def test_request_exception_returns_error(monkeypatch):
    from tools import tavily_proxy
    import requests as _req
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())

    def boom(*a, **kw):
        raise _req.ConnectionError("offline")

    monkeypatch.setattr(tavily_proxy.requests, "post", boom)
    out = tavily_proxy.search("q")
    assert "error" in out
    assert "ConnectionError" in out["error"]


def test_non_json_response_returns_error(monkeypatch):
    from tools import tavily_proxy
    monkeypatch.setenv("TAVILY_PROXY_TOKEN", "t")
    monkeypatch.setattr("launcher.config_store.default_store",
                        lambda: _BadStore())
    cap = _Capture()
    _install_stub(monkeypatch, cap,
                  _StubResponse(payload=ValueError("not json"),
                                text="<html>error</html>"))
    out = tavily_proxy.search("q")
    assert "error" in out
    assert "non-JSON" in out["error"]


# ── token never leaks into committed source ──────────────────────────────


def test_no_real_token_in_source_tree():
    """Belt-and-suspenders: the user's actual proxy token must NEVER
    appear in any tracked source file. Keep this guard so a future
    edit doesn't accidentally hardcode it.

    The needle is assembled at runtime so this test file itself doesn't
    contain the literal token string."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Split + join so this source file isn't itself a leak.
    needle = "th-" + "UQqV-" + "SQTDVp" + "92f0URybg2" + "Yquawhdb"
    leaked = []
    for dirpath, dirs, files in os.walk(root):
        # Prune dirs that aren't in version control / are huge.
        dirs[:] = [d for d in dirs if d not in {
            ".venv", "node_modules", "__pycache__", ".git", "temp",
            ".wlwl-ass", "voice-website",
        }]
        for fn in files:
            if not fn.endswith((".py", ".md", ".json", ".toml", ".txt",
                                ".cmd", ".ps1", ".sh", ".yml", ".yaml")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    if needle in f.read():
                        leaked.append(full)
            except OSError:
                pass
    assert not leaked, f"real token found in source tree: {leaked}"
