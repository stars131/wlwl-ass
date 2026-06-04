"""Tests for the orphan-bot recovery path in launcher/bot_manager.py.

Scenario these guard:
  * api_server crashes / restarts. Its in-memory ``BotManager._procs``
    is empty, but the fsapp.py / qqapp.py subprocess it spawned earlier
    is still alive and still holds the singleton lock port.
  * The new BotManager must (a) detect the orphan with a PID via
    ``_find_port_holder_pid``, (b) adopt it on ``start()`` instead of
    spawning a doomed duplicate, (c) kill it on ``stop()`` even with no
    Popen handle, (d) recover cleanly via ``restart()``.

We avoid real subprocesses — every external interaction goes through
monkeypatch seams in the module, so tests stay fast and OS-independent.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from launcher import bot_manager


# ── _find_port_holder_pid ────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_find_port_holder_pid_returns_none_for_free_port():
    port = _free_port()
    # Right after the socket above is closed, nothing should hold the port.
    assert bot_manager._find_port_holder_pid(port) is None


def test_find_port_holder_pid_returns_current_pid_for_listening_socket():
    """The PID returned for a LISTENing socket on this process is our own."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        pid = bot_manager._find_port_holder_pid(port)
        # On some sandboxed CI runners netstat/lsof are blocked — accept
        # None there, but the discovery must NEVER return a wrong PID.
        assert pid is None or pid == os.getpid()
    finally:
        s.close()


# ── BotManager.status surfaces lock_holder_pid ───────────────────────────


def _fake_spec(monkeypatch, *, lock_port=19999):
    spec = bot_manager.BotSpec(
        key="testbot",
        display_name="TestBot",
        script="nonexistent.py",
        mykey_fields=(),
        sdk_modules=(),
        lock_port=lock_port,
        log_filename="testbot.log",
    )
    monkeypatch.setitem(bot_manager.BOT_SPECS, "testbot", spec)
    return spec


def test_status_populates_lock_holder_pid_when_port_held(monkeypatch, tmp_path):
    spec = _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: port == 19999)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid",
                        lambda port: 23876 if port == 19999 else None)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {})

    bm = bot_manager.BotManager(str(tmp_path))
    st = bm.status("testbot")
    assert st.running_external is True
    assert st.running_self is False
    assert st.lock_holder_pid == 23876


def test_status_no_pid_when_port_free(monkeypatch, tmp_path):
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: False)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid",
                        lambda port: pytest.fail("should not be called when port free"))
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {})
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda base, key: False)

    bm = bot_manager.BotManager(str(tmp_path))
    st = bm.status("testbot")
    assert st.lock_holder_pid is None
    assert st.running_external is False


# ── start() adopts orphan instead of spawning ────────────────────────────


def test_start_adopts_orphan_external_pid(monkeypatch, tmp_path):
    """When the lock port is held by a known PID, start() must NOT spawn
    a doomed duplicate. It should register that PID and report success."""
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: True)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", lambda port: 23876)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {})
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda base, key: False)

    spawned: list = []
    monkeypatch.setattr(
        bot_manager.subprocess, "Popen",
        lambda *a, **kw: spawned.append((a, kw)) or pytest.fail("should not spawn"),
    )

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.start("testbot")
    assert ok is True
    assert "23876" in msg
    assert spawned == []


def test_start_refuses_when_external_pid_undiscoverable(monkeypatch, tmp_path):
    spec = _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: True)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", lambda port: None)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {})
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda base, key: False)

    monkeypatch.setattr(
        bot_manager.subprocess, "Popen",
        lambda *a, **kw: pytest.fail("should not spawn when external holder unknown"),
    )

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.start("testbot")
    assert ok is False
    assert "restart" in msg.lower() or "taskkill" in msg.lower()


# ── stop() kills external orphan via lock-port discovery ─────────────────


def test_stop_kills_external_lock_port_holder(monkeypatch, tmp_path):
    """stop() must kill whoever holds the lock port even when BotManager
    has no Popen handle and no registry entry."""
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: False)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid",
                        lambda port: 23876 if port == 19999 else None)

    killed: list[int] = []

    def fake_kill(pid, *, timeout=5.0):
        killed.append(pid)
        return True, "killed"

    monkeypatch.setattr(bot_manager, "_kill_pid", fake_kill)

    class _FakeRegistry:
        def get_by_label(self, label): return []
        def unregister_label(self, label): pass

    import launcher.process_registry as proc_reg
    monkeypatch.setattr(proc_reg, "get_registry", lambda base: _FakeRegistry())

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.stop("testbot")
    assert ok is True
    assert 23876 in killed
    assert "23876" in msg


def test_stop_kills_registered_orphans(monkeypatch, tmp_path):
    """If process_registry has an entry for bot:<key> not matching our
    Popen handle, stop() must kill that PID too."""
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: False)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", lambda port: None)

    killed: list[int] = []

    def fake_kill(pid, *, timeout=5.0):
        killed.append(pid)
        return True, "killed"

    monkeypatch.setattr(bot_manager, "_kill_pid", fake_kill)

    class _FakeRegistry:
        def __init__(self):
            self.unregistered = []
        def get_by_label(self, label):
            return [{"label": label, "pid": 99999}]
        def unregister_label(self, label):
            self.unregistered.append(label)

    fake_reg = _FakeRegistry()
    import launcher.process_registry as proc_reg
    monkeypatch.setattr(proc_reg, "get_registry", lambda base: fake_reg)

    bm = bot_manager.BotManager(str(tmp_path))
    ok, _ = bm.stop("testbot")
    assert ok is True
    assert 99999 in killed
    assert "bot:testbot" in fake_reg.unregistered


def test_stop_idempotent_when_nothing_running(monkeypatch, tmp_path):
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: False)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", lambda port: None)
    monkeypatch.setattr(bot_manager, "_kill_pid",
                        lambda pid, **k: pytest.fail("nothing to kill"))

    class _FakeRegistry:
        def get_by_label(self, label): return []
        def unregister_label(self, label): pass

    import launcher.process_registry as proc_reg
    monkeypatch.setattr(proc_reg, "get_registry", lambda base: _FakeRegistry())

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.stop("testbot")
    assert ok is True
    assert msg == "未在运行"


def test_stop_reports_unknown_external_lock_holder(monkeypatch, tmp_path):
    _fake_spec(monkeypatch, lock_port=19999)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", lambda port: None)
    monkeypatch.setattr(bot_manager, "_port_in_use", lambda port, **k: True)
    monkeypatch.setattr(bot_manager, "_kill_pid",
                        lambda pid, **k: pytest.fail("unknown pid cannot be killed"))

    class _FakeRegistry:
        def get_by_label(self, label): return []
        def unregister_label(self, label): pass

    import launcher.process_registry as proc_reg
    monkeypatch.setattr(proc_reg, "get_registry", lambda base: _FakeRegistry())

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.stop("testbot")
    assert ok is False
    assert "19999" in msg
    assert "PID" in msg


# ── restart() unblocks the orphan-from-previous-lifetime case ────────────


def test_restart_chains_stop_then_start(monkeypatch, tmp_path):
    """restart() must kill the orphan via stop(), wait for the port to
    release, then spawn a fresh subprocess via start()."""
    _fake_spec(monkeypatch, lock_port=19999)

    port_state = {"held": True, "holder_pid": 23876}

    def fake_port_in_use(port, **k):
        return port_state["held"]

    def fake_find_pid(port):
        return port_state["holder_pid"] if port_state["held"] else None

    def fake_kill(pid, *, timeout=5.0):
        # killing the holder releases the port
        port_state["held"] = False
        port_state["holder_pid"] = None
        return True, "killed"

    monkeypatch.setattr(bot_manager, "_port_in_use", fake_port_in_use)
    monkeypatch.setattr(bot_manager, "_find_port_holder_pid", fake_find_pid)
    monkeypatch.setattr(bot_manager, "_kill_pid", fake_kill)
    monkeypatch.setattr(bot_manager, "_load_mykeys", lambda base: {"fs_app_id": "x"})
    monkeypatch.setattr(bot_manager, "_registered_alive", lambda base, key: False)
    # Pretend SDK is "installed" by leaving sdk_modules empty in _fake_spec.

    class _FakeRegistry:
        def __init__(self):
            self.registered = []
        def get_by_label(self, label): return []
        def unregister_label(self, label): pass
        def register(self, label, pid, **k):
            self.registered.append((label, pid))
            return {"label": label, "pid": pid}

    fake_reg = _FakeRegistry()
    import launcher.process_registry as proc_reg
    monkeypatch.setattr(proc_reg, "get_registry", lambda base: fake_reg)

    class _FakeProc:
        pid = 55555
        def poll(self): return None

    spawned: list = []
    def fake_popen(*a, **kw):
        spawned.append((a, kw))
        return _FakeProc()
    monkeypatch.setattr(bot_manager.subprocess, "Popen", fake_popen)

    # Need the frontends dir to exist so we can write the log file.
    os.makedirs(os.path.join(str(tmp_path), "temp"), exist_ok=True)
    os.makedirs(os.path.join(str(tmp_path), "frontends"), exist_ok=True)

    bm = bot_manager.BotManager(str(tmp_path))
    ok, msg = bm.restart("testbot")
    assert ok is True, msg
    # stop killed the orphan
    assert port_state["holder_pid"] is None
    # start then spawned a fresh subprocess
    assert len(spawned) == 1
    # The new pid is registered
    assert ("bot:testbot", 55555) in fake_reg.registered


# ── _kill_pid edge cases ─────────────────────────────────────────────────


def test_kill_pid_returns_ok_for_zero():
    ok, msg = bot_manager._kill_pid(0)
    assert ok is True
    assert "no pid" in msg.lower()


def test_kill_pid_returns_ok_for_negative():
    ok, msg = bot_manager._kill_pid(-1)
    assert ok is True
