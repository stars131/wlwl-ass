"""Mykey loading: discovers credentials from env vars / config_store /
``temp/launcher_api_configs.json``.

**Sources, low → high priority (later wins on key collision):**

  1. Shell env vars + ``.env`` (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``
     and ``GA_*`` alternates, parsed by :mod:`launcher.dotenv_shim`).
     This is the fastest first-run path: drop one line in ``.env`` and go.
  2. :mod:`launcher.config_store` — the canonical hierarchical store
     (``~/.wlwl-ass/config.json`` user layer + ``<project>/.wlwl-ass/config.json``
     project layer). Written by :mod:`launcher.cli_init` /
     :mod:`launcher.api_server` ``/api/credentials`` PUT / ``python -m
     launcher.config set``.
  3. ``temp/launcher_api_configs.json`` — Qt / Tauri launcher's API
     configs panel. Holds the user's full set of LLM endpoints with all
     advanced fields (mixin, reasoning_effort, fake_cc_system_prompt,
     custom mixins, …). All entries activate simultaneously.

**``mykey.py`` is gone.** The previous Python-data-file format
(``mykey.py`` + ``mykey_local_override.py`` + ``mykey.json``) has been
fully retired — see ``launcher/config_migrate.py`` for the one-shot
upgrade path. There is no longer any ``import mykey`` in the agent.

Caching: re-loading is gated by mtime+size signature on the candidate
files plus the ``project_root`` the read was scoped to. Callers that
want to inspect a non-default root (tests, multi-project launchers)
pass ``project_root=`` to :func:`reload_mykeys`; the cache key
automatically includes the root so two roots can coexist without
stomping each other.

This module owns the cached state. ``llmcore/__init__.py`` exposes
``llmcore.mykeys`` via PEP 562 ``__getattr__`` that delegates here.
"""
import json
import os

# Self-contained — keep the OSError-safe print here too so a bad terminal
# doesn't kill the agent during mykeys reload.
from llmcore._utils import safeprint as print

# Module-level state; persists across imports because Python caches modules.
_mykey_paths: list[str] = []
_mykey_signature: tuple | None = None


def _project_root(override: str | None = None) -> str:
    """Resolve the project root for credential lookups.

    Resolution order: explicit ``override`` argument > ``WLWL_PROJECT_ROOT``
    env var > the directory above this file (the live install).
    """
    if override:
        return override
    env = os.environ.get("WLWL_PROJECT_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def _candidate_mykey_paths(project_root: str | None = None):
    """Where do we look for credentials? Project root, in stat order.

    Order doesn't reflect priority (that's resolved in :func:`_load_mykeys`).
    Listed solely so :func:`_candidate_mykey_signature` can stat each one
    for the cache-invalidation key.
    """
    root = _project_root(project_root)
    return [
        os.path.join(root, ".env"),
        os.path.join(root, ".wlwl-ass", "config.json"),
        os.path.expanduser("~/.wlwl-ass/config.json"),
        os.path.join(root, "temp", "launcher_api_configs.json"),
    ]


def _candidate_mykey_signature(project_root: str | None = None):
    """Lightweight (mtime, size) per file → reload cache key.

    The resolved root is baked into the signature too so a caller switching
    project_root invalidates the cache automatically — without that, two
    different roots with the same on-disk mtimes would silently alias.
    """
    root = _project_root(project_root)
    sig = [("__root__", root)]
    for path in _candidate_mykey_paths(root):
        if os.path.exists(path):
            st = os.stat(path)
            sig.append((os.path.basename(path), st.st_mtime_ns, st.st_size))
        else:
            sig.append((os.path.basename(path), None, None))
    return tuple(sig)


def _load_from_env(project_root: str | None = None) -> dict:
    """Load .env into os.environ, then synthesize mykey-shaped configs.

    Drains :mod:`launcher.config_store` (~/.wlwl-ass/config.json +
    <project>/.wlwl-ass/config.json) — the canonical store — first; falls back
    to :mod:`launcher.dotenv_shim`'s minimal env-var synthesizer when
    config_store is empty (typical first-run state).
    """
    try:
        from launcher import dotenv_shim as _denv
        _denv.bootstrap(project_root=_project_root(project_root))  # .env -> os.environ
    except Exception:
        pass

    out: dict = {}

    # 1) Preferred: launcher.config_store (overlays env vars itself)
    try:
        from launcher import config_store as _cs
        project_path = _cs.project_config_path(_project_root(project_root))
        store = _cs.ConfigStore(project_path=project_path)
        out.update(_cs.synthesize_mykeys_from_store(store))
    except Exception:
        pass

    # 2) Fallback: legacy dotenv synthesizer (unchanged path)
    if not out:
        try:
            from launcher import dotenv_shim as _denv
            env = dict(os.environ)
            env.update(_denv.parse_env_file(_denv._default_env_path(_project_root(project_root))))
            out.update(_denv.synthesize_mykeys(env))
        except Exception:
            pass

    return out


def _load_from_launcher_configs(project_root: str | None = None) -> tuple[dict, list[str]]:
    """Read ``temp/launcher_api_configs.json`` and emit mykey-shaped configs.

    Each entry has ``kind`` ∈ {native_oai, native_claude, mixin} plus
    arbitrary backend fields. We delegate the variable-name + payload
    extraction to :func:`launcher.api_config.safe_config_var_name` and
    :func:`launcher.api_config._config_payload` so there's a single
    source of truth for the mykey-shape contract.

    Returns ``(flat_dict, [path])``; ``({}, [])`` if the file is missing
    or malformed. Mixin configs land at their own ``mixin_config_<name>``
    keys (or just ``mixin_config`` when name is empty / "default") so
    multiple mixins can coexist.
    """
    root = _project_root(project_root)
    path = os.path.join(root, "temp", "launcher_api_configs.json")
    if not os.path.isfile(path):
        return {}, []

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Warn] launcher_api_configs.json present but failed to parse: {exc}")
        return {}, []

    configs = raw.get("configs", raw if isinstance(raw, list) else [])
    if not isinstance(configs, list):
        return {}, []

    try:
        from launcher.api_config import (
            normalize_config,
            safe_config_var_name,
            validate_config,
            _config_payload,
        )
    except Exception as exc:
        print(f"[Warn] cannot load launcher.api_config helpers: {exc}")
        return {}, []

    out: dict = {}
    used_var_names: set[str] = set()
    skipped: list[str] = []
    for raw_cfg in configs:
        if not isinstance(raw_cfg, dict):
            continue
        cfg = normalize_config(raw_cfg)
        ok, _msg = validate_config(cfg)
        if not ok:
            # Skip invalid entries — the launcher UI surfaces validation
            # errors at save time; runtime should treat broken rows as
            # absent rather than crash the whole load.
            continue
        # Drop entries whose apikey is the literal masking sentinel.
        # ``save_api_configs`` already recovers real values from the prior
        # on-disk file, but legacy round-trips could persist ``***`` —
        # don't dispatch those (they 401 every request).
        if cfg.get("kind") != "mixin":
            apikey = str(cfg.get("apikey") or "").strip()
            if not apikey or apikey == "***":
                skipped.append(str(cfg.get("name") or "<unnamed>"))
                continue
        var_name = safe_config_var_name(cfg.get("kind"), cfg.get("name"))
        # De-duplicate name collisions deterministically: foo, foo_2, foo_3…
        base, idx = var_name, 2
        while var_name in used_var_names:
            var_name = f"{base}_{idx}"
            idx += 1
        used_var_names.add(var_name)
        out[var_name] = _config_payload(cfg)

    if skipped:
        try:
            import sys as _sys
            _sys.stderr.write(
                f"[Info] Skipped masked launcher_api_configs entries: "
                f"{', '.join(skipped)}\n"
            )
        except OSError:
            pass

    return out, [path] if out else []


def _load_mykeys(project_root: str | None = None):
    """Locate + merge credentials from all available sources.

    Resolution (each later source overrides earlier on key-name collision):

      1. ``.env`` / env var synthesis + config_store  (lowest priority)
      2. ``temp/launcher_api_configs.json`` (highest — explicit launcher
         saves win)

    If the union is empty, raise — caller has nothing to dispatch with.

    ``project_root`` scopes the file reads (``temp/launcher_api_configs.json``
    and ``<root>/.wlwl-ass/config.json``) to a specific directory. Env vars +
    ``~/.wlwl-ass/config.json`` (user layer) are always global — that's by
    design: tokens shouldn't disappear when you swap project dirs.
    """
    global _mykey_paths

    merged: dict = {}
    used_paths: list[str] = []

    env_synth = _load_from_env(project_root)
    if env_synth:
        merged.update(env_synth)
        used_paths.append("<env>")

    launcher_mk, launcher_paths = _load_from_launcher_configs(project_root)
    if launcher_mk:
        merged.update(launcher_mk)
        used_paths.extend(launcher_paths)

    if not merged:
        raise Exception(
            "[ERROR] No usable LLM config found. Pick one:\n"
            "  • Drop OPENAI_API_KEY (or ANTHROPIC_API_KEY) into .env, or\n"
            "  • Run `python -m launcher.cli_init` for the interactive wizard, or\n"
            "  • Use `python -m launcher.config set providers.openai.api_key sk-...`, or\n"
            "  • Save through the GUI's API 配置 tab (writes temp/launcher_api_configs.json)."
        )

    _mykey_paths = used_paths
    return merged


def reload_mykeys(project_root: str | None = None):
    """Reload only if the on-disk signature changed.

    Returns ``(mykeys_dict, did_reload)``. Stores the dict in this module's
    globals so subsequent calls return the cached value without re-stat'ing.

    ``project_root`` is forwarded to the underlying loaders. Switching
    project_root between calls invalidates the cache (the resolved root is
    part of the signature), so isolated tmp_path tests don't bleed into
    each other.
    """
    global _mykey_signature
    sig = _candidate_mykey_signature(project_root)
    if sig == _mykey_signature:
        return globals().get('mykeys', {}), False
    mk = _load_mykeys(project_root)
    _mykey_signature = _candidate_mykey_signature(project_root)
    # Diagnostic log goes to stderr so callers like ``python -m launcher.doctor
    # --json`` can keep stdout clean for machine consumers.
    try:
        import sys as _sys
        _sys.stderr.write(f'[Info] Load mykeys from {", ".join(_mykey_paths)}\n')
    except OSError:
        pass
    globals().update(mykeys=mk)
    return mk, True
