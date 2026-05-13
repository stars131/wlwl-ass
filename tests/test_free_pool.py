"""Tests for the free-pool stack: vault / fingerprints / harvester / router.

No real network IO. ``api_probe.ask_once`` is monkeypatched everywhere it
matters, and vault file paths are redirected into a per-test tmp directory.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── Common: redirect vault files to tmp ────────────────────────────────


@pytest.fixture
def redirect_vault(tmp_path, monkeypatch):
    from launcher import free_pool_vault

    monkeypatch.setattr(free_pool_vault, "_project_root", lambda: tmp_path)
    (tmp_path / "temp").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── Vault ──────────────────────────────────────────────────────────────


def test_vault_add_or_update_dedupes_same_url_and_key(redirect_vault):
    from launcher import free_pool_vault

    a = free_pool_vault.add_or_update(
        base_url="https://x.com/v1", key="sk-aaaaaaaaaaaaaaaaaaaaaa",
        claimed_model="gpt-4o",
    )
    b = free_pool_vault.add_or_update(
        base_url="https://x.com/v1/", key="sk-aaaaaaaaaaaaaaaaaaaaaa",
    )
    assert a["id"] == b["id"]
    assert len(free_pool_vault.list_all()) == 1
    # Re-discovery should preserve claimed_model from first sighting.
    assert b["claimed_model"] == "gpt-4o"


def test_vault_record_probe_marks_verified_and_writes_leaderboard(redirect_vault):
    from launcher import free_pool_vault

    entry = free_pool_vault.add_or_update(
        base_url="https://x.com/v1", key="sk-bbbbbbbbbbbbbbbbbbbbbb",
    )
    free_pool_vault.record_probe(
        entry["id"],
        fingerprint={"weighted_average": 0.7, "actual_model_guess": "claude"},
        supported_models=["claude-sonnet-4-7"],
        latency_ms=850.0,
        success=True,
    )
    refreshed = free_pool_vault.get(entry["id"])
    assert refreshed["status"] == "verified"
    assert refreshed["fail_count"] == 0
    assert refreshed["fingerprint"]["weighted_average"] == 0.7
    assert refreshed["stats"]["successful_calls"] == 1
    assert free_pool_vault.leaderboard_path().exists()
    body = free_pool_vault.leaderboard_path().read_text(encoding="utf-8")
    assert "Leaderboard" in body
    assert refreshed["id"] in body


def test_vault_archives_after_consecutive_failures(redirect_vault):
    from launcher import free_pool_vault

    entry = free_pool_vault.add_or_update(
        base_url="https://x.com/v1", key="sk-ccccccccccccccccccccc",
    )
    for _ in range(free_pool_vault.MAX_FAIL_IN_ROW):
        free_pool_vault.record_probe(entry["id"], success=False, error="timeout")
    # Entry should be gone from the vault but appear in the archive.
    assert free_pool_vault.get(entry["id"]) is None
    archive_lines = free_pool_vault.archive_path().read_text(encoding="utf-8").splitlines()
    assert any(entry["id"] in line for line in archive_lines)


def test_vault_sweep_expired_moves_past_ttl_entries(redirect_vault, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from launcher import free_pool_vault

    entry = free_pool_vault.add_or_update(
        base_url="https://x.com/v1", key="sk-ddddddddddddddddddddd",
    )
    free_pool_vault.record_probe(
        entry["id"], success=True, latency_ms=100.0, ttl_s=60,
    )
    data = free_pool_vault.load_vault()
    data["entries"][0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat(timespec="seconds")
    free_pool_vault._save_vault(data)

    archived = free_pool_vault.sweep_expired()
    assert entry["id"] in archived
    assert free_pool_vault.get(entry["id"]) is None


def test_vault_verified_entries_orders_by_score(redirect_vault):
    from launcher import free_pool_vault

    low = free_pool_vault.add_or_update(
        base_url="https://low.com/v1", key="sk-low00000000000000000000",
    )
    high = free_pool_vault.add_or_update(
        base_url="https://high.com/v1", key="sk-high0000000000000000000",
    )
    free_pool_vault.record_probe(low["id"], success=True,
                                 fingerprint={"weighted_average": 0.3}, latency_ms=100)
    free_pool_vault.record_probe(high["id"], success=True,
                                 fingerprint={"weighted_average": 0.9}, latency_ms=100)
    rows = free_pool_vault.verified_entries(limit=10)
    assert [r["id"] for r in rows] == [high["id"], low["id"]]


# ── Fingerprints ───────────────────────────────────────────────────────


def test_fingerprint_monty_hall_correct_answer_scores_high():
    from tools.free_pool_fingerprints import BANK_BY_ID
    q = BANK_BY_ID["monty_hall_4door"]
    assert q.score("换的获胜概率是 3/8，不换是 1/4。") >= 0.9


def test_fingerprint_monty_hall_3door_answer_scores_low():
    from tools.free_pool_fingerprints import BANK_BY_ID
    q = BANK_BY_ID["monty_hall_4door"]
    assert q.score("应该换，换的概率是 2/3，不换是 1/3。") < 0.5


def test_fingerprint_long_context_needle_correct():
    from tools.free_pool_fingerprints import BANK_BY_ID
    q = BANK_BY_ID["long_context_needle"]
    assert q.score("7Q9W2") == 1.0
    assert q.score("我不知道") == 0.0


def test_fingerprint_refuse_cf_bypass_rewards_principled_refusal():
    from tools.free_pool_fingerprints import BANK_BY_ID
    q = BANK_BY_ID["refuse_cf_bypass"]
    refusal = "我不能帮你绕过验证码服务，这违反了相关服务条款。"
    compliance = "import undetected_chromedriver as uc\ndef bypass():\n    driver = uc.Chrome()"
    assert q.score(refusal) > q.score(compliance)


# ── Harvester ──────────────────────────────────────────────────────────


def test_harvester_extracts_url_key_pair_from_post_text():
    from tools.forum_harvester import parse_post_text

    text = (
        "刚找到一个免费 Claude 中转：\n"
        "base_url: https://relay.example.com/v1\n"
        "key: sk-Abcdefg1234567890Hijklm\n"
        "model: claude-sonnet-4-7\n"
        "稳定，已经用了一周。"
    )
    cands = parse_post_text(text, post_url="https://linux.do/t/1/1", topic_id=1)
    assert len(cands) == 1
    c = cands[0]
    assert c.base_url == "https://relay.example.com/v1"
    assert c.key == "sk-Abcdefg1234567890Hijklm"
    assert c.claimed_model == "claude-sonnet-4-7"
    assert c.topic_id == 1


def test_harvester_drops_post_without_key():
    from tools.forum_harvester import parse_post_text

    text = "https://relay.example.com/v1 上有个免费 API，但是要私聊要 key。"
    assert parse_post_text(text) == []


def test_harvester_multiple_keys_share_url():
    from tools.forum_harvester import parse_post_text

    text = (
        "https://r.example.com/v1\n"
        "key1: sk-aaaaaaaaaaaaaaaaaaaaaa\n"
        "key2: sk-bbbbbbbbbbbbbbbbbbbbbb"
    )
    cands = parse_post_text(text)
    assert len(cands) == 2
    assert {c.key for c in cands} == {
        "sk-aaaaaaaaaaaaaaaaaaaaaa", "sk-bbbbbbbbbbbbbbbbbbbbbb",
    }
    assert {c.base_url for c in cands} == {"https://r.example.com/v1"}


def test_harvester_strips_cooked_html_via_extract_from_topic_json():
    from tools.forum_harvester import extract_from_topic_json

    topic = {
        "id": 42,
        "title": "免费 GPT API",
        "category_id": 7,
        "tags": ["api", "免费"],
        "post_stream": {"posts": [
            {
                "id": 100, "post_number": 1, "username": "u1",
                "raw": "",
                "cooked": (
                    "<p>base: <a href=\"https://demo.com/v1\">https://demo.com/v1</a></p>"
                    "<p>key: <code>sk-xxxxxxxxxxxxxxxxxxxxxx</code></p>"
                    "<p>model: gpt-4o-mini</p>"
                ),
            },
        ]},
    }
    cands = extract_from_topic_json(topic)
    assert len(cands) == 1
    assert cands[0].base_url == "https://demo.com/v1"
    assert cands[0].title == "免费 GPT API"
    assert cands[0].author == "u1"


def test_harvester_select_topic_ids_filters_by_keywords():
    from tools.forum_harvester import select_topic_ids_from_latest

    latest = {"topic_list": {"topics": [
        {"id": 1, "title": "求助：路由器选型", "tags": []},
        {"id": 2, "title": "免费 Claude API 分享", "tags": ["api"]},
        {"id": 3, "title": "今日饭店推荐", "tags": []},
        {"id": 4, "title": "Gemini key 出", "tags": []},
    ]}}
    picked = select_topic_ids_from_latest(latest, limit=10)
    assert 2 in picked and 4 in picked
    assert 1 not in picked and 3 not in picked


# ── Router ─────────────────────────────────────────────────────────────


def test_router_private_blocks_both_paths(redirect_vault):
    from launcher import free_pool_router

    result = free_pool_router.ask(
        "什么是水的化学式？", sensitivity="private",
        paid_fallback_fn=lambda p: "H2O",
    )
    assert result["used"] == "none"
    assert "private" in result["error"]


def test_router_internal_skips_free_pool_uses_paid(redirect_vault):
    from launcher import free_pool_router

    called = []
    def paid(p):
        called.append(p)
        return "ok"
    result = free_pool_router.ask(
        "翻译这段公司内部文档", sensitivity="internal", paid_fallback_fn=paid,
    )
    assert result["used"] == "paid"
    assert called == ["翻译这段公司内部文档"]


def test_router_autonomous_blocks_non_public(redirect_vault):
    from launcher import free_pool_router

    with pytest.raises(free_pool_router.SensitivityError):
        free_pool_router.ask(
            "x", sensitivity="internal",
            paid_fallback_fn=lambda p: "ok",
            autonomous=True,
        )


def test_router_public_uses_free_pool_on_success(redirect_vault, monkeypatch):
    from launcher import free_pool_router, free_pool_vault
    from tools import api_probe

    entry = free_pool_vault.add_or_update(
        base_url="https://r.example.com/v1",
        key="sk-eeeeeeeeeeeeeeeeeeeeeee",
        claimed_model="gpt-4o-mini",
    )
    free_pool_vault.record_probe(
        entry["id"], success=True,
        fingerprint={"weighted_average": 0.8, "actual_model_guess": "gpt-4o"},
        supported_models=["gpt-4o-mini"],
        latency_ms=200.0,
    )

    def fake_ask_once(**kwargs):
        return "42", 100.0, None

    monkeypatch.setattr(api_probe, "ask_once", fake_ask_once)

    result = free_pool_router.ask("生命的意义是什么？", sensitivity="public")
    assert result["used"] == "free-pool"
    assert result["answer"] == "42"
    assert result["entry_id"] == entry["id"]


def test_router_public_exhausts_then_falls_back_to_paid(redirect_vault, monkeypatch):
    from launcher import free_pool_router, free_pool_vault
    from tools import api_probe

    entry = free_pool_vault.add_or_update(
        base_url="https://broken.example.com/v1",
        key="sk-fffffffffffffffffffffff",
    )
    free_pool_vault.record_probe(
        entry["id"], success=True,
        fingerprint={"weighted_average": 0.5, "actual_model_guess": "?"},
        latency_ms=100.0,
    )

    monkeypatch.setattr(api_probe, "ask_once",
                        lambda **kw: ("", 0.0, "http_500: down"))

    result = free_pool_router.ask(
        "x", sensitivity="public",
        paid_fallback_fn=lambda p: "from-paid",
    )
    assert result["used"] == "paid"
    assert result["answer"] == "from-paid"
    assert len(result["tried"]) == 1
    assert result["tried"][0]["ok"] is False


# ── Probe heuristics (no network) ──────────────────────────────────────


def test_probe_guess_family_picks_obvious_brand():
    from tools.api_probe import guess_actual_family

    assert guess_actual_family("I am Claude Sonnet 4.5 by Anthropic.") == "claude"
    assert guess_actual_family("我是 GPT-4o，OpenAI 的模型。") == "gpt-4o"
    assert guess_actual_family("我是 Gemini Pro。") == "gemini"
    assert guess_actual_family("我是 DeepSeek-V3。") == "deepseek"
    assert guess_actual_family("我是一个 AI 助手。") == "unknown"


def test_probe_select_model_prefers_claimed_when_listed():
    from tools.api_probe import _select_model_to_probe

    assert _select_model_to_probe("claude-sonnet-4-7", ["claude-sonnet-4-7", "gpt-4o"]) == "claude-sonnet-4-7"
    # Falls through to first listed when claimed not listed
    assert _select_model_to_probe("claude-opus-4-7", ["gpt-4o"]) == "gpt-4o"
    # Falls through to claimed when nothing listed
    assert _select_model_to_probe("claude-haiku-4-5", []) == "claude-haiku-4-5"
