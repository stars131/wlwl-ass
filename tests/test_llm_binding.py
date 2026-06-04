"""Tests for ``launcher.llm_binding`` — per-bot LLM binding parsing + resolution.

The helpers here are pure functions, so we exercise them directly without
spinning up a real mykeys / agent stack. Integration with ``fsapp.py`` and
``fsapp_concierge.py`` is covered separately.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from launcher.llm_binding import (  # noqa: E402
    parse_binding,
    resolve_to_config_names,
    resolve_to_config_names_by_priority,
    synthesize_mixin_entry,
)


# ── parse_binding ─────────────────────────────────────────────────────────


def test_parse_binding_empty_inputs_return_empty():
    assert parse_binding(None) == ("", "")
    assert parse_binding("") == ("", "")
    assert parse_binding("   ") == ("", "")


def test_parse_binding_config():
    assert parse_binding("config:deepseek官方") == ("config", "deepseek官方")
    # Surrounding whitespace tolerated.
    assert parse_binding("  config:glm  ") == ("config", "glm")
    # Whitespace inside the name half is stripped from edges only.
    assert parse_binding("config:  name with spaces ") == ("config", "name with spaces")


def test_parse_binding_profile():
    assert parse_binding("profile:glm") == ("profile", "glm")
    assert parse_binding("PROFILE:glm") == ("profile", "glm")  # case-insensitive prefix


def test_parse_binding_invalid_prefix_returns_empty():
    assert parse_binding("xyz:abc") == ("", "")
    assert parse_binding("config") == ("", "")  # missing colon
    assert parse_binding("profile:") == ("", "")  # missing name
    assert parse_binding("config:") == ("", "")


# ── resolve_to_config_names ───────────────────────────────────────────────


def _profiles(profiles_map):
    return {"active": None, "profiles": profiles_map}


def _configs(*names_and_kinds):
    """Build a list of public-shaped config dicts. Each arg is (name, kind)."""
    return [{"name": n, "kind": k} for n, k in names_and_kinds]


def test_resolve_config_happy_path():
    configs = _configs(("deepseek官方", "native_oai"), ("glm", "native_oai"))
    out = resolve_to_config_names("config", "glm", _profiles({}), configs)
    assert out == ["glm"]


def test_resolve_config_missing_returns_empty():
    configs = _configs(("deepseek官方", "native_oai"))
    out = resolve_to_config_names("config", "ghost", _profiles({}), configs)
    assert out == []


def test_resolve_profile_happy_path_preserves_order():
    configs = _configs(
        ("deepseek官方", "native_oai"),
        ("glm", "native_oai"),
        ("grok", "native_oai"),
        ("grokciallo", "native_oai"),
    )
    profiles = _profiles({"glm": ["glm", "grok", "grokciallo"]})
    out = resolve_to_config_names("profile", "glm", profiles, configs)
    assert out == ["glm", "grok", "grokciallo"]


def test_resolve_profile_by_priority_uses_category_then_priority():
    configs = [
        {"name": "voice-high", "kind": "native_oai", "category": "voice", "priority": 100},
        {"name": "language-low", "kind": "native_oai", "category": "language", "priority": 1},
        {"name": "language-high", "kind": "native_oai", "category": "language", "priority": 8},
        {"name": "multimodal", "kind": "native_oai", "category": "multimodal", "priority": 3},
    ]
    profiles = _profiles({"runtime": ["voice-high", "language-low", "multimodal", "language-high"]})
    out = resolve_to_config_names_by_priority("profile", "runtime", profiles, configs)
    assert out == ["language-high", "language-low", "multimodal", "voice-high"]


def test_resolve_profile_drops_missing_member():
    configs = _configs(("glm", "native_oai"), ("grok", "native_oai"))
    profiles = _profiles({"glm": ["glm", "ghost", "grok"]})
    out = resolve_to_config_names("profile", "glm", profiles, configs)
    assert out == ["glm", "grok"]


def test_resolve_profile_drops_mixin_members():
    """Mixin members would create nested MixinSession instances. Skip them."""
    configs = _configs(
        ("glm", "native_oai"),
        ("inner_mixin", "mixin"),
        ("grok", "native_oai"),
    )
    profiles = _profiles({"glm": ["glm", "inner_mixin", "grok"]})
    out = resolve_to_config_names("profile", "glm", profiles, configs)
    assert out == ["glm", "grok"]


def test_resolve_profile_unknown_name_returns_empty():
    out = resolve_to_config_names("profile", "ghost", _profiles({}), _configs(("glm", "native_oai")))
    assert out == []


def test_resolve_profile_empty_members_returns_empty():
    profiles = _profiles({"glm": []})
    out = resolve_to_config_names("profile", "glm", profiles, _configs(("glm", "native_oai")))
    assert out == []


def test_resolve_empty_kind_returns_empty():
    out = resolve_to_config_names("", "anything", _profiles({}), _configs(("glm", "native_oai")))
    assert out == []


# ── synthesize_mixin_entry ───────────────────────────────────────────────


def test_synthesize_mixin_entry_shape():
    key, payload = synthesize_mixin_entry("feishu", ["glm", "grok", "grokciallo"])
    assert key == "mixin_config_bot_feishu"
    assert payload["kind"] == "mixin"
    # llm_nos uses public names (not var_names) — MixinSession.__init__ looks
    # up sub-sessions by ``backend.name``, which matches the public name.
    assert payload["llm_nos"] == ["glm", "grok", "grokciallo"]
    # max_retries scales with chain length so a 3-member chain has at least
    # 4 attempts (full round + one extra). Spring-back stays at 5min.
    assert payload["max_retries"] >= 3
    assert payload["spring_back"] == 300


def test_synthesize_mixin_entry_normalizes_bot_key():
    key, _ = synthesize_mixin_entry("  Feishu_Concierge  ", ["glm"])
    assert key == "mixin_config_bot_feishu_concierge"


def test_synthesize_mixin_entry_rejects_empty_list():
    import pytest
    with pytest.raises(ValueError):
        synthesize_mixin_entry("feishu", [])
