"""Persistent launcher startup options."""
from __future__ import annotations

import json
import os


CONFIG_FILE = "launcher_options.json"

# Bot 启动开关已被移除：bot 的开/关由「凭据齐全 + SDK 装好」自动决定，
# 由 launcher.api_server._auto_start_configured_bots 在 GUI 启动时统一处理。
# 用户如需临时停掉某个 bot，使用 GUI 的 Bots tab。
DEFAULT_OPTIONS = {
    "scheduler": True,
    "llm_no": 0,
    "permission_mode": "auto",
    "project_root": "",
    "use_project_context": True,
    "autonomous_enabled": False,
}

PROJECT_OPTION_KEYS = (
    "llm_no",
    "permission_mode",
    "project_root",
    "use_project_context",
    "autonomous_enabled",
)
PERMISSION_MODES = {"ask", "auto", "read-only", "dangerous"}


def config_path(base_dir):
    return os.path.join(base_dir, "temp", CONFIG_FILE)


def normalize_options(options=None):
    raw = dict(DEFAULT_OPTIONS)
    raw.update(options or {})
    out = {}
    for key in ("scheduler", "use_project_context", "autonomous_enabled"):
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "on"}
        out[key] = bool(value)
    try:
        out["llm_no"] = max(0, int(raw.get("llm_no", 0)))
    except (TypeError, ValueError):
        out["llm_no"] = 0
    mode = str(raw.get("permission_mode") or DEFAULT_OPTIONS["permission_mode"]).strip()
    out["permission_mode"] = mode if mode in PERMISSION_MODES else DEFAULT_OPTIONS["permission_mode"]
    out["project_root"] = str(raw.get("project_root") or "").strip()
    return out


def project_options(options=None):
    normalized = normalize_options(options)
    return {key: normalized[key] for key in PROJECT_OPTION_KEYS}


def load_options(base_dir):
    path = config_path(base_dir)
    if not os.path.isfile(path):
        return normalize_options()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return normalize_options()
    return normalize_options(data)


def save_options(base_dir, options):
    normalized = normalize_options(options)
    os.makedirs(os.path.join(base_dir, "temp"), exist_ok=True)
    path = config_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return normalized
