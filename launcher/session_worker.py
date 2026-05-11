"""Child-process worker for GUI project sessions.

Protocol is newline-delimited JSON over stdio. Parent sends commands on stdin;
worker emits events on stdout. Human/debug logs must go to stderr so they do
not corrupt the JSON stream.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
from typing import Any

from launcher.launch_config import DEFAULT_OPTIONS

FILE_HINT = "If you need to show files to user, use [FILE:filepath] in your response."
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr


def _decode_project(raw: str) -> dict[str, Any]:
    data = base64.urlsafe_b64decode(raw.encode("ascii"))
    project = json.loads(data.decode("utf-8"))
    if not isinstance(project, dict):
        raise ValueError("project payload must be an object")
    return project


def _emit(event: dict[str, Any]) -> None:
    _PROTOCOL_OUT.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    _PROTOCOL_OUT.flush()


class Worker:
    def __init__(self, base_dir: str, project: dict[str, Any], resume_task_id: str | None):
        from agentmain import AgentRuntimeContext, GeneraticAgent

        self.base_dir = base_dir
        self.project = project
        self.current_cancel = threading.Event()
        self.current_assistant_id: str | None = None
        context = AgentRuntimeContext(
            project_id=str(project["id"]),
            project_name=str(project.get("name") or project["id"]),
            project_root=str(project.get("project_root") or base_dir),
            llm_no=int(project.get("llm_no", 0) or 0),
            llm_config_name=str(project.get("llm_config_name") or ""),
            permission_mode=str(project.get("permission_mode") or DEFAULT_OPTIONS["permission_mode"]),
            use_project_context=bool(project.get("use_project_context", True)),
            autonomous_enabled=bool(project.get("autonomous_enabled", False)),
            resume_task_id=resume_task_id,
        )
        self.agent = GeneraticAgent(runtime_context=context)
        self.agent.inc_out = True
        self.thread = threading.Thread(target=self.agent.run, name="session-worker-agent", daemon=True)
        self.thread.start()

    def send(self, text: str, assistant_id: str) -> None:
        self.current_cancel = threading.Event()
        self.current_assistant_id = assistant_id
        task_queue = self.agent.put_task(f"{FILE_HINT}\n\n{text}", source="gui")
        threading.Thread(
            target=self._drain_task,
            args=(task_queue, assistant_id, self.current_cancel),
            name="session-worker-drain",
            daemon=True,
        ).start()

    def _drain_task(self, task_queue: queue.Queue, assistant_id: str, cancel: threading.Event) -> None:
        try:
            while not cancel.is_set():
                try:
                    item = task_queue.get(timeout=0.5)
                except queue.Empty:
                    if not self.thread.is_alive():
                        raise RuntimeError("agent thread exited")
                    continue
                if "next" in item:
                    _emit({"event": "next", "assistant_id": assistant_id, "text": str(item.get("next") or "")})
                if "done" in item:
                    _emit({"event": "done", "assistant_id": assistant_id, "text": str(item.get("done") or "")})
                    return
            _emit({"event": "aborted", "assistant_id": assistant_id})
        except Exception as exc:
            _emit({"event": "error", "assistant_id": assistant_id, "detail": str(exc)})
        finally:
            if self.current_assistant_id == assistant_id:
                self.current_assistant_id = None

    def abort(self) -> None:
        self.current_cancel.set()
        try:
            self.agent.abort()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.abort()
        shutdown = getattr(self.agent, "shutdown", None)
        if callable(shutdown):
            shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--project-b64", required=True)
    parser.add_argument("--resume-task-id", default=None)
    args = parser.parse_args(argv)
    os.chdir(args.base_dir)
    try:
        worker = Worker(args.base_dir, _decode_project(args.project_b64), args.resume_task_id)
    except Exception as exc:
        _emit({"event": "fatal", "detail": str(exc)})
        return 1
    _emit({"event": "ready"})
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            _emit({"event": "log", "text": f"bad command JSON: {exc}"})
            continue
        cmd = str(msg.get("cmd") or "")
        if cmd == "send":
            text = str(msg.get("text") or "").strip()
            assistant_id = str(msg.get("assistant_id") or "")
            if not text or not assistant_id:
                _emit({"event": "error", "assistant_id": assistant_id, "detail": "text and assistant_id are required"})
                continue
            worker.send(text, assistant_id)
        elif cmd == "abort":
            worker.abort()
        elif cmd == "shutdown":
            worker.shutdown()
            return 0
        else:
            _emit({"event": "log", "text": f"unknown command: {cmd}"})
    worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
