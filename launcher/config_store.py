"""Centralised config + credentials store.

Replaces the historical ``mykey.py`` + ``mykey_local_override.py`` data
files with one well-defined hierarchical store. (Those files have been
retired as of the post-mykey refactor — see ``launcher.config_migrate``
for the one-shot upgrade path.)

**Layers (highest priority → lowest)**

  1. **Process env vars**    – ``GA_*`` / ``OPENAI_API_KEY`` / etc. (read-only).
  2. **Project store**       – ``<project>/.wlwl-ass/config.json`` (gitignored).
  3. **User store**          – ``~/.wlwl-ass/config.json`` (cross-project secrets).
  4. **.env**                – dotenv path, parsed by ``launcher.dotenv_shim``.

Each layer is a flat JSON file with this **schema**::

    {
      "version": 1,
      "providers": {                # LLM API configs
        "openai":   {"api_key": "...", "base_url": "...", "model": "..."},
        "anthropic":{"api_key": "...", "base_url": "...", "model": "..."},
        "<custom>": {...}           # arbitrary additional providers
      },
      "bots": {                     # IM bot credentials
        "telegram": {"bot_token": "...", "allowed_users": [...]},
        "feishu":   {"app_id": "...", "app_secret": "...", "allowed_users": [...]},
        "qq":       {"app_id": "...", "app_secret": "..."},
        ...
      },
      "settings": {                 # non-secret runtime preferences
        "permission_mode": "auto",
        "default_provider": "openai",
        "langfuse": {               # optional: opt-in tracing.
          "public_key": "pk-lf-...",
          "secret_key": "sk-lf-...",
          "host": "https://cloud.langfuse.com"
        },
        ...
      }
    }

**File-system protection**: credential files are written ``chmod 600``
on POSIX. On Windows, ACL inheritance from the user-private home dir
is the default protection.

**Atomic writes**: every save goes through ``write-tmp + os.replace``,
so a crash mid-write never produces a half-written config.

**Optional OS keyring**: if the ``keyring`` package is installed (``pip
install keyring``), credentials can be stored in Windows Credential
Manager / macOS Keychain / Linux libsecret instead of plain JSON. The
JSON files then hold only ``{"@keyring": "ga.<scope>.<name>"}`` markers.
This is opt-in via ``set_keyring_enabled(True)``.

**Threading**: each file is guarded by an in-process ``threading.Lock``;
multi-process write-write races are still possible but unlikely given
the single-user single-launcher topology. We can upgrade to ``msvcrt`` /
``fcntl`` advisory locks later if needed.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
from copy import deepcopy
from typing import Any, Iterable

# Optional. Falls back gracefully when not installed.
try:
    import keyring as _keyring
    import keyring.errors as _keyring_errors
    _KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover — broad on purpose
    _keyring = None
    _keyring_errors = None
    _KEYRING_AVAILABLE = False


SCHEMA_VERSION = 1
KEYRING_SERVICE = "wlwl-ass"
_KEYRING_MARKER = "@keyring"

# Per-path lock cache so two threads writing the same file serialize, while
# project + user files don't block each other.
_FILE_LOCKS: dict[str, threading.Lock] = {}
_LOCK_REGISTRY = threading.Lock()


def _file_lock(path: str) -> threading.Lock:
    with _LOCK_REGISTRY:
        lock = _FILE_LOCKS.get(path)
        if lock is None:
            lock = _FILE_LOCKS[path] = threading.Lock()
        return lock


# ─── Path resolution ─────────────────────────────────────────────────────


def user_config_path() -> str:
    """``~/.wlwl-ass/config.json``. Always returned even if the file doesn't exist."""
    override = os.environ.get("WLWL_USER_CONFIG_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".wlwl-ass", "config.json")


def project_root() -> str:
    """The repo containing ``launcher/`` — used as the project scope.

    Mirrors :func:`launcher.activity_log._project_root`. ``WLWL_PROJECT_ROOT``
    overrides for tests / containers."""
    override = os.environ.get("WLWL_PROJECT_ROOT")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def project_config_path(project_dir: str | None = None) -> str:
    """``<project>/.wlwl-ass/config.json``."""
    override = os.environ.get("WLWL_PROJECT_CONFIG_FILE")
    if override:
        return override
    return os.path.join(project_dir or project_root(), ".wlwl-ass", "config.json")


# ─── Empty / schema helpers ──────────────────────────────────────────────


def _empty_config() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "providers": {},
        "bots": {},
        "settings": {},
    }


def _normalize_loaded(data: Any) -> dict[str, Any]:
    """Tolerate older / partial schemas — return a dict with all top-level
    keys present and correct shape."""
    if not isinstance(data, dict):
        return _empty_config()
    out = _empty_config()
    out["version"] = int(data.get("version") or SCHEMA_VERSION)
    for k in ("providers", "bots", "settings"):
        v = data.get(k)
        if isinstance(v, dict):
            out[k] = v
    return out


# ─── Atomic JSON read/write ──────────────────────────────────────────────


def _load_file(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return _empty_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_config()
    return _normalize_loaded(data)


def _save_file(path: str, data: dict[str, Any]) -> None:
    """Atomic write + chmod 600. Caller already holds ``_file_lock(path)``."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
    os.replace(tmp, path)
    # POSIX-only — chmod is a no-op on Windows for these bits, but the
    # default user-home ACL already restricts other users.
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ─── Keyring resolution ──────────────────────────────────────────────────


def keyring_available() -> bool:
    return _KEYRING_AVAILABLE


def _resolve_secret(value: Any) -> Any:
    """If ``value`` is a keyring marker dict, look up the real value.
    Returns ``""`` (empty string) if keyring is unavailable or lookup fails,
    so consumers see "missing" rather than a literal marker dict."""
    if isinstance(value, dict) and _KEYRING_MARKER in value:
        if not _KEYRING_AVAILABLE:
            return ""
        ref = str(value[_KEYRING_MARKER])
        try:
            return _keyring.get_password(KEYRING_SERVICE, ref) or ""
        except Exception:
            return ""
    return value


def _store_in_keyring(reference: str, plaintext: str) -> bool:
    if not _KEYRING_AVAILABLE:
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, reference, plaintext)
        return True
    except Exception:
        return False


def _delete_from_keyring(reference: str) -> bool:
    if not _KEYRING_AVAILABLE:
        return False
    try:
        _keyring.delete_password(KEYRING_SERVICE, reference)
        return True
    except _keyring_errors.PasswordDeleteError:  # type: ignore[union-attr]
        return False
    except Exception:
        return False


# ─── Public API: ConfigStore ─────────────────────────────────────────────


_SECRET_KEY_PATTERN = re.compile(
    r"(api_key|apikey|secret|token|password|passwd)$",
    re.IGNORECASE,
)


def is_secret_key(name: str) -> bool:
    """Heuristic — used by :meth:`ConfigStore.list_redacted` to mask values
    that are clearly credentials. False positives (e.g. ``allowed_users``)
    are intentionally excluded."""
    return bool(_SECRET_KEY_PATTERN.search(name))


class ConfigStore:
    """Two-layer (project + user) hierarchical config.

    All read accessors merge the layers (project overrides user). All write
    accessors target a specific layer — either ``"project"`` or ``"user"``,
    explicit by parameter — never both. Default writes go to ``"user"``
    so credentials are reusable across projects on the same machine.
    """

    def __init__(
        self,
        *,
        project_path: str | None = None,
        user_path: str | None = None,
    ):
        self._project_path = project_path or project_config_path()
        self._user_path = user_path or user_config_path()

    # -- internal layer access ------------------------------------------

    def _layer_path(self, layer: str) -> str:
        if layer == "project":
            return self._project_path
        if layer == "user":
            return self._user_path
        raise ValueError(f"layer must be 'project' or 'user', got {layer!r}")

    def _read_layer(self, layer: str) -> dict[str, Any]:
        return _load_file(self._layer_path(layer))

    def _write_layer(self, layer: str, data: dict[str, Any]) -> None:
        path = self._layer_path(layer)
        with _file_lock(path):
            _save_file(path, data)

    # -- merging --------------------------------------------------------

    def _merged_raw(self) -> dict[str, Any]:
        """Raw merge before keyring resolution; project overrides user
        section-by-section, dict values merged shallowly per-section."""
        user = self._read_layer("user")
        proj = self._read_layer("project")
        merged = _empty_config()
        for section in ("providers", "bots", "settings"):
            base: dict[str, Any] = dict(user.get(section, {}))
            for k, v in (proj.get(section) or {}).items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k] = {**base[k], **v}
                else:
                    base[k] = v
            merged[section] = base
        return merged

    def _resolve_section(self, section: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for entity, fields in section.items():
            if not isinstance(fields, dict):
                out[entity] = fields
                continue
            out[entity] = {k: _resolve_secret(v) for k, v in fields.items()}
        return out

    # -- read API -------------------------------------------------------

    def all(self) -> dict[str, Any]:
        """Fully merged + keyring-resolved view. **Contains plaintext
        secrets**; never log this verbatim — see :meth:`list_redacted`."""
        merged = self._merged_raw()
        return {
            "version": SCHEMA_VERSION,
            "providers": self._resolve_section(merged["providers"]),
            "bots":      self._resolve_section(merged["bots"]),
            "settings":  dict(merged["settings"]),
        }

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path read: ``providers.openai.api_key``."""
        cur: Any = self.all()
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_provider(self, name: str) -> dict[str, Any]:
        """Convenience: full provider record (with secrets resolved)."""
        return dict(self.all()["providers"].get(name, {}))

    def get_bot(self, name: str) -> dict[str, Any]:
        return dict(self.all()["bots"].get(name, {}))

    def list_redacted(self) -> dict[str, Any]:
        """All config with secrets replaced by ``"***<lastN>"`` markers.
        Suitable for logging / printing in the CLI."""
        view = self.all()
        for section_name in ("providers", "bots"):
            for entity, fields in view[section_name].items():
                if not isinstance(fields, dict):
                    continue
                for k in list(fields.keys()):
                    if is_secret_key(k):
                        v = str(fields[k] or "")
                        fields[k] = ("***" + v[-4:]) if len(v) > 6 else ("***" if v else "")
        return view

    # -- write API ------------------------------------------------------

    def set_provider(
        self,
        name: str,
        fields: dict[str, Any],
        *,
        layer: str = "user",
        merge: bool = True,
        use_keyring: bool = False,
    ) -> dict[str, Any]:
        """Upsert a provider entry.

        :param fields: keys typically include ``api_key`` / ``base_url`` /
            ``model``. Extra keys are kept verbatim.
        :param merge: if True, only update keys present in ``fields``;
            existing other keys remain. If False, ``fields`` becomes the
            full record.
        :param use_keyring: route ``api_key`` (or any :func:`is_secret_key`
            field) into the OS keyring instead of the JSON file.
        """
        return self._set_section_entry("providers", name, fields,
                                       layer=layer, merge=merge,
                                       use_keyring=use_keyring)

    def set_bot(
        self,
        name: str,
        fields: dict[str, Any],
        *,
        layer: str = "user",
        merge: bool = True,
        use_keyring: bool = False,
    ) -> dict[str, Any]:
        return self._set_section_entry("bots", name, fields,
                                       layer=layer, merge=merge,
                                       use_keyring=use_keyring)

    def set_setting(
        self,
        name: str,
        value: Any,
        *,
        layer: str = "user",
    ) -> Any:
        data = self._read_layer(layer)
        data["settings"][name] = value
        self._write_layer(layer, data)
        return value

    def delete_provider(self, name: str, *, layer: str = "user") -> bool:
        return self._delete_section_entry("providers", name, layer=layer)

    def delete_bot(self, name: str, *, layer: str = "user") -> bool:
        return self._delete_section_entry("bots", name, layer=layer)

    def delete_setting(self, name: str, *, layer: str = "user") -> bool:
        data = self._read_layer(layer)
        if name not in data["settings"]:
            return False
        del data["settings"][name]
        self._write_layer(layer, data)
        return True

    # -- internal: section CRUD ----------------------------------------

    def _set_section_entry(
        self,
        section: str,
        name: str,
        fields: dict[str, Any],
        *,
        layer: str,
        merge: bool,
        use_keyring: bool,
    ) -> dict[str, Any]:
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        data = self._read_layer(layer)
        existing = data[section].get(name, {}) if merge else {}
        if not isinstance(existing, dict):
            existing = {}
        new_fields: dict[str, Any] = dict(existing)
        for k, v in fields.items():
            if use_keyring and is_secret_key(k) and isinstance(v, str) and v:
                ref = f"{section}.{name}.{k}"
                if _store_in_keyring(ref, v):
                    new_fields[k] = {_KEYRING_MARKER: ref}
                else:
                    new_fields[k] = v   # keyring failed; fall back to plain
            else:
                new_fields[k] = v
        data[section][name] = new_fields
        self._write_layer(layer, data)
        return deepcopy(new_fields)

    def _delete_section_entry(self, section: str, name: str, *, layer: str) -> bool:
        data = self._read_layer(layer)
        entry = data[section].get(name)
        if entry is None:
            return False
        # Best-effort keyring cleanup.
        if isinstance(entry, dict):
            for k, v in entry.items():
                if isinstance(v, dict) and _KEYRING_MARKER in v:
                    _delete_from_keyring(str(v[_KEYRING_MARKER]))
        del data[section][name]
        self._write_layer(layer, data)
        return True

    # -- env-var integration -------------------------------------------

    def overlay_env(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        """Return :meth:`all` further overlaid with recognized env vars
        (``OPENAI_API_KEY`` etc.). Used by the legacy synthesizer in
        :mod:`launcher.dotenv_shim` so env always wins."""
        if env is None:
            env = dict(os.environ)
        view = self.all()

        def _pick(*names: str) -> str:
            for n in names:
                v = env.get(n, "")
                if v:
                    return v
            return ""

        oai_key = _pick("WLWL_OPENAI_API_KEY", "OPENAI_API_KEY")
        if oai_key:
            cur = dict(view["providers"].get("openai", {}))
            cur["api_key"] = oai_key
            cur.setdefault("base_url",
                           _pick("WLWL_OPENAI_BASE_URL", "OPENAI_BASE_URL")
                           or "https://api.openai.com/v1")
            cur.setdefault("model",
                           _pick("WLWL_OPENAI_MODEL", "OPENAI_MODEL") or "gpt-5.4")
            view["providers"]["openai"] = cur

        anth_key = _pick("WLWL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        if anth_key:
            cur = dict(view["providers"].get("anthropic", {}))
            cur["api_key"] = anth_key
            cur.setdefault("base_url",
                           _pick("WLWL_ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL")
                           or "https://api.anthropic.com")
            cur.setdefault("model",
                           _pick("WLWL_ANTHROPIC_MODEL", "ANTHROPIC_MODEL")
                           or "claude-opus-4-7")
            view["providers"]["anthropic"] = cur

        # Bot tokens via env — same pattern as providers.
        bot_env_map = {
            "telegram":  ("TG_BOT_TOKEN",       "bot_token"),
            "feishu":    ("FS_APP_ID",          "app_id"),
            "feishu":    ("FS_APP_SECRET",      "app_secret"),    # noqa: F601 (intentional second key)
            "qq":        ("QQ_APP_ID",          "app_id"),
            "qq":        ("QQ_APP_SECRET",      "app_secret"),    # noqa: F601
            "wecom":     ("WECOM_BOT_ID",       "bot_id"),
            "wecom":     ("WECOM_SECRET",       "secret"),        # noqa: F601
            "dingtalk":  ("DINGTALK_CLIENT_ID", "client_id"),
            "dingtalk":  ("DINGTALK_CLIENT_SECRET", "client_secret"),  # noqa: F601
        }
        # Walk individually so all keys for one bot can be set.
        for bot_name, key_pairs in [
            ("telegram",  [("TG_BOT_TOKEN", "bot_token")]),
            ("feishu",    [("FS_APP_ID", "app_id"), ("FS_APP_SECRET", "app_secret")]),
            ("qq",        [("QQ_APP_ID", "app_id"), ("QQ_APP_SECRET", "app_secret")]),
            ("wecom",     [("WECOM_BOT_ID", "bot_id"), ("WECOM_SECRET", "secret")]),
            ("dingtalk",  [("DINGTALK_CLIENT_ID", "client_id"),
                           ("DINGTALK_CLIENT_SECRET", "client_secret")]),
        ]:
            cur = dict(view["bots"].get(bot_name, {}))
            changed = False
            for env_name, field in key_pairs:
                v = env.get(env_name, "").strip()
                if v:
                    cur[field] = v
                    changed = True
            if changed:
                view["bots"][bot_name] = cur

        return view


# ─── Module-level convenience singletons ─────────────────────────────────


_DEFAULT_STORE: ConfigStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> ConfigStore:
    """Process-wide singleton — recreated when ``WLWL_PROJECT_ROOT`` /
    ``WLWL_USER_CONFIG_FILE`` change between test cases (rare)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = ConfigStore()
        return _DEFAULT_STORE


def reset_default_store() -> None:
    """Drop the singleton (test helper)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = None


# ─── Synthesis to legacy mykey shape ─────────────────────────────────────
#
# This is what ``llmcore._keys`` actually consumes: a flat dict of
# session-config dicts. We take the structured config_store view and
# emit dicts in the same shape ``mykey.py`` would have.

def synthesize_mykeys_from_store(store: ConfigStore | None = None) -> dict[str, Any]:
    """Produce a mykey-compatible flat dict from the config store, with
    env vars overlaid. Returns ``{}`` if no providers are configured —
    callers fall through to the legacy mykey loaders.

    Provider naming convention:
      * ``providers.openai``    → ``native_oai_config_<name>``
      * ``providers.anthropic`` → ``native_claude_config_<name>``
      * ``providers.<other>``   → routed by the entry's ``kind`` field;
                                  defaults to ``native_oai_config_<name>``.
    """
    store = store or default_store()
    view = store.overlay_env()

    out: dict[str, Any] = {}
    names: list[str] = []

    for prov_name, fields in view["providers"].items():
        if not isinstance(fields, dict):
            continue
        api_key = str(fields.get("api_key") or "").strip()
        if not api_key:
            continue
        kind = str(fields.get("kind") or "").strip().lower()
        if not kind:
            kind = "native_claude" if prov_name.lower() == "anthropic" else "native_oai"
        prefix = "native_claude_config" if kind == "native_claude" else "native_oai_config"
        var_name = f"{prefix}_{_safe_slug(prov_name)}"
        cfg = {
            "name": prov_name,
            "apikey": api_key,
            "apibase": str(fields.get("base_url") or fields.get("apibase") or "").strip(),
            "model": str(fields.get("model") or "").strip(),
        }
        # Anthropic relays (non-sk-ant- prefix) need the CC fingerprint.
        if kind == "native_claude" and not api_key.startswith("sk-ant-"):
            cfg["fake_cc_system_prompt"] = True
        # Pass through any extra fields users set (max_retries, reasoning_effort,
        # api_mode, etc.) without enumerating them — config_store is the
        # contract, and we don't want to re-do this list every time.
        for k, v in fields.items():
            if k in ("api_key", "kind", "base_url", "apibase", "model"):
                continue
            cfg[k] = v
        out[var_name] = cfg
        names.append(prov_name)

    # Bot tokens — emit at the legacy top-level names so frontends/*app.py
    # finds them via mykey.py. This is the bridge that makes the GUI bot
    # tab actually work without anyone editing a .py file.
    bot_env_map = {
        "telegram":  [("bot_token", "tg_bot_token"),
                      ("allowed_users", "tg_allowed_users")],
        "feishu":    [("app_id", "fs_app_id"),
                      ("app_secret", "fs_app_secret"),
                      ("allowed_users", "fs_allowed_users")],
        "qq":        [("app_id", "qq_app_id"),
                      ("app_secret", "qq_app_secret")],
        "wecom":     [("bot_id", "wecom_bot_id"),
                      ("secret", "wecom_secret")],
        "dingtalk":  [("client_id", "dingtalk_client_id"),
                      ("client_secret", "dingtalk_client_secret")],
    }
    for bot_name, mapping in bot_env_map.items():
        bot_fields = view["bots"].get(bot_name)
        if not isinstance(bot_fields, dict):
            continue
        for store_key, mykey_name in mapping:
            v = bot_fields.get(store_key)
            if v in (None, "", []):
                continue
            out[mykey_name] = v

    if names:
        # Only emit a default mixin if the user didn't provide one.
        out.setdefault("mixin_config", {
            "llm_nos": names,
            "max_retries": 5,
            "base_delay": 0.5,
        })
    return out


def _safe_slug(name: str) -> str:
    s = re.sub(r"\W+", "_", name.strip().lower()).strip("_")
    return s or "default"
