from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_api_config_category_priority_defaults_and_inference():
    from launcher import api_config

    plain = api_config.normalize_config({
        "kind": "native_oai",
        "name": "plain",
        "apikey": "k",
        "apibase": "https://x/v1",
        "model": "gpt",
    })
    assert plain["category"] == "language"
    assert plain["priority"] == 0

    image = api_config.normalize_config({
        "kind": "native_oai",
        "name": "image",
        "apikey": "k",
        "apibase": "https://x/v1",
        "model": "gpt-image-1",
        "image_capable": "true",
        "priority": "7",
    })
    assert image["category"] == "multimodal"
    assert image["priority"] == 7

    voice = api_config.normalize_config({
        "kind": "native_oai",
        "name": "voice",
        "apikey": "k",
        "apibase": "https://x/v1",
        "model": "whisper-1",
        "audio_capable": "true",
    })
    assert voice["category"] == "voice"


def test_api_config_sorts_by_category_priority_and_preserves_mixin_members():
    from launcher import api_config

    configs = api_config.prepare_api_configs([
        {"kind": "native_oai", "name": "lang-low", "apikey": "k", "apibase": "https://x/v1", "model": "m", "priority": 1},
        {"kind": "native_oai", "name": "voice", "apikey": "k", "apibase": "https://x/v1", "model": "whisper-1", "category": "voice", "priority": 99},
        {"kind": "native_oai", "name": "lang-high", "apikey": "k", "apibase": "https://x/v1", "model": "m", "priority": 5},
        {"kind": "native_oai", "name": "lang-peer", "apikey": "k", "apibase": "https://x/v1", "model": "m", "priority": 5},
        {"kind": "mixin", "name": "mix", "llm_nos": [0, 2], "category": "language", "priority": 0},
    ])

    assert [c["name"] for c in configs] == [
        "lang-high",
        "lang-peer",
        "lang-low",
        "mix",
        "voice",
    ]
    mix = next(c for c in configs if c["name"] == "mix")
    assert mix["llm_nos"] == ["lang-low", "lang-high"]


def test_save_api_configs_persists_category_priority_order(tmp_path):
    from launcher import api_config

    saved = api_config.save_api_configs(str(tmp_path), [
        {"kind": "native_oai", "name": "a", "apikey": "k", "apibase": "https://x/v1", "model": "m", "priority": 1},
        {"kind": "native_oai", "name": "b", "apikey": "k", "apibase": "https://x/v1", "model": "m", "priority": 3},
        {"kind": "native_oai", "name": "v", "apikey": "k", "apibase": "https://x/v1", "model": "whisper-1", "audio_capable": True, "priority": 10},
    ])
    assert [(c["name"], c["category"], c["priority"]) for c in saved] == [
        ("b", "language", 3),
        ("a", "language", 1),
        ("v", "voice", 10),
    ]

    raw = json.loads((tmp_path / "temp" / "launcher_api_configs.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in raw["configs"]] == ["b", "a", "v"]
