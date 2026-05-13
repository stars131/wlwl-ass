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
REQUEST_MODES = {"auto", "chat", "task", "canvas"}
TEXT_EXTS = {
    ".bat",
    ".cmd",
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
CANVAS_TEXT_LIMIT = 160_000
AUTONOMOUS_INTERVAL_S = 10 * 60
AUTONOMOUS_INTERVAL_MIN_S = 60
AUTONOMOUS_INTERVAL_MAX_S = 24 * 60 * 60
AUTONOMOUS_THREAD_JOIN_TIMEOUT_S = 2.0
AUTONOMOUS_PROMPT = (
    "Autonomous flow is enabled for this session. The user is away. "
    "Read memory/autonomous_operation_sop.md and follow it. "
    "Choose exactly one safe, bounded autonomous action for this run. "
    "If there is no TODO, plan useful TODOs and stop. If there is a TODO, "
    "execute one item, write the required report, update history/TODO via the SOP helper, "
    "then stop. Do not wait for user input; record decisions needing approval in the report."
)

TASK_KEYWORDS = (
    "修复",
    "实现",
    "落地",
    "更新",
    "同步",
    "提交",
    "推送",
    "运行",
    "启动",
    "停止",
    "安装",
    "修改",
    "删除",
    "创建",
    "配置",
    "保存",
    "执行",
    "部署",
    "测试",
    "验证",
    "重构",
    "接入",
    "fix",
    "implement",
    "update",
    "sync",
    "commit",
    "push",
    "run",
    "start",
    "stop",
    "install",
    "delete",
    "deploy",
    "test",
    "refactor",
)
CANVAS_KEYWORDS = (
    "方案",
    "prd",
    "文档",
    "报告",
    "表格",
    "页面",
    "原型",
    "canvas",
    "成果",
    "设计",
    "写一份",
    "生成一份",
    "整理成",
    "readme",
    "markdown",
    "plan",
    "proposal",
    "document",
    "report",
    "table",
    "prototype",
)
CHAT_KEYWORDS = (
    "什么",
    "为什么",
    "解释",
    "分析",
    "建议",
    "怎么看",
    "能不能",
    "是否",
    "区别",
    "how",
    "what",
    "why",
    "explain",
    "analyze",
    "suggest",
)


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


def _classify_intent(text: str, requested_mode: str = "auto") -> dict[str, Any]:
    requested = requested_mode if requested_mode in REQUEST_MODES else "auto"
    lowered = (text or "").lower()
    has_task = any(k.lower() in lowered for k in TASK_KEYWORDS)
    has_canvas = any(k.lower() in lowered for k in CANVAS_KEYWORDS)
    has_chat = any(k.lower() in lowered for k in CHAT_KEYWORDS)

    if requested == "chat":
        mode = "chat"
    elif requested == "task":
        mode = "task_canvas" if has_canvas else "task"
    elif requested == "canvas":
        mode = "task_canvas" if has_task else "canvas"
    elif has_task and has_canvas:
        mode = "task_canvas"
    elif has_task:
        mode = "task"
    elif has_canvas:
        mode = "canvas"
    else:
        mode = "chat"

    confidence = 0.82 if requested != "auto" else 0.62
    if requested == "auto":
        if has_task or has_canvas:
            confidence = 0.78
        if has_chat and not (has_task or has_canvas):
            confidence = 0.74

    if mode in ("task", "task_canvas"):
        intent = "operation"
    elif mode == "canvas":
        intent = "artifact"
    else:
        intent = "conversation"

    return {
        "requested_mode": requested,
        "mode": mode,
        "intent": intent,
        "confidence": confidence,
        "signals": {
            "task": has_task,
            "canvas": has_canvas,
            "chat": has_chat,
        },
    }


def _autonomous_intent() -> dict[str, Any]:
    return {
        "requested_mode": "task",
        "mode": "task_canvas",
        "intent": "autonomous",
        "confidence": 1.0,
        "signals": {
            "task": True,
            "canvas": True,
            "chat": False,
        },
    }


def _clamp_autonomous_interval(value: Any) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = AUTONOMOUS_INTERVAL_S
    return max(AUTONOMOUS_INTERVAL_MIN_S, min(AUTONOMOUS_INTERVAL_MAX_S, seconds))


def _make_task_steps(mode: str, phase: str = "running") -> list[dict[str, str]]:
    if mode not in ("task", "task_canvas"):
        return []
    execute_status = "running" if phase == "running" else phase
    steps = [
        {"id": "understand", "label": "理解需求", "status": "done"},
        {"id": "execute", "label": "执行操作", "status": execute_status},
    ]
    if mode == "task_canvas":
        artifact_status = "pending" if phase == "running" else phase
        steps.append({"id": "artifact", "label": "整理成果", "status": artifact_status})
    finish_status = "pending" if phase == "running" else phase
    steps.append({"id": "finish", "label": "输出结果", "status": finish_status})
    return steps


def _artifact_kind(path_or_title: str) -> str:
    ext = os.path.splitext(path_or_title.lower())[1]
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".json", ".yaml", ".yml", ".toml"):
        return "config"
    if ext in TEXT_EXTS:
        return "code"
    return "file"


def _read_artifact_text(path: str) -> str:
    ext = os.path.splitext(path.lower())[1]
    if ext not in TEXT_EXTS:
        return ""
    try:
        if os.path.getsize(path) > CANVAS_TEXT_LIMIT:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _artifact_dir(base_dir: str, project_id: str) -> str:
    path = os.path.join(base_dir, "temp", "project_artifacts", project_id)
    os.makedirs(path, exist_ok=True)
    return path


def _persist_artifact(base_dir: str, project_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    path = os.path.join(_artifact_dir(base_dir, project_id), f"{artifact['id']}.json")
    artifact["artifact_path"] = path
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return artifact


def _summarize_for_chat(text: str, limit: int = 700) -> str:
    cleaned = _strip_files(_clean_reply(text))
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    picked: list[str] = []
    total = 0
    for line in lines:
        if line.startswith("#"):
            continue
        picked.append(line)
        total += len(line)
        if total >= limit or len(picked) >= 5:
            break
    summary = "\n".join(picked).strip()
    if len(summary) > limit:
        summary = summary[:limit].rstrip() + "..."
    return summary


def _build_done_payload(
    *,
    base_dir: str,
    project_id: str,
    assistant_id: str,
    raw_text: str,
    mode: str,
) -> dict[str, Any]:
    cleaned = _clean_reply(raw_text)
    body = _strip_files(cleaned)
    file_paths = [p for p in re.findall(r"\[FILE:([^\]]+)\]", raw_text or "") if os.path.exists(p)]
    artifacts: list[dict[str, Any]] = []

    if mode in ("canvas", "task_canvas"):
        artifact = {
            "id": f"{assistant_id}-canvas",
            "title": "会话成果",
            "kind": "markdown",
            "source": "assistant",
            "content": body or cleaned,
            "created_at": _now(),
        }
        artifacts.append(_persist_artifact(base_dir, project_id, artifact))

    for idx, file_path in enumerate(file_paths, 1):
        title = os.path.basename(file_path) or f"artifact-{idx}"
        artifact = {
            "id": f"{assistant_id}-file-{idx}",
            "title": title,
            "kind": _artifact_kind(file_path),
            "source": "file",
            "path": file_path,
            "content": _read_artifact_text(file_path),
            "created_at": _now(),
        }
        artifacts.append(_persist_artifact(base_dir, project_id, artifact))

    if mode in ("canvas", "task_canvas") and artifacts:
        summary = _summarize_for_chat(body)
        title = artifacts[0].get("title") or "成果"
        content = f"已生成「{title}」，已放入右侧 Canvas。"
        if summary:
            content += f"\n\n{summary}"
    else:
        content = body or "..."
        if artifacts:
            content = (content + "\n\n" if content else "") + "\n".join(
                f"生成文件: {a.get('path') or a.get('title')}" for a in artifacts
            )

    return {
        "content": content or "...",
        "debug_content": cleaned if cleaned != content else "",
        "artifacts": artifacts,
    }


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
        self._assistant_modes: dict[str, str] = {}
        self._autonomous_enabled = bool(project.get("autonomous_enabled", False))
        self._autonomous_interval_s = _clamp_autonomous_interval(
            project.get("autonomous_interval_s", AUTONOMOUS_INTERVAL_S)
        )
        self._autonomous_stop = threading.Event()
        self._autonomous_thread: threading.Thread | None = None
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
        if self._autonomous_enabled:
            self.set_autonomous(True, trigger_now=True)

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

    def _append_message(
        self,
        role: str,
        content: str,
        *,
        status: str = "done",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            if extra:
                msg.update(extra)
            messages.append(msg)
            self._save_history_unlocked(messages[-200:])
            return msg

    def _update_message(
        self,
        msg_id: str,
        *,
        content: str | None = None,
        status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            messages = self._load_history_unlocked()
            for msg in messages:
                if msg.get("id") == msg_id:
                    if content is not None:
                        msg["content"] = content
                    if status is not None:
                        msg["status"] = status
                    if extra:
                        msg.update(extra)
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
            mode = self._assistant_modes.get(assistant_id, "chat")
            extra = {"task": {"steps": _make_task_steps(mode, "running")}} if mode in ("task", "task_canvas") else None
            self._update_message(assistant_id, content=_clean_reply(current), status="running", extra=extra)
            return
        if kind == "done" and assistant_id:
            mode = self._assistant_modes.get(assistant_id, "chat")
            payload = _build_done_payload(
                base_dir=self.base_dir,
                project_id=self.project_id,
                assistant_id=assistant_id,
                raw_text=str(event.get("text") or ""),
                mode=mode,
            )
            extra = {
                "debug_content": payload["debug_content"],
                "artifacts": payload["artifacts"],
            }
            if mode in ("task", "task_canvas"):
                extra["task"] = {"steps": _make_task_steps(mode, "done")}
            self._update_message(assistant_id, content=payload["content"], status="done", extra=extra)
            self._log(f"[assistant] {payload['content']}")
            self._finish_assistant(assistant_id)
            return
        if kind == "aborted" and assistant_id:
            with self.lock:
                partial = self._partials.get(assistant_id, "")
            mode = self._assistant_modes.get(assistant_id, "chat")
            extra = {"task": {"steps": _make_task_steps(mode, "aborted")}} if mode in ("task", "task_canvas") else None
            self._update_message(assistant_id, content=_clean_reply(partial) if partial else "已停止。", status="aborted", extra=extra)
            self._log("[assistant] aborted")
            self._finish_assistant(assistant_id)
            return
        if kind == "error" and assistant_id:
            detail = str(event.get("detail") or event.get("text") or "unknown error")
            mode = self._assistant_modes.get(assistant_id, "chat")
            extra = {"debug_content": detail}
            if mode in ("task", "task_canvas"):
                extra["task"] = {"steps": _make_task_steps(mode, "error")}
            self._update_message(assistant_id, content=f"错误: {detail}", status="error", extra=extra)
            self._log(f"[error] {detail}")
            self._finish_assistant(assistant_id)
            return
        if kind == "fatal":
            self._start_error = str(event.get("detail") or "worker fatal error")
            self._ready.set()

    def _finish_assistant(self, assistant_id: str) -> None:
        with self.lock:
            self._partials.pop(assistant_id, None)
            self._assistant_modes.pop(assistant_id, None)
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

    def _ensure_autonomous_thread(self) -> None:
        with self.lock:
            if self._autonomous_thread and self._autonomous_thread.is_alive():
                return
            stop_event = self._autonomous_stop
            interval = self._autonomous_interval_s
            self._autonomous_thread = threading.Thread(
                target=self._autonomous_loop,
                args=(stop_event, interval),
                name=f"session-autonomous-{self.project_id}",
                daemon=True,
            )
            self._autonomous_thread.start()

    def _autonomous_loop(self, stop_event: threading.Event, interval_s: int) -> None:
        # Each loop reads the interval it was started with; set_autonomous rotates
        # the thread when the interval changes, so we don't need to re-read self.
        while not stop_event.wait(interval_s):
            self._maybe_start_autonomous_turn("interval")

    def set_autonomous(
        self,
        enabled: bool,
        *,
        trigger_now: bool = True,
        interval_s: Any = None,
    ) -> None:
        """Enable/disable the autonomous loop.

        Always retires the previous loop thread (by rotating the stop event) and
        joins it briefly outside the lock. This avoids the race where re-enabling
        immediately after disabling would see the old thread still ``is_alive()``
        and skip starting a new one, leaving the session without any worker.
        """
        enabled = bool(enabled)
        join_target: threading.Thread | None = None
        with self.lock:
            if interval_s is not None:
                self._autonomous_interval_s = _clamp_autonomous_interval(interval_s)
            self._autonomous_enabled = enabled
            previous_thread = self._autonomous_thread
            previous_stop = self._autonomous_stop
            previous_stop.set()
            self._autonomous_stop = threading.Event()
            self._autonomous_thread = None
            if previous_thread is not None and previous_thread.is_alive():
                join_target = previous_thread
        if join_target is not None and join_target is not threading.current_thread():
            join_target.join(timeout=AUTONOMOUS_THREAD_JOIN_TIMEOUT_S)
        if enabled:
            self._ensure_autonomous_thread()
            if trigger_now:
                self._maybe_start_autonomous_turn("enabled")

    def _maybe_start_autonomous_turn(self, reason: str) -> bool:
        if not self.is_alive():
            return False
        with self.lock:
            if not self._autonomous_enabled or self.busy:
                return False
            self.busy = True
        mode = "task_canvas"
        intent = _autonomous_intent()
        self._append_message(
            "system",
            f"Autonomous flow triggered ({reason}).",
            extra={"requested_mode": "task"},
        )
        assistant_msg = self._append_message(
            "assistant",
            "",
            status="running",
            extra={
                "mode": mode,
                "intent": intent,
                "task": {"steps": _make_task_steps(mode, "running")},
                "artifacts": [],
                "debug_content": "",
            },
        )
        assistant_id = assistant_msg["id"]
        with self.lock:
            self._current_assistant_id = assistant_id
            self._partials[assistant_id] = ""
            self._assistant_modes[assistant_id] = mode
        self._log(f"[autonomous] triggered ({reason})")
        try:
            self._send_command({
                "cmd": "send",
                "text": AUTONOMOUS_PROMPT,
                "assistant_id": assistant_id,
                "mode": mode,
                "source": "autonomous",
            })
        except Exception:
            with self.lock:
                self.busy = False
                self._current_assistant_id = None
                self._partials.pop(assistant_id, None)
                self._assistant_modes.pop(assistant_id, None)
            self._update_message(assistant_id, content="Autonomous flow failed to start.", status="error")
            raise
        return True

    def is_alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def messages(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._load_history_unlocked()

    def send(self, text: str, requested_mode: str = "auto") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("message text is required")
        if not self.is_alive():
            raise RuntimeError("session is not running")
        with self.lock:
            if self.busy:
                raise RuntimeError("session is busy")
            self.busy = True
        intent = _classify_intent(text, requested_mode)
        mode = str(intent["mode"])
        user_msg = self._append_message(
            "user",
            text,
            extra={"requested_mode": intent["requested_mode"]},
        )
        assistant_msg = self._append_message(
            "assistant",
            "",
            status="running",
            extra={
                "mode": mode,
                "intent": intent,
                "task": {"steps": _make_task_steps(mode, "running")} if mode in ("task", "task_canvas") else None,
                "artifacts": [],
                "debug_content": "",
            },
        )
        with self.lock:
            self._current_assistant_id = assistant_msg["id"]
            self._partials[assistant_msg["id"]] = ""
            self._assistant_modes[assistant_msg["id"]] = mode
        self._log(f"[user] {text}")
        try:
            self._send_command({"cmd": "send", "text": text, "assistant_id": assistant_msg["id"], "mode": mode})
        except Exception:
            with self.lock:
                self.busy = False
                self._current_assistant_id = None
                self._partials.pop(assistant_msg["id"], None)
                self._assistant_modes.pop(assistant_msg["id"], None)
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
        self._autonomous_stop.set()
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
