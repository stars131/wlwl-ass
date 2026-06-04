"""wlwl-ass web launcher.

One-click startup now uses the browser UI only:
  1. start launcher.api_server
  2. start the Vite React frontend in gui/
  3. open the browser to the web UI

The optional desktop shell is intentionally bypassed. The gui/ source tree is
reused as the browser frontend.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / "gui"
TEMP_DIR = ROOT / "temp"
STATE_PATH = TEMP_DIR / "web-launcher.json"
LAUNCHER_LOCK_PORT = 19736
DEFAULT_API_PORT = int(os.environ.get("WLWL_API_PORT", "18800") or "18800")
DEFAULT_WEB_PORT = int(os.environ.get("WLWL_WEB_PORT", "1420") or "1420")
READY_TIMEOUT_S = 60.0

sys.path.insert(0, str(ROOT))


def acquire_singleton() -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LAUNCHER_LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def _url_ok(url: str, timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 500
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _api_healthy(port: int) -> bool:
    return _url_ok(f"http://127.0.0.1:{port}/api/health")


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_npm() -> str | None:
    if os.name == "nt":
        for name in ("npm.cmd", "npm.CMD", "npm.exe", "npm"):
            found = shutil.which(name)
            if found:
                return found
    return shutil.which("npm")


def _wait_until(name: str, predicate, timeout_s: float = READY_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not become ready within {timeout_s:.0f}s")


def _pump_output(proc: subprocess.Popen, label: str) -> None:
    stream = proc.stdout
    if stream is None:
        return
    try:
        for raw in stream:
            line = raw.rstrip()
            if line:
                print(f"[{label}] {line}", flush=True)
    except Exception:
        pass


def _start_api(port: int) -> tuple[int, subprocess.Popen | None]:
    if _api_healthy(port):
        print(f"[Launch] Reusing existing API server on http://127.0.0.1:{port}", flush=True)
        return port, None

    api_port = port if _port_available(port) else _free_port()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["WLWL_PROJECT_ROOT"] = str(ROOT)
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "launcher.api_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(api_port),
    ]
    print(f"[Launch] Starting API server on http://127.0.0.1:{api_port}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    threading.Thread(target=_pump_output, args=(proc, "api"), daemon=True).start()
    _wait_until("API server", lambda: proc.poll() is None and _api_healthy(api_port))
    return api_port, proc


def _start_web(api_base: str, port: int) -> tuple[str, subprocess.Popen]:
    if not GUI_DIR.is_dir():
        raise RuntimeError(f"Missing frontend directory: {GUI_DIR}")
    npm = _find_npm()
    if npm is None:
        raise RuntimeError("npm was not found. Install Node.js 20+ and rerun one-click startup.")
    if not (GUI_DIR / "package.json").is_file():
        raise RuntimeError(f"Missing frontend package.json: {GUI_DIR / 'package.json'}")

    web_port = port if _port_available(port) else _free_port()
    env = os.environ.copy()
    env["VITE_GA_API_BASE"] = api_base
    env["VITE_WEB_PORT"] = str(web_port)
    token = os.environ.get("WLWL_API_AUTH_TOKEN", "").strip()
    if token:
        env["VITE_GA_API_AUTH_TOKEN"] = token

    cmd = [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(web_port)]
    print(f"[Launch] Starting web UI on http://127.0.0.1:{web_port}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(GUI_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    threading.Thread(target=_pump_output, args=(proc, "web"), daemon=True).start()
    url = f"http://127.0.0.1:{web_port}/"
    _wait_until("Web UI", lambda: proc.poll() is None and _url_ok(url))
    return url, proc


def _write_state(api_port: int, web_url: str, api_proc: subprocess.Popen | None, web_proc: subprocess.Popen) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_base": f"http://127.0.0.1:{api_port}",
        "api_port": api_port,
        "web_url": web_url,
        "api_pid": api_proc.pid if api_proc is not None else None,
        "web_pid": web_proc.pid,
        "updated_at": time.time(),
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _open_existing() -> bool:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    url = str(state.get("web_url") or "")
    if not url:
        return False
    if _url_ok(url):
        print(f"[Launch] Opening existing web UI: {url}", flush=True)
        webbrowser.open(url)
        return True
    return False


def _terminate(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[Launch] Stopping {name}...", flush=True)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start wlwl-ass browser UI")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    lock = acquire_singleton()
    if lock is None:
        if _open_existing():
            return 0
        print("[Launch] Another launcher is already running.", flush=True)
        return 0

    api_proc: subprocess.Popen | None = None
    web_proc: subprocess.Popen | None = None
    api_port = args.api_port
    try:
        api_port, api_proc = _start_api(args.api_port)
        api_base = f"http://127.0.0.1:{api_port}"
        web_url, web_proc = _start_web(api_base, args.web_port)
        _write_state(api_port, web_url, api_proc, web_proc)
        print(f"[Launch] Browser UI ready: {web_url}", flush=True)
        print(f"[Launch] API base: {api_base}", flush=True)
        if not args.no_browser:
            webbrowser.open(web_url)

        while True:
            if web_proc.poll() is not None:
                return int(web_proc.returncode or 0)
            if api_proc is not None and api_proc.poll() is not None:
                print("[Launch] API server exited; stopping web UI.", flush=True)
                return int(api_proc.returncode or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        _terminate(web_proc, "web UI")
        if api_proc is not None:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{api_port}/api/shutdown",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=1)
            except Exception:
                pass
            _terminate(api_proc, "API server")
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
