from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


class _FakeStore:
    def __init__(self):
        self.settings = {
            "radar": {
                "notify_to": "ou_old",
                "quiet_hours": "22-8",
                "watchlist": ["openai/codex", "bad", "anthropics/claude-code"],
            },
            "tavily": {"api_key": "tvly-old", "url": "https://old.example/tavily/search"},
        }
        self.providers = {
            "grok": {
                "api_key": "xai-old",
                "model": "grok-4-fast-reasoning",
                "base_url": "https://old.example/grok/v1",
            }
        }
        self.bots = {
            "feishu": {"app_id": "cli_old", "app_secret": "secret-old"},
        }

    def get(self, path, default=None):
        cur = {
            "settings": self.settings,
            "providers": self.providers,
            "bots": self.bots,
        }
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_provider(self, name):
        return dict(self.providers.get(name, {}))

    def get_bot(self, name):
        return dict(self.bots.get(name, {}))

    def set_setting(self, name, value, **_kw):
        self.settings[name] = value
        return value

    def set_provider(self, name, fields, *, merge=True, **_kw):
        if merge:
            self.providers[name] = {**self.providers.get(name, {}), **fields}
        else:
            self.providers[name] = dict(fields)
        return self.providers[name]

    def set_bot(self, name, fields, *, merge=True, **_kw):
        if merge:
            self.bots[name] = {**self.bots.get(name, {}), **fields}
        else:
            self.bots[name] = dict(fields)
        return self.bots[name]


def _patch_store(monkeypatch, store):
    monkeypatch.setattr("launcher.config_store.default_store", lambda: store)
    monkeypatch.setattr("launcher.radar_control._env_get", lambda _name: "")
    monkeypatch.setattr(
        "launcher.radar_control.status",
        lambda log_lines=20: {"alive": False, "pid": 0, "log_path": "x", "log_tail": []},
    )


def test_radar_config_get_masks_secrets_and_filters_watchlist(monkeypatch):
    from launcher import api_server

    store = _FakeStore()
    _patch_store(monkeypatch, store)

    status, body = api_server._route_radar_config_get({})

    assert status == 200
    assert body["config"]["feishu_app_id"] == "***"
    assert body["config"]["feishu_app_secret"] == "***"
    assert body["config"]["grok_api_key"] == "***"
    assert body["config"]["grok_url"] == "https://old.example/grok/v1"
    assert body["config"]["tavily_api_key"] == "***"
    assert body["config"]["tavily_url"] == "https://old.example/tavily/search"
    assert body["config"]["watchlist"] == ["openai/codex", "anthropics/claude-code"]


def test_radar_config_put_preserves_masked_secrets_and_writes_radar_fields(monkeypatch):
    from launcher import api_server

    store = _FakeStore()
    _patch_store(monkeypatch, store)

    status, body = api_server._route_radar_config_put({
        "body": {
            "feishu_app_id": "***",
            "feishu_app_secret": "secret-new",
            "notify_to": "ou_new",
            "quiet_hours": "off",
            "watchlist": "openai/codex\ninvalid\nsst/opencode",
            "grok_api_key": "***",
            "grok_url": "https://proxy.example/grok/v1",
            "grok_model": "grok-test",
            "tavily_api_key": "tvly-new",
            "tavily_url": "https://proxy.example/tavily",
        }
    })

    assert status == 200
    assert store.bots["feishu"]["app_id"] == "cli_old"
    assert store.bots["feishu"]["app_secret"] == "secret-new"
    assert store.providers["grok"]["api_key"] == "xai-old"
    assert store.providers["grok"]["base_url"] == "https://proxy.example/grok/v1"
    assert store.providers["grok"]["model"] == "grok-test"
    assert store.providers["grok"]["kind"] == "native_oai"
    assert store.settings["tavily"]["api_key"] == "tvly-new"
    assert store.settings["tavily"]["url"] == "https://proxy.example/tavily"
    assert store.settings["radar"]["notify_to"] == "ou_new"
    assert store.settings["radar"]["quiet_hours"] == "off"
    assert store.settings["radar"]["watchlist"] == ["openai/codex", "sst/opencode"]
    assert body["config"]["notify_to"] == "ou_new"
    assert body["config"]["grok_url"] == "https://proxy.example/grok/v1"
    assert body["config"]["tavily_url"] == "https://proxy.example/tavily"
