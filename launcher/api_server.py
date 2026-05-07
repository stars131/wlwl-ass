"""HTTP API server for the wlwl-ass GUI (Tauri webview).

Phase 0 ships /api/health, /api/version, /api/openapi.json.

Phase 1 adds business endpoints backed by ProjectManager / api_config:

  GET    /api/projects                     -> list projects (running flag included)
  POST   /api/projects                     -> create new project; body: {name, options?}
  GET    /api/projects/<id>                -> single project detail
  DELETE /api/projects/<id>                -> stop + remove
  POST   /api/projects/<id>/start          -> start the streamlit subprocess
  POST   /api/projects/<id>/stop           -> stop the subprocess
  POST   /api/projects/<id>/activate       -> mark as last-active
  POST   /api/projects/<id>/pin            -> body: {pinned: bool}
  PATCH  /api/projects/<id>                -> body: {name?: str}  (rename)
  PUT    /api/projects/<id>/llm            -> body: {config_name?: str, llm_no?: int}
                                              ADR-0006 per-session API selection.

  GET    /api/configs                      -> list api_config entries (apikey masked)
  PUT    /api/configs                      -> replace the launcher config list

Run standalone:

    python -m launcher.api_server --port 18800
"""
from __future__ import annotations

import argparse
import atexit
import http.server
import json
import os
import platform
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import unquote, urlparse

API_VERSION = "v1"
APP_VERSION = "0.1.0"
READY_MARKER = "__GA_READY__"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_started_at = time.monotonic()
_project_manager = None  # lazy ProjectManager instance
_bot_manager = None  # lazy BotManager instance
_scheduler_proc: subprocess.Popen | None = None  # L4 reflect/scheduler.py child


# ─── Lazy backend wiring ───────────────────────────────────────────────


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pm():
    """Singleton ProjectManager. Imported lazily so health-only callers
    don't pay the import cost."""
    global _project_manager
    if _project_manager is None:
        from launcher.project_manager import ProjectManager
        _project_manager = ProjectManager(_project_root())
    return _project_manager


def _bm():
    """Singleton BotManager."""
    global _bot_manager
    if _bot_manager is None:
        from launcher.bot_manager import BotManager
        _bot_manager = BotManager(_project_root())
    return _bot_manager


# ─── Route handlers ────────────────────────────────────────────────────


def _route_health(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {
        "status": "ok",
        "uptime_s": round(time.monotonic() - _started_at, 3),
        "pid": os.getpid(),
    }


def _route_version(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {
        "version": APP_VERSION,
        "api": API_VERSION,
        "python": platform.python_version(),
        "platform": sys.platform,
    }


def _route_openapi(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {
        "openapi": "3.1.0",
        "info": {
            "title": "wlwl-ass API",
            "version": APP_VERSION,
            "description": "Phase 1: projects, configs, profiles. See module docstring.",
        },
        "paths": {p: {} for p in _all_paths()},
    }


def _route_projects_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = _pm().list()
    return 200, {"projects": data["projects"], "active_id": data["active_id"]}


def _route_projects_create(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = req["body"]
    name = str(body.get("name") or "").strip() or "新对话"
    options = body.get("options") if isinstance(body.get("options"), dict) else None
    project = _pm().create(name, auto_start=False, options=options)
    return 201, {"project": project}


def _route_project_get(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    project = _pm().get(req["params"]["id"])
    if not project:
        return 404, {"error": "not_found", "id": req["params"]["id"]}
    return 200, {"project": project}


def _route_project_delete(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    if not _pm().get(pid):
        return 404, {"error": "not_found", "id": pid}
    _pm().delete(pid)
    return 200, {"deleted": pid}


def _route_project_start(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    if not _pm().get(pid):
        return 404, {"error": "not_found", "id": pid}
    body = req.get("body") or {}
    resume_task_id = body.get("resume_task_id")
    try:
        _pm().start(pid, resume_task_id=resume_task_id) if resume_task_id else _pm().start(pid)
    except Exception as exc:
        return 500, {"error": "start_failed", "detail": str(exc)}
    return 200, {"project": _pm().get(pid)}


def _route_project_checkpoints(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """List the auto-checkpoints belonging to a session.

    auto_checkpoint generates task_ids of the shape ``auto-{project_id}-...``.
    We surface that subset (sorted newest-first) so the GUI's "恢复" UI
    can list resume points without having to walk the full task tree."""
    pid = req["params"]["id"]
    if not _pm().get(pid):
        return 404, {"error": "not_found", "id": pid}
    from launcher.checkpoint_manager import get_manager

    cm = get_manager(_project_root())
    prefix = f"auto-{pid}-"
    matching: list[dict[str, Any]] = []
    for task in cm.list_tasks():
        tid = str(task.get("task_id") or "")
        if tid.startswith(prefix):
            cps = cm.list_checkpoints(tid)
            if cps:
                latest = cps[-1]
                matching.append({
                    "task_id": tid,
                    "checkpoint_count": task.get("count"),
                    "latest_saved_at": task.get("latest_saved_at"),
                    "latest_note": task.get("latest_note") or "",
                    "latest_id": latest.get("id"),
                })
    matching.sort(key=lambda r: str(r.get("latest_saved_at") or ""), reverse=True)
    return 200, {"checkpoints": matching}


def _route_project_stop(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    if not _pm().get(pid):
        return 404, {"error": "not_found", "id": pid}
    _pm().stop(pid)
    return 200, {"project": _pm().get(pid)}


def _route_project_activate(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    if not _pm().set_active(pid):
        return 404, {"error": "not_found", "id": pid}
    return 200, {"project": _pm().get(pid)}


def _route_project_pin(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    pinned = bool(req["body"].get("pinned", True))
    if not _pm().pin(pid, pinned):
        return 404, {"error": "not_found", "id": pid}
    return 200, {"project": _pm().get(pid)}


def _route_project_patch(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pid = req["params"]["id"]
    body = req["body"]
    if "name" in body:
        ok = _pm().rename(pid, str(body["name"] or ""))
        if not ok:
            return 400, {"error": "rename_failed"}
    return 200, {"project": _pm().get(pid)}


def _route_project_set_llm(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """ADR-0006: PUT /api/projects/<id>/llm with {config_name?, llm_no?}."""
    pid = req["params"]["id"]
    body = req["body"]
    config_name = body.get("config_name")
    llm_no = body.get("llm_no")
    if config_name is None and llm_no is None:
        return 400, {"error": "missing_field", "expected": "config_name or llm_no"}
    updated = _pm().set_llm(pid, config_name=config_name, llm_no=llm_no)
    if updated is None:
        return 404, {"error": "not_found", "id": pid}
    return 200, {"project": _pm().get(pid)}


def _route_configs_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.api_config import list_api_configs

    return 200, {"configs": list_api_configs(_project_root())}


def _route_configs_save(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """PUT /api/configs — replace the whole list (mirrors Qt launcher save)."""
    from launcher.api_config import list_api_configs, save_api_configs

    body = req["body"]
    incoming = body.get("configs")
    if not isinstance(incoming, list):
        return 400, {"error": "missing_field", "expected": "configs: [..]"}
    try:
        save_api_configs(_project_root(), incoming)
    except ValueError as exc:
        return 400, {"error": "invalid_config", "detail": str(exc)}
    # Return through list_api_configs so apikey is masked, matching GET path.
    return 200, {"configs": list_api_configs(_project_root())}


def _route_profiles_get(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.profiles import load_profiles

    return 200, load_profiles(_project_root())


def _route_profiles_set_active(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.profiles import set_active_profile

    body = req.get("body") or {}
    name = body.get("name")
    return 200, set_active_profile(_project_root(), str(name) if name is not None else None)


def _route_profiles_upsert(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.profiles import upsert_profile

    body = req.get("body") or {}
    name = str(body.get("name") or "").strip()
    members = body.get("members")
    if not isinstance(members, list):
        members = []
    try:
        return 200, upsert_profile(_project_root(), name, members)
    except ValueError as exc:
        return 400, {"error": "invalid_profile", "detail": str(exc)}


def _route_profiles_rename(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.profiles import rename_profile

    old_name = unquote(req["params"]["name"])
    new_name = str((req.get("body") or {}).get("new_name") or "").strip()
    try:
        return 200, rename_profile(_project_root(), old_name, new_name)
    except KeyError:
        return 404, {"error": "not_found", "name": old_name}
    except ValueError as exc:
        return 400, {"error": "invalid_profile", "detail": str(exc)}


def _route_profiles_delete(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.profiles import delete_profile

    name = unquote(req["params"]["name"])
    try:
        return 200, delete_profile(_project_root(), name)
    except KeyError:
        return 404, {"error": "not_found", "name": name}


def _route_settings_get(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """GET /api/settings — read launcher_options.json normalised."""
    from launcher.launch_config import load_options

    return 200, {"settings": load_options(_project_root())}


def _route_settings_put(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """PUT /api/settings — overwrite launcher_options.json with normalised body."""
    from launcher.launch_config import load_options, save_options

    body = req["body"]
    if not isinstance(body, dict):
        return 400, {"error": "invalid_body"}
    # Merge over existing so the UI can send partial updates.
    merged = {**load_options(_project_root()), **body}
    saved = save_options(_project_root(), merged)
    return 200, {"settings": saved}


# ─── Bot credentials editor ───────────────────────────────────────────
#
# Backed by :mod:`launcher.config_store` (~/.wlwl-ass/config.json + project
# layer) — the single source of truth. The legacy Python-data-file path
# (mykey.py / mykey_local_override.py) has been retired; writes always
# go to ``config_store.set_bot``, no derivative files.
#
# We surface ONLY a known whitelist of fields per bot. The GUI never sees
# unrelated keys (LLM apikeys, Sophub tokens, Langfuse, etc).

_BOT_CRED_FIELDS = {
    "tg": ["tg_bot_token", "tg_allowed_users"],
    "qq": ["qq_app_id", "qq_app_secret", "qq_allowed_users"],
    "feishu": ["fs_app_id", "fs_app_secret", "fs_allowed_users"],
    "wecom": ["wecom_bot_id", "wecom_secret", "wecom_allowed_users", "wecom_welcome_message"],
    "dingtalk": ["dingtalk_client_id", "dingtalk_client_secret", "dingtalk_allowed_users"],
}
_ALL_BOT_FIELDS = {f for fs in _BOT_CRED_FIELDS.values() for f in fs}

# Bidirectional mapping between the legacy flat field name (``fs_app_id``)
# and config_store coordinates (``bots.feishu.app_id``). One source of
# truth — both _read and _write resolve through this table.
_FIELD_TO_STORE: dict[str, tuple[str, str]] = {
    "tg_bot_token":            ("telegram", "bot_token"),
    "tg_allowed_users":        ("telegram", "allowed_users"),
    "qq_app_id":               ("qq",       "app_id"),
    "qq_app_secret":           ("qq",       "app_secret"),
    "qq_allowed_users":        ("qq",       "allowed_users"),
    "fs_app_id":               ("feishu",   "app_id"),
    "fs_app_secret":           ("feishu",   "app_secret"),
    "fs_allowed_users":        ("feishu",   "allowed_users"),
    "wecom_bot_id":            ("wecom",    "bot_id"),
    "wecom_secret":            ("wecom",    "secret"),
    "wecom_allowed_users":     ("wecom",    "allowed_users"),
    "wecom_welcome_message":   ("wecom",    "welcome_message"),
    "dingtalk_client_id":      ("dingtalk", "client_id"),
    "dingtalk_client_secret":  ("dingtalk", "client_secret"),
    "dingtalk_allowed_users":  ("dingtalk", "allowed_users"),
}


def _read_credentials() -> dict[str, Any]:
    """Read whitelisted bot credentials.

    Single source of truth: :func:`llmcore.reload_mykeys` already merges
    ``.env`` / ``~/.wlwl-ass/config.json`` / ``temp/launcher_api_configs.json``.
    We then overlay ``config_store`` directly so any per-field write done
    in this process (e.g. PUT /api/credentials) is visible immediately,
    without waiting for llmcore's mtime cache to invalidate.
    """
    out: dict[str, Any] = {}

    try:
        import llmcore
        merged = llmcore.reload_mykeys(project_root=_project_root())[0]
        for k in _ALL_BOT_FIELDS:
            v = merged.get(k)
            if v not in (None, "", []):
                out[k] = v
    except Exception:
        pass

    # Fresh per-field overlay from config_store (covers in-process writes).
    try:
        from launcher import config_store as _cs
        store = _cs.default_store()
        for flat_name, (bot_name, store_field) in _FIELD_TO_STORE.items():
            v = store.get(f"bots.{bot_name}.{store_field}")
            if v not in (None, "", []):
                out[flat_name] = v
    except Exception:
        pass

    return out


def _route_credentials_get(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """GET /api/credentials → {fields: {bot:[..]}, values: {bot:{field:val}}}"""
    creds = _read_credentials()
    grouped: dict[str, dict[str, Any]] = {}
    for bot, fields in _BOT_CRED_FIELDS.items():
        grouped[bot] = {f: creds.get(f, "") for f in fields}
        # Mask secrets but leave list-valued fields intact for round-trip.
        for f in fields:
            if any(token in f for token in ("secret", "token")) and grouped[bot][f]:
                grouped[bot][f] = "***"
    return 200, {"fields": _BOT_CRED_FIELDS, "values": grouped}


def _route_credentials_put(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """PUT /api/credentials body: {<field>: <value>, ...}

    Only fields in the whitelist are written. ``***`` values are treated
    as "no change" so the UI can echo the masked GET payload back without
    nuking real values. Writes go to ``launcher.config_store``.
    """
    body = req["body"]
    if not isinstance(body, dict):
        return 400, {"error": "invalid_body"}
    incoming = {k: v for k, v in body.items() if k in _ALL_BOT_FIELDS}
    if not incoming:
        return 400, {"error": "no_known_fields"}

    try:
        from launcher import config_store as _cs
        store = _cs.default_store()
    except Exception as exc:  # pragma: no cover — store import shouldn't fail
        return 500, {"error": "config_store_unavailable", "detail": str(exc)}

    # Group incoming flat fields by bot, then set or delete each.
    by_bot: dict[str, dict[str, Any]] = {}
    deletes: dict[str, list[str]] = {}
    for flat_name, value in incoming.items():
        bot_name, store_field = _FIELD_TO_STORE[flat_name]
        if value == "***":
            continue  # masked round-trip — preserve existing
        if isinstance(value, str) and not value.strip():
            deletes.setdefault(bot_name, []).append(store_field)
            continue
        by_bot.setdefault(bot_name, {})[store_field] = value

    # Apply sets first, then targeted deletes for cleared fields.
    for bot_name, fields in by_bot.items():
        store.set_bot(bot_name, fields, layer="user", merge=True)
    for bot_name, fields in deletes.items():
        existing = store.get_bot(bot_name)
        for f in fields:
            existing.pop(f, None)
        # Replace whole entry with the trimmed copy so the cleared field
        # actually disappears (set_bot with merge=True wouldn't drop it).
        if existing:
            store.set_bot(bot_name, existing, layer="user", merge=False)
        else:
            store.delete_bot(bot_name, layer="user")

    return _route_credentials_get(req)


def _route_bots_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.bot_manager import BOT_SPECS

    statuses = _bm().status_all()
    rows = []
    for key, spec in BOT_SPECS.items():
        st = statuses[key]
        rows.append({
            "key": key,
            "display_name": spec.display_name,
            "script": spec.script,
            "configured": st.configured,
            "missing_fields": st.missing_fields,
            "sdk_installed": st.sdk_installed,
            "missing_modules": st.missing_modules,
            "running_self": st.running_self,
            "running_external": st.running_external,
            "running": st.running,
            "log_path": st.log_path,
        })
    return 200, {"bots": rows}


def _route_bot_start(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.bot_manager import BOT_SPECS

    key = req["params"]["key"]
    if key not in BOT_SPECS:
        return 404, {"error": "unknown_bot", "key": key}
    ok, message = _bm().start(key)
    if not ok:
        return 409, {"error": "start_failed", "key": key, "message": message}
    return 200, {"key": key, "message": message}


def _route_bot_stop(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.bot_manager import BOT_SPECS

    key = req["params"]["key"]
    if key not in BOT_SPECS:
        return 404, {"error": "unknown_bot", "key": key}
    ok, message = _bm().stop(key)
    if not ok:
        return 500, {"error": "stop_failed", "key": key, "message": message}
    return 200, {"key": key, "message": message}


def _route_bot_install_sdk(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/bots/<key>/install_sdk → run ``pip install`` for the bot's
    declared sdk_modules. Synchronous; the operation is short enough (~10s)
    that we return when finished and let the UI show the result. Captures
    stdout/stderr into a temp log so users can debug failures.
    """
    from launcher.bot_manager import BOT_SPECS

    key = req["params"]["key"]
    if key not in BOT_SPECS:
        return 404, {"error": "unknown_bot", "key": key}
    spec = BOT_SPECS[key]
    if not spec.sdk_modules:
        return 400, {"error": "no_sdk_for_bot", "key": key}
    # Map import names to PyPI package names where they differ.
    pkg_map = {
        "lark_oapi": "lark-oapi",
        "wecom_aibot_sdk": "wecom-aibot-sdk",
        "dingtalk_stream": "dingtalk-stream",
        "telegram": "python-telegram-bot",
        "Crypto": "pycryptodome",
    }
    packages = [pkg_map.get(mod, mod) for mod in spec.sdk_modules]
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    log_dir = os.path.join(_project_root(), "temp")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"pip_install_{key}.log")
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
            logf.write(f"$ {' '.join(cmd)}\n")
            logf.flush()
            import subprocess as _sp
            r = _sp.run(cmd, stdout=logf, stderr=_sp.STDOUT, timeout=300)
    except Exception as exc:
        return 500, {"error": "install_failed", "detail": str(exc), "log_path": log_path}
    return (200 if r.returncode == 0 else 500), {
        "ok": r.returncode == 0,
        "returncode": r.returncode,
        "packages": packages,
        "log_path": log_path,
    }


def _route_project_open(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/projects/<id>/open → open the streamlit URL in the system
    default browser. Workaround for the Tauri webview not handling
    ``<a target="_blank">`` (no shell plugin loaded)."""
    pid = req["params"]["id"]
    project = _pm().get(pid)
    if not project:
        return 404, {"error": "not_found", "id": pid}
    port = project.get("port")
    if not port:
        return 400, {"error": "no_port"}
    url = f"http://127.0.0.1:{port}/"
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess as _sp
            _sp.Popen(["open", url])
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open", url])
    except Exception as exc:
        return 500, {"error": "open_failed", "detail": str(exc), "url": url}
    return 200, {"opened": True, "url": url}


def _route_llm_test(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/llm/test body:
        {kind, apibase, apikey, model, name?}

    Issues a tiny synchronous request against the configured endpoint to
    verify credentials. Returns:

        {ok: bool, latency_ms: float, status: int, sample?: str, error?: str}

    Behaviour by ``kind``:
      * native_oai / (any non-claude kind) → POST {apibase}/chat/completions
        with one user message and ``max_tokens: 1``.
      * native_claude → POST {apibase}/messages (Anthropic shape) with
        ``max_tokens: 1``.
      * mixin → use the resolved member configs by name. The frontend can
        also POST resolved fields directly; we treat ``llm_nos`` as a hint
        and look members up via list_api_configs.
    """
    body = req.get("body") or {}
    kind = str(body.get("kind") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    apibase = str(body.get("apibase") or "").strip().rstrip("/")
    apikey = str(body.get("apikey") or "").strip()
    model = str(body.get("model") or "").strip()

    # If the UI sends a masked/empty key but provides a name, look up the
    # stored config server-side so the user doesn't have to re-enter it.
    if (not apikey or apikey == "***") and name:
        from launcher.api_config import get_api_config_raw
        try:
            stored = get_api_config_raw(_project_root(), name)
        except Exception:
            stored = None
        if stored:
            apikey = apikey if apikey and apikey != "***" else str(stored.get("apikey") or "")
            apibase = apibase or str(stored.get("apibase") or "").rstrip("/")
            model = model or str(stored.get("model") or "")
            kind = kind or str(stored.get("kind") or "").lower()

    if kind == "mixin":
        return 400, {"error": "mixin_not_directly_testable",
                     "detail": "Test each member config individually."}
    if not apibase or not apikey or not model:
        return 400, {"error": "missing_field",
                     "expected": "apibase, apikey, model (or a stored name)"}

    import requests as _rq
    from llmcore._utils import auto_make_url
    is_claude = kind == "native_claude" or "anthropic" in apibase or "claude" in apibase
    t0 = time.monotonic()
    try:
        if is_claude:
            url = auto_make_url(apibase, "messages")
            headers = {
                "x-api-key": apikey,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            resp = _rq.post(url, json=payload, headers=headers, timeout=20)
        else:
            url = auto_make_url(apibase, "chat/completions")
            headers = {
                "Authorization": f"Bearer {apikey}",
                "content-type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            resp = _rq.post(url, json=payload, headers=headers, timeout=20)
    except Exception as exc:
        return 200, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                     "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
    elapsed = round((time.monotonic() - t0) * 1000, 1)
    # An OpenAI-compatible endpoint must return JSON. If it returned HTML
    # (very common when the apibase host is correct but the path was
    # wrong, e.g. provider's landing page), surface that as a failure
    # even though HTTP was 200 — otherwise the user thinks their key works
    # when actually we hit the marketing site.
    content_type = (resp.headers.get("content-type") or "").lower()
    sample = ""
    try:
        body_text = resp.text or ""
        sample = body_text[:300]
    except Exception:
        pass
    looks_like_html = "html" in content_type or sample.lstrip().lower().startswith(("<!doctype", "<html"))
    ok = (resp.status_code < 400) and (not looks_like_html)
    err = None
    if resp.status_code >= 400:
        err = f"HTTP {resp.status_code}"
    elif looks_like_html:
        err = "endpoint returned HTML (apibase wrong? expected JSON from /chat/completions)"
    # Feed token usage into llmcore counters when we got a clean JSON
    # response — even a 1-token test costs money, so the GUI's Token
    # Usage tab should reflect it.
    if ok:
        try:
            from llmcore._usage import _record_usage
            data = resp.json()
            usage = data.get("usage") if isinstance(data, dict) else None
            if usage:
                _record_usage(usage, "messages" if is_claude else "chat_completions")
        except Exception:
            pass
    return 200, {
        "ok": ok,
        "status": resp.status_code,
        "latency_ms": elapsed,
        "sample": sample,
        "error": err,
        "url": url,
    }


def _route_bot_log(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return the last N lines of a bot's log file. Useful for debugging from
    the UI without the user opening the file system."""
    from launcher.bot_manager import BOT_SPECS

    key = req["params"]["key"]
    if key not in BOT_SPECS:
        return 404, {"error": "unknown_bot", "key": key}
    st = _bm().status(key)
    path = st.log_path
    if not path or not os.path.isfile(path):
        return 200, {"key": key, "path": path, "lines": [], "exists": False}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as exc:
        return 500, {"error": "read_failed", "detail": str(exc)}
    # Cap the response so we don't send 50 MB of bot chatter to a webview
    tail = text[-16000:] if len(text) > 16000 else text
    lines = tail.splitlines()[-200:]
    return 200, {"key": key, "path": path, "lines": lines, "exists": True}


def _route_activity_recent(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return the latest agent activity events (tool calls + turn boundaries)."""
    from launcher import activity_log as _alog

    raw_limit = req.get("query", {}).get("limit", "200")
    try:
        limit = max(1, min(2000, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 200
    events = _alog.iter_recent(limit)
    return 200, {"events": events, "path": _alog.latest_path()}


def _route_skill_outcomes(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Aggregate turn_end events into per-skill outcome counts."""
    from launcher import activity_log as _alog

    summary = _alog.summarize_outcomes()
    skills = [{"name": name, **stats} for name, stats in summary.items()]
    skills.sort(key=lambda s: s["total"], reverse=True)
    return 200, {"skills": skills}


def _route_skills_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return SOP files merged with their outcome counts (the skill catalogue)."""
    from launcher import skills as _skills

    return 200, {"skills": _skills.list_skills()}


def _route_token_usage(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Snapshot llmcore's running token-usage counters."""
    import llmcore as _llm

    return 200, _llm.get_token_usage()


def _route_token_usage_reset(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Zero the token-usage counters (GUI 'reset' button)."""
    import llmcore as _llm

    _llm.reset_token_usage()
    return 200, _llm.get_token_usage()


def _route_onboarding_status(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """First-run detection: does the user have a usable LLM config?"""
    from launcher import onboarding as _ob

    return 200, _ob.status()


def _route_onboarding_save(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Wizard submit — persist provider + key/base/model into ``.env``."""
    from launcher import onboarding as _ob

    body = req.get("body") or {}
    provider = str(body.get("provider", "") or "")
    apikey = str(body.get("apikey", "") or "")
    base_url = str(body.get("base_url", "") or "")
    model = str(body.get("model", "") or "")
    try:
        result = _ob.save_minimal(provider, apikey=apikey, base_url=base_url, model=model)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, result


def _route_doctor(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run health-check diagnostics. ``?network=1`` opts into apibase probes."""
    from launcher import doctor as _doctor

    network = str(req.get("query", {}).get("network", "")).lower() in ("1", "true", "yes")
    return 200, _doctor.run_diagnostics(include_network=network)


# ── Process registry (#19) ────────────────────────────────────────────


def _route_processes_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.process_registry import get_registry
    return 200, {"processes": get_registry(_project_root()).list()}


def _route_processes_register(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.process_registry import get_registry
    body = req.get("body") or {}
    label = str(body.get("label") or "").strip()
    pid = body.get("pid")
    if not label or not isinstance(pid, int):
        return 400, {"error": "missing_field", "expected": "label, pid (int)"}
    entry = get_registry(_project_root()).register(
        label, int(pid),
        kind=str(body.get("kind") or "proc"),
        cmd=body.get("cmd"),
        meta=body.get("meta") if isinstance(body.get("meta"), dict) else None,
    )
    return 200, {"entry": entry}


def _route_processes_kill(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.process_registry import get_registry
    body = req.get("body") or {}
    target = body.get("pid", body.get("label"))
    force = bool(body.get("force"))
    if target is None:
        return 400, {"error": "missing_field", "expected": "pid or label"}
    ok, msg = get_registry(_project_root()).kill(target, force=force)
    return (200 if ok else 404), {"ok": ok, "message": msg}


def _route_processes_cleanup(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.process_registry import get_registry
    removed = get_registry(_project_root()).cleanup_dead()
    return 200, {"removed": removed}


# ── Checkpoint manager (#20) ─────────────────────────────────────────


def _route_checkpoints_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.checkpoint_manager import get_manager
    return 200, {"tasks": get_manager(_project_root()).list_tasks()}


def _route_checkpoints_get_task(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.checkpoint_manager import get_manager
    task_id = req["params"]["task_id"]
    return 200, {"checkpoints": get_manager(_project_root()).list_checkpoints(task_id)}


def _route_checkpoints_save(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.checkpoint_manager import get_manager
    task_id = req["params"]["task_id"]
    body = req.get("body") or {}
    state = body.get("state")
    note = str(body.get("note") or "")
    if state is None:
        return 400, {"error": "missing_field", "expected": "state"}
    cp = get_manager(_project_root()).save(task_id, state, note=note)
    return 200, {"checkpoint": cp}


def _route_checkpoints_load(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.checkpoint_manager import get_manager
    task_id = req["params"]["task_id"]
    cp_id = req.get("query", {}).get("id")
    cp = get_manager(_project_root()).load(task_id, checkpoint_id=cp_id)
    if cp is None:
        return 404, {"error": "not_found"}
    return 200, {"checkpoint": cp}


def _route_checkpoints_clear(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher.checkpoint_manager import get_manager
    task_id = req["params"]["task_id"]
    n = get_manager(_project_root()).clear(task_id)
    return 200, {"removed": n}


# ── Trajectory export (#32) ──────────────────────────────────────────


def _route_trajectory_export(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from launcher import trajectory as _traj
    q = req.get("query", {})
    out = _traj.export_compressed(
        max_blob_chars=int(q.get("max_blob_chars", 200)),
        include_args=str(q.get("include_args", "1")).lower() not in {"0", "false", ""},
    )
    return 200, out


# ── Skills self-improve (#6) ─────────────────────────────────────────


def _route_skills_proposals_list(_req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from memory import skill_self_improve as _ssi
    return 200, {"proposals": _ssi.list_proposals()}


def _route_skills_proposals_create(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from memory import skill_self_improve as _ssi
    body = req.get("body") or {}
    skill_id = str(body.get("skill_id") or "").strip()
    diff = str(body.get("diff") or "")
    reason = str(body.get("reason") or "")
    if not skill_id or not diff:
        return 400, {"error": "missing_field", "expected": "skill_id, diff"}
    p = _ssi.propose_patch(skill_id, diff, reason=reason)
    return 200, {"proposal": p}


def _route_skills_proposals_decide(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from memory import skill_self_improve as _ssi
    pid = req["params"]["proposal_id"]
    body = req.get("body") or {}
    decision = str(body.get("decision") or "")
    if decision not in {"accept", "reject"}:
        return 400, {"error": "invalid_decision", "expected": "accept or reject"}
    if decision == "accept":
        ok, msg = _ssi.accept(pid)
    else:
        ok, msg = _ssi.reject(pid)
    return (200 if ok else 404), {"ok": ok, "message": msg}


def _route_skills_proposals_preview(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Dry-run a proposal: return the (before, after) text without writing."""
    from memory import skill_self_improve as _ssi
    pid = req["params"]["proposal_id"]
    return 200, _ssi.preview_patch(pid)


# Path patterns. `<id>` is the projects' generated id.
ROUTES: list[tuple[str, str, Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]]] = [
    ("GET", "/api/health", _route_health),
    ("GET", "/api/version", _route_version),
    ("GET", "/api/openapi.json", _route_openapi),
    ("GET", "/api/projects", _route_projects_list),
    ("POST", "/api/projects", _route_projects_create),
    ("GET", "/api/projects/<id>", _route_project_get),
    ("DELETE", "/api/projects/<id>", _route_project_delete),
    ("POST", "/api/projects/<id>/start", _route_project_start),
    ("POST", "/api/projects/<id>/stop", _route_project_stop),
    ("POST", "/api/projects/<id>/activate", _route_project_activate),
    ("POST", "/api/projects/<id>/pin", _route_project_pin),
    ("PATCH", "/api/projects/<id>", _route_project_patch),
    ("PUT", "/api/projects/<id>/llm", _route_project_set_llm),
    ("GET", "/api/projects/<id>/checkpoints", _route_project_checkpoints),
    ("GET", "/api/configs", _route_configs_list),
    ("PUT", "/api/configs", _route_configs_save),
    ("GET", "/api/profiles", _route_profiles_get),
    ("POST", "/api/profiles", _route_profiles_upsert),
    ("PUT", "/api/profiles/active", _route_profiles_set_active),
    ("PATCH", "/api/profiles/<name>", _route_profiles_rename),
    ("DELETE", "/api/profiles/<name>", _route_profiles_delete),
    ("GET", "/api/bots", _route_bots_list),
    ("POST", "/api/bots/<key>/start", _route_bot_start),
    ("POST", "/api/bots/<key>/stop", _route_bot_stop),
    ("POST", "/api/bots/<key>/install_sdk", _route_bot_install_sdk),
    ("GET", "/api/bots/<key>/log", _route_bot_log),
    ("POST", "/api/projects/<id>/open", _route_project_open),
    ("POST", "/api/llm/test", _route_llm_test),
    ("GET", "/api/settings", _route_settings_get),
    ("PUT", "/api/settings", _route_settings_put),
    ("GET", "/api/credentials", _route_credentials_get),
    ("PUT", "/api/credentials", _route_credentials_put),
    ("GET", "/api/activity", _route_activity_recent),
    ("GET", "/api/skills/outcomes", _route_skill_outcomes),
    ("GET", "/api/skills", _route_skills_list),
    ("GET", "/api/token_usage", _route_token_usage),
    ("POST", "/api/token_usage/reset", _route_token_usage_reset),
    ("GET", "/api/onboarding/status", _route_onboarding_status),
    ("POST", "/api/onboarding/save", _route_onboarding_save),
    ("GET", "/api/doctor", _route_doctor),
    ("GET", "/api/processes", _route_processes_list),
    ("POST", "/api/processes", _route_processes_register),
    ("POST", "/api/processes/kill", _route_processes_kill),
    ("POST", "/api/processes/cleanup", _route_processes_cleanup),
    ("GET", "/api/checkpoints", _route_checkpoints_list),
    ("GET", "/api/checkpoints/<task_id>", _route_checkpoints_get_task),
    ("POST", "/api/checkpoints/<task_id>", _route_checkpoints_save),
    ("GET", "/api/checkpoints/<task_id>/load", _route_checkpoints_load),
    ("DELETE", "/api/checkpoints/<task_id>", _route_checkpoints_clear),
    ("GET", "/api/trajectory/export", _route_trajectory_export),
    ("GET", "/api/skills/proposals", _route_skills_proposals_list),
    ("POST", "/api/skills/proposals", _route_skills_proposals_create),
    ("POST", "/api/skills/proposals/<proposal_id>/decide", _route_skills_proposals_decide),
    ("GET", "/api/skills/proposals/<proposal_id>/preview", _route_skills_proposals_preview),
]


def _all_paths() -> list[str]:
    return [p for _m, p, _h in ROUTES]


def _match_route(method: str, path: str):
    """Returns (handler, params_dict) or (None, None)."""
    for m, pattern, handler in ROUTES:
        if m != method:
            continue
        params = _match_pattern(pattern, path)
        if params is not None:
            return handler, params
    return None, None


def _match_pattern(pattern: str, path: str):
    p_parts = pattern.split("/")
    a_parts = path.split("/")
    if len(p_parts) != len(a_parts):
        return None
    out: dict[str, str] = {}
    for pp, ap in zip(p_parts, a_parts):
        if pp.startswith("<") and pp.endswith(">"):
            out[pp[1:-1]] = ap
        elif pp != ap:
            return None
    return out


# ─── HTTP plumbing ─────────────────────────────────────────────────────


# SSE streaming endpoints. Handlers receive the request handler instance
# and the matched path params, then write the response themselves —
# headers + body — until the client disconnects. Paths use the same
# `<id>` syntax as ROUTES.
SSE_ROUTES: list[tuple[str, Callable[["_Handler", dict[str, str]], None]]] = []


def _sse_route(pattern: str):
    def deco(fn: Callable[["_Handler", dict[str, str]], None]):
        SSE_ROUTES.append((pattern, fn))
        return fn
    return deco


def _send_sse_headers(handler: "_Handler") -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")  # nginx hint
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()


def _send_sse(handler: "_Handler", data: str, *, event: str | None = None) -> bool:
    """Write a single SSE frame. Returns False if the client has disconnected."""
    try:
        if event:
            handler.wfile.write(f"event: {event}\n".encode("utf-8"))
        for line in data.splitlines() or [""]:
            handler.wfile.write(f"data: {line}\n".encode("utf-8"))
        handler.wfile.write(b"\n")
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _tail_log_file(
    handler: "_Handler",
    path: str,
    *,
    initial_chars: int = 4000,
    poll_interval: float = 0.5,
    heartbeat: float = 25.0,
    max_seconds: float = 600.0,
) -> None:
    """Stream a log file as SSE: send the existing tail then follow new appends.

    Stops when the client disconnects, when max_seconds elapses, or when the
    file is rotated/deleted.
    """
    _send_sse_headers(handler)
    if not _send_sse(handler, json.dumps({"path": path, "exists": os.path.isfile(path)}), event="meta"):
        return

    if not os.path.isfile(path):
        # No file yet — keep heartbeating until it appears or the client leaves.
        deadline = time.monotonic() + max_seconds
        last_beat = time.monotonic()
        while time.monotonic() < deadline:
            if os.path.isfile(path):
                break
            now = time.monotonic()
            if now - last_beat > heartbeat:
                if not _send_sse(handler, "ping", event="heartbeat"):
                    return
                last_beat = now
            time.sleep(poll_interval)
        if not os.path.isfile(path):
            _send_sse(handler, json.dumps({"reason": "timeout"}), event="end")
            return
        if not _send_sse(handler, json.dumps({"path": path, "exists": True}), event="meta"):
            return

    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        _send_sse(handler, json.dumps({"error": str(exc)}), event="error")
        return

    try:
        # Seek to the start of the last `initial_chars` bytes.
        f.seek(0, 2)  # end
        size = f.tell()
        f.seek(max(0, size - initial_chars))
        # If we cut mid-line, drop the partial first line for tidiness.
        if size > initial_chars:
            f.readline()
        head = f.read()
        if head and not _send_sse(handler, head, event="append"):
            return

        deadline = time.monotonic() + max_seconds
        last_beat = time.monotonic()
        while time.monotonic() < deadline:
            chunk = f.read()
            if chunk:
                if not _send_sse(handler, chunk, event="append"):
                    return
                last_beat = time.monotonic()
                continue
            now = time.monotonic()
            if now - last_beat > heartbeat:
                if not _send_sse(handler, "ping", event="heartbeat"):
                    return
                last_beat = now
            # Detect rotation/truncation: if file shrunk, restart from end.
            try:
                cur_size = os.path.getsize(path)
            except OSError:
                _send_sse(handler, json.dumps({"reason": "deleted"}), event="end")
                return
            if cur_size < f.tell():
                f.seek(0)
            time.sleep(poll_interval)
        _send_sse(handler, json.dumps({"reason": "max_seconds"}), event="end")
    finally:
        try:
            f.close()
        except Exception:
            pass


@_sse_route("/api/projects/<id>/log/stream")
def _stream_project_log(handler: "_Handler", params: dict[str, str]) -> None:
    project = _pm().get(params["id"])
    if not project:
        handler._send_json({"error": "not_found"}, status=404)
        return
    log_path = project.get("log_path") or os.path.join(
        _project_root(), "temp", "project_logs", f"{project['id']}.log"
    )
    _tail_log_file(handler, log_path)


@_sse_route("/api/bots/<key>/log/stream")
def _stream_bot_log(handler: "_Handler", params: dict[str, str]) -> None:
    from launcher.bot_manager import BOT_SPECS

    key = params["key"]
    if key not in BOT_SPECS:
        handler._send_json({"error": "unknown_bot"}, status=404)
        return
    log_path = _bm().status(key).log_path
    _tail_log_file(handler, log_path)


@_sse_route("/api/activity/stream")
def _stream_activity(handler: "_Handler", params: dict[str, str]) -> None:
    """Tail the agent activity log (current day's JSONL)."""
    from launcher import activity_log as _alog

    _tail_log_file(handler, _alog.latest_path())


def _match_sse(path: str):
    for pattern, fn in SSE_ROUTES:
        params = _match_pattern(pattern, path)
        if params is not None:
            return fn, params
    return None, None


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "WlwlAssAPI/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("WLWL_API_DEBUG"):
            super().log_message(format, *args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        handler, params = _match_route(method, path)
        if handler is None:
            self._send_json({"error": "not_found", "path": path, "method": method}, status=404)
            return
        body = self._read_body() if method in {"POST", "PUT", "PATCH", "DELETE"} else {}
        from urllib.parse import parse_qs
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        req = {"method": method, "path": path, "params": params, "body": body, "query": query}
        try:
            status, payload = handler(req)
        except Exception as exc:
            self._send_json(
                {"error": "internal", "type": type(exc).__name__, "detail": str(exc)},
                status=500,
            )
            return
        self._send_json(payload, status=status)

    def do_GET(self) -> None:
        # SSE routes are matched first since they take ownership of the
        # response (they write headers + a long-lived body themselves).
        path = urlparse(self.path).path
        sse_handler, sse_params = _match_sse(path)
        if sse_handler is not None:
            try:
                sse_handler(self, sse_params or {})
            except Exception as exc:
                # Client most likely disconnected; safe to swallow.
                if os.environ.get("WLWL_API_DEBUG"):
                    print(f"[SSE] {path}: {exc}")
            return
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_server(host: str, port: int) -> _ThreadingServer:
    return _ThreadingServer((host, port), _Handler)


# ─── Auto-start hooks (bots + scheduler) ──────────────────────────────


def _auto_start_configured_bots() -> None:
    """Start every IM bot whose credentials + SDK are ready.

    Replaces the old `launch_options.<key>` opt-in flag pattern: if the user
    bothered to put `fs_app_id` + `fs_app_secret` in the config and `pip
    install lark_oapi`, we assume they want the bot up. Bots already running
    (own subprocess from a previous boot, or external instance holding the
    single-instance lock port) are skipped.
    """
    try:
        bm = _bm()
        statuses = bm.status_all()
    except Exception as exc:
        print(f"[api_server] auto-start bots: status probe failed: {exc}", flush=True)
        return
    for key, st in statuses.items():
        if st.running:
            continue
        if not (st.configured and st.sdk_installed):
            continue
        try:
            ok, msg = bm.start(key)
        except Exception as exc:
            print(f"[api_server] auto-start bot {key} crashed: {exc}", flush=True)
            continue
        print(f"[api_server] auto-start bot {key}: {msg}", flush=True)


def _auto_start_scheduler() -> None:
    """Spawn `agentmain.py --reflect reflect/scheduler.py` if enabled in launch_options."""
    global _scheduler_proc
    try:
        from launcher.launch_config import load_options
        opts = load_options(_project_root())
    except Exception as exc:
        print(f"[api_server] auto-start scheduler: load_options failed: {exc}", flush=True)
        return
    if not opts.get("scheduler", True):
        print("[api_server] scheduler disabled in launch_options; skipping", flush=True)
        return
    if _scheduler_proc is not None and _scheduler_proc.poll() is None:
        return  # already running in this process
    root = _project_root()
    cmd = [
        sys.executable,
        os.path.join(root, "agentmain.py"),
        "--reflect",
        os.path.join(root, "reflect", "scheduler.py"),
        "--llm_no",
        str(opts.get("llm_no", 0)),
    ]
    try:
        _scheduler_proc = subprocess.Popen(
            cmd,
            cwd=root,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        print(f"[api_server] scheduler started pid={_scheduler_proc.pid}", flush=True)
    except Exception as exc:
        print(f"[api_server] scheduler spawn failed: {exc}", flush=True)
        _scheduler_proc = None


def _shutdown_children() -> None:
    """atexit hook: kill every bot we spawned + the scheduler subprocess.

    Idempotent; safe to call from both the `finally` of serve() and the atexit
    handler (whichever fires first wins). External bot instances (running on
    their single-instance lock port) are not touched — those weren't ours.
    """
    global _scheduler_proc
    try:
        if _bot_manager is not None:
            _bot_manager.stop_all()
    except Exception as exc:
        print(f"[api_server] stop_all bots failed: {exc}", flush=True)
    if _scheduler_proc is not None and _scheduler_proc.poll() is None:
        try:
            _scheduler_proc.terminate()
            try:
                _scheduler_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _scheduler_proc.kill()
        except Exception as exc:
            print(f"[api_server] scheduler shutdown failed: {exc}", flush=True)
    _scheduler_proc = None


def serve(port: int | None = None, host: str = "127.0.0.1") -> None:
    """Run the API server (blocking). Prints `__GA_READY__ port=...` once listening.

    Before serving, auto-spawns:
      - every IM bot whose credentials + SDK are present (BotManager.start)
      - the L4 reflect/scheduler.py child (if launch_options.scheduler=True)
    On exit (KeyboardInterrupt / SIGTERM / atexit) all spawned children are killed.
    """
    if port is None:
        port = _free_port()
    httpd = _make_server(host, port)
    _auto_start_configured_bots()
    _auto_start_scheduler()
    atexit.register(_shutdown_children)
    print(f"{READY_MARKER} port={port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        _shutdown_children()


def serve_threaded(port: int | None = None) -> tuple[int, threading.Thread]:
    """Run the server in a daemon thread; returns (port, thread). For tests."""
    if port is None:
        port = _free_port()
    httpd = _make_server("127.0.0.1", port)

    def _run() -> None:
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    time.sleep(0.02)
    return port, thread


def reset_state_for_tests() -> None:
    """Reset module-level singletons. Tests use this between cases."""
    global _project_manager, _bot_manager, _scheduler_proc, _started_at
    _project_manager = None
    _bot_manager = None
    if _scheduler_proc is not None and _scheduler_proc.poll() is None:
        try:
            _scheduler_proc.kill()
        except Exception:
            pass
    _scheduler_proc = None
    _started_at = time.monotonic()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wlwl-ass GUI API server")
    parser.add_argument("--port", type=int, default=None, help="TCP port (default: pick free)")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    serve(port=args.port, host=args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

