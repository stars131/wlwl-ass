"""Tests for BotManager.start crash-on-init detection.

When a bot's frontend has a SyntaxError / ImportError / missing config,
the subprocess exits within milliseconds. Pre-2026-05-17, BotManager
returned ``(True, "已启动")`` based purely on the spawn succeeding —
the actual crash was invisible until someone tailed the log file.

These tests pin the new behaviour: start() polls the process for up to
EARLY_DEATH_S seconds and surfaces the log tail + crash classification.
"""
from __future__ import annotations

import os
import sys
import textwrap
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_classify_crash_syntax_error():
    from launcher.bot_manager import BotManager
    msg = BotManager._classify_crash("Traceback...\nSyntaxError: invalid")
    assert "SyntaxError" in msg


def test_classify_crash_module_not_found():
    from launcher.bot_manager import BotManager
    msg = BotManager._classify_crash("ModuleNotFoundError: No module named 'foo'")
    assert "缺依赖" in msg or "依赖" in msg


def test_classify_crash_port_in_use():
    from launcher.bot_manager import BotManager
    msg = BotManager._classify_crash("OSError: [Errno 98] Address already in use")
    assert "端口" in msg or "占用" in msg


def test_classify_crash_empty():
    from launcher.bot_manager import BotManager
    msg = BotManager._classify_crash("")
    assert "无日志" in msg or "环境" in msg


def test_classify_crash_unknown():
    from launcher.bot_manager import BotManager
    msg = BotManager._classify_crash("KeyError: 'foo'\n random stuff")
    assert "未识别" in msg


def test_tail_log_handles_missing_file(tmp_path):
    from launcher.bot_manager import BotManager
    out = BotManager._tail_log(str(tmp_path / "nope.log"), 1000)
    assert "could not read" in out


def test_tail_log_returns_last_n_bytes(tmp_path):
    from launcher.bot_manager import BotManager
    path = tmp_path / "x.log"
    path.write_text("HEADER\n" + "x" * 2000 + "TAIL_MARKER", encoding="utf-8")
    out = BotManager._tail_log(str(path), 500)
    assert "TAIL_MARKER" in out
    assert "HEADER" not in out


def test_early_death_surfaces_in_start(tmp_path, monkeypatch):
    """A frontend that exits immediately must produce a False return from
    start() with the log tail in the message."""
    # Build a fake bot project: a tiny frontends/ dir with a script that
    # raises ImportError immediately.
    project = tmp_path
    (project / "frontends").mkdir()
    (project / "temp").mkdir()
    bad_script = project / "frontends" / "boom_app.py"
    bad_script.write_text(textwrap.dedent("""
        import sys
        print("about to crash", flush=True)
        raise ImportError("the test purposely raises this")
    """), encoding="utf-8")

    from launcher import bot_manager as bm
    # Inject a fake BOT_SPEC for our test bot
    from launcher.bot_manager import BotSpec
    fake_spec = BotSpec(
        key="boom", display_name="Boom",
        script="boom_app.py",
        mykey_fields=(),  # no config required
        sdk_modules=(),
        lock_port=None,
        log_filename="boom.log",
        auto_start=False,
    )
    monkeypatch.setitem(bm.BOT_SPECS, "boom", fake_spec)

    mgr = bm.BotManager(str(project))
    # Tighten EARLY_DEATH_S so the test stays fast — still well above
    # the 50ms Python startup overhead.
    monkeypatch.setattr(mgr, "EARLY_DEATH_S", 3.0)
    ok, msg = mgr.start("boom")
    assert ok is False
    assert "立刻退出" in msg or "退出" in msg
    assert "ImportError" in msg or "依赖" in msg or "缺依赖" in msg


def test_healthy_bot_doesnt_trigger_early_death(tmp_path, monkeypatch):
    """A frontend that stays alive for several seconds should pass start()."""
    project = tmp_path
    (project / "frontends").mkdir()
    (project / "temp").mkdir()
    good_script = project / "frontends" / "good_app.py"
    good_script.write_text(textwrap.dedent("""
        import time, sys
        print("healthy start", flush=True)
        time.sleep(30)
    """), encoding="utf-8")

    from launcher import bot_manager as bm
    from launcher.bot_manager import BotSpec
    fake_spec = BotSpec(
        key="good", display_name="Good",
        script="good_app.py",
        mykey_fields=(),
        sdk_modules=(),
        lock_port=None,
        log_filename="good.log",
        auto_start=False,
    )
    monkeypatch.setitem(bm.BOT_SPECS, "good", fake_spec)

    mgr = bm.BotManager(str(project))
    monkeypatch.setattr(mgr, "EARLY_DEATH_S", 0.5)  # short for fast test
    try:
        ok, msg = mgr.start("good")
        assert ok is True
        assert "已启动" in msg or "pid=" in msg
    finally:
        # Tear down the surviving subprocess.
        mgr.stop("good", timeout=2)
