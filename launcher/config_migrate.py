"""One-shot migration: import legacy mykey/override + launcher_api_configs.json
into the new :class:`launcher.config_store.ConfigStore`.

.. deprecated:: post-mykey-retirement
    The mykey.py support path has been removed from runtime
    (:mod:`llmcore._keys` no longer reads mykey.py / mykey.json /
    mykey_local_override.py). This module is kept for one release as a
    one-shot migration helper for users upgrading from older versions:
    run ``python -m launcher.config migrate`` once before upgrading,
    or run it on the new version with the old files still present.
    Slated for removal in the next release. The :func:`migrate`
    function emits a deprecation warning on each call.

Idempotent — running twice is safe (same source values overwrite same store
keys with the same data). Writes a ``.wlwl-ass/.migrated`` marker after success
so the launcher only auto-runs migration once.

Migration is **non-destructive**: the legacy files are left in place. Users
can delete them after verifying the new config works.
"""
from __future__ import annotations

import json
import os
import runpy
import sys
from typing import Any

from launcher.config_store import (
    ConfigStore,
    default_store,
    project_root,
)


def _migrated_marker() -> str:
    return os.path.join(project_root(), ".wlwl-ass", ".migrated")


def already_migrated() -> bool:
    return os.path.isfile(_migrated_marker())


def mark_migrated() -> None:
    path = _migrated_marker()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("1\n")


def _load_legacy_mykey() -> dict[str, Any]:
    """Read mykey.py + mykey_local_override.py via runpy (no side effects on
    llmcore module state). Returns flat dict keyed by top-level name."""
    out: dict[str, Any] = {}
    for name in ("mykey.py", "mykey_local_override.py"):
        path = os.path.join(project_root(), name)
        if not os.path.isfile(path):
            continue
        try:
            ns = runpy.run_path(path)
        except Exception as exc:
            print(f"[migrate] skip {name}: {exc}")
            continue
        for k, v in ns.items():
            if k.startswith("_"):
                continue
            out[k] = v
    return out


def _load_launcher_api_json() -> list[dict[str, Any]]:
    path = os.path.join(project_root(), "temp", "launcher_api_configs.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    configs = data.get("configs", data if isinstance(data, list) else [])
    return [c for c in configs if isinstance(c, dict)]


# ─── Heuristics ──────────────────────────────────────────────────────────


_PLACEHOLDER_FRAGMENTS = (
    "<your", "your-key", "your-openai", "your-anthropic", "your-relay",
    "your-key>", "sk-your", "sk-user-<", "sk-ant-<", "cr_<your",
    "cli_xxx", "f0f1b798xxxx", "***",
)


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    s = value.strip().lower()
    if not s:
        return True
    return any(p in s for p in _PLACEHOLDER_FRAGMENTS)


def _is_session_dict(name: str, value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not any(t in name for t in ("api", "config", "cookie")):
        return False
    if "mixin" in name.lower():
        return False
    return "apikey" in value


# Top-level bot field names → (bot_name, store_field).
_BOT_FIELD_MAP = {
    "tg_bot_token":           ("telegram", "bot_token"),
    "tg_allowed_users":       ("telegram", "allowed_users"),
    "fs_app_id":              ("feishu",   "app_id"),
    "fs_app_secret":          ("feishu",   "app_secret"),
    "fs_allowed_users":       ("feishu",   "allowed_users"),
    "qq_app_id":              ("qq",       "app_id"),
    "qq_app_secret":          ("qq",       "app_secret"),
    "wecom_bot_id":           ("wecom",    "bot_id"),
    "wecom_secret":           ("wecom",    "secret"),
    "dingtalk_client_id":     ("dingtalk", "client_id"),
    "dingtalk_client_secret": ("dingtalk", "client_secret"),
}


# ─── Migration ───────────────────────────────────────────────────────────


def migrate(
    *,
    store: ConfigStore | None = None,
    layer: str = "user",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run migration. Returns a report dict.

    .. deprecated:: post-mykey-retirement
        Slated for removal in the next release. Emits a deprecation
        warning on stderr each call.

    ``layer="user"`` (default) writes to ``~/.wlwl-ass/config.json`` so secrets
    are reusable across projects. Pass ``layer="project"`` to scope to
    ``<project>/.wlwl-ass/config.json`` (gitignored)."""
    try:
        sys.stderr.write(
            "[config_migrate] DEPRECATED: one-shot mykey.py → config_store "
            "migrator; will be removed in the next release.\n"
        )
    except OSError:
        pass
    store = store or default_store()
    report = {
        "providers_imported": [],
        "bots_imported": [],
        "settings_imported": [],
        "skipped_placeholder": [],
        "legacy_files_seen": [],
        "dry_run": dry_run,
    }

    # 1) Legacy mykey.py + override → providers + bots.
    legacy = _load_legacy_mykey()
    if legacy:
        for name in ("mykey.py", "mykey_local_override.py"):
            p = os.path.join(project_root(), name)
            if os.path.isfile(p):
                report["legacy_files_seen"].append(name)

    # Providers from session-config dicts.
    for entry_name, value in legacy.items():
        if not _is_session_dict(entry_name, value):
            continue
        apikey = value.get("apikey")
        if _is_placeholder(apikey):
            report["skipped_placeholder"].append(entry_name)
            continue
        # Friendly provider name: "native_oai_config_deepseekzhoumo" → "deepseekzhoumo".
        prov_name = _strip_kind_prefix(entry_name) or value.get("name") or entry_name
        kind = "native_claude" if "claude" in entry_name.lower() else "native_oai"
        fields: dict[str, Any] = {
            "kind": kind,
            "api_key": apikey,
            "base_url": value.get("apibase", ""),
            "model": value.get("model", ""),
        }
        # Pass through everything else (api_mode, max_retries, reasoning_effort, …).
        for k, v in value.items():
            if k in ("name", "apikey", "apibase", "model"):
                continue
            fields[k] = v
        if not dry_run:
            store.set_provider(prov_name, fields, layer=layer, merge=True)
        report["providers_imported"].append(prov_name)

    # Bots from top-level fields (tg_bot_token / fs_app_id / etc.).
    bots_collected: dict[str, dict[str, Any]] = {}
    for entry_name, value in legacy.items():
        if entry_name not in _BOT_FIELD_MAP:
            continue
        if value in (None, "", []):
            continue
        bot_name, store_field = _BOT_FIELD_MAP[entry_name]
        bots_collected.setdefault(bot_name, {})[store_field] = value
    for bot_name, fields in bots_collected.items():
        if not dry_run:
            store.set_bot(bot_name, fields, layer=layer, merge=True)
        report["bots_imported"].append(bot_name)

    # langfuse_config (mykey top-level) → settings.langfuse
    lf = legacy.get("langfuse_config")
    if isinstance(lf, dict) and lf:
        if not dry_run:
            store.set_setting("langfuse", lf, layer=layer)
        report["settings_imported"].append("langfuse")

    # 2) launcher_api_configs.json → providers (Qt launcher's source-of-truth).
    json_configs = _load_launcher_api_json()
    if json_configs:
        report["legacy_files_seen"].append("temp/launcher_api_configs.json")
    for cfg in json_configs:
        if not isinstance(cfg, dict):
            continue
        apikey = cfg.get("apikey")
        if _is_placeholder(apikey):
            name = cfg.get("name") or "<unnamed>"
            report["skipped_placeholder"].append(f"launcher_api_configs.json:{name}")
            continue
        kind_raw = str(cfg.get("kind") or "native_oai").strip().lower()
        kind = "native_claude" if "claude" in kind_raw else "native_oai"
        prov_name = cfg.get("name") or kind
        fields: dict[str, Any] = {
            "kind": kind,
            "api_key": apikey,
            "base_url": cfg.get("apibase", ""),
            "model": cfg.get("model", ""),
        }
        for k, v in cfg.items():
            if k in ("name", "apikey", "apibase", "model", "kind"):
                continue
            fields[k] = v
        if not dry_run:
            store.set_provider(prov_name, fields, layer=layer, merge=True)
        if prov_name not in report["providers_imported"]:
            report["providers_imported"].append(prov_name)

    if not dry_run:
        mark_migrated()
    return report


def _strip_kind_prefix(name: str) -> str:
    for prefix in ("native_oai_config_", "native_claude_config_",
                   "native_oai_config", "native_claude_config",
                   "claude_config_", "claude_config",
                   "oai_config_", "oai_config"):
        if name.startswith(prefix):
            return name[len(prefix):].lstrip("_")
    return ""
