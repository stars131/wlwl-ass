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
import time

CONFIG_FILE = "launcher_api_configs.json"
BACKUPS_DIR = "backups"
MAX_BACKUPS = 10  # mirrors cc-switch's rotation policy
SUPPORTED_KINDS = {"native_oai", "native_claude", "mixin"}
API_CONFIG_CATEGORIES = ("language", "multimodal", "voice", "utility")
API_CONFIG_CATEGORY_ORDER = {name: i for i, name in enumerate(API_CONFIG_CATEGORIES)}
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
    "category",
    "priority",
    "api_mode",
    "stream",
    "max_tokens",
    "max_retries",
    "connect_timeout",
    "read_timeout",
    "temperature",
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

# Field-level constraint dictionaries — consumed by GUI dropdowns, the
# `wlwl config tune` CLI for validation hints, and normalize_config for
# coercion. Lower-cased and stripped before comparison.
REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
THINKING_TYPE_VALUES = ("adaptive", "enabled", "disabled")
API_MODE_VALUES = ("chat_completions", "responses")


def config_path(base_dir):
    return os.path.join(base_dir, "temp", CONFIG_FILE)


def backups_dir(base_dir):
    return os.path.join(base_dir, "temp", BACKUPS_DIR)


def _ensure_temp(base_dir):
    os.makedirs(os.path.join(base_dir, "temp"), exist_ok=True)


def _rotate_backup(base_dir):
    """Copy the current config file into ``temp/backups/<ts>.json`` and
    prune the directory to :data:`MAX_BACKUPS` most-recent entries.

    Called *before* every successful write of ``launcher_api_configs.json``
    so a bad save (typo, corrupted import, accidental wipe) can be
    recovered by hand. Silent no-op when the file doesn't exist yet —
    nothing to back up on a fresh install."""
    src = config_path(base_dir)
    if not os.path.isfile(src):
        return None
    dst_dir = backups_dir(base_dir)
    os.makedirs(dst_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(dst_dir, f"{CONFIG_FILE.replace('.json', '')}-{ts}.json")
    # Avoid overwriting if two saves land in the same second — append .N.
    if os.path.exists(dst):
        for n in range(1, 100):
            cand = dst[:-5] + f".{n}.json"
            if not os.path.exists(cand):
                dst = cand
                break
    try:
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
    except OSError:
        return None
    _prune_backups(dst_dir)
    return dst


def _prune_backups(dst_dir):
    """Keep only the :data:`MAX_BACKUPS` newest backup files."""
    try:
        entries = []
        for name in os.listdir(dst_dir):
            if not name.startswith(CONFIG_FILE.replace(".json", "")):
                continue
            full = os.path.join(dst_dir, name)
            if os.path.isfile(full):
                entries.append((os.path.getmtime(full), full))
        entries.sort(reverse=True)
        for _mtime, path in entries[MAX_BACKUPS:]:
            try:
                os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def list_backups(base_dir):
    """Return (mtime, path) tuples newest-first. Used by `wlwl config
    backups` to surface recoverable snapshots."""
    dst_dir = backups_dir(base_dir)
    if not os.path.isdir(dst_dir):
        return []
    items = []
    for name in os.listdir(dst_dir):
        if not name.startswith(CONFIG_FILE.replace(".json", "")):
            continue
        full = os.path.join(dst_dir, name)
        if os.path.isfile(full):
            items.append((os.path.getmtime(full), full))
    items.sort(reverse=True)
    return items


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
    return prepare_api_configs(configs)


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
    normalized = prepare_api_configs(sanitized)
    for config in normalized:
        ok, msg = validate_config(config)
        if not ok:
            raise ValueError(msg)
    _ensure_temp(base_dir)
    path = config_path(base_dir)
    # Rotate a snapshot of the *previous* contents before we overwrite —
    # makes typos / accidental wipes recoverable via `wlwl config backups`.
    _rotate_backup(base_dir)
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
        c["llm_nos"] = [_parse_llm_ref(x.strip()) for x in c["llm_nos"].split(",") if x.strip()]
    for key in ("stream", "fake_cc_system_prompt", "audio_capable", "image_capable"):
        if isinstance(c.get(key), str):
            c[key] = c[key].strip().lower() in {"1", "true", "yes", "on"}
    c["category"] = infer_config_category(c)
    c["priority"] = normalize_priority(c.get("priority"))
    for key in ("max_tokens", "max_retries", "connect_timeout", "read_timeout", "thinking_budget_tokens"):
        if isinstance(c.get(key), str) and c[key].strip():
            try:
                c[key] = int(c[key]) if key in {"max_tokens", "max_retries", "thinking_budget_tokens"} else float(c[key])
            except ValueError:
                pass
    if isinstance(c.get("temperature"), str) and c["temperature"].strip():
        try:
            c["temperature"] = float(c["temperature"])
        except ValueError:
            pass
    for enum_key, allowed in (
        ("reasoning_effort", REASONING_EFFORT_VALUES),
        ("thinking_type", THINKING_TYPE_VALUES),
        ("api_mode", API_MODE_VALUES),
    ):
        v = c.get(enum_key)
        if isinstance(v, str):
            v2 = v.strip().lower()
            c[enum_key] = v2 if v2 in allowed or v2 == "" else v
    return c


def _parse_llm_ref(value):
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def normalize_priority(value):
    if isinstance(value, bool) or value in ("", None):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def infer_config_category(config):
    explicit = str(config.get("category") or "").strip().lower()
    if explicit in API_CONFIG_CATEGORIES:
        return explicit
    modelish = " ".join(
        str(config.get(key) or "").lower()
        for key in ("name", "model", "apibase")
    )
    if config.get("audio_capable") or any(
        token in modelish
        for token in ("whisper", "tts", "speech", "audio", "transcribe", "realtime")
    ):
        return "voice"
    if config.get("image_capable") or any(
        token in modelish
        for token in ("vision", "qwen-vl", "-vl", "image-generation", "dall-e", "gpt-image")
    ):
        return "multimodal"
    if any(
        token in modelish
        for token in ("embedding", "embed", "rerank", "search", "moderation")
    ):
        return "utility"
    return "language"


def prepare_api_configs(configs):
    normalized = [
        normalize_config(c)
        for c in (configs or [])
        if isinstance(c, dict)
    ]
    _resolve_mixin_members_by_name(normalized)
    return sort_api_configs(normalized)


def _resolve_mixin_members_by_name(configs):
    names_by_index = {
        i: str(c.get("name") or "").strip()
        for i, c in enumerate(configs)
        if str(c.get("name") or "").strip()
    }
    for c in configs:
        if c.get("kind") != "mixin" or not isinstance(c.get("llm_nos"), list):
            continue
        resolved = []
        for ref in c.get("llm_nos") or []:
            parsed = _parse_llm_ref(ref)
            if isinstance(parsed, int) and parsed in names_by_index:
                resolved.append(names_by_index[parsed])
            else:
                resolved.append(parsed)
        c["llm_nos"] = resolved
    return configs


def sort_api_configs(configs):
    def key(item):
        idx, c = item
        category = str(c.get("category") or "language")
        return (
            API_CONFIG_CATEGORY_ORDER.get(category, len(API_CONFIG_CATEGORIES)),
            -normalize_priority(c.get("priority")),
            idx,
        )

    return [c for _idx, c in sorted(enumerate(configs or []), key=key)]


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
        keys = ("name", "llm_nos", "category", "priority", "max_retries", "base_delay", "spring_back")
    else:
        keys = ("name", "apikey", "apibase", "model", *COMMON_OPTIONAL_FIELDS)
    return {k: config[k] for k in keys if k in config and config[k] not in ("", None, [])}
