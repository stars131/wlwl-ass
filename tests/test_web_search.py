"""Tests for tools/web_search.py — Grok native + Tavily concurrent search.

Run with: pytest tests/test_web_search.py -v

All HTTP calls are mocked. No real network IO.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tools import web_search as ws


# ── helpers ─────────────────────────────────────────────────────────


def _mk_response(status=200, json_data=None, text=""):
    r = mock.MagicMock()
    r.status_code = status
    r.text = text
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no json")
    return r


def _grok_ok(answer="Grok says hi.", citations=None):
    return _mk_response(json_data={
        "choices": [{"message": {"content": answer}}],
        "citations": citations or ["https://x.ai/about", "https://example.com/post"],
    })


def _tavily_ok(answer="tavily summary", results=None):
    if results is None:
        results = [
            {"title": "Result 1", "url": "https://a.com/1", "content": "snippet 1", "score": 0.9},
            {"title": "Result 2", "url": "https://b.com/2", "content": "snippet 2", "score": 0.8},
        ]
    return _mk_response(json_data={"answer": answer, "results": results})


def _route(url, *args, **kwargs):
    """Single-route side_effect: dispatch by url."""
    if "x.ai" in url:
        return _grok_ok()
    if "tavily" in url:
        return _tavily_ok()
    raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _set_keys(monkeypatch):
    for name in (
        "XAI_API_URL",
        "XAI_BASE_URL",
        "XAI_API_BASE_URL",
        "GROK_API_URL",
        "GROK_BASE_URL",
        "GROK_API_BASE_URL",
        "TAVILY_URL",
        "TAVILY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    monkeypatch.setenv("XAI_BASE_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("TAVILY_BASE_URL", "https://api.tavily.com")


# ── happy path ──────────────────────────────────────────────────────


def test_happy_path_returns_both_sections():
    with mock.patch.object(ws.requests, "post", side_effect=_route):
        out = ws.web_search("what is rust?")
    assert "# Web search: what is rust?" in out
    assert "## Grok answer" in out
    assert "Grok says hi." in out
    assert "## Grok sources" in out
    assert "https://x.ai/about" in out
    assert "## Tavily summary" in out
    assert "tavily summary" in out
    assert "## Tavily results" in out
    assert "Result 1" in out
    assert "https://a.com/1" in out


def test_grok_only_when_tavily_500():
    def _route_t500(url, *a, **kw):
        if "x.ai" in url: return _grok_ok()
        return _mk_response(status=500, text="upstream busy")
    with mock.patch.object(ws.requests, "post", side_effect=_route_t500):
        out = ws.web_search("x")
    assert "Grok says hi." in out
    assert "## Tavily" in out
    assert "[error] tavily HTTP 500" in out


def test_tavily_only_when_grok_request_exception():
    import requests as _req
    def _route_grok_exc(url, *a, **kw):
        if "x.ai" in url:
            raise _req.exceptions.ConnectTimeout("timed out")
        return _tavily_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_route_grok_exc):
        out = ws.web_search("x")
    assert "[error] grok request failed" in out
    assert "ConnectTimeout" in out
    assert "Result 1" in out  # tavily side intact


def test_both_fail_returns_partial_with_note():
    def _route_both(url, *a, **kw):
        return _mk_response(status=500, text="boom")
    with mock.patch.object(ws.requests, "post", side_effect=_route_both):
        out = ws.web_search("x")
    assert "[error] grok HTTP 500" in out
    assert "[error] tavily HTTP 500" in out
    assert "双路均失败" in out


def test_grok_response_shape_unexpected():
    def _route_bad(url, *a, **kw):
        if "x.ai" in url:
            return _mk_response(json_data={"unexpected": "shape"})
        return _tavily_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_route_bad):
        out = ws.web_search("x")
    assert "[error] grok response shape unexpected" in out
    assert "Result 1" in out


def test_tavily_non_json():
    def _route_t_bad(url, *a, **kw):
        if "x.ai" in url: return _grok_ok()
        return _mk_response(status=200, text="plain text")  # .json raises ValueError
    with mock.patch.object(ws.requests, "post", side_effect=_route_t_bad):
        out = ws.web_search("x")
    assert "Grok says hi." in out
    assert "[error] tavily returned non-JSON" in out


# ── credential resolution ───────────────────────────────────────────


def test_missing_grok_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with mock.patch.object(ws, "_get_grok_key", return_value=""):
        with mock.patch.object(ws.requests, "post", side_effect=_route):
            out = ws.web_search("x")
    assert "[error] grok api key missing" in out
    assert "Result 1" in out  # tavily still works


def test_missing_tavily_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with mock.patch.object(ws, "_get_tavily_key", return_value=""):
        with mock.patch.object(ws.requests, "post", side_effect=_route):
            out = ws.web_search("x")
    assert "[error] tavily api key missing" in out
    assert "Grok says hi." in out


def test_both_missing_keys(monkeypatch):
    with mock.patch.object(ws, "_get_grok_key", return_value=""), \
         mock.patch.object(ws, "_get_tavily_key", return_value=""):
        out = ws.web_search("x")
    assert "[error] grok api key missing" in out
    assert "[error] tavily api key missing" in out


# ── payload shape ───────────────────────────────────────────────────


def test_grok_payload_has_search_parameters():
    captured = {}
    def _capture(url, *a, **kw):
        if "x.ai" in url:
            captured["grok"] = kw.get("json")
            return _grok_ok()
        return _tavily_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_capture):
        ws.web_search("query", sources=["web", "news"])
    sp = captured["grok"]["search_parameters"]
    assert sp["mode"] == "on"
    assert sp["sources"] == [{"type": "web"}, {"type": "news"}]
    assert sp["max_search_results"] == 8


def test_grok_uses_configured_url(monkeypatch):
    captured = {}
    monkeypatch.setenv("XAI_API_URL", "https://proxy.example/grok/v1")

    def _capture(url, *a, **kw):
        if "proxy.example/grok" in url:
            captured["grok_url"] = url
            return _grok_ok()
        if "tavily" in url:
            return _tavily_ok()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch.object(ws.requests, "post", side_effect=_capture):
        ws.web_search("query")

    assert captured["grok_url"] == "https://proxy.example/grok/v1/chat/completions"


def test_tavily_payload_advanced_depth():
    captured = {}
    def _capture(url, *a, **kw):
        if "tavily" in url:
            captured["tavily"] = kw.get("json")
            return _tavily_ok()
        return _grok_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_capture):
        ws.web_search("query", depth="advanced", max_results=10)
    p = captured["tavily"]
    assert p["search_depth"] == "advanced"
    assert p["max_results"] == 10
    assert p["query"] == "query"


def test_tavily_uses_configured_url(monkeypatch):
    captured = {}
    monkeypatch.setenv("TAVILY_BASE_URL", "https://proxy.example/api/tavily")

    def _capture(url, *a, **kw):
        if "proxy.example/api/tavily" in url:
            captured["tavily_url"] = url
            return _tavily_ok()
        if "x.ai" in url:
            return _grok_ok()
        raise AssertionError(f"unexpected URL: {url}")

    with mock.patch.object(ws.requests, "post", side_effect=_capture):
        ws.web_search("query")

    assert captured["tavily_url"] == "https://proxy.example/api/tavily/search"


def test_tavily_max_results_clamped():
    captured = {}
    def _capture(url, *a, **kw):
        if "tavily" in url:
            captured["t"] = kw.get("json")
            return _tavily_ok()
        return _grok_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_capture):
        ws.web_search("q", max_results=999)
    assert captured["t"]["max_results"] == 20  # capped at 20


def test_empty_query():
    out = ws.web_search("")
    assert "[web_search error] query is required" in out


# ── concurrency ─────────────────────────────────────────────────────


def test_grok_and_tavily_run_in_parallel():
    """If serial: 0.5 + 0.5 = 1.0s. Parallel: ≈ 0.5s. Allow margin."""
    def _slow(url, *a, **kw):
        time.sleep(0.5)
        return _grok_ok() if "x.ai" in url else _tavily_ok()
    with mock.patch.object(ws.requests, "post", side_effect=_slow):
        t0 = time.monotonic()
        out = ws.web_search("q")
        elapsed = time.monotonic() - t0
    assert "Grok says hi." in out
    assert "Result 1" in out
    assert elapsed < 0.9, f"web_search took {elapsed:.2f}s — looks serial, not parallel"
