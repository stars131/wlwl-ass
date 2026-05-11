from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
ROOT = os.path.dirname(HERE)


def _sandbox_tmp(name: str) -> str:
    path = os.path.join(ROOT, ".test-work", f"{name}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    return path


class _FakeRuntime:
    def __init__(self, base_dir, project, resume_task_id=None):
        self.base_dir = base_dir
        self.project_id = project["id"]
        self.log_path = os.path.join(base_dir, "temp", "project_logs", f"{self.project_id}.log")
        self.pid = 12345
        self._messages = []
        self.closed = False

    def is_alive(self):
        return not self.closed

    def messages(self):
        return list(self._messages)

    def send(self, text):
        self._messages.append({
            "id": f"{self.project_id}-1",
            "seq": 1,
            "role": "user",
            "content": text,
            "status": "done",
            "created_at": "2026-01-01T00:00:00",
        })
        self._messages.append({
            "id": f"{self.project_id}-2",
            "seq": 2,
            "role": "assistant",
            "content": "ok",
            "status": "done",
            "created_at": "2026-01-01T00:00:01",
        })

    def abort_current(self):
        pass

    def close(self, timeout=5):
        self.closed = True


def test_project_start_uses_native_runtime(monkeypatch):
    from launcher import session_runtime
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-start")
    started = []

    def fake_start(self, project, resume_task_id=None):
        started.append((project, resume_task_id))
        runtime = _FakeRuntime(self.base_dir, project, resume_task_id)
        self._runtimes[project["id"]] = runtime
        return runtime

    monkeypatch.setattr(session_runtime.SessionRuntimeRegistry, "start", fake_start)
    try:
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)

        assert pm.start(project["id"])
        with open(os.path.join(tmp_path, "temp", "projects.json"), encoding="utf-8") as f:
            loaded = json.load(f)
        row = loaded["projects"][0]

        assert started[0][0]["id"] == project["id"]
        assert row["port"] is None
        assert "stapp.py" not in json.dumps(row)
        assert pm.get(project["id"])["running"] is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_api_project_messages(monkeypatch):
    from launcher import api_server, session_runtime
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-api")

    def fake_start(self, project, resume_task_id=None):
        runtime = _FakeRuntime(self.base_dir, project, resume_task_id)
        self._runtimes[project["id"]] = runtime
        return runtime

    monkeypatch.setattr(api_server, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(api_server, "_project_manager", None)
    monkeypatch.setattr(session_runtime.SessionRuntimeRegistry, "start", fake_start)

    try:
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)
        pm.start(project["id"])
        monkeypatch.setattr(api_server, "_project_manager", pm)

        status, payload = api_server._route_project_send_message({
            "params": {"id": project["id"]},
            "body": {"text": "hello"},
        })

        assert status == 202
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
        assert payload["messages"][1]["content"] == "ok"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_agent_shutdown_sentinel_exits(monkeypatch):
    import agentmain

    class FakeAgent(agentmain.GeneraticAgent):
        def __init__(self):
            self.task_queue = __import__("queue").Queue()
            self.is_running = False
            self.stop_sig = False
            self.handler = None

    agent = FakeAgent()
    thread = threading.Thread(target=agent.run)
    thread.start()
    agent.shutdown()
    thread.join(timeout=2)

    assert not thread.is_alive()
