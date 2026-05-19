"""Integration tests for per-bot LLM binding wiring.

Covers:
- ``launcher/bot_manager.py::_read_llm_binding`` reads from config_store
- ``BotManager.start()`` propagates the binding into the spawned subprocess
  via the ``WLWL_BOT_LLM_BINDING`` env var
- ``launcher/api_server.py::_route_bots_list`` returns the per-bot
  ``llm_binding`` field
- ``_route_bot_set_llm`` validates input + writes the store
- ``_route_bot_llm_options`` exposes the picker source

Heavy seams (real subprocesses, real Lark WS, real api_server HTTP) are
monkeypatched so tests run in milliseconds.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── _read_llm_binding ────────────────────────────────────────────────────


def test_read_llm_binding_returns_empty_when_no_field(monkeypatch):
    from launcher import bot_manager

    class _FakeStore:
        def get_bot(self, key):
            return {}

    monkeypatch.setattr(
        "launcher.config_store.default_store",
        lambda: _FakeStore(),
    )
    assert bot_manager._read_llm_binding("/tmp/whatever", "feishu") == ""


def test_read_llm_binding_returns_value_when_set(monkeypatch):
    from launcher import bot_manager

    class _FakeStore:
        def get_bot(self, key):
            return {"llm_binding": "profile:glm"} if key == "feishu" else {}

    monkeypatch.setattr(
        "launcher.config_store.default_store",
        lambda: _FakeStore(),
    )
    assert bot_manager._read_llm_binding("/tmp/whatever", "feishu") == "profile:glm"
    assert bot_manager._read_llm_binding("/tmp/whatever", "tg") == ""


def test_read_llm_binding_swallows_store_failure(monkeypatch):
    """A broken config_store should never block bot startup."""
    from launcher import bot_manager

    def _broken():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("launcher.config_store.default_store", _broken)
    assert bot_manager._read_llm_binding("/tmp/whatever", "feishu") == ""


# ── BotManager.start env injection ───────────────────────────────────────


def test_bot_manager_start_injects_binding_env_var(monkeypatch, tmp_path):
    """When the store has a binding, start() must surface it to the subprocess."""
    from launcher import bot_manager

    monkeypatch.setattr(
        bot_manager, "_read_llm_binding",
        lambda base, key: "config:glm" if key == "feishu" else "",
    )
    # SDK + ports must look healthy for start() to take the spawn path.
    monkeypatch.setattr(bot_manager, "_sdk_available", lambda mods: True)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **kw: False)
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda *a, **kw: False)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {
        "fs_app_id": "x", "fs_app_secret": "y",
    })

    captured_env: dict = {}

    class _FakeProc:
        pid = 12345
        def poll(self): return None  # alive forever — start() won't detect early death

    def _fake_popen(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return _FakeProc()

    monkeypatch.setattr(bot_manager.subprocess, "Popen", _fake_popen)
    # process_registry import side effect — neuter it.
    class _FakeReg:
        def unregister_label(self, *a, **kw): pass
        def register(self, *a, **kw): pass
        def get_by_label(self, *a, **kw): return []
        def cleanup_dead(self): pass
    monkeypatch.setattr(
        "launcher.process_registry.get_registry",
        lambda base: _FakeReg(),
    )

    bm = bot_manager.BotManager(base_dir=str(tmp_path))
    ok, msg = bm.start("feishu")
    assert ok, msg
    assert captured_env.get("WLWL_BOT_KEY") == "feishu"
    assert captured_env.get("WLWL_BOT_LLM_BINDING") == "config:glm"


def test_bot_manager_start_omits_binding_env_var_when_empty(monkeypatch, tmp_path):
    """Default (empty) binding must NOT set the env var — bot should see the
    legacy behaviour, not 'WLWL_BOT_LLM_BINDING=''."""
    from launcher import bot_manager

    monkeypatch.setattr(bot_manager, "_read_llm_binding", lambda base, key: "")
    monkeypatch.setattr(bot_manager, "_sdk_available", lambda mods: True)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **kw: False)
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda *a, **kw: False)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {
        "fs_app_id": "x", "fs_app_secret": "y",
    })

    captured_env: dict = {}

    class _FakeProc:
        pid = 12345
        def poll(self): return None

    def _fake_popen(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return _FakeProc()

    monkeypatch.setattr(bot_manager.subprocess, "Popen", _fake_popen)
    class _FakeReg:
        def unregister_label(self, *a, **kw): pass
        def register(self, *a, **kw): pass
        def get_by_label(self, *a, **kw): return []
        def cleanup_dead(self): pass
    monkeypatch.setattr(
        "launcher.process_registry.get_registry",
        lambda base: _FakeReg(),
    )

    bm = bot_manager.BotManager(base_dir=str(tmp_path))
    ok, _ = bm.start("feishu")
    assert ok
    assert "WLWL_BOT_LLM_BINDING" not in captured_env


# ── api_server: list + set + options routes ──────────────────────────────


def _fake_status(running=False):
    """Build a minimal BotStatus-shaped object for _route_bots_list."""
    class _S:
        def __init__(self):
            self.configured = True
            self.missing_fields = []
            self.sdk_installed = True
            self.missing_modules = []
            self.running_self = running
            self.running_external = False
            self.running = running
            self.lock_holder_pid = None
            self.log_path = "/tmp/x.log"
    return _S()


def test_route_bots_list_includes_llm_binding(monkeypatch):
    from launcher import api_server

    class _FakeBM:
        def status_all(self):
            return {"feishu": _fake_status(), "feishu_concierge": _fake_status()}
        def status(self, key):
            return _fake_status()

    class _FakeStore:
        def get_bot(self, key):
            return {"llm_binding": "profile:glm"} if key == "feishu" else {}

    monkeypatch.setattr(api_server, "_bm", lambda: _FakeBM())
    monkeypatch.setattr(
        "launcher.config_store.default_store",
        lambda: _FakeStore(),
    )
    # Trim BOT_SPECS to just the two we care about for stable assertions.
    from launcher.bot_manager import BOT_SPECS, BotSpec
    monkeypatch.setattr(
        "launcher.bot_manager.BOT_SPECS",
        {"feishu": BOT_SPECS["feishu"], "feishu_concierge": BOT_SPECS["feishu_concierge"]},
    )

    status, body = api_server._route_bots_list({})
    assert status == 200
    by_key = {b["key"]: b for b in body["bots"]}
    assert by_key["feishu"]["llm_binding"] == "profile:glm"
    assert by_key["feishu_concierge"]["llm_binding"] == ""


def test_route_bot_set_llm_happy_path(monkeypatch):
    from launcher import api_server

    written: dict = {}

    class _FakeStore:
        def set_bot(self, name, fields, *, merge=True, **kw):
            written["name"] = name
            written["fields"] = dict(fields)
            written["merge"] = merge
            return fields

    class _FakeBM:
        def status(self, key):
            return _fake_status(running=True)

    monkeypatch.setattr(api_server, "_bm", lambda: _FakeBM())
    monkeypatch.setattr(
        "launcher.config_store.default_store",
        lambda: _FakeStore(),
    )

    status, body = api_server._route_bot_set_llm({
        "params": {"key": "feishu"},
        "body": {"binding": "profile:glm"},
    })
    assert status == 200
    assert body == {"key": "feishu", "binding": "profile:glm", "restart_required": True}
    assert written["name"] == "feishu"
    assert written["fields"] == {"llm_binding": "profile:glm"}
    assert written["merge"] is True


def test_route_bot_set_llm_empty_clears(monkeypatch):
    """Empty body clears the binding (revert to default)."""
    from launcher import api_server

    class _FakeStore:
        def set_bot(self, name, fields, **kw):
            self.last = (name, fields)
            return fields

    store = _FakeStore()
    monkeypatch.setattr(api_server, "_bm", lambda: type("BM", (), {"status": lambda self, k: _fake_status(running=False)})())
    monkeypatch.setattr(
        "launcher.config_store.default_store",
        lambda: store,
    )

    status, body = api_server._route_bot_set_llm({
        "params": {"key": "feishu"},
        "body": {"binding": ""},
    })
    assert status == 200
    assert body["binding"] == ""
    assert body["restart_required"] is False
    assert store.last == ("feishu", {"llm_binding": ""})


def test_route_bot_set_llm_rejects_unknown_bot(monkeypatch):
    from launcher import api_server

    status, body = api_server._route_bot_set_llm({
        "params": {"key": "no_such_bot"},
        "body": {"binding": "config:glm"},
    })
    assert status == 404
    assert body["error"] == "unknown_bot"


def test_route_bot_set_llm_rejects_unsupported_bot(monkeypatch):
    """tg / qq / wecom etc. accept the field on read but cannot be written
    until their frontend learns to honor WLWL_BOT_LLM_BINDING."""
    from launcher import api_server

    status, body = api_server._route_bot_set_llm({
        "params": {"key": "tg"},
        "body": {"binding": "config:glm"},
    })
    assert status == 400
    assert body["error"] == "binding_not_supported_for_bot"
    assert "feishu" in body["supported"]


def test_route_bot_set_llm_rejects_invalid_syntax(monkeypatch):
    from launcher import api_server

    status, body = api_server._route_bot_set_llm({
        "params": {"key": "feishu"},
        "body": {"binding": "garbage:foo"},
    })
    assert status == 400
    assert body["error"] == "invalid_binding_syntax"


def test_route_bot_llm_options_shape(monkeypatch):
    from launcher import api_server

    monkeypatch.setattr(
        "launcher.api_config.list_api_configs",
        lambda root: [
            {"name": "glm", "var_name": "native_oai_config_glm", "kind": "native_oai"},
            {"name": "mixin1", "var_name": "mixin_config_mixin1", "kind": "mixin"},  # filtered out
            {"name": "", "var_name": "x", "kind": "native_oai"},  # filtered out (empty name)
            {"name": "grok", "var_name": "native_oai_config_grok", "kind": "native_oai"},
        ],
    )
    monkeypatch.setattr(
        "launcher.profiles.load_profiles",
        lambda root: {"active": "glm", "profiles": {"glm": ["glm", "grok"]}},
    )

    status, body = api_server._route_bot_llm_options({})
    assert status == 200
    names = [c["name"] for c in body["configs"]]
    assert names == ["glm", "grok"]  # mixin + empty filtered
    assert body["profiles"] == [{"name": "glm", "members": ["glm", "grok"]}]
