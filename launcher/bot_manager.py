"""Centralised lifecycle + status detection for the 6 chat-platform bots.

Each bot frontend (`frontends/<key>app.py`) already self-protects with a
`ensure_single_instance(<port>)` lock — we treat that port as the canonical
"running" indicator. This module adds:

- BOT_SPECS: declarative metadata (display name, script, mykey fields, SDKs).
- BotManager: spawn / stop our own child procs, plus detect external instances
  via the lock port so the UI never shows two of the same bot as "running".

The launcher imports BOT_SPECS to render rows; tests can replace the runtime
helpers with fakes since everything goes through small, mockable seams.
"""
from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class BotSpec:
    key: str
    display_name: str
    script: str  # filename inside frontends/
    mykey_fields: tuple[str, ...]  # required keys in the merged credential view for "configured"
    sdk_modules: tuple[str, ...]   # required python imports for "sdk_installed"
    lock_port: int | None  # frontends/*app.py ensure_single_instance port; None if absent
    log_filename: str  # name under temp/ that the bot writes to


BOT_SPECS: dict[str, BotSpec] = {
    "tg": BotSpec(
        key="tg", display_name="Telegram", script="tgapp.py",
        mykey_fields=("tg_bot_token",), sdk_modules=("telegram",),
        lock_port=None, log_filename="tgapp.log",
    ),
    "qq": BotSpec(
        key="qq", display_name="QQ", script="qqapp.py",
        mykey_fields=("qq_app_id", "qq_app_secret"), sdk_modules=("botpy",),
        lock_port=19528, log_filename="qqapp.log",
    ),
    "feishu": BotSpec(
        key="feishu", display_name="飞书", script="fsapp.py",
        mykey_fields=("fs_app_id", "fs_app_secret"), sdk_modules=("lark_oapi",),
        lock_port=None, log_filename="fsapp.log",
    ),
    "wecom": BotSpec(
        key="wecom", display_name="企业微信", script="wecomapp.py",
        mykey_fields=("wecom_bot_id", "wecom_secret"), sdk_modules=("wecom_aibot_sdk",),
        lock_port=19529, log_filename="wecomapp.log",
    ),
    "dingtalk": BotSpec(
        key="dingtalk", display_name="钉钉", script="dingtalkapp.py",
        mykey_fields=("dingtalk_client_id", "dingtalk_client_secret"),
        sdk_modules=("dingtalk_stream",),
        lock_port=19530, log_filename="dingtalkapp.log",
    ),
    "wechat": BotSpec(
        key="wechat", display_name="微信", script="wechatapp.py",
        mykey_fields=(), sdk_modules=("Crypto", "qrcode"),
        lock_port=None, log_filename="wechatapp.log",
    ),
}


def _load_mykeys(base_dir: str) -> dict:
    """Return the merged credential dict for the bot status checks.

    Single-source-of-truth: delegates to :func:`llmcore.reload_mykeys`,
    which already merges ``.env`` / ``~/.wlwl-ass/config.json`` /
    ``temp/launcher_api_configs.json``. ``base_dir`` scopes the
    **per-project** file reads (project .wlwl-ass/config.json,
    temp/launcher_api_configs.json) — env vars and the
    user-layer ``~/.wlwl-ass/config.json`` are always global by design.

    Failures fall back to an empty dict so a missing/broken mykey doesn't
    block the bots tab from rendering "not configured" rows.
    """
    try:
        import llmcore
        return dict(llmcore.reload_mykeys(project_root=base_dir)[0])
    except Exception:
        return {}


def _sdk_available(modules: tuple[str, ...]) -> bool:
    for mod in modules:
        if importlib.util.find_spec(mod) is None:
            return False
    return True


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class BotStatus:
    key: str
    display_name: str
    configured: bool
    missing_fields: list[str] = field(default_factory=list)
    sdk_installed: bool = False
    missing_modules: list[str] = field(default_factory=list)
    running_self: bool = False     # we spawned the proc and it's alive
    running_external: bool = False  # someone else holds the lock port
    log_path: str = ""

    @property
    def running(self) -> bool:
        return self.running_self or self.running_external


class BotManager:
    """Spawn, stop, and inspect chat-platform bots from the Qt launcher."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.frontends_dir = os.path.join(base_dir, "frontends")
        self.temp_dir = os.path.join(base_dir, "temp")
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ── status ────────────────────────────────────────────────────────

    def status(self, key: str) -> BotStatus:
        spec = BOT_SPECS[key]
        keys = _load_mykeys(self.base_dir)
        missing_fields = [f for f in spec.mykey_fields
                          if not str(keys.get(f, "") or "").strip()]
        missing_modules = [m for m in spec.sdk_modules
                           if importlib.util.find_spec(m) is None]
        proc = self._procs.get(key)
        running_self = proc is not None and proc.poll() is None
        running_external = False
        if not running_self and spec.lock_port is not None:
            running_external = _port_in_use(spec.lock_port)
        return BotStatus(
            key=key,
            display_name=spec.display_name,
            configured=not missing_fields,
            missing_fields=missing_fields,
            sdk_installed=not missing_modules,
            missing_modules=missing_modules,
            running_self=running_self,
            running_external=running_external,
            log_path=os.path.join(self.temp_dir, spec.log_filename),
        )

    def status_all(self) -> dict[str, BotStatus]:
        return {k: self.status(k) for k in BOT_SPECS}

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self, key: str) -> tuple[bool, str]:
        spec = BOT_SPECS[key]
        st = self.status(key)
        if st.running_self:
            return True, "已在运行（本进程）"
        if st.running_external:
            return False, "外部进程占用单例锁端口，无法启动"
        if not st.configured:
            return False, f"缺少配置: {', '.join(st.missing_fields)}"
        if not st.sdk_installed:
            return False, f"缺少依赖: pip install {' '.join(st.missing_modules)}"
        with self._lock:
            try:
                proc = subprocess.Popen(
                    [sys.executable, os.path.join(self.frontends_dir, spec.script)],
                    cwd=self.base_dir,
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as exc:
                return False, f"启动失败: {exc}"
            self._procs[key] = proc
        # Best-effort: surface the bot in the central process registry too.
        try:
            from launcher.process_registry import get_registry
            get_registry(self.base_dir).register(
                f"bot:{key}",
                proc.pid,
                kind="bot",
                cmd=[sys.executable, spec.script],
                meta={"display_name": spec.display_name},
            )
        except Exception as exc:
            print(f"[BotManager] process_registry.register failed: {exc}")
        return True, f"已启动 (pid={proc.pid})"

    def stop(self, key: str, timeout: float = 5.0) -> tuple[bool, str]:
        with self._lock:
            proc = self._procs.get(key)
        if proc is None or proc.poll() is not None:
            self._procs.pop(key, None)
            return True, "未在运行"
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
        except Exception as exc:
            return False, f"停止失败: {exc}"
        finally:
            self._procs.pop(key, None)
            try:
                from launcher.process_registry import get_registry
                get_registry(self.base_dir).unregister_label(f"bot:{key}")
            except Exception:
                pass
        return True, "已停止"

    def stop_all(self) -> None:
        for key in list(self._procs):
            try:
                self.stop(key)
            except Exception:
                pass

    def detach_all(self) -> None:
        """Forget tracked procs without killing — bots keep running headless."""
        with self._lock:
            self._procs.clear()

    # ── helpers ───────────────────────────────────────────────────────

    def configured_keys(self) -> list[str]:
        return [k for k, st in self.status_all().items() if st.configured]

    def open_log(self, key: str) -> bool:
        spec = BOT_SPECS[key]
        path = os.path.join(self.temp_dir, spec.log_filename)
        if not os.path.isfile(path):
            return False
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception:
            return False
