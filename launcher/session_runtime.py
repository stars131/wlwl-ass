"""Subprocess-backed GUI agent sessions.

The React chat drawer talks to ``launcher.api_server``. The API process keeps
message history and lifecycle state, while each project session runs its own
``GeneraticAgent`` in a child Python process. That preserves the native chat
API and restores a reliable stop path: if graceful shutdown stalls, the child
process tree can be killed without taking down the API.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
READY_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 5.0

TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_reply(text: str) -> str:
    for pat in TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or "..."


def _strip_files(text: str) -> str:
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def _build_done_text(raw_text: str) -> str:
    files = [p for p in re.findall(r"\[FILE:([^\]]+)\]", raw_text or "") if os.path.exists(p)]
    body = _strip_files(_clean_reply(raw_text))
    if files:
        body = (body + "\n\n" if body else "") + "\n".join(f"生成文件: {p}" for p in files)
    return body or "..."


def _encode_project(project: dict[str, Any]) -> str:
    raw = json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=5,
            )
    else:
        with contextlib.suppress(Exception):
            os.kill(pid, 15)
        time.sleep(0.2)
        with contextlib.suppress(Exception):
            os.kill(pid, 9)


class SessionRuntime:
    def __init__(self, base_dir: str, project: dict[str, Any], *, resume_task_id: str | None = None):
        self.base_dir = base_dir
        self.project_id = str(project["id"])
        self.name = str(project.get("name") or self.project_id)
        self.lock = threading.RLock()
        self.busy = False
        self.closed = False
        self.state = "starting"
        self.pid: int | None = None
        self._message_seq = 0
        self._current_assistant_id: str | None = None
        self._partials: dict[str, str] = {}
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._writer_lock = threading.Lock()
        self.log_path = os.path.join(base_dir, "temp", "project_logs", f"{self.project_id}.log")
        self.history_path = os.path.join(base_dir, "temp", "project_messages", f"{self.project_id}.json")
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.history_path), exist_ok=True)

        self._log_handle = open(self.log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        self._log_handle.write(f"\n=== session subprocess starting {datetime.now().isoformat()} ===\n")
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "launcher.session_worker",
            "--base-dir",
            base_dir,
            "--project-b64",
            _encode_project(project),
        ]
        if resume_task_id:
            cmd.extend(["--resume-task-id", str(resume_task_id)])
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            cmd,
            cwd=base_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._reader_loop, name=f"session-reader-{self.project_id}", daemon=True)
        self._reader.start()
        if not self._ready.wait(timeout=READY_TIMEOUT_S):
            detail = self._start_error or f"worker did not become ready within {READY_TIMEOUT_S:.0f}s"
            self.close(timeout=1.0)
            raise RuntimeError(detail)
        if self._start_error:
            self.close(timeout=1.0)
            raise RuntimeError(self._start_error)
        self.state = "running"
        self._register_process(cmd)
        self._log(f"=== session subprocess ready pid={self.pid} ===")

    def _register_process(self, cmd: list[str]) -> None:
        if not self.pid:
            return
        try:
            from launcher.process_registry import get_registry

            get_registry(self.base_dir).register(
                f"session:{self.project_id}",
                self.pid,
                kind="session",
                cmd=cmd,
                meta={"name": self.name},
            )
        except Exception as exc:
            self._log(f"[registry] register failed: {exc}")

    def _unregister_process(self) -> None:
        try:
            from launcher.process_registry import get_registry

            get_registry(self.base_dir).unregister_label(f"session:{self.project_id}")
        except Exception:
            pass

    def _load_history_unlocked(self) -> list[dict[str, Any]]:
        if not os.path.isfile(self.history_path):
            return []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def _save_history_unlocked(self, messages: list[dict[str, Any]]) -> None:
        tmp = self.history_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.history_path)

    def _append_message(self, role: str, content: str, *, status: str = "done") -> dict[str, Any]:
        with self.lock:
            messages = self._load_history_unlocked()
            next_seq = max([int(m.get("seq", 0) or 0) for m in messages] + [self._message_seq]) + 1
            self._message_seq = next_seq
            msg = {
                "id": f"{self.project_id}-{next_seq}",
                "seq": next_seq,
                "role": role,
                "content": content,
                "status": status,
                "created_at": _now(),
                "updated_at": _now(),
            }
            messages.append(msg)
            self._save_history_unlocked(messages[-200:])
            return msg

    def _update_message(self, msg_id: str, *, content: str | None = None, status: str | None = None) -> None:
        with self.lock:
            messages = self._load_history_unlocked()
            for msg in messages:
                if msg.get("id") == msg_id:
                    if content is not None:
                        msg["content"] = content
                    if status is not None:
                        msg["status"] = status
                    msg["updated_at"] = _now()
                    break
            self._save_history_unlocked(messages)

    def _log(self, line: str) -> None:
        try:
            self._log_handle.write(line.rstrip() + "\n")
        except Exception:
            pass

    def _reader_loop(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._log(f"[protocol] non-json stdout: {line[:500]}")
                    continue
                self._handle_event(event if isinstance(event, dict) else {})
        except Exception as exc:
            self._start_error = self._start_error or f"reader failed: {exc}"
        finally:
            if not self._ready.is_set():
                rc = self.proc.poll()
                self._start_error = self._start_error or f"worker exited before ready (returncode={rc})"
                self._ready.set()
            with self.lock:
                if self.busy and self._current_assistant_id:
                    self._update_message(
                        self._current_assistant_id,
                        content="会话进程已退出。",
                        status="error",
                    )
                self.busy = False
                self._current_assistant_id = None
                if not self.closed:
                    self.state = "stopped"

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event") or "")
        if kind == "ready":
            self._ready.set()
            return
        if kind == "log":
            self._log(str(event.get("text") or ""))
            return
        assistant_id = str(event.get("assistant_id") or "")
        if kind == "next" and assistant_id:
            chunk = str(event.get("text") or "")
            with self.lock:
                current = self._partials.get(assistant_id, "") + chunk
                self._partials[assistant_id] = current
            self._update_message(assistant_id, content=_clean_reply(current), status="running")
            return
        if kind == "done" and assistant_id:
            final_text = _build_done_text(str(event.get("text") or ""))
            self._update_message(assistant_id, content=final_text, status="done")
            self._log(f"[assistant] {final_text}")
            self._finish_assistant(assistant_id)
            return
        if kind == "aborted" and assistant_id:
            with self.lock:
                partial = self._partials.get(assistant_id, "")
            self._update_message(assistant_id, content=_clean_reply(partial) if partial else "已停止。", status="aborted")
            self._log("[assistant] aborted")
            self._finish_assistant(assistant_id)
            return
        if kind == "error" and assistant_id:
            detail = str(event.get("detail") or event.get("text") or "unknown error")
            self._update_message(assistant_id, content=f"错误: {detail}", status="error")
            self._log(f"[error] {detail}")
            self._finish_assistant(assistant_id)
            return
        if kind == "fatal":
            self._start_error = str(event.get("detail") or "worker fatal error")
            self._ready.set()

    def _finish_assistant(self, assistant_id: str) -> None:
        with self.lock:
            self._partials.pop(assistant_id, None)
            if self._current_assistant_id == assistant_id:
                self._current_assistant_id = None
                self.busy = False

    def _send_command(self, payload: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise RuntimeError("session process is not running")
        if self.proc.stdin is None:
            raise RuntimeError("session stdin is closed")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._writer_lock:
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()

    def is_alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def messages(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._load_history_unlocked()

    def send(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("message text is required")
        if not self.is_alive():
            raise RuntimeError("session is not running")
        with self.lock:
            if self.busy:
                raise RuntimeError("session is busy")
            self.busy = True
        user_msg = self._append_message("user", text)
        assistant_msg = self._append_message("assistant", "", status="running")
        with self.lock:
            self._current_assistant_id = assistant_msg["id"]
            self._partials[assistant_msg["id"]] = ""
        self._log(f"[user] {text}")
        try:
            self._send_command({"cmd": "send", "text": text, "assistant_id": assistant_msg["id"]})
        except Exception:
            with self.lock:
                self.busy = False
                self._current_assistant_id = None
            self._update_message(assistant_msg["id"], content="发送失败。", status="error")
            raise
        return user_msg

    def abort_current(self) -> None:
        with contextlib.suppress(Exception):
            self._send_command({"cmd": "abort", "assistant_id": self._current_assistant_id})

    def close(self, timeout: float = STOP_TIMEOUT_S) -> None:
        if self.closed:
            return
        self.closed = True
        self.state = "stopping"
        with contextlib.suppress(Exception):
            self._send_command({"cmd": "shutdown"})
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if self.pid:
                _kill_process_tree(self.pid)
            with contextlib.suppress(Exception):
                self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=2)
        self.state = "stopped"
        self._unregister_process()
        self._log(f"=== session subprocess stopped {datetime.now().isoformat()} ===")
        with contextlib.suppress(Exception):
            self._log_handle.close()


class SessionRuntimeRegistry:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._lock = threading.RLock()
        self._runtimes: dict[str, SessionRuntime] = {}

    def start(self, project: dict[str, Any], *, resume_task_id: str | None = None) -> SessionRuntime:
        project_id = str(project["id"])
        with self._lock:
            runtime = self._runtimes.get(project_id)
            if runtime and runtime.is_alive():
                return runtime
            if runtime:
                runtime.close(timeout=1.0)
            runtime = SessionRuntime(self.base_dir, project, resume_task_id=resume_task_id)
            self._runtimes[project_id] = runtime
            return runtime

    def get(self, project_id: str) -> SessionRuntime | None:
        with self._lock:
            runtime = self._runtimes.get(project_id)
            if runtime and runtime.is_alive():
                return runtime
            if runtime:
                self._runtimes.pop(project_id, None)
            return None

    def stop(self, project_id: str) -> None:
        with self._lock:
            runtime = self._runtimes.pop(project_id, None)
        if runtime:
            runtime.close()

    def stop_all(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()
