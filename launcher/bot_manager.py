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
import re
import socket
import subprocess
import sys
import threading
import time
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
    auto_start: bool = True  # whether GUI launch should start it automatically


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
        lock_port=19532, log_filename="fsapp.log",
    ),
    "feishu_concierge": BotSpec(
        key="feishu_concierge", display_name="飞书·小秘书",
        script="fsapp_concierge.py",
        # owner_open_id is soft — escalate worker auto-degrades to JSONL stub
        # when it's absent. Only the two app credentials are hard requirements
        # for the WS loop to even open. This matches the owner bot's posture
        # (allowed_users / system_prompt are also not in mykey_fields).
        mykey_fields=("fs_concierge_app_id", "fs_concierge_app_secret"),
        sdk_modules=("lark_oapi",),
        lock_port=19533, log_filename="fsapp_concierge.log",
        # Phase 2 has shipped; auto-start when configured + SDK installed,
        # same posture as the owner feishu bot.
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
        lock_port=19531, log_filename="wechatapp.log", auto_start=False,
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


def _find_port_holder_pid(port: int) -> int | None:
    """Best-effort discovery of the PID owning the LISTEN socket at <port>.

    Returns the PID if found, None otherwise. All branches swallow their
    own errors so this helper never raises.

    Why this exists: when api_server restarts, its in-memory ``self._procs``
    is wiped but the bot subprocesses it spawned keep running (they don't
    share a process group on Windows). The lock port (`19528..19532`) stays
    held by an orphaned PID we no longer track. Without knowing that PID we
    can neither adopt the orphan nor kill it. ``_port_in_use`` only answers
    "is the port reachable" — not "who owns it".

    Tries (in order): psutil if installed → netstat -ano (Windows) →
    lsof / ss (POSIX)."""
    if not port:
        return None
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status != psutil.CONN_LISTEN:
                continue
            if not c.laddr or c.laddr.port != port:
                continue
            if c.pid:
                return int(c.pid)
    except Exception:
        pass

    if os.name == "nt":
        try:
            r = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            for line in r.stdout.splitlines():
                if "LISTENING" not in line:
                    continue
                parts = line.split()
                # netstat row: "  TCP    127.0.0.1:19532   0.0.0.0:0   LISTENING   23876"
                if len(parts) < 5:
                    continue
                local = parts[1]
                if not (local.endswith(f":{port}") or local.endswith(f"]:{port}")):
                    continue
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
        except Exception:
            pass
        return None

    # POSIX fallbacks.
    try:
        r = subprocess.run(
            ["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            first = (r.stdout or "").strip().splitlines()
            if first:
                try:
                    return int(first[0])
                except ValueError:
                    pass
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if f":{port}" not in line:
                    continue
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def _kill_pid(pid: int, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Force-kill a PID cross-platform. No-op + ok=True if already dead."""
    if not pid or pid <= 0:
        return True, "no pid"
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            # taskkill rc=128 means "no such process" — treat as success.
            ok = r.returncode in (0, 128)
            return ok, f"taskkill rc={r.returncode}"
        import signal as _sig
        try:
            os.kill(pid, _sig.SIGTERM)
        except ProcessLookupError:
            return True, "no such process"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True, "terminated"
            except OSError:
                return True, "gone"
            time.sleep(0.1)
        try:
            os.kill(pid, _sig.SIGKILL)
        except ProcessLookupError:
            pass
        return True, "killed"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _registered_alive(base_dir: str, key: str) -> bool:
    try:
        from launcher.process_registry import get_registry
        reg = get_registry(base_dir)
        reg.cleanup_dead()
        return any(bool(row.get("alive")) for row in reg.get_by_label(f"bot:{key}"))
    except Exception:
        return False


def _read_llm_binding(base_dir: str, key: str) -> str:
    """Read ``config_store.bots.<key>.llm_binding`` for a fresh spawn.

    Lives here (rather than inlined into ``start()``) so tests can monkeypatch
    the lookup. Failures are swallowed — a missing config_store should never
    block a bot from starting; the bot just runs with default LLM selection.
    """
    try:
        from launcher.config_store import default_store
        return str(default_store().get_bot(key).get("llm_binding") or "").strip()
    except Exception as exc:
        print(f"[BotManager] _read_llm_binding({key}) failed: {exc!r}")
        return ""


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
    lock_holder_pid: int | None = None  # PID owning lock_port if discoverable
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
        lock_holder_pid: int | None = None
        if not running_self and spec.lock_port is not None:
            running_external = _port_in_use(spec.lock_port)
            if running_external:
                lock_holder_pid = _find_port_holder_pid(spec.lock_port)
        if not running_self and not running_external:
            running_external = _registered_alive(self.base_dir, key)
        return BotStatus(
            key=key,
            display_name=spec.display_name,
            configured=not missing_fields,
            missing_fields=missing_fields,
            sdk_installed=not missing_modules,
            missing_modules=missing_modules,
            running_self=running_self,
            running_external=running_external,
            lock_holder_pid=lock_holder_pid,
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
            # An orphan from a previous api_server lifetime is holding the
            # lock port. Adopting it into process_registry lets later
            # stop() / status() calls find and manage it instead of trying
            # to spawn a duplicate that would immediately self-exit.
            pid = st.lock_holder_pid
            if pid:
                try:
                    from launcher.process_registry import get_registry
                    reg = get_registry(self.base_dir)
                    reg.unregister_label(f"bot:{key}")
                    reg.register(
                        f"bot:{key}",
                        pid,
                        kind="bot",
                        cmd=[sys.executable, spec.script],
                        meta={"display_name": spec.display_name, "external": True},
                    )
                except Exception as exc:
                    print(f"[BotManager] adopt external pid={pid} failed: {exc}")
                return True, f"已在运行（外部进程 pid={pid}，已接管）"
            return (
                False,
                f"外部进程占用单例锁端口 {spec.lock_port} 但找不到 PID — "
                f"请手动 taskkill 占用 {spec.lock_port} 的进程，或调用 restart"
            )
        if not st.configured:
            return False, f"缺少配置: {', '.join(st.missing_fields)}"
        if not st.sdk_installed:
            return False, f"缺少依赖: pip install {' '.join(st.missing_modules)}"
        with self._lock:
            os.makedirs(self.temp_dir, exist_ok=True)
            log_path = os.path.join(self.temp_dir, spec.log_filename)
            log_f = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            log_f.write(f"\n\n=== spawn {key} pid=pending ===\n")
            try:
                # Inject WLWL_BOT_KEY so the spawned bot knows its own
                # identity — used by do_process kill-guard to refuse
                # self-suicide ("agent killed bot:feishu while it WAS
                # bot:feishu" — see fsapp.log 2026-05-17 turn 23 incident).
                child_env = os.environ.copy()
                child_env["WLWL_BOT_KEY"] = key
                # Per-bot LLM binding (ADR-pending 2026-05-19). Empty/missing
                # = keep current "first config wins" default. Format:
                # "config:<name>" or "profile:<name>". Only feishu bots honor
                # it today; other bots will simply ignore the env var.
                binding = _read_llm_binding(self.base_dir, key)
                if binding:
                    child_env["WLWL_BOT_LLM_BINDING"] = binding
                    log_f.write(f"=== llm_binding={binding!r} ===\n")
                proc = subprocess.Popen(
                    [sys.executable, os.path.join(self.frontends_dir, spec.script)],
                    cwd=self.base_dir,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as exc:
                log_f.close()
                return False, f"启动失败: {exc}"
            log_f.write(f"=== spawned {key} pid={proc.pid} ===\n")
            self._procs[key] = proc
        # Best-effort: surface the bot in the central process registry too.
        try:
            from launcher.process_registry import get_registry
            get_registry(self.base_dir).unregister_label(f"bot:{key}")
            get_registry(self.base_dir).register(
                f"bot:{key}",
                proc.pid,
                kind="bot",
                cmd=[sys.executable, spec.script],
                meta={"display_name": spec.display_name},
            )
        except Exception as exc:
            print(f"[BotManager] process_registry.register failed: {exc}")

        # Crash-on-init detection. If the process exits within EARLY_DEATH_S
        # seconds, it almost certainly hit an ImportError / SyntaxError /
        # missing-config before reaching the long-conn loop. Surface that
        # back to the caller (GUI / CLI) with the last few log lines, so
        # the user doesn't have to dig through temp/*.log to find why
        # "started OK" actually means "died 200ms later".
        early_msg = self._poll_early_death(key, proc, log_path)
        if early_msg:
            with self._lock:
                self._procs.pop(key, None)
            return False, early_msg

        return True, f"已启动 (pid={proc.pid})"

    EARLY_DEATH_S = 2.0
    LOG_TAIL_BYTES = 4_000

    def _poll_early_death(self, key: str, proc: "subprocess.Popen",
                          log_path: str) -> str:
        """Wait up to EARLY_DEATH_S for the process to die. Returns an
        empty string if still alive; otherwise a diagnostic string."""
        deadline = time.monotonic() + self.EARLY_DEATH_S
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                tail = self._tail_log(log_path, self.LOG_TAIL_BYTES)
                hint = self._classify_crash(tail)
                msg = (
                    f"启动后立刻退出 (pid={proc.pid}, rc={rc}, "
                    f"运行 <{self.EARLY_DEATH_S:.1f}s) — {hint}\n"
                    f"--- 最近日志 ({log_path}) ---\n{tail}"
                )
                return msg
            time.sleep(0.05)
        return ""

    @staticmethod
    def _tail_log(log_path: str, n_bytes: int) -> str:
        try:
            size = os.path.getsize(log_path)
            with open(log_path, "rb") as f:
                if size > n_bytes:
                    f.seek(size - n_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            return f"<could not read log: {exc!r}>"

    @staticmethod
    def _classify_crash(tail: str) -> str:
        """Return a one-line hint based on the log tail. The 2026-05-17
        tokenjuice incident is the canonical example: agent restarted
        itself, restart hit a SyntaxError, no obvious symptom in the GUI.
        """
        if not tail:
            return "无日志输出，可能是 SDK 未安装或环境损坏"
        if "SyntaxError" in tail:
            return "Python SyntaxError — 检查最近 git diff 或运行 `python -m launcher.preflight`"
        if "ModuleNotFoundError" in tail or "ImportError" in tail:
            return "缺依赖 — 检查 `pip list` 或 BotSpec.sdk_modules"
        if "PermissionError" in tail:
            return "文件权限错误 — 检查日志/配置路径写权限"
        if "Address already in use" in tail or "OSError: [WinError 10048]" in tail:
            return "端口已被占用 — 用 `bot_manager.restart` 强制接管"
        if "Connection refused" in tail or "ConnectionError" in tail:
            return "外部连接失败 — 检查 Feishu/网络可达性"
        return "未识别的早期退出 — 查看完整日志"

    def stop(self, key: str, timeout: float = 5.0) -> tuple[bool, str]:
        """Stop a bot — owned subprocess, registry entry, or external orphan.

        Tries three sources in order: (1) Popen handle in ``self._procs``,
        (2) PIDs in ``process_registry.json`` labelled ``bot:<key>``,
        (3) whoever currently owns the lock port. The lock-port fallback
        is what unblocks the orphan-PID-from-previous-api_server case."""
        spec = BOT_SPECS[key]
        messages: list[str] = []

        with self._lock:
            proc = self._procs.pop(key, None)

        # (1) Our tracked subprocess, if alive.
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    try:
                        proc.wait(timeout=timeout)
                    except Exception:
                        pass
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=timeout)
                messages.append(f"tracked pid={proc.pid} stopped")
            except Exception as exc:
                return False, f"停止失败: {exc}"

        # (2) Anything labelled bot:<key> in the registry (e.g. orphans
        #     adopted by a previous start() call).
        try:
            from launcher.process_registry import get_registry
            reg = get_registry(self.base_dir)
            for row in reg.get_by_label(f"bot:{key}"):
                pid = int(row.get("pid") or 0)
                if pid and (proc is None or pid != proc.pid):
                    ok, msg = _kill_pid(pid, timeout=timeout)
                    messages.append(f"registry pid={pid}: {msg}")
            reg.unregister_label(f"bot:{key}")
        except Exception as exc:
            messages.append(f"registry cleanup failed: {exc}")

        # (3) Whoever currently owns the lock port (orphan no api_server
        #     instance ever tracked).
        if spec.lock_port is not None:
            holder = _find_port_holder_pid(spec.lock_port)
            if holder and (proc is None or holder != proc.pid):
                ok, msg = _kill_pid(holder, timeout=timeout)
                messages.append(f"lock-port pid={holder}: {msg}")
            elif not messages and _port_in_use(spec.lock_port):
                return (
                    False,
                    f"单例锁端口 {spec.lock_port} 被占用，但无法识别 PID；"
                    "请手动释放该端口后重试",
                )

        # Wait briefly for the lock port to actually release so a follow-up
        # start() doesn't race the kernel's TIME_WAIT.
        if spec.lock_port is not None:
            deadline = time.monotonic() + min(timeout, 3.0)
            while time.monotonic() < deadline:
                if not _port_in_use(spec.lock_port):
                    break
                time.sleep(0.1)
            if _port_in_use(spec.lock_port):
                return (
                    False,
                    f"单例锁端口 {spec.lock_port} 仍被占用；"
                    "孤儿进程可能无法自动清理",
                )

        if not messages:
            return True, "未在运行"
        return True, "; ".join(messages)

    def restart(self, key: str, timeout: float = 5.0) -> tuple[bool, str]:
        """Stop the bot (including orphans + external holders) then start.

        Use this when ``start()`` returns "外部进程占用单例锁端口" — the
        previous instance was an orphan from a prior api_server lifetime
        and the user wants a clean managed restart."""
        stop_ok, stop_msg = self.stop(key, timeout=timeout)
        if not stop_ok:
            return False, f"重启-stop 阶段失败: {stop_msg}"
        start_ok, start_msg = self.start(key)
        prefix = f"[stop] {stop_msg} | [start] "
        return start_ok, prefix + start_msg

    def stop_all(self) -> None:
        for key in list(BOT_SPECS):
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
