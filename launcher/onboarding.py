"""First-run onboarding helpers.

Two responsibilities:

1. :func:`status` — tell the GUI whether the user has a usable LLM
   configuration. We treat a config as "real" when it has an ``apikey``
   that doesn't look like one of the template placeholders shipped by
   the docs (``sk-YOUR-KEY``, ``<your-…>``, etc). Pure placeholders /
   no configs = onboarding needed.

2. :func:`save_minimal` — write the user-supplied key/base/model into
   ``<repo>/.env`` so :mod:`launcher.dotenv_shim` can synthesize a
   session on next start. ``.env`` is uniformly safe to append to:
   gitignored, parsed by the shim, no Python syntax to break.

Power users who need multi-channel / mixin / advanced fields can save
through the GUI's "API 配置" tab, which writes
``temp/launcher_api_configs.json``. The wizard intentionally stays out
of that file — it's the launcher's territory.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Substrings that mark a value as a template placeholder rather than a
# real key. Conservatively long list so users who paste a real key with
# the literal ``YOUR`` substring are still recognized (rare).
_PLACEHOLDER_PATTERNS = (
    "<your",          # mykey_template uses '<your-…>'
    "your-key",
    "your-openai",
    "your-anthropic",
    "your-relay",
    "your-key>",
    "sk-your",        # mykey_template_minimal: 'sk-YOUR-KEY'
    "sk-user-<",
    "sk-ant-<",
    "cr_<your",
    "cli_xxx",
    "f0f1b798xxxx",
)

# Keys treated as session config containers. Mirrors the dispatcher in
# ``agentmain.py`` (which scans for any of: api / config / cookie).
_CONFIG_KEY_HINT_RE = re.compile(r"(api|config|cookie)", re.IGNORECASE)

# Recognized providers for ``save_minimal``. The labels also serve as the
# first segment in the env var names the dotenv synthesizer reads.
_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "key_var": "OPENAI_API_KEY",
        "base_var": "OPENAI_BASE_URL",
        "model_var": "OPENAI_MODEL",
        "default_base": "https://api.openai.com/v1",
        "default_model": "gpt-5.4",
    },
    "anthropic": {
        "key_var": "ANTHROPIC_API_KEY",
        "base_var": "ANTHROPIC_BASE_URL",
        "model_var": "ANTHROPIC_MODEL",
        "default_base": "https://api.anthropic.com",
        "default_model": "claude-opus-4-7",
    },
}


def _project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def _env_path() -> str:
    """Allow tests to redirect via ``WLWL_ENV_FILE``; otherwise <repo>/.env."""
    override = os.environ.get("WLWL_ENV_FILE")
    if override:
        return override
    return os.path.join(_project_root(), ".env")


def _is_placeholder(value: str) -> bool:
    """``True`` when the apikey value is one of the template placeholders."""
    if not value:
        return True
    low = value.lower()
    return any(p in low for p in _PLACEHOLDER_PATTERNS)


def _has_real_session(mykeys: dict[str, Any]) -> bool:
    """True iff at least one config-shaped entry carries a non-placeholder key."""
    for name, value in mykeys.items():
        if not _CONFIG_KEY_HINT_RE.search(name):
            continue
        if not isinstance(value, dict):
            continue
        # Mixin doesn't carry its own key — it points at other configs by
        # name. The other configs already get checked on their own pass.
        if "mixin" in name.lower():
            continue
        apikey = value.get("apikey", "")
        if isinstance(apikey, str) and apikey and not _is_placeholder(apikey):
            return True
    return False


def status() -> dict[str, Any]:
    """Snapshot whether the user needs to be walked through setup.

    Returns ``{needs_setup, reason, env_recognized, has_mykey, providers}``.
    Errors loading the merged credential view are surfaced as
    ``needs_setup=True`` with a ``reason`` describing the load failure —
    don't leave the user staring at a blank screen because of a stack
    trace they won't see.
    """
    out: dict[str, Any] = {
        "needs_setup": False,
        "reason": "",
        "env_recognized": False,
        "has_mykey": False,
        "providers": list(_PROVIDERS.keys()),
    }
    try:
        import llmcore
        mk, _changed = llmcore.reload_mykeys()
    except Exception as exc:
        # Loading itself failed — definitely first-run-like. Tell the GUI
        # so it can render the same wizard and unblock the user.
        out["needs_setup"] = True
        out["reason"] = f"mykey load failed: {type(exc).__name__}: {exc}"
        return out

    out["has_mykey"] = bool(mk)
    if _has_real_session(mk):
        return out

    # No real key found in mykey/launcher_api_configs/etc. — see whether
    # the dotenv synthesizer would have produced something from the env.
    # If yes, this is *not* first-run; the user already configured via
    # ``.env`` or a global env var.
    try:
        from launcher import dotenv_shim
        synth = dotenv_shim.synthesize_mykeys()
    except Exception:
        synth = {}
    if synth:
        out["env_recognized"] = True
        return out

    out["needs_setup"] = True
    out["reason"] = "No usable LLM config found (placeholders only)."
    return out


def _read_env_pairs(path: str) -> tuple[dict[str, str], list[str]]:
    """Read a ``.env`` into both a kv-dict and the verbatim line list so we
    can patch existing values in place without scrambling the user's
    comments and ordering."""
    if not os.path.isfile(path):
        return {}, []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}, []
    from launcher.dotenv_shim import parse_env_file
    return parse_env_file(path), lines


_ENV_LINE_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _patch_or_append(lines: list[str], key: str, value: str) -> list[str]:
    """Replace the existing ``KEY=...`` line if present; otherwise append.
    Preserves surrounding comments / blank lines so the file looks like
    something a human edited, not a generated artifact."""
    rendered = f"{key}={_quote(value)}"
    for i, line in enumerate(lines):
        m = _ENV_LINE_KEY_RE.match(line)
        if m and m.group(1) == key:
            lines[i] = rendered
            return lines
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(rendered)
    return lines


def _quote(value: str) -> str:
    """Wrap in double-quotes only when the value contains characters that
    confuse the parser (whitespace, ``#``, quotes). Plain alnum/punct keys
    stay unquoted so the file reads like other ``.env`` files."""
    if not value:
        return '""'
    needs_quote = any(c in value for c in ' \t#"\'\n')
    if not needs_quote:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def save_minimal(provider: str, *, apikey: str, base_url: str = "",
                 model: str = "") -> dict[str, Any]:
    """Persist the wizard's answers into ``.env``. Returns the resulting
    onboarding status (so the GUI can re-render without a separate fetch).

    Validation is intentionally minimal: empty key → ValueError; unknown
    provider → ValueError. We do NOT call out to the LLM here; that's the
    GUI's "test connection" affordance to add later if desired.
    """
    if provider not in _PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    if not apikey or not apikey.strip():
        raise ValueError("apikey is required")
    spec = _PROVIDERS[provider]
    base_value = (base_url or "").strip() or spec["default_base"]
    model_value = (model or "").strip() or spec["default_model"]

    path = _env_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _existing_pairs, lines = _read_env_pairs(path)
    lines = _patch_or_append(lines, spec["key_var"], apikey.strip())
    lines = _patch_or_append(lines, spec["base_var"], base_value)
    lines = _patch_or_append(lines, spec["model_var"], model_value)
    text = "\n".join(lines).rstrip("\n") + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    # Also seed the live process so the user doesn't have to restart.
    os.environ[spec["key_var"]] = apikey.strip()
    os.environ[spec["base_var"]] = base_value
    os.environ[spec["model_var"]] = model_value

    return status()
