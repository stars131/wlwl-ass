"""wlwl-ass launcher entry — Tauri GUI is the only supported interface.

Run:
  python launch.pyw                # 启动 Tauri GUI
                                   #   优先使用 gui/src-tauri/target/release/
                                   #   下的打包 binary；否则用 `npm run tauri:dev`。
                                   #   两者都不可用时直接报错退出（不再 fallback Qt）。

GUI 启动后，配套的 `launcher.api_server` 子进程由 Tauri 的 Rust shell 自己拉起，
api_server 启动时会自动起所有「凭据齐全 + SDK 装好」的 IM bot（飞书/TG/QQ/...
企微/钉钉/微信）以及 L4 scheduler。详见 launcher/api_server.py 中的
`_auto_start_configured_bots` 与 `_auto_start_scheduler`。
"""
import os
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LAUNCHER_LOCK_PORT = 19736  # singleton lock — 防止两个 launcher 同时跑

script_dir = os.path.dirname(os.path.abspath(__file__))
gui_dir = os.path.join(script_dir, "gui")


def acquire_singleton():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LAUNCHER_LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


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
    isn't available on this machine."""
    binary = _find_tauri_binary()
    if binary:
        print(f"[Launch] Starting Tauri binary: {binary}")
        return subprocess.call([binary])
    if _have_tauri_dev():
        print("[Launch] Starting Tauri dev (npm run tauri:dev)…")
        return subprocess.call(
            ["npm", "run", "tauri:dev"],
            cwd=gui_dir,
            shell=(os.name == "nt"),
        )
    return None


if __name__ == "__main__":
    lock = acquire_singleton()
    if lock is None:
        print("[Launch] Another launcher is already running.")
        sys.exit(0)

    rc = run_tauri()
    if rc is None:
        print(
            "[Launch] Tauri GUI is not available — no packaged binary at "
            "gui/src-tauri/target/release/, and node/npm + gui/package.json "
            "are missing for dev mode. Run start_from_zero.cmd to bootstrap "
            "the toolchain, or install Node.js >=20.9 and Rust cargo.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(rc)
