"""Tests for the api_server auto-start hooks.

Verifies the two startup behaviors that replace the old per-launcher bot/sched
flag handling (previously in launcher/qt_launcher.py and launch.pyw):

  * _auto_start_configured_bots: starts any bot whose credentials AND SDK are
    present, skips bots already running (own or external), skips
    not-configured / sdk-missing bots.
  * _auto_start_scheduler: spawns reflect/scheduler.py only when
    launch_options.scheduler == True; no spawn otherwise.

Tests use monkeypatch on module-level singletons rather than launching real
subprocesses, since these hooks gate subprocess.Popen.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── helpers ────────────────────────────────────────────────────────────


@dataclass
class _FakeStatus:
    configured: bool = False
    sdk_installed: bool = False
    running: bool = False


class _FakeBotManager:
    """Drop-in replacement for BotManager — records start() calls without
    actually spawning anything."""

    def __init__(self, statuses: dict[str, _FakeStatus]):
        self._statuses = statuses
        self.started: list[str] = []

    def status_all(self):
        return dict(self._statuses)

    def start(self, key: str):
        self.started.append(key)
        # Flip the in-memory status so a second call would skip it
        self._statuses[key].running = True
        return True, f"started:{key}"

    def stop_all(self):
        for st in self._statuses.values():
            st.running = False


# ── _auto_start_configured_bots ─────────────────────────────────────────


def test_autostart_only_starts_configured_and_sdk_ready_bots(monkeypatch):
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(configured=True, sdk_installed=True),       # ✅ start
        "tg":     _FakeStatus(configured=True, sdk_installed=False),       # ❌ sdk missing
        "qq":     _FakeStatus(configured=False, sdk_installed=True),       # ❌ no creds
        "wecom":  _FakeStatus(configured=True, sdk_installed=True, running=True),  # ❌ already up
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()

    assert fake.started == ["feishu"]


def test_autostart_status_probe_failure_is_swallowed(monkeypatch, capsys):
    from launcher import api_server

    class Boom:
        def status_all(self):
            raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(api_server, "_bm", lambda: Boom())
    api_server._auto_start_configured_bots()  # must not raise
    assert "status probe failed" in capsys.readouterr().out


def test_autostart_skips_already_running_bot(monkeypatch):
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(configured=True, sdk_installed=True, running=True),
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()
    assert fake.started == []


def test_autostart_skips_wechat_by_default(monkeypatch):
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(configured=True, sdk_installed=True),
        "wechat": _FakeStatus(configured=True, sdk_installed=True),
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()
    assert fake.started == ["feishu"]


# ── _auto_start_scheduler ───────────────────────────────────────────────


def test_scheduler_skipped_when_disabled(monkeypatch, tmp_path):
    from launcher import api_server

    monkeypatch.setattr(api_server, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "launcher.launch_config.load_options",
        lambda base_dir: {"scheduler": False, "llm_no": 0},
    )
    spawned: list = []
    monkeypatch.setattr(
        api_server.subprocess, "Popen",
        lambda *a, **kw: spawned.append((a, kw)) or _DummyProc(),
    )
    api_server._scheduler_proc = None
    api_server._auto_start_scheduler()
    assert spawned == []
    assert api_server._scheduler_proc is None


def test_scheduler_spawned_when_enabled(monkeypatch, tmp_path):
    from launcher import api_server

    monkeypatch.setattr(api_server, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "launcher.launch_config.load_options",
        lambda base_dir: {"scheduler": True, "llm_no": 0},
    )
    spawned: list = []

    def _fake_popen(cmd, **kw):
        spawned.append(cmd)
        return _DummyProc()

    monkeypatch.setattr(api_server.subprocess, "Popen", _fake_popen)
    api_server._scheduler_proc = None
    try:
        api_server._auto_start_scheduler()
        assert len(spawned) == 1
        cmd = spawned[0]
        # Should reference agentmain.py --reflect reflect/scheduler.py
        assert any("agentmain.py" in str(p) for p in cmd)
        assert "--reflect" in cmd
        assert any(p.endswith("scheduler.py") for p in cmd)
        assert api_server._scheduler_proc is not None
    finally:
        api_server._scheduler_proc = None


class _DummyProc:
    pid = 42
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


# ── _shutdown_children idempotency ──────────────────────────────────────


def test_shutdown_children_is_idempotent(monkeypatch):
    from launcher import api_server

    fake = _FakeBotManager({"feishu": _FakeStatus(running=True)})
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    api_server._scheduler_proc = _DummyProc()

    api_server._shutdown_children()
    api_server._shutdown_children()  # second call must not raise

    assert api_server._scheduler_proc is None
