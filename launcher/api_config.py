"""Launcher-managed API config cards (Qt / Tauri "API 配置" tab).

Persists to ``temp/launcher_api_configs.json`` and that's it. Read by
:func:`llmcore._keys._load_from_launcher_configs` at agent startup —
no mirror to ``config_store``, no ``mykey_local_override.py`` file.

The previous "profile" mechanism (named subsets of configs that were
selectively materialized into ``mykey_local_override.py``) has been
retired alongside ``mykey.py`` itself; all saved configs activate
simultaneously now. Use distinct config names if you want to keep
multiple endpoints side-by-side and reference them via ``mixin_config``.
"""
import json
import os
import re

CONFIG_FILE = "launcher_api_configs.json"
SUPPORTED_KINDS = {"native_oai", "native_claude", "mixin"}
REQUIRED_FIELDS = {
    "native_oai": ("name", "apikey", "apibase", "model"),
    "native_claude": ("name", "apikey", "apibase", "model"),
    "mixin": ("name", "llm_nos"),
}
KIND_PREFIX = {
    "native_oai": "native_oai_config",
    "native_claude": "native_claude_config",
    "mixin": "mixin_config",
}
COMMON_OPTIONAL_FIELDS = (
    "api_mode",
    "stream",
    "max_tokens",
    "max_retries",
    "connect_timeout",
    "read_timeout",
    "reasoning_effort",
    "thinking_type",
    "thinking_budget_tokens",
    "fake_cc_system_prompt",
    # Capability opt-ins consumed by the new vision/voice/image_generate
    # tools. They're plain bools — saved alongside the config so the UI
    # round-trip preserves them and the tools' picker functions can read
    # them via load_api_configs().
    "audio_capable",
    "image_capable",
)


def config_path(base_dir):
    return os.path.join(base_dir, "temp", CONFIG_FILE)


def _ensure_temp(base_dir):
    os.makedirs(os.path.join(base_dir, "temp"), exist_ok=True)


def load_api_configs(base_dir):
    path = config_path(base_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    configs = data.get("configs", data if isinstance(data, list) else [])
    return [normalize_config(c) for c in configs if isinstance(c, dict)]


def save_api_configs(base_dir, configs):
    """Persist the launcher-managed config list to
    ``temp/launcher_api_configs.json``.

    Re-reads the existing file first so masked apikeys (``"***"``) sent
    back from the UI don't clobber real values — we recover the original
    from the prior on-disk row.
    """
    existing_by_name = {
        str(c.get("name") or ""): c
        for c in load_api_configs(base_dir)
    }
    sanitized = []
    for raw in configs:
        if not isinstance(raw, dict):
            continue
        c = dict(raw)
        if c.get("apikey") in (None, "", "***"):
            prev = existing_by_name.get(str(c.get("name") or ""))
            if prev and prev.get("apikey") and prev["apikey"] != "***":
                c["apikey"] = prev["apikey"]
        sanitized.append(c)
    normalized = [normalize_config(c) for c in sanitized]
    for config in normalized:
        ok, msg = validate_config(config)
        if not ok:
            raise ValueError(msg)
    _ensure_temp(base_dir)
    path = config_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"configs": normalized}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return normalized


def list_api_configs(base_dir):
    return [public_config(c) for c in load_api_configs(base_dir)]


def get_api_config_raw(base_dir, name):
    """Return the *unmasked* config dict for ``name`` or None.

    Server-only helper; never returned over the wire. Used by /api/llm/test
    so the GUI can re-test a stored config without re-typing the apikey."""
    for c in load_api_configs(base_dir):
        if str(c.get("name") or "") == str(name or ""):
            return dict(c)
    return None


def normalize_config(config):
    c = dict(config or {})
    c["kind"] = str(c.get("kind") or "native_oai").strip()
    c["name"] = str(c.get("name") or "").strip()
    if c["kind"] == "mixin" and isinstance(c.get("llm_nos"), str):
        c["llm_nos"] = [int(x.strip()) for x in c["llm_nos"].split(",") if x.strip().isdigit()]
    for key in ("stream", "fake_cc_system_prompt", "audio_capable", "image_capable"):
        if isinstance(c.get(key), str):
            c[key] = c[key].strip().lower() in {"1", "true", "yes", "on"}
    for key in ("max_tokens", "max_retries", "connect_timeout", "read_timeout", "thinking_budget_tokens"):
        if isinstance(c.get(key), str) and c[key].strip():
            try:
                c[key] = int(c[key]) if key in {"max_tokens", "max_retries", "thinking_budget_tokens"} else float(c[key])
            except ValueError:
                pass
    return c


def public_config(config):
    c = dict(config)
    if c.get("apikey"):
        c["apikey"] = "***"
    c["var_name"] = safe_config_var_name(c.get("kind"), c.get("name"))
    return c


def validate_config(config):
    kind = str(config.get("kind") or "").strip()
    if kind not in SUPPORTED_KINDS:
        return False, f"unsupported config kind: {kind}"
    for field in REQUIRED_FIELDS[kind]:
        value = config.get(field)
        if value is None or value == "" or value == []:
            return False, f"missing required field: {field}"
    return True, ""


def safe_config_var_name(kind, name):
    prefix = KIND_PREFIX.get(str(kind or ""), "native_oai_config")
    slug = re.sub(r"\W+", "_", str(name or "default").strip().lower()).strip("_")
    slug = slug or "default"
    if slug[0].isdigit():
        slug = "m_" + slug
    return f"{prefix}_{slug}"


def _config_payload(config):
    kind = config.get("kind")
    if kind == "mixin":
        keys = ("name", "llm_nos", "max_retries", "base_delay", "spring_back")
    else:
        keys = ("name", "apikey", "apibase", "model", *COMMON_OPTIONAL_FIELDS)
    return {k: config[k] for k in keys if k in config and config[k] not in ("", None, [])}
