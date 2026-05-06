"""Project lifecycle: persistent metadata + streamlit subprocess management."""
import json, os, random, secrets, socket, subprocess, sys, threading, time
from datetime import datetime

from launcher.launch_config import DEFAULT_OPTIONS, project_options

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _now():
    return datetime.now().isoformat(timespec="seconds")

try:
    import psutil

    def _pid_alive(pid):
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
except ImportError:
    if os.name == "nt":
        def _pid_alive(pid):
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=CREATE_NO_WINDOW,
                )
                return f" {pid} " in r.stdout
            except Exception:
                return False
    else:
        def _pid_alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except Exception:
                return False


def _port_alive(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def _port_free(port):
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


class ProjectManager:
    PORT_LO, PORT_HI = 18501, 18599

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.frontends_dir = os.path.join(base_dir, "frontends")
        self.json_path = os.path.join(base_dir, "temp", "projects.json")
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        self.lock = threading.Lock()
        self._procs = {}  # id -> Popen (only for processes we spawned this session)
        self._load()

    def _load(self):
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

    def _normalize_project(self, project):
        now = _now()
        project.setdefault("last_error", "")
        project.setdefault("pinned", False)
        project.setdefault("description", "")
        project.setdefault("created_at", now)
        project.setdefault("last_active", project.get("created_at") or now)
        project.setdefault("updated_at", project.get("last_active") or now)
        project.setdefault("llm_no", int(DEFAULT_OPTIONS["llm_no"]))
        project.setdefault("llm_config_name", "")  # ADR-0006: prefer name over index
        project.setdefault("permission_mode", DEFAULT_OPTIONS["permission_mode"])
        project.setdefault("project_root", DEFAULT_OPTIONS["project_root"])
        project.setdefault("use_project_context", DEFAULT_OPTIONS["use_project_context"])
        project.setdefault("autonomous_enabled", DEFAULT_OPTIONS["autonomous_enabled"])
        return project

    def _touch_project(self, project):
        stamp = _now()
        project["updated_at"] = stamp
        return stamp

    def _save(self):
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"projects": self.projects, "active_id": self.active_id}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.json_path)

    def _by_id(self, pid):
        for p in self.projects:
            if p["id"] == pid:
                return p
        return None

    def get(self, project_id):
        with self.lock:
            project = self._by_id(project_id)
            return {**project, "running": self.is_running(project)} if project else None

    def _used_ports(self):
        return {p["port"] for p in self.projects if p.get("port")}

    def _alloc_port(self):
        used = self._used_ports()
        ports = list(range(self.PORT_LO, self.PORT_HI + 1))
        random.shuffle(ports)
        for port in ports:
            if port in used:
                continue
            if _port_free(port):
                return port
        raise RuntimeError("no free port in range")

    def _gen_id(self):
        existing = {p["id"] for p in self.projects}
        for _ in range(20):
            pid = "p_" + secrets.token_hex(4)
            if pid not in existing:
                return pid
        raise RuntimeError("id collision")

    def is_running(self, project):
        pid = project.get("pid")
        port = project.get("port")
        if not pid or not port:
            return False
        return _pid_alive(pid) and _port_alive(port)

    def list(self):
        with self.lock:
            out = []
            for p in self.projects:
                out.append({**p, "running": self.is_running(p)})
            pinned = [p for p in out if p.get("pinned")]
            normal = [p for p in out if not p.get("pinned")]
            pinned.sort(key=lambda p: str(p.get("last_active") or ""), reverse=True)
            normal.sort(key=lambda p: str(p.get("last_active") or ""), reverse=True)
            return {"projects": pinned + normal, "active_id": self.active_id}

    def create(self, name, auto_start=True, options=None):
        with self.lock:
            name = (name or "").strip() or "新对话"
            now = _now()
            opts = project_options(options)
            project = {
                "id": self._gen_id(),
                "name": name,
                "port": self._alloc_port(),
                "pid": None,
                "created_at": now,
                "last_active": now,
                "updated_at": now,
                "llm_no": opts["llm_no"],
                "llm_config_name": "",
                "permission_mode": opts["permission_mode"],
                "project_root": opts["project_root"],
                "use_project_context": opts["use_project_context"],
                "autonomous_enabled": opts["autonomous_enabled"],
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

    def set_llm(self, project_id, *, config_name=None, llm_no=None):
        """Update per-session LLM selection. Either config_name OR llm_no.

        config_name="" clears the override (falls back to llm_no / default).
        Returns the updated project dict (without runtime fields like running).
        """
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return None
            if config_name is not None:
                project["llm_config_name"] = str(config_name).strip()
            if llm_no is not None:
                try:
                    project["llm_no"] = max(0, int(llm_no))
                except (TypeError, ValueError):
                    pass
            self._touch_project(project)
            self._save()
            return dict(project)

    def update_options(self, project_id, options):
        opts = project_options(options)
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            project.update(opts)
            self._touch_project(project)
            self._save()
        return True

    def _read_log_tail(self, log_path, max_chars=1200):
        if not log_path or not os.path.exists(log_path):
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-max_chars:].strip()
        except Exception:
            return ""

    def _spawn(self, project, *, resume_task_id=None):
        env = os.environ.copy()
        env["WLWL_PROJECT_NAME"] = project["name"]
        env["WLWL_PROJECT_ID"] = project["id"]
        env["WLWL_LLM_NO"] = str(project.get("llm_no", 0))
        env["WLWL_LLM_CONFIG_NAME"] = str(project.get("llm_config_name") or "")
        env["WLWL_PERMISSION_MODE"] = str(project.get("permission_mode") or DEFAULT_OPTIONS["permission_mode"])
        env["WLWL_PROJECT_ROOT"] = str(project.get("project_root") or "")
        env["WLWL_USE_PROJECT_CONTEXT"] = "1" if project.get("use_project_context", True) else "0"
        env["WLWL_AUTONOMOUS_ENABLED"] = "1" if project.get("autonomous_enabled", False) else "0"
        env["PYTHONUNBUFFERED"] = "1"
        # Resume hook: when the user picks "恢复" from a session card, the
        # backend forwards the chosen task_id here. The new session's
        # wlwl-ass agent will see WLWL_AUTO_CHECKPOINT_TASK_ID, load the
        # latest matching checkpoint via launcher.auto_checkpoint, and
        # inject its working/key_info before processing the first user
        # message.
        if resume_task_id:
            env["WLWL_AUTO_CHECKPOINT_TASK_ID"] = str(resume_task_id)
        cmd = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            os.path.join(self.frontends_dir, "stapp.py"),
            "--global.developmentMode",
            "false",
            "--server.port",
            str(project["port"]),
            "--server.address",
            "localhost",
            "--server.headless",
            "true",
        ]
        log_dir = os.path.join(self.base_dir, "temp", "project_logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{project['id']}.log")
        project["log_path"] = log_path
        log_f = open(log_path, "a", encoding="utf-8", errors="replace")
        log_f.write(f"\n\n=== spawn {datetime.now().isoformat()} ===\n")
        log_f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=self.base_dir,
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._procs[project["id"]] = proc
        # Best-effort registration with the central process registry. Failure
        # here must not break ``start`` — keep the launcher self-contained.
        try:
            from launcher.process_registry import get_registry
            get_registry(self.base_dir).register(
                f"session:{project['id']}",
                proc.pid,
                kind="streamlit",
                cmd=cmd,
                meta={"port": project.get("port"), "name": project.get("name")},
            )
        except Exception as exc:
            print(f"[ProjectManager] process_registry.register failed: {exc}")
        return proc.pid

    def start(self, project_id, *, resume_task_id=None):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            if self.is_running(project):
                return True
            if not _port_free(project["port"]) and not _port_alive(project["port"]):
                project["port"] = self._alloc_port()
            elif not _port_free(project["port"]) and _port_alive(project["port"]):
                project["port"] = self._alloc_port()
            project["last_error"] = ""
            project["pid"] = self._spawn(project, resume_task_id=resume_task_id)
            stamp = self._touch_project(project)
            project["last_active"] = stamp
            self._save()
        deadline = time.time() + 15
        while time.time() < deadline:
            if _port_alive(project["port"]):
                with self.lock:
                    project["last_error"] = ""
                    self._touch_project(project)
                    self._save()
                return True
            proc = self._procs.get(project_id)
            if proc is not None and proc.poll() is not None:
                break
            if project.get("pid") and not _pid_alive(project["pid"]):
                break
            time.sleep(0.3)
        tail = self._read_log_tail(project.get("log_path"))
        reason = f"Project '{project['name']}' failed to start on port {project['port']}"
        if tail:
            last_line = tail.splitlines()[-1].strip()
            if last_line:
                reason = f"{reason}: {last_line}"
        self._procs.pop(project_id, None)
        with self.lock:
            project["pid"] = None
            project["last_error"] = reason
            self._touch_project(project)
            self._save()
        raise RuntimeError(reason)

    def stop(self, project_id, timeout=5):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            pid = project.get("pid")
        if not pid:
            return True
        proc = self._procs.get(project_id)
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                if _pid_alive(pid):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            creationflags=CREATE_NO_WINDOW,
                        )
                    else:
                        try:
                            os.kill(pid, 15)
                        except Exception:
                            pass
        except Exception as e:
            print(f"[ProjectManager] stop {project_id} error: {e}")
        self._procs.pop(project_id, None)
        with self.lock:
            project["pid"] = None
            project["last_error"] = ""
            self._touch_project(project)
            self._save()
        # Detach from the registry too — safe even if it was never registered.
        try:
            from launcher.process_registry import get_registry
            get_registry(self.base_dir).unregister_label(f"session:{project_id}")
        except Exception:
            pass
        return True

    def rename(self, project_id, name):
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

    def delete(self, project_id, stop_first=True):
        if stop_first:
            self.stop(project_id)
        with self.lock:
            self.projects = [p for p in self.projects if p["id"] != project_id]
            if self.active_id == project_id:
                self.active_id = self.projects[0]["id"] if self.projects else None
            self._save()
        return True

    def set_active(self, project_id):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            self.active_id = project_id
            stamp = self._touch_project(project)
            project["last_active"] = stamp
            self._save()
        return True

    def touch(self, project_id):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            stamp = self._touch_project(project)
            project["last_active"] = stamp
            self._save()
        return True

    def pin(self, project_id, pinned=True):
        with self.lock:
            project = self._by_id(project_id)
            if not project:
                return False
            project["pinned"] = bool(pinned)
            self._touch_project(project)
            self._save()
        return True

    def shutdown_all(self):
        for p in list(self.projects):
            self.stop(p["id"])

    def detach_all(self):
        self._procs.clear()
