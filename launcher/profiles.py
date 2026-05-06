"""Launcher API config profile storage.

Profiles are lightweight named groups of API config names used by the GUI to
filter config pickers. They do not change which configs are loaded by
llmcore; they only persist UI selection state.
"""
from __future__ import annotations

import json
import os
from typing import Any


CONFIG_FILE = "launcher_profiles.json"


def profile_path(base_dir: str) -> str:
    return os.path.join(base_dir, "temp", CONFIG_FILE)


def _empty() -> dict[str, Any]:
    return {"active": None, "profiles": {}}


def _normalize(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _empty()
    raw_profiles = data.get("profiles")
    profiles: dict[str, list[str]] = {}
    if isinstance(raw_profiles, dict):
        for name, members in raw_profiles.items():
            clean_name = str(name or "").strip()
            if not clean_name:
                continue
            if not isinstance(members, list):
                members = []
            seen: set[str] = set()
            clean_members: list[str] = []
            for member in members:
                value = str(member or "").strip()
                if value and value not in seen:
                    seen.add(value)
                    clean_members.append(value)
            profiles[clean_name] = clean_members
    active = data.get("active")
    active_name = str(active or "").strip() if active is not None else ""
    return {
        "active": active_name if active_name in profiles else None,
        "profiles": profiles,
    }


def load_profiles(base_dir: str) -> dict[str, Any]:
    path = profile_path(base_dir)
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _normalize(json.load(f))
    except Exception:
        return _empty()


def save_profiles(base_dir: str, state: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(state)
    os.makedirs(os.path.join(base_dir, "temp"), exist_ok=True)
    path = profile_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return normalized


def set_active_profile(base_dir: str, name: str | None) -> dict[str, Any]:
    state = load_profiles(base_dir)
    clean_name = str(name or "").strip()
    state["active"] = clean_name if clean_name and clean_name in state["profiles"] else None
    return save_profiles(base_dir, state)


def upsert_profile(base_dir: str, name: str, members: list[Any] | None = None) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("profile name is required")
    state = load_profiles(base_dir)
    state["profiles"][clean_name] = _normalize({"profiles": {clean_name: members or []}})["profiles"][clean_name]
    return save_profiles(base_dir, state)


def rename_profile(base_dir: str, old_name: str, new_name: str) -> dict[str, Any]:
    old_clean = str(old_name or "").strip()
    new_clean = str(new_name or "").strip()
    if not old_clean or not new_clean:
        raise ValueError("old and new profile names are required")
    state = load_profiles(base_dir)
    if old_clean not in state["profiles"]:
        raise KeyError(old_clean)
    if new_clean != old_clean and new_clean in state["profiles"]:
        raise ValueError(f"profile already exists: {new_clean}")
    members = state["profiles"].pop(old_clean)
    state["profiles"][new_clean] = members
    if state["active"] == old_clean:
        state["active"] = new_clean
    return save_profiles(base_dir, state)


def delete_profile(base_dir: str, name: str) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    state = load_profiles(base_dir)
    if clean_name not in state["profiles"]:
        raise KeyError(clean_name)
    del state["profiles"][clean_name]
    if state["active"] == clean_name:
        state["active"] = None
    return save_profiles(base_dir, state)
