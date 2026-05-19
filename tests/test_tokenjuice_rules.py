"""Tokenjuice rule sanity tests.

The 2026-05-17 turn-23 incident traced to a SyntaxError in BUILTIN_RULES
that masked a separate missing-replacements bug. These tests lock in:

1. Every rule loads cleanly (would have caught the SyntaxError).
2. patterns/replacements lengths match (would have caught the missing
   ``replacements=[]`` on the git_log_compact rule).
3. compact_output() doesn't crash for any rule's tool name.
4. The Rule.__post_init__ validator rejects bad rules at construction.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tokenjuice import BUILTIN_RULES, Rule, compact_output  # noqa: E402


def test_every_builtin_rule_lengths_match():
    """The bug that bricked the import: patterns/replacements mismatch."""
    for rule in BUILTIN_RULES:
        assert len(rule.patterns) == len(rule.replacements), (
            f"{rule.name}: {len(rule.patterns)} patterns vs "
            f"{len(rule.replacements)} replacements"
        )


def test_post_init_rejects_mismatched_rule():
    """The validator must catch the same bug class going forward."""
    with pytest.raises(ValueError, match="must be the same length"):
        Rule(
            name="bad",
            tool="x",
            patterns=["a", "b"],
            replacements=["only_one"],
        )


def test_post_init_rejects_bad_regex():
    """A typo in a regex (e.g. unbalanced paren) should fail loudly at
    Rule(...) construction, not deep inside compact_output()."""
    with pytest.raises(ValueError, match="not a valid regex"):
        Rule(
            name="bad",
            tool="x",
            patterns=["(unbalanced"],
            replacements=[""],
        )


def test_compact_output_runs_each_rule_without_crashing():
    """Smoke test: run compact_output for each built-in tool name and
    confirm the result has the right structure."""
    samples = {
        "git status": "On branch main\nnothing added to commit, use 'git add'",
        "git log": "abc1234 commit msg\ndef5678 another\n" * 50,
        "pip install": "Collecting numpy\nDownloading numpy.whl\nSuccessfully installed numpy",
        "npm install": "added 12 packages, audited 100\nnpm WARN deprecated foo",
        "cargo build": "Compiling foo v0.1.0\nFinished release",
        "docker pull": "abc123def456: Pull complete\nabc123def456: Already exists",
        "pytest": "===\nPASSED test_a\n...\n===",
    }
    for tool, raw in samples.items():
        result = compact_output(tool, raw)
        assert isinstance(result.compressed, str)
        assert result.original_len == len(raw)
        assert result.compressed_len == len(result.compressed)


def test_compact_output_handles_empty_inputs():
    result = compact_output("git", "", "")
    assert result.compressed == ""
    assert result.original_len == 0
    assert result.saved_pct == 0


def test_compact_output_handles_unknown_tool():
    """Unknown tool name still gets the global truncation guard."""
    result = compact_output("rustc", "line\n" * 500)
    assert "lines omitted" in result.compressed
    assert "global_truncation" in result.rules_applied
