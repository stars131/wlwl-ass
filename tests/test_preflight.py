"""Tests for launcher.preflight self-check.

Locks in the basic invariant: when nothing is broken, preflight returns
exit code 0 and all checks pass. When we introduce a known-bad module,
preflight catches it.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_preflight_imports_passes_clean():
    from launcher.preflight import run_preflight
    report = run_preflight(["imports"])
    failed = [r for r in report.results if not r.ok]
    assert not failed, f"unexpected import failures: {failed}"


def test_preflight_frontends_passes_clean():
    from launcher.preflight import run_preflight
    report = run_preflight(["frontends"])
    failed = [r for r in report.results if not r.ok]
    assert not failed, f"unexpected frontend syntax failures: {failed}"


def test_preflight_tokenjuice_rules_pass():
    from launcher.preflight import run_preflight
    report = run_preflight(["tokenjuice"])
    assert report.all_ok


def test_preflight_workers_pass():
    from launcher.preflight import run_preflight
    report = run_preflight(["workers"])
    assert report.all_ok


def test_preflight_detects_broken_module(tmp_path, monkeypatch):
    """When a target module has a SyntaxError, preflight surfaces it
    with line + message instead of silently continuing."""
    from launcher.preflight import _check_import

    bad_module_path = tmp_path / "broken_module.py"
    bad_module_path.write_text("def x(\n  # missing paren\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = _check_import("broken_module")
    assert result.ok is False
    assert "SyntaxError" in result.detail
    assert "broken_module" in result.detail or "line" in result.detail.lower()


def test_preflight_unknown_group_reports_error():
    from launcher.preflight import run_preflight
    report = run_preflight(["nonexistent_group"])
    assert not report.all_ok
    assert any("unknown group" in r.detail for r in report.results)


def test_preflight_json_output_shape():
    from launcher.preflight import run_preflight
    d = run_preflight(["tokenjuice"]).to_dict()
    assert "all_ok" in d
    assert isinstance(d["results"], list)
    assert all(set(r) >= {"name", "ok", "detail", "elapsed_ms"} for r in d["results"])
