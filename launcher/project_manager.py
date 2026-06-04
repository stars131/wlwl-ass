"""Project lifecycle: persistent metadata + native in-process sessions."""
from __future__ import annotations

import json
import contextlib
import os
import random
import secrets
import socket
import threading
from datetime import datetime

from launcher.launch_config import DEFAULT_OPTIONS, load_options, project_options
from launcher.session_runtime import SessionRuntimeRegistry


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _port_free(port: int) -> bool:
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


class ProjectManager:
    PORT_LO, PORT_HI = 18501, 18599

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.frontends_dir = os.path.join(base_dir, "frontends")
        self.json_path = os.path.join(base_dir, "temp", "projects.json")
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        self.lock = threading.Lock()
        self._sessions = SessionRuntimeRegistry(base_dir)
        self._load()

    def _load(self) -> None:
        if os.path.isfile(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.projects = data.get("projects", [])
                for project in self.projects:
                    self._normalize_project(project)
                self.active_id = data.get("active_id")
                return
            except Exception as e:
                print(f"[ProjectManager] load failed, starting fresh: {e}")
        self.projects = []
        self.active_id = None

    def _normalize_project(self, project: dict) -> dict:
        now = _now()
        project.setdefault("last_error", "")
        project.setdefault("pinned", False)
        project.setdefault("description", "")
        project.setdefault("created_at", now)
        project.setdefault("last_active", project.get("created_at") or now)
        project.setdefault("updated_at", project.get("last_active") or now)
        project.setdefault("llm_no", int(DEFAULT_OPTIONS["llm_no"]))
        project.setdefault("llm_config_name", "")
        project.setdefault("llm_profile_name", "")
        project.setdefault("permission_mode", DEFAULT_OPTIONS["permission_mode"])
        project.setdefault("project_root", DEFAULT_OPTIONS["project_root"])
        project.setdefault("use_project_context", DEFAULT_OPTIONS["use_project_context"])
        project.setdefault("autonomous_enabled", DEFAULT_OPTIONS["autonomous_enabled"])
        project.setdefault("autonomous_interval_s", DEFAULT_OPTIONS["autonomous_interval_s"])
        project.setdefault("port", None)
        project.setdefault("pid", None)
        return project

    def _touch_project(self, project: dict) -> str:
        stamp = _now()
        project["updated_at"] = stamp
        return stamp

    def _save(self) -> None:
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"projects": self.projects, "active_id": self.active_id}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.json_path)

    def _by_id(self, pid: str) -> dict | None:
        for project in self.projects:
            if project["id"] == pid:
                return project
        return None

    def get(self, project_id: str) -> dict | None:
        with self.lock:
            project = self._by_id(project_id)
            return {**project, "running": self.is_running(project)} if project else None

    def _used_ports(self) -> set[int]:
        return {p["port"] for p in self.projects if p.get("port")}

    def _alloc_port(self) -> int:
        used = self._used_ports()
        ports = list(range(self.PORT_LO, self.PORT_HI + 1))
        random.shuffle(ports)
        for port in ports:
            if port in used:
                continue
            if _port_free(port):
                return port
        raise RuntimeError("no free port in range")

    def _gen_id(self) -> str:
        existing = {p["id"] for p in self.projects}
        for _ in range(20):
            pid = "p_" + secrets.token_hex(4)
            if pid not in existing:
                return pid
        raise RuntimeError("id collision")

    def is_running(self, project: dict | None) -> bool:
        return bool(project and self._sessions.get(project["id"]))

    def list(self) -> dict:
        with self.lock:
            out = []
            for project in self.projects:
                out.append({**project, "running": self.is_running(project)})
            pinned = [p for p in out if p.get("pinned")]
            normal = [p for p in out if not p.get("pinned")]
            pinned.sort(key=lambda p: str(p.get("last_active") or ""), reverse=True)
            normal.sort(key=lambda p: str(p.get("last_active") or ""), reverse=True)
            return {"projects": pinned + normal, "active_id": self.active_id}

    def create(self, name: str, auto_start: bool = True, options: dict | None = None) -> dict:
        with self.lock:
            name = (name or "").strip() or "新对话"
            now = _now()
            opts = project_options(load_options(self.base_dir) if options is None else options)
            project = {
                "id": self._gen_id(),
                "name": name,
                "port": None,
                "pid": None,
                "created_at": now,
                "last_active": now,
                "updated_at": now,
                "llm_no": opts["llm_no"],
                "llm_config_name": "",
                "llm_profile_name": "",
                "permission_mode": opts["permission_mode"],
                "project_root": opts["project_root"],
                "use_project_context": opts["use_project_context"],
                "autonomous_enabled": opts["autonomous_enabled"],
                "autonomous_interval_s": opts["autonomous_interval_s"],
                "last_error": "",
                "pinned": False,
                "description": "",
            }
            self.projects.append(project)
            self.active_id = project["id"]
            self._save()
        if auto_start:
            self.start(project["id"])
        return project

    def set_llm(self, project_id: str, *, config_name=None, profile_name=None, llm_no=None) -> dict | None:
        """Update per-session LLM selection.

        A session can pin one config, bind to one profile, or fall back to
        ``llm_no``. Config/profile writes clear the other name field so the UI
        and runtime cannot disagree about the active binding.
        """
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return None
            if config_name is not None:
                project["llm_config_name"] = str(config_name).strip()
                project["llm_profile_name"] = ""
            if profile_name is not None:
                project["llm_profile_name"] = str(profile_name).strip()
                project["llm_config_name"] = ""
            if llm_no is not None:
                try:
                    project["llm_no"] = max(0, int(llm_no))
                except (TypeError, ValueError):
                    pass
            self._touch_project(project)
            self._save()
            updated = dict(project)
        runtime = self.session(project_id)
        if runtime and hasattr(runtime, "update_project"):
            runtime.update_project(updated)
        return updated

    def update_options(self, project_id: str, options: dict) -> bool:
        opts = project_options(options)
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            project.update(opts)
            self._touch_project(project)
            self._save()
            updated = dict(project)
        runtime = self.session(project_id)
        if runtime and hasattr(runtime, "update_project"):
            runtime.update_project(updated)
        return True

    def set_autonomous(self, project_id: str, enabled: bool) -> dict | None:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return None
            project["autonomous_enabled"] = bool(enabled)
            self._touch_project(project)
            self._save()
        runtime = self.session(project_id)
        if runtime:
            if hasattr(runtime, "update_project"):
                runtime.update_project(dict(project))
            runtime.set_autonomous(bool(enabled), trigger_now=True)
        return self.get(project_id)

    def _read_log_tail(self, log_path: str | None, max_chars: int = 1200) -> str:
        if not log_path or not os.path.exists(log_path):
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-max_chars:].strip()
        except Exception:
            return ""

    def start(self, project_id: str, *, resume_task_id=None) -> bool:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            if self.is_running(project):
                return True
            log_dir = os.path.join(self.base_dir, "temp", "project_logs")
            os.makedirs(log_dir, exist_ok=True)
            project["port"] = None
            project["pid"] = None
            project["last_error"] = ""
            project["log_path"] = os.path.join(log_dir, f"{project['id']}.log")
            project_snapshot = dict(project)
        try:
            runtime = self._sessions.start(project_snapshot, resume_task_id=resume_task_id)
        except Exception as exc:
            reason = f"Project '{project_snapshot['name']}' failed to start native session: {exc}"
            tail = self._read_log_tail(project_snapshot.get("log_path"))
            if tail:
                last_line = tail.splitlines()[-1].strip()
                if last_line:
                    reason = f"{reason}: {last_line}"
            with self.lock:
                project = self._by_id(project_id)
                if project:
                    project["pid"] = None
                    project["last_error"] = reason
                    self._touch_project(project)
                    self._save()
            raise RuntimeError(reason) from exc
        with self.lock:
            project = self._by_id(project_id)
            if project:
                project["log_path"] = runtime.log_path
                project["pid"] = runtime.pid
                project["port"] = None
                project["last_error"] = ""
                stamp = self._touch_project(project)
                project["last_active"] = stamp
                self._save()
        return True

    def stop(self, project_id: str, timeout: int = 5) -> bool:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
        try:
            self._sessions.stop(project_id)
        except Exception as e:
            print(f"[ProjectManager] stop {project_id} error: {e}")
        with self.lock:
            project = self._by_id(project_id)
            if project:
                project["pid"] = None
                project["last_error"] = ""
                self._touch_project(project)
                self._save()
        return True

    def abort_current_message(self, project_id: str):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return None
        runtime = self.session(project_id)
        if not runtime:
            return None
        restart_needed = False
        try:
            restart_needed = bool(runtime.abort_current())
        except Exception as exc:
            print(f"[ProjectManager] abort current message {project_id} error: {exc}")
            with contextlib.suppress(Exception):
                restart_needed = not runtime.is_alive()
        if restart_needed or not runtime.is_alive():
            self.start(project_id)
            runtime = self.session(project_id)
        self.touch(project_id)
        return runtime

    def session(self, project_id: str):
        return self._sessions.get(project_id)

    def rename(self, project_id: str, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            project["name"] = name
            self._touch_project(project)
            self._save()
        return True

    def delete(self, project_id: str, stop_first: bool = True) -> bool:
        if stop_first:
            self.stop(project_id)
        with self.lock:
            self.projects = [p for p in self.projects if p["id"] != project_id]
            if self.active_id == project_id:
                self.active_id = self.projects[0]["id"] if self.projects else None
            self._save()
        return True

    def set_active(self, project_id: str) -> bool:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            self.active_id = project_id
            stamp = self._touch_project(project)
            project["last_active"] = stamp
            self._save()
        return True

    def touch(self, project_id: str) -> bool:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            stamp = self._touch_project(project)
            project["last_active"] = stamp
            self._save()
        return True

    def pin(self, project_id: str, pinned: bool = True) -> bool:
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            project["pinned"] = bool(pinned)
            self._touch_project(project)
            self._save()
        return True

    def shutdown_all(self) -> None:
        self._sessions.stop_all()
        with self.lock:
            for project in self.projects:
                project["pid"] = None
                self._touch_project(project)
            self._save()

    def detach_all(self) -> None:
        self._sessions = SessionRuntimeRegistry(self.base_dir)
