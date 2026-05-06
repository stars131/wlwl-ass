"""wlwl-ass launcher entry — Tauri GUI by default, Qt and webview as fallbacks.

Phase 3 cutover (2026-05-02):
  python launch.pyw                     # 默认: 新 Tauri GUI（gui/）
                                        #   if a packaged binary or
                                        #   `npm run tauri:dev` is available.
                                        #   Falls back to Qt automatically.
  python launch.pyw --qt-legacy         # 强制 Qt main window (PySide6)
  python launch.pyw --legacy-shell      # 旧的 webview + Streamlit 流
  python launch.pyw --feishu --tg ...   # 启动时自动开 bot（写入 launcher_options.json）

The Tauri shell spawns its own Python `launcher.api_server` subprocess.
This script's only Tauri responsibility is locating the binary or the dev
command and exec-ing it.
"""
import argparse
import atexit
import ctypes
import importlib.util
import os
import random
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from launcher.project_manager import ProjectManager
from launcher.shell_server import serve as serve_shell
from launcher.launch_config import load_options, project_options, save_options

WINDOW_WIDTH, WINDOW_HEIGHT, RIGHT_PADDING, TOP_PADDING = 820, 900, 0, 100

script_dir = os.path.dirname(os.path.abspath(__file__))
frontends_dir = os.path.join(script_dir, "frontends")
gui_dir = os.path.join(script_dir, "gui")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LAUNCHER_LOCK_PORT = 19736  # singleton lock shared by all paths

window = None
pm = None


def find_free_port(lo=18400, hi=18499):
    """Free port for the shell HTTP server (separate range from project streamlits)."""
    ports = list(range(lo, hi + 1)); random.shuffle(ports)
    for port in ports:
        try:
            sock = socket.socket(); sock.bind(("127.0.0.1", port)); sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in {lo}-{hi}")


def get_screen_width():
    try: return ctypes.windll.user32.GetSystemMetrics(0)
    except Exception: return 1920


def acquire_singleton():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try: s.bind(("127.0.0.1", LAUNCHER_LOCK_PORT)); s.listen(1); return s
    except OSError: return None


def get_feishu_startup_status():
    """Legacy helper for --legacy-shell path; Qt path uses BotManager instead.

    Reads the merged credential view through llmcore so this is the same
    source-of-truth as everything else (no separate runpy parser).
    """
    try:
        import llmcore
        keys = llmcore.reload_mykeys(project_root=script_dir)[0]
    except Exception as exc:
        return False, f"config load failed: {exc}"
    app_id = str(keys.get("fs_app_id", "") or "").strip()
    app_secret = str(keys.get("fs_app_secret", "") or "").strip()
    if not app_id or not app_secret: return False, "fs_app_id/fs_app_secret not configured"
    if importlib.util.find_spec("lark_oapi") is None: return False, "lark_oapi not installed"
    return True, "configured"


def spawn_background(script_name):
    process = subprocess.Popen(
        [sys.executable, os.path.join(frontends_dir, script_name)],
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    atexit.register(process.kill)
    return process


def on_closing():
    """Called when the user clicks the close button (legacy webview shell only)."""
    if pm is None: return True
    running = [p for p in pm.list()["projects"] if p["running"]]
    if not running:
        return True
    msg = (f"有 {len(running)} 个项目正在后台运行：\n  "
           + "\n  ".join(p["name"] for p in running)
           + "\n\n确定 → 保留后台继续运行\n取消 → 全部停止后退出")
    keep = False
    try:
        keep = bool(window.evaluate_js(f"confirm({repr(msg)})"))
    except Exception as e:
        print(f"[Launch] close-confirm dialog failed, defaulting to keep: {e}")
        keep = True
    if keep:
        pm.detach_all()
        print(f"[Launch] {len(running)} project(s) detached, still running in background")
    else:
        pm.shutdown_all()
        print("[Launch] all projects stopped")
    return True


def _apply_cli_overrides(args):
    launch_options = load_options(script_dir)
    cli_overrides = {}
    for key in ("tg", "qq", "feishu", "wecom", "dingtalk", "wechat"):
        if getattr(args, key, False):
            cli_overrides[key] = True
    if args.sched is not None:
        cli_overrides["scheduler"] = args.sched
    if args.llm_no is not None:
        cli_overrides["llm_no"] = args.llm_no
    if cli_overrides:
        launch_options = save_options(script_dir, {**launch_options, **cli_overrides})
    return launch_options


# ─── Tauri default path ───────────────────────────────────────────────


def _find_tauri_binary():
    """Look for a packaged Tauri build under gui/src-tauri/target/release/.

    Returns the executable path or None. Bundle layout differs per platform
    so we look at the folders Tauri actually emits.
    """
    base = os.path.join(gui_dir, "src-tauri", "target", "release")
    candidates = []
    if os.name == "nt":
        candidates.append(os.path.join(base, "wlwl-ass-gui.exe"))
        candidates.append(os.path.join(base, "wlwl-ass.exe"))
    elif sys.platform == "darwin":
        candidates.append(os.path.join(
            base, "bundle", "macos", "wlwl-ass.app", "Contents", "MacOS", "wlwl-ass"
        ))
        candidates.append(os.path.join(base, "wlwl-ass-gui"))
    else:
        candidates.append(os.path.join(base, "wlwl-ass-gui"))
        candidates.append(os.path.join(base, "wlwl-ass"))
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _have_tauri_dev():
    if not os.path.isdir(gui_dir):
        return False
    if shutil.which("npm") is None:
        return False
    return os.path.isfile(os.path.join(gui_dir, "package.json"))


def run_tauri():
    """Try to start the Tauri GUI. Returns the exit code, or None if Tauri
    isn't available on this machine (caller should fall back to Qt)."""
    binary = _find_tauri_binary()
    if binary:
        print(f"[Launch] Starting Tauri binary: {binary}")
        return subprocess.call([binary])
    if _have_tauri_dev():
        print("[Launch] Starting Tauri dev (npm run tauri:dev)…")
        return subprocess.call(["npm", "run", "tauri:dev"], cwd=gui_dir, shell=(os.name == "nt"))
    print("[Launch] Tauri GUI not found (no packaged binary, no node/npm). Falling back to Qt.")
    return None


def run_qt(launch_options):
    """Qt launcher (PySide6); BotManager auto-starts bots from launch_options."""
    from launcher.qt_launcher import main as qt_main
    return qt_main()


def run_legacy_shell(launch_options):
    """Webview + Streamlit default-session flow (kept for backward compat)."""
    global pm, window
    pm = ProjectManager(script_dir)

    if not pm.projects:
        print("[Launch] First run — creating default project")
        active = pm.create("默认对话", auto_start=False)
        try:
            pm.update_options(active["id"], project_options(launch_options))
            pm.start(active["id"])
        except Exception as exc:
            print(f"[Launch] {exc}")
    else:
        active = next((p for p in pm.projects if p["id"] == pm.active_id), None) or pm.projects[0]
        if not pm.is_running(active):
            print(f"[Launch] Auto-starting last active project: {active['name']}")
            try:
                pm.start(active["id"])
            except Exception as exc:
                print(f"[Launch] {exc}")

    shell_port = find_free_port()
    serve_shell(pm, shell_port, script_dir)
    print(f"[Launch] Shell on http://127.0.0.1:{shell_port}/")

    if launch_options.get("tg"): spawn_background("tgapp.py"); print("[Launch] Telegram Bot started")
    if launch_options.get("qq"): spawn_background("qqapp.py"); print("[Launch] QQ Bot started")
    feishu_ready, feishu_reason = get_feishu_startup_status()
    if launch_options.get("feishu") and feishu_ready:
        spawn_background("fsapp.py"); print("[Launch] Feishu Bot started")
    elif launch_options.get("feishu"):
        print(f"[Launch] Feishu Bot requested but not started: {feishu_reason}")
    if launch_options.get("wecom"): spawn_background("wecomapp.py"); print("[Launch] WeCom Bot started")
    if launch_options.get("dingtalk"): spawn_background("dingtalkapp.py"); print("[Launch] DingTalk Bot started")
    if launch_options.get("wechat"): spawn_background("wechatapp.py"); print("[Launch] WeChat Bot started")

    if launch_options.get("scheduler", True):
        scheduler_proc = subprocess.Popen(
            [sys.executable, os.path.join(script_dir, "agentmain.py"),
             "--reflect", os.path.join(script_dir, "reflect", "scheduler.py"),
             "--llm_no", str(launch_options.get("llm_no", 0))],
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        atexit.register(scheduler_proc.kill)
        print("[Launch] Task Scheduler started")

    if os.name == "nt":
        x_pos = get_screen_width() - WINDOW_WIDTH - RIGHT_PADDING
    else:
        x_pos = 100

    import webview
    window = webview.create_window(
        title="wlwl-ass",
        url=f"http://127.0.0.1:{shell_port}/",
        width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
        x=x_pos, y=TOP_PADDING,
        resizable=True, text_select=True,
    )
    window.events.closing += on_closing
    webview.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="wlwl-ass launcher (Tauri GUI by default).",
    )
    parser.add_argument("port", nargs="?", default="0", help=argparse.SUPPRESS)
    parser.add_argument("--tg", action="store_true", help="Start Telegram bot at launch")
    parser.add_argument("--qq", action="store_true", help="Start QQ bot at launch")
    parser.set_defaults(feishu=False)
    parser.add_argument("--feishu", "--fs", dest="feishu", action="store_true",
                        help="Start Feishu bot at launch")
    parser.add_argument("--no-feishu", dest="feishu", action="store_false",
                        help="Disable Feishu bot at launch")
    parser.add_argument("--wecom", action="store_true", help="Start WeCom bot at launch")
    parser.add_argument("--dingtalk", "--dt", dest="dingtalk", action="store_true",
                        help="Start DingTalk bot at launch")
    parser.add_argument("--wechat", action="store_true", help="Start personal WeChat bot at launch")
    parser.set_defaults(sched=None)
    parser.add_argument("--sched", dest="sched", action="store_true",
                        help="Enable L4 scheduler")
    parser.add_argument("--no-sched", dest="sched", action="store_false",
                        help="Disable L4 scheduler")
    parser.add_argument("--llm_no", type=int, default=None,
                        help="Default LLM index (saved to launcher_options.json)")
    parser.add_argument("--qt-legacy", "--qt", dest="qt_legacy", action="store_true",
                        help="Force the previous Qt main window (PySide6)")
    parser.add_argument("--legacy-shell", action="store_true",
                        help="Use the original webview + Streamlit default-session shell")
    args = parser.parse_args()

    launch_options = _apply_cli_overrides(args)

    lock = acquire_singleton()
    if lock is None:
        print("[Launch] Another launcher is already running.")
        sys.exit(0)

    if args.legacy_shell:
        run_legacy_shell(launch_options)
        sys.exit(0)

    if args.qt_legacy:
        sys.exit(run_qt(launch_options))

    # Default: try Tauri, fall back to Qt.
    rc = run_tauri()
    if rc is not None:
        sys.exit(rc)
    sys.exit(run_qt(launch_options))
