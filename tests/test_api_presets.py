"""Tests for the cc-switch-inspired preset library + config tooling.

Covered:
- launcher.api_presets — table integrity + lookup helpers
- launcher.api_config — backup rotation + pruning + list_backups
- launcher.api_endpoint_probe — deep-link parser (positive + negative)
- launcher.cli_config — argparse plumbing + presets/list happy paths

We don't probe real network endpoints here — that's intentionally a
separate manual `wlwl config probe` UX, since it depends on the user's
egress.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── preset table ──────────────────────────────────────────────────────────


def test_presets_have_required_fields():
    from launcher import api_presets
    seen_ids = set()
    for r in api_presets.PRESETS:
        for f in ("id", "provider", "kind", "apibase", "model", "category"):
            assert r.get(f), f"preset {r.get('id')!r} missing {f!r}"
        assert r["id"] not in seen_ids, f"duplicate preset id: {r['id']}"
        seen_ids.add(r["id"])
        assert r["kind"] in ("native_oai", "native_claude")
        assert r["category"] in api_presets.CATEGORIES
        assert r["apibase"].startswith(("http://", "https://"))
        assert r["id"] == r["id"].lower(), f"preset id must be lowercase: {r['id']}"


def test_presets_count_at_least_30():
    from launcher import api_presets
    assert len(api_presets.PRESETS) >= 30, (
        "preset library shrunk — should retain >=30 entries for usefulness"
    )


def test_get_preset_returns_copy():
    from launcher import api_presets
    p = api_presets.get_preset("kimi")
    assert p is not None
    p["apibase"] = "tampered"
    # Original table must not be mutated.
    assert api_presets.get_preset("kimi")["apibase"] != "tampered"


def test_get_preset_unknown_returns_none():
    from launcher import api_presets
    assert api_presets.get_preset("does-not-exist") is None
    assert api_presets.get_preset("") is None
    assert api_presets.get_preset(None) is None


def test_find_preset_substring_match():
    from launcher import api_presets
    hits = api_presets.find_preset("kimi")
    ids = {h["id"] for h in hits}
    assert "kimi" in ids
    # Multiple Kimi variants exist; loose match should find them all.
    assert len(ids) >= 2


def test_list_presets_filters():
    from launcher import api_presets
    official = api_presets.list_presets(category="official")
    assert all(r["category"] == "official" for r in official)
    assert any(r["id"] == "anthropic" for r in official)
    claude_only = api_presets.list_presets(kind="native_claude")
    assert all(r["kind"] == "native_claude" for r in claude_only)


def test_preset_to_config_shape():
    from launcher import api_presets
    p = api_presets.get_preset("deepseek")
    cfg = api_presets.preset_to_config(p, name="my-deepseek", apikey="sk-x")
    assert cfg == {
        "kind": "native_claude",
        "name": "my-deepseek",
        "apikey": "sk-x",
        "apibase": p["apibase"],
        "model": p["model"],
    }


# ── backup rotation ────────────────────────────────────────────────────────


def _seed_config(tmp_path, configs):
    """Write a minimal launcher_api_configs.json directly so we don't
    trigger the rotation logic on the *first* write."""
    from pathlib import Path
    tmp_path = Path(tmp_path)
    cfg_dir = tmp_path / "temp"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "launcher_api_configs.json").write_text(
        json.dumps({"configs": configs}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_backup_rotation_creates_snapshot(tmp_path):
    from launcher import api_config
    _seed_config(str(tmp_path), [
        {"kind": "native_oai", "name": "a", "apikey": "k", "apibase": "https://x/v1", "model": "m"}
    ])
    api_config.save_api_configs(str(tmp_path), [
        {"kind": "native_oai", "name": "a", "apikey": "k2", "apibase": "https://x/v1", "model": "m"}
    ])
    backups = api_config.list_backups(str(tmp_path))
    assert len(backups) == 1
    _, path = backups[0]
    snapshot = json.loads(open(path, encoding="utf-8").read())
    # The snapshot should hold the OLD apikey, not the new one.
    assert snapshot["configs"][0]["apikey"] == "k"


def test_backup_rotation_prunes_past_max(tmp_path):
    """Drive >MAX_BACKUPS writes and confirm pruning kicks in."""
    from launcher import api_config
    _seed_config(str(tmp_path), [
        {"kind": "native_oai", "name": "a", "apikey": "k0", "apibase": "https://x/v1", "model": "m"}
    ])
    for i in range(api_config.MAX_BACKUPS + 5):
        api_config.save_api_configs(str(tmp_path), [
            {"kind": "native_oai", "name": "a", "apikey": f"k{i+1}", "apibase": "https://x/v1", "model": "m"}
        ])
        # Without this, several saves can land in the same wallclock
        # second and the per-second collision suffix kicks in — fine in
        # production, but the test wants distinct mtimes for pruning.
        time.sleep(0.01)
    backups = api_config.list_backups(str(tmp_path))
    assert len(backups) <= api_config.MAX_BACKUPS, (
        f"prune failed: {len(backups)} > {api_config.MAX_BACKUPS}"
    )


def test_backup_rotation_noop_on_fresh_install(tmp_path):
    """First save should NOT create a backup — nothing to back up yet."""
    from launcher import api_config
    api_config.save_api_configs(str(tmp_path), [
        {"kind": "native_oai", "name": "a", "apikey": "k", "apibase": "https://x/v1", "model": "m"}
    ])
    assert api_config.list_backups(str(tmp_path)) == []


# ── deep-link parser ──────────────────────────────────────────────────────


def test_deep_link_preset_only():
    from launcher import api_endpoint_probe
    cfg = api_endpoint_probe.parse_deep_link("wlwl-config://provider?preset=deepseek")
    assert cfg["kind"] == "native_claude"
    assert cfg["apibase"].startswith("https://")
    assert cfg["model"]


def test_deep_link_overrides():
    from launcher import api_endpoint_probe
    cfg = api_endpoint_probe.parse_deep_link(
        "wlwl-config://provider?preset=kimi&name=my-kimi&model=kimi-test&apikey=sk-xx"
    )
    assert cfg["name"] == "my-kimi"
    assert cfg["model"] == "kimi-test"
    assert cfg["apikey"] == "sk-xx"


def test_deep_link_freeform_requires_apibase_and_model():
    import pytest
    from launcher import api_endpoint_probe
    with pytest.raises(ValueError) as exc:
        api_endpoint_probe.parse_deep_link(
            "wlwl-config://provider?name=foo&kind=native_oai"
        )
    assert "apibase" in str(exc.value)


def test_deep_link_freeform_ok():
    from launcher import api_endpoint_probe
    cfg = api_endpoint_probe.parse_deep_link(
        "wlwl-config://provider?name=foo&kind=native_oai&apibase=https://x/v1&model=m"
    )
    assert cfg["name"] == "foo"
    assert cfg["kind"] == "native_oai"
    assert cfg["apibase"] == "https://x/v1"


def test_deep_link_rejects_bad_scheme():
    import pytest
    from launcher import api_endpoint_probe
    with pytest.raises(ValueError):
        api_endpoint_probe.parse_deep_link("https://example.com/?preset=kimi")


def test_deep_link_rejects_unknown_preset():
    import pytest
    from launcher import api_endpoint_probe
    with pytest.raises(ValueError) as exc:
        api_endpoint_probe.parse_deep_link("wlwl-config://provider?preset=does-not-exist")
    assert "preset" in str(exc.value).lower()


def test_deep_link_ccswitch_alias_scheme():
    """For migration friendliness, ccswitch:// (cc-switch's own scheme)
    is accepted as an alias."""
    from launcher import api_endpoint_probe
    cfg = api_endpoint_probe.parse_deep_link("ccswitch://provider?preset=anthropic")
    assert cfg["apibase"] == "https://api.anthropic.com"


# ── CLI argparse / dispatch ───────────────────────────────────────────────


def test_cli_config_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import cli_config
    rc = cli_config.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no API configs" in out or "no API" in out


def test_cli_config_presets_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import cli_config
    rc = cli_config.main(["presets", "--category", "official"])
    assert rc == 0
    out = capsys.readouterr().out
    # Sanity: the canonical Anthropic + OpenAI rows are present.
    assert "anthropic" in out
    assert "openai" in out


def test_cli_config_add_preset_writes_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import api_config, cli_config
    rc = cli_config.main([
        "add", "--preset", "deepseek", "--name", "ds",
        "--apikey", "sk-fake",
    ])
    assert rc == 0
    saved = api_config.load_api_configs(str(tmp_path))
    assert any(c["name"] == "ds" for c in saved)


def test_cli_config_add_duplicate_blocked_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import cli_config
    cli_config.main(["add", "--preset", "kimi", "--name", "k", "--apikey", "sk1"])
    capsys.readouterr()
    rc = cli_config.main(["add", "--preset", "kimi", "--name", "k", "--apikey", "sk2"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "already exists" in err or "force" in err


def test_cli_config_use_reorders(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import api_config, cli_config
    cli_config.main(["add", "--preset", "kimi", "--name", "first", "--apikey", "k1"])
    cli_config.main(["add", "--preset", "deepseek", "--name", "second", "--apikey", "k2"])
    rc = cli_config.main(["use", "second"])
    assert rc == 0
    saved = api_config.load_api_configs(str(tmp_path))
    assert saved[0]["name"] == "second"


def test_cli_config_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import api_config, cli_config
    cli_config.main(["add", "--preset", "kimi", "--name", "k", "--apikey", "sk"])
    rc = cli_config.main(["remove", "k"])
    assert rc == 0
    assert not any(c["name"] == "k" for c in api_config.load_api_configs(str(tmp_path)))


def test_cli_config_export_masks_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from launcher import cli_config
    cli_config.main(["add", "--preset", "kimi", "--name", "k", "--apikey", "sk-secret"])
    capsys.readouterr()
    rc = cli_config.main(["export"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sk-secret" not in out
    assert "***" in out


# ── /config slash command (REPL) ──────────────────────────────────────────


class _FakeAgent:
    """Minimal stub matching SharedCommandHandler's expectations."""
    pass


def test_repl_config_slash_list(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from frontends.cli_commands import SharedCommandHandler
    h = SharedCommandHandler(_FakeAgent())
    result = h.handle("/config list")
    assert result.handled
    assert "no API configs" in (result.message or "") or "name" in (result.message or "")


def test_repl_config_slash_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from frontends.cli_commands import SharedCommandHandler
    h = SharedCommandHandler(_FakeAgent())
    result = h.handle("/config presets --category official")
    assert result.handled
    assert "anthropic" in (result.message or "")


def test_repl_config_slash_bare_invokes_list(tmp_path, monkeypatch):
    """`/config` with no sub should default to listing — matches the
    `wlwl config` semantics."""
    monkeypatch.setenv("WLWL_PROJECT_ROOT", str(tmp_path))
    from frontends.cli_commands import SharedCommandHandler
    h = SharedCommandHandler(_FakeAgent())
    result = h.handle("/config")
    assert result.handled
    assert "no API configs" in (result.message or "") or "name" in (result.message or "")


def test_repl_config_slash_bad_subcommand_shows_usage():
    from frontends.cli_commands import SharedCommandHandler
    h = SharedCommandHandler(_FakeAgent())
    result = h.handle("/config bogus")
    assert result.handled
    assert "用法" in (result.message or "") or "list" in (result.message or "")
