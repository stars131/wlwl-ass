import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LoadedProjectContext:
    root: str
    files: list[str]
    text: str = ""
    warnings: list[str] | None = None


def repo_root():
    return os.path.dirname(os.path.abspath(__file__))


def find_project_root(start_dir=None):
    cur = Path(start_dir or os.getcwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for path in (cur, *cur.parents):
        if (path / ".git").exists():
            return str(path)
    return str(cur)


def discover_context_files(root):
    root_path = Path(root).resolve()
    names = ("CLAUDE.md", "AGENTS.md", "README.md")
    return [str(root_path / name) for name in names if (root_path / name).is_file()]


def load_project_context(root=None, enabled=True):
    root = find_project_root(root)
    if not enabled:
        return LoadedProjectContext(root=root, files=[], text="", warnings=[])
    files = discover_context_files(root)
    chunks, warnings = [], []
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            chunks.append(f"\n[Project Context: {os.path.basename(path)}]\n{text[:12000]}\n")
        except Exception as e:
            warnings.append(f"{path}: {e}")
    return LoadedProjectContext(root=root, files=files, text="".join(chunks), warnings=warnings)


def get_project_id():
    return str(os.environ.get("WLWL_PROJECT_ID", "") or "").strip()


def get_project_name():
    return str(os.environ.get("WLWL_PROJECT_NAME", "") or "").strip()


def get_model_responses_root(base_dir=None):
    base_dir = base_dir or repo_root()
    return os.path.join(base_dir, "temp", "model_responses")


def get_model_responses_dir(base_dir=None, project_id=None):
    project_id = get_project_id() if project_id is None else str(project_id or "").strip()
    root = get_model_responses_root(base_dir)
    return os.path.join(root, project_id) if project_id else root


def get_model_response_globs(base_dir=None, project_id=None):
    return (os.path.join(get_model_responses_dir(base_dir, project_id), "model_responses_*.txt"),)


def get_model_response_log_path(pid=None, base_dir=None, project_id=None):
    pid = os.getpid() if pid is None else pid
    return os.path.join(get_model_responses_dir(base_dir, project_id), f"model_responses_{pid}.txt")
