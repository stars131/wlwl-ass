"""Tests for the api_server auto-start hooks.

Verifies the two startup behaviors that replace the old per-launcher bot/sched
flag handling (previously in launcher/qt_launcher.py and launch.pyw):

  * _auto_start_configured_bots: starts any bot whose credentials AND SDK are
    present; adopts external orphans with discoverable PID; skips
    truly unmanageable orphans; skips not-configured / sdk-missing bots.
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
    running_self: bool = False
    running_external: bool = False
    lock_holder_pid: int | None = None

    @property
    def running(self) -> bool:
        return self.running_self or self.running_external


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
        self._statuses[key].running_self = True
        return True, f"started:{key}"

    def stop_all(self):
        for st in self._statuses.values():
            st.running_self = False
            st.running_external = False


# ── _auto_start_configured_bots ─────────────────────────────────────────


def test_autostart_only_starts_configured_and_sdk_ready_bots(monkeypatch):
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(configured=True, sdk_installed=True),       # ✅ start
        "tg":     _FakeStatus(configured=True, sdk_installed=False),       # ❌ sdk missing
        "qq":     _FakeStatus(configured=False, sdk_installed=True),       # ❌ no creds
        "wecom":  _FakeStatus(configured=True, sdk_installed=True, running_self=True),  # ❌ already up
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
        "feishu": _FakeStatus(configured=True, sdk_installed=True, running_self=True),
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()
    assert fake.started == []


def test_autostart_adopts_external_orphan_with_known_pid(monkeypatch):
    """When a bot is external (running_external=True) AND a PID is
    discoverable, auto-start MUST call start() so the adoption path runs.

    Without this, a fsapp.py from a previous api_server lifetime stays
    "external" forever and the GUI shows yellow with no way to manage it."""
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(
            configured=True, sdk_installed=True,
            running_external=True, lock_holder_pid=40952,
        ),
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()
    assert fake.started == ["feishu"]


def test_autostart_skips_orphan_with_unknown_pid(monkeypatch, capsys):
    """When a bot is external but no PID was discovered, we can't manage it.
    Don't call start() — it would error out — but log a hint."""
    from launcher import api_server

    fake = _FakeBotManager({
        "feishu": _FakeStatus(
            configured=True, sdk_installed=True,
            running_external=True, lock_holder_pid=None,
        ),
    })
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    monkeypatch.setattr(api_server, "_bm", lambda: fake)

    api_server._auto_start_configured_bots()
    assert fake.started == []
    captured = capsys.readouterr().out
    assert "external orphan" in captured.lower()
    assert "restart" in captured.lower()


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

    fake = _FakeBotManager({"feishu": _FakeStatus(running_self=True)})
    monkeypatch.setattr(api_server, "_bot_manager", fake)
    api_server._scheduler_proc = _DummyProc()

    api_server._shutdown_children()
    api_server._shutdown_children()  # second call must not raise

    assert api_server._scheduler_proc is None
