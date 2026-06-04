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


MODE_INSTRUCTIONS = {
    "chat": (
        "GUI mode: chat. Answer the user directly and cleanly. "
        "Do not include internal debug notes, raw tool traces, or hidden tags."
    ),
    "task": (
        "GUI mode: task. Treat this as an operation workflow. "
        "When done, return a concise user-facing report with these sections: "
        "Completed, Changed, Verified, Notes. Keep raw logs out of the answer."
    ),
    "canvas": (
        "GUI mode: canvas. Produce a polished reusable artifact such as a document, "
        "plan, table, or code draft. The UI will place the final content in Canvas. "
        "Keep it self-contained and avoid debug traces."
    ),
    "task_canvas": (
        "GUI mode: task plus canvas. Execute the operation and produce a polished "
        "artifact if appropriate. Final response should briefly summarize the task "
        "result; reusable long-form output can be included as the artifact body."
    ),
}


def _decode_project(raw: str) -> dict[str, Any]:
    data = base64.urlsafe_b64decode(raw.encode("ascii"))
    project = json.loads(data.decode("utf-8"))
    if not isinstance(project, dict):
        raise ValueError("project payload must be an object")
    return project


def _emit(event: dict[str, Any]) -> None:
    _PROTOCOL_OUT.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    _PROTOCOL_OUT.flush()


def _runtime_context_from_project(
    base_dir: str,
    project: dict[str, Any],
    resume_task_id: str | None = None,
):
    from agentmain import AgentRuntimeContext

    return AgentRuntimeContext(
        project_id=str(project["id"]),
        project_name=str(project.get("name") or project["id"]),
        project_root=str(project.get("project_root") or base_dir),
        llm_no=int(project.get("llm_no", 0) or 0),
        llm_config_name=str(project.get("llm_config_name") or ""),
        llm_profile_name=str(project.get("llm_profile_name") or ""),
        permission_mode=str(project.get("permission_mode") or DEFAULT_OPTIONS["permission_mode"]),
        use_project_context=bool(project.get("use_project_context", True)),
        autonomous_enabled=bool(project.get("autonomous_enabled", False)),
        resume_task_id=resume_task_id,
    )


class Worker:
    def __init__(self, base_dir: str, project: dict[str, Any], resume_task_id: str | None):
        from agentmain import GeneraticAgent

        self.base_dir = base_dir
        self.project = project
        self.current_cancel = threading.Event()
        self.current_assistant_id: str | None = None
        context = _runtime_context_from_project(base_dir, project, resume_task_id)
        self.agent = GeneraticAgent(runtime_context=context)
        self.agent.inc_out = True
        self.thread = threading.Thread(target=self.agent.run, name="session-worker-agent", daemon=True)
        self.thread.start()

    def update_context(self, project: dict[str, Any]) -> None:
        if not isinstance(project, dict):
            return
        self.project = project
        context = _runtime_context_from_project(self.base_dir, project)
        self.agent.runtime_context = context

    def send(
        self,
        text: str,
        assistant_id: str,
        mode: str = "chat",
        source: str = "gui",
        history_context: str = "",
    ) -> None:
        self.current_cancel = threading.Event()
        self.current_assistant_id = assistant_id
        self.update_context(self.project)
        instruction = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["chat"])
        parts = [FILE_HINT, instruction]
        history_context = (history_context or "").strip()
        if history_context:
            parts.append(
                "Same-project chat history (oldest to newest, compacted; use only as context, do not answer it again):\n"
                f"{history_context}"
            )
        parts.append(f"Current user message:\n{text}")
        task_queue = self.agent.put_task("\n\n".join(parts), source=source)
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
            mode = str(msg.get("mode") or "chat")
            source = str(msg.get("source") or "gui")
            history_context = str(msg.get("history_context") or "")
            if not text or not assistant_id:
                _emit({"event": "error", "assistant_id": assistant_id, "detail": "text and assistant_id are required"})
                continue
            worker.send(text, assistant_id, mode, source, history_context)
        elif cmd == "abort":
            worker.abort()
        elif cmd == "update_context":
            project = msg.get("project")
            if isinstance(project, dict):
                worker.update_context(project)
                _emit({"event": "log", "text": "runtime context updated"})
        elif cmd == "shutdown":
            worker.shutdown()
            return 0
        else:
            _emit({"event": "log", "text": f"unknown command: {cmd}"})
    worker.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
