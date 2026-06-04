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
        self.autonomous_calls = []

    def is_alive(self):
        return not self.closed

    def messages(self):
        return list(self._messages)

    def send(self, text, requested_mode="auto"):
        self._messages.append({
            "id": f"{self.project_id}-1",
            "seq": 1,
            "role": "user",
            "content": text,
            "status": "done",
            "requested_mode": requested_mode,
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

    def set_autonomous(self, enabled, trigger_now=True):
        self.autonomous_calls.append((enabled, trigger_now))

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


def test_project_create_uses_saved_default_options():
    from launcher.launch_config import save_options
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-default-options")
    try:
        save_options(tmp_path, {
            "scheduler": True,
            "llm_no": 2,
            "permission_mode": "auto",
            "project_root": "",
            "use_project_context": True,
            "autonomous_enabled": True,
        })
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)

        assert project["llm_no"] == 2
        assert project["autonomous_enabled"] is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_project_set_llm_profile_clears_config_and_persists():
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-profile-llm")
    try:
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)

        updated = pm.set_llm(project["id"], config_name="glm")
        assert updated["llm_config_name"] == "glm"
        assert updated["llm_profile_name"] == ""

        updated = pm.set_llm(project["id"], profile_name="daily")
        assert updated["llm_profile_name"] == "daily"
        assert updated["llm_config_name"] == ""

        with open(os.path.join(tmp_path, "temp", "projects.json"), encoding="utf-8") as f:
            row = json.load(f)["projects"][0]
        assert row["llm_profile_name"] == "daily"
        assert row["llm_config_name"] == ""
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_api_settings_persists_autonomous_enabled(monkeypatch):
    from launcher import api_server

    tmp_path = _sandbox_tmp("native-settings-autonomous")
    monkeypatch.setattr(api_server, "_project_root", lambda: tmp_path)

    try:
        status, payload = api_server._route_settings_put({
            "body": {"autonomous_enabled": True},
        })
        assert status == 200
        assert payload["settings"]["autonomous_enabled"] is True

        status, payload = api_server._route_settings_get({})
        assert status == 200
        assert payload["settings"]["autonomous_enabled"] is True
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


def test_api_project_send_autostarts_stopped_session(monkeypatch):
    from launcher import api_server, session_runtime
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-api-autostart-send")
    started = []

    def fake_start(self, project, resume_task_id=None):
        started.append(project["id"])
        runtime = _FakeRuntime(self.base_dir, project, resume_task_id)
        self._runtimes[project["id"]] = runtime
        return runtime

    monkeypatch.setattr(api_server, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(api_server, "_project_manager", None)
    monkeypatch.setattr(session_runtime.SessionRuntimeRegistry, "start", fake_start)

    try:
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)
        monkeypatch.setattr(api_server, "_project_manager", pm)

        status, payload = api_server._route_project_send_message({
            "params": {"id": project["id"]},
            "body": {"text": "continue"},
        })

        assert status == 202
        assert started == [project["id"]]
        assert payload["running"] is True
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
        assert payload["messages"][0]["content"] == "continue"
        assert pm.get(project["id"])["running"] is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_api_project_messages_settles_stale_running_history(monkeypatch):
    from launcher import api_server
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-api-stale-running")
    monkeypatch.setattr(api_server, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(api_server, "_project_manager", None)

    try:
        pm = ProjectManager(tmp_path)
        project = pm.create("demo", auto_start=False)
        monkeypatch.setattr(api_server, "_project_manager", pm)

        messages_dir = os.path.join(tmp_path, "temp", "project_messages")
        os.makedirs(messages_dir, exist_ok=True)
        messages_path = os.path.join(messages_dir, f"{project['id']}.json")
        with open(messages_path, "w", encoding="utf-8") as f:
            json.dump({
                "messages": [{
                    "id": f"{project['id']}-1",
                    "seq": 1,
                    "role": "assistant",
                    "content": "",
                    "status": "running",
                    "created_at": "2026-01-01T00:00:00",
                    "task": {"steps": [{"id": "execute", "label": "execute", "status": "running"}]},
                }],
            }, f)

        status, payload = api_server._route_project_messages({"params": {"id": project["id"]}})

        assert status == 200
        assert payload["running"] is False
        assert payload["messages"][0]["status"] == "aborted"
        assert payload["messages"][0]["content"] == "Stopped."
        assert payload["messages"][0]["task"]["steps"][0]["status"] == "aborted"
        with open(messages_path, encoding="utf-8") as f:
            saved = json.load(f)["messages"][0]
        assert saved["status"] == "aborted"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_api_project_messages_passes_mode(monkeypatch):
    from launcher import api_server, session_runtime
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-api-mode")
    seen = []

    class ModeRuntime(_FakeRuntime):
        def send(self, text, requested_mode="auto"):
            seen.append((text, requested_mode))
            return super().send(text, requested_mode)

    def fake_start(self, project, resume_task_id=None):
        runtime = ModeRuntime(self.base_dir, project, resume_task_id)
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

        status, _payload = api_server._route_project_send_message({
            "params": {"id": project["id"]},
            "body": {"text": "run tests", "mode": "task"},
        })

        assert status == 202
        assert seen == [("run tests", "task")]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_api_project_patch_updates_autonomous_runtime(monkeypatch):
    from launcher import api_server, session_runtime
    from launcher.project_manager import ProjectManager

    tmp_path = _sandbox_tmp("native-api-autonomous")

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
        runtime = pm.session(project["id"])
        monkeypatch.setattr(api_server, "_project_manager", pm)

        status, payload = api_server._route_project_patch({
            "params": {"id": project["id"]},
            "body": {"autonomous_enabled": True},
        })

        assert status == 200
        assert payload["project"]["autonomous_enabled"] is True
        assert runtime.autonomous_calls == [(True, True)]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _build_partial_autonomous_runtime(tmp_path, *, project_id="p_auto"):
    """Build a SessionRuntime by bypassing __init__ and assigning only the
    attributes that ``_maybe_start_autonomous_turn`` exercises.

    Using __new__ here is intentional: the real __init__ spawns a subprocess
    and a reader thread, which we don't want in unit tests. If a future
    change to ``_maybe_start_autonomous_turn`` reads a new attribute, the
    test will fail with AttributeError at the call site, surfacing the
    missing dependency rather than silently passing. Add the attribute here
    when that happens.
    """
    from launcher.session_runtime import SessionRuntime

    class _FakeProc:
        def poll(self):
            return None

    class _FakeLog:
        def write(self, _text):
            pass

    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime.base_dir = tmp_path
    runtime.project_id = project_id
    runtime.lock = threading.RLock()
    runtime.busy = False
    runtime.closed = False
    runtime.proc = _FakeProc()
    runtime._message_seq = 0
    runtime._current_assistant_id = None
    runtime._partials = {}
    runtime._assistant_modes = {}
    runtime._autonomous_enabled = True
    runtime._autonomous_interval_s = 60
    runtime._autonomous_stop = threading.Event()
    runtime._autonomous_thread = None
    runtime._log_handle = _FakeLog()
    runtime.history_path = os.path.join(tmp_path, "temp", "project_messages", f"{project_id}.json")
    os.makedirs(os.path.dirname(runtime.history_path), exist_ok=True)
    return runtime


def test_session_runtime_autonomous_turn_sends_worker_command(monkeypatch):
    from launcher.session_runtime import SessionRuntime

    tmp_path = _sandbox_tmp("native-runtime-autonomous")
    sent = []

    def fake_send_command(self, payload):
        sent.append(payload)

    monkeypatch.setattr(SessionRuntime, "_send_command", fake_send_command)

    try:
        runtime = _build_partial_autonomous_runtime(tmp_path)

        assert runtime._maybe_start_autonomous_turn("test") is True

        messages = runtime.messages()
        assert [m["role"] for m in messages] == ["system", "assistant"]
        assert messages[1]["intent"]["intent"] == "autonomous"
        assert sent[0]["source"] == "autonomous"
        assert sent[0]["mode"] == "task_canvas"
        assert runtime.busy is True
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_session_runtime_autonomous_turn_cleans_state_on_send_error(monkeypatch):
    """_send_command failing must not leak _current_assistant_id / partials,
    otherwise the next user message reuses stale state."""
    from launcher.session_runtime import SessionRuntime

    tmp_path = _sandbox_tmp("native-runtime-autonomous-error")

    def boom(self, _payload):
        raise RuntimeError("worker pipe broken")

    monkeypatch.setattr(SessionRuntime, "_send_command", boom)

    try:
        runtime = _build_partial_autonomous_runtime(tmp_path)

        try:
            runtime._maybe_start_autonomous_turn("error-test")
        except RuntimeError:
            pass

        assert runtime.busy is False
        assert runtime._current_assistant_id is None
        assert runtime._partials == {}
        assert runtime._assistant_modes == {}
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_session_runtime_abort_forces_aborted_message_and_releases_busy(monkeypatch):
    from launcher import session_runtime
    from launcher.session_runtime import SessionRuntime

    tmp_path = _sandbox_tmp("native-runtime-abort")
    sent = []
    killed = []

    class _FakeStdin:
        def close(self):
            pass

    class _FakeProc:
        def __init__(self):
            self.stdin = _FakeStdin()
            self._waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self._waits += 1
            if self._waits == 1:
                raise session_runtime.subprocess.TimeoutExpired("fake", timeout)
            return 0

        def kill(self):
            pass

    class _FakeLog:
        def write(self, _text):
            pass

        def close(self):
            pass

    def fake_send_command(self, payload):
        sent.append(payload)

    monkeypatch.setattr(SessionRuntime, "_send_command", fake_send_command)
    monkeypatch.setattr(SessionRuntime, "_unregister_process", lambda self: None)
    monkeypatch.setattr(session_runtime, "_kill_process_tree", lambda pid: killed.append(pid))

    try:
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.base_dir = tmp_path
        runtime.project_id = "p_abort"
        runtime.lock = threading.RLock()
        runtime.busy = True
        runtime.closed = False
        runtime.state = "running"
        runtime.pid = 4321
        runtime.proc = _FakeProc()
        runtime._message_seq = 0
        runtime._current_assistant_id = None
        runtime._partials = {}
        runtime._assistant_modes = {}
        runtime._cancelled_assistant_ids = set()
        runtime._autonomous_stop = threading.Event()
        runtime._log_handle = _FakeLog()
        runtime.history_path = os.path.join(tmp_path, "temp", "project_messages", "p_abort.json")
        os.makedirs(os.path.dirname(runtime.history_path), exist_ok=True)

        assistant = runtime._append_message("assistant", "", status="running", extra={"mode": "task"})
        assistant_id = assistant["id"]
        runtime._current_assistant_id = assistant_id
        runtime._partials[assistant_id] = "partial reply"
        runtime._assistant_modes[assistant_id] = "task"

        assert runtime.abort_current(grace_s=0) is True

        messages = runtime.messages()
        assert sent == [{"cmd": "abort", "assistant_id": assistant_id}]
        assert messages[-1]["status"] == "aborted"
        assert messages[-1]["content"] == "partial reply"
        assert messages[-1]["task"]["steps"][-1]["status"] == "aborted"
        assert runtime.busy is False
        assert runtime._current_assistant_id is None
        assert runtime.closed is True
        assert runtime.state == "stopped"
        assert killed == [4321]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_session_runtime_messages_settles_idle_stale_running_history():
    from launcher.session_runtime import SessionRuntime

    tmp_path = _sandbox_tmp("native-runtime-stale-running")

    try:
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.project_id = "p_stale"
        runtime.lock = threading.RLock()
        runtime.busy = False
        runtime._current_assistant_id = None
        runtime.history_path = os.path.join(tmp_path, "temp", "project_messages", "p_stale.json")
        os.makedirs(os.path.dirname(runtime.history_path), exist_ok=True)
        with open(runtime.history_path, "w", encoding="utf-8") as f:
            json.dump({
                "messages": [{
                    "id": "p_stale-1",
                    "seq": 1,
                    "role": "assistant",
                    "content": "",
                    "status": "running",
                    "created_at": "2026-01-01T00:00:00",
                    "task": {"steps": [
                        {"id": "execute", "label": "execute", "status": "running"},
                        {"id": "finish", "label": "finish", "status": "pending"},
                    ]},
                }],
            }, f)

        messages = runtime.messages()

        assert messages[0]["status"] == "aborted"
        assert messages[0]["content"] == "Stopped."
        assert [step["status"] for step in messages[0]["task"]["steps"]] == ["aborted", "aborted"]
        with open(runtime.history_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["messages"][0]["status"] == "aborted"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_history_context_filters_current_and_running_messages():
    from launcher.session_runtime import _build_history_context

    context = _build_history_context([
        {"id": "m1", "role": "system", "content": "Autonomous flow triggered."},
        {"id": "m2", "role": "user", "content": "old user request"},
        {"id": "m3", "role": "assistant", "content": "old answer", "status": "done"},
        {"id": "m4", "role": "assistant", "content": "still streaming", "status": "running"},
        {"id": "m5", "role": "assistant", "content": "failed answer", "status": "error"},
        {"id": "m6", "role": "tool", "content": "tool output"},
    ], exclude_ids={"m2"}, max_chars=1_000, recent_messages=10)

    assert "[system] Autonomous flow triggered." in context
    assert "old user request" not in context
    assert "[assistant] old answer" in context
    assert "still streaming" not in context
    assert "[assistant (error)] failed answer" in context
    assert "tool output" not in context


def test_session_runtime_send_passes_compact_same_project_history(monkeypatch):
    from launcher.session_runtime import SessionRuntime

    tmp_path = _sandbox_tmp("native-runtime-history-send")
    sent = []

    class _FakeProc:
        def poll(self):
            return None

    class _FakeLog:
        def write(self, _text):
            pass

    def fake_send_command(self, payload):
        sent.append(payload)

    monkeypatch.setattr(SessionRuntime, "_send_command", fake_send_command)

    try:
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.base_dir = tmp_path
        runtime.project_id = "p_history"
        runtime.lock = threading.RLock()
        runtime.busy = False
        runtime.closed = False
        runtime.proc = _FakeProc()
        runtime._message_seq = 0
        runtime._current_assistant_id = None
        runtime._partials = {}
        runtime._assistant_modes = {}
        runtime._cancelled_assistant_ids = set()
        runtime._log_handle = _FakeLog()
        runtime.history_path = os.path.join(tmp_path, "temp", "project_messages", "p_history.json")
        os.makedirs(os.path.dirname(runtime.history_path), exist_ok=True)
        with open(runtime.history_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [
                {"id": "p_history-1", "seq": 1, "role": "user", "content": "remember alpha", "status": "done"},
                {"id": "p_history-2", "seq": 2, "role": "assistant", "content": "alpha stored", "status": "done"},
                {"id": "p_history-3", "seq": 3, "role": "assistant", "content": "unfinished", "status": "running"},
            ]}, f)

        user_msg = runtime.send("what did I ask before?", requested_mode="chat")

        assert user_msg["role"] == "user"
        assert user_msg["content"] == "what did I ask before?"
        assert sent and sent[0]["cmd"] == "send"
        assert sent[0]["text"] == "what did I ask before?"
        assert "remember alpha" in sent[0]["history_context"]
        assert "alpha stored" in sent[0]["history_context"]
        assert "unfinished" not in sent[0]["history_context"]
        assert "what did I ask before?" not in sent[0]["history_context"]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_session_worker_injects_history_context_before_current_message(monkeypatch):
    from launcher import session_worker

    recorded = []

    class _FakeAgent:
        runtime_context = None

        def put_task(self, prompt, source="gui"):
            recorded.append((prompt, source))
            q = __import__("queue").Queue()
            q.put({"done": "ok"})
            return q

    worker = session_worker.Worker.__new__(session_worker.Worker)
    worker.agent = _FakeAgent()
    worker.project = {"id": "p_worker"}
    worker.thread = threading.current_thread()

    monkeypatch.setattr(worker, "update_context", lambda _project: None)

    worker.send(
        "current question",
        "assistant-1",
        mode="chat",
        source="gui",
        history_context="[user] earlier question\n\n[assistant] earlier answer",
    )

    prompt, source = recorded[0]
    assert source == "gui"
    assert "Same-project chat history" in prompt
    assert "[user] earlier question" in prompt
    assert prompt.index("Same-project chat history") < prompt.index("Current user message:")
    assert prompt.endswith("Current user message:\ncurrent question")


def test_runtime_profile_injection_targets_virtual_mixin(monkeypatch):
    import agentmain

    agent = agentmain.GeneraticAgent.__new__(agentmain.GeneraticAgent)
    agent.runtime_context = agentmain.AgentRuntimeContext(
        project_root="D:/tmp/project",
        llm_profile_name="daily",
    )

    monkeypatch.setattr(
        "launcher.profiles.load_profiles",
        lambda _root: {"active": None, "profiles": {"daily": ["slow", "fast"]}},
    )
    monkeypatch.setattr(
        "launcher.api_config.list_api_configs",
        lambda _root: [
            {"name": "slow", "kind": "native_oai", "category": "language", "priority": 1},
            {"name": "fast", "kind": "native_oai", "category": "language", "priority": 9},
        ],
    )

    mykeys = {}
    target_name, signature = agent._inject_runtime_profile_mixin(mykeys)

    assert target_name == "bot_session_daily"
    assert signature == "fast|slow"
    assert mykeys["mixin_config_bot_session_daily"]["name"] == target_name
    assert mykeys["mixin_config_bot_session_daily"]["llm_nos"] == ["fast", "slow"]


def test_session_intent_classifier():
    from launcher.session_runtime import _classify_intent

    assert _classify_intent("解释一下这个报错")["mode"] == "chat"
    assert _classify_intent("运行测试并修复失败")["mode"] == "task"
    assert _classify_intent("写一份产品方案")["mode"] == "canvas"
    assert _classify_intent("实现这个功能并生成文档")["mode"] == "task_canvas"
    assert _classify_intent("解释一下这个报错", "task")["mode"] == "task"


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
