"""Tests for the lenient JSON extractor in fsapp_concierge.

Real LLMs do not return clean JSON. They wrap it in code fences, prepend
explainer prose, use single quotes, include trailing commas, leak
``<think>`` tags from reasoning models. _parse_llm_json must extract
the dict from any of these — or return None so the caller falls back
to rules.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture
def parse():
    """Import only the parser — avoids dragging in the WS deps."""
    from frontends.fsapp_concierge import _parse_llm_json
    return _parse_llm_json


def test_clean_json_passes_through(parse):
    assert parse('{"intent": "schedule", "payload": {}}') == {
        "intent": "schedule", "payload": {},
    }


def test_json_fence_stripped(parse):
    raw = '```json\n{"intent": "qa"}\n```'
    assert parse(raw) == {"intent": "qa"}


def test_bare_fence_stripped(parse):
    raw = "```\n{\"intent\": \"qa\"}\n```"
    assert parse(raw) == {"intent": "qa"}


def test_preamble_stripped(parse):
    raw = 'Sure, here you go:\n{"intent": "schedule", "payload": {"topic_summary": "晚饭"}}'
    assert parse(raw) == {"intent": "schedule", "payload": {"topic_summary": "晚饭"}}


def test_postamble_stripped(parse):
    raw = '{"intent": "qa"} — let me know if you need more.'
    assert parse(raw) == {"intent": "qa"}


def test_thinking_tag_stripped(parse):
    raw = '<thinking>This looks like a schedule intent.</thinking>{"intent": "schedule"}'
    assert parse(raw) == {"intent": "schedule"}


def test_think_tag_stripped(parse):
    raw = '<think>foo</think>\n{"intent": "qa"}'
    assert parse(raw) == {"intent": "qa"}


def test_single_quotes_tolerated(parse):
    raw = "{'intent': 'qa', 'payload': {}}"
    assert parse(raw) == {"intent": "qa", "payload": {}}


def test_trailing_comma_tolerated(parse):
    raw = '{"intent": "qa", "payload": {},}'
    assert parse(raw) == {"intent": "qa", "payload": {}}


def test_nested_dict(parse):
    raw = 'preamble\n{"intent": "schedule", "payload": {"hints": {"window": "evening"}}}\npostamble'
    out = parse(raw)
    assert out["payload"]["hints"]["window"] == "evening"


def test_returns_none_for_pure_garbage(parse):
    assert parse("this is not json at all") is None


def test_returns_none_for_empty(parse):
    assert parse("") is None
    assert parse("   \n  ") is None


def test_returns_none_for_array_only(parse):
    """We require a dict at top level; bare arrays are not valid intent payloads."""
    assert parse("[1,2,3]") is None


def test_returns_none_for_non_string(parse):
    assert parse(None) is None
    assert parse(123) is None
    assert parse({"already": "dict"}) is None


def test_handles_chinese_in_values(parse):
    raw = '{"topic_summary": "晚上一起吃饭"}'
    assert parse(raw) == {"topic_summary": "晚上一起吃饭"}


def test_finds_first_balanced_block(parse):
    """When the LLM emits multiple JSON-ish chunks (e.g., examples then
    the real answer), we extract the first complete dict and use that.
    This matches LLM training-data prevalence: the first dict is usually
    the canonical reply."""
    raw = 'Here are some examples: {"intent": "smalltalk"}\nFinal answer: {"intent": "qa"}'
    out = parse(raw)
    # Either is reasonable; ensure we got *something* parseable from the first dict
    assert out == {"intent": "smalltalk"}


def test_quoted_curly_braces_in_string_dont_confuse_parser(parse):
    raw = '{"payload": "she said {hello}"}'
    out = parse(raw)
    assert out == {"payload": "she said {hello}"}
