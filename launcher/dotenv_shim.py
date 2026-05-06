"""Stdlib-only ``.env`` loader + mykey synthesizer.

Two responsibilities, glued together because they're useful in the same code
path (first-run / containerized / CI deployments):

1. :func:`load_env` — read a ``.env`` file and seed ``os.environ`` *without*
   clobbering values the shell already exported. Bash-style: ``KEY=value``
   per line, ``#`` comments, single/double quotes, no shell expansion.

2. :func:`synthesize_mykeys` — turn well-known API env vars
   (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, plus ``WLWL_``-prefixed
   alternates) into the dict shape :mod:`llmcore` expects. Used as a
   last-resort fallback when nothing in :mod:`launcher.config_store`
   or ``temp/launcher_api_configs.json`` is configured, so a user can
   drop a ``.env`` in the repo root and start the agent without
   touching anything else.

Why no external ``python-dotenv`` dependency? wlwl-ass's design rule is
"stdlib only for the launcher path" so first-run never fails on a missing
package. We support a minimal surface — quotes, comments, blank lines —
which covers >99% of ``.env`` files in the wild.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Patterns recognized by ``synthesize_mykeys``. Order matters only for the
# first-match prefix logic (we strip ``WLWL_`` before lookup).
_ENV_LINE_RE = re.compile(
    r"""^\s*
        (?:export\s+)?       # optional bash 'export'
        ([A-Za-z_][A-Za-z0-9_]*)   # KEY
        \s*=\s*
        (.*?)                # VALUE (lazy — strip trailing comment below)
        \s*$
    """,
    re.VERBOSE,
)


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a ``.env`` file into a flat ``dict``.

    Handles:
      * ``KEY=value`` and ``export KEY=value``
      * ``"`` / ``'`` quoted values (escapes inside double-quotes: ``\\n``,
        ``\\t``, ``\\\\``, ``\\"`` only — no shell substitution)
      * ``#`` line comments and trailing comments on unquoted values
      * Blank lines

    Unparseable lines are silently dropped — the goal is robustness against
    hand-edited files, not strict validation. Caller can warn separately if
    they want.
    """
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        value = _strip_value(value)
        if key:
            out[key] = value
    return out


def _strip_value(v: str) -> str:
    """Strip surrounding quotes; for unquoted values, drop trailing ``#``
    comments. Process double-quote escapes (``\\n`` etc.); single-quote
    values are taken literally per POSIX shell convention."""
    v = v.strip()
    if not v:
        return ""
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        return (
            inner.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\r", "\r")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1]
    # Unquoted: drop everything from the first unescaped ``#``.
    if "#" in v:
        idx = v.index("#")
        v = v[:idx].rstrip()
    return v


def load_env(path: str | None = None, *, override: bool = False) -> int:
    """Load a ``.env`` file into ``os.environ``. Returns the number of vars
    actually set. Existing env vars are preserved unless ``override=True``
    (matches python-dotenv's default and the principle that the shell wins).
    """
    if path is None:
        path = _default_env_path()
    pairs = parse_env_file(path)
    n = 0
    for k, v in pairs.items():
        if not override and k in os.environ:
            continue
        os.environ[k] = v
        n += 1
    return n


def _default_env_path() -> str:
    """Where to find ``.env``.

    Resolution order: ``WLWL_ENV_FILE`` env var (used by tests + the
    onboarding wizard to point at a non-default file) > ``WLWL_PROJECT_ROOT``
    + ``/.env`` (so ``llmcore.reload_mykeys(project_root=…)`` cascades to
    a scoped .env naturally) > ``<repo_root>/.env`` (production default;
    ``launcher/`` sits one level below the root).
    """
    override = os.environ.get("WLWL_ENV_FILE")
    if override:
        return override
    project_root = os.environ.get("WLWL_PROJECT_ROOT")
    if project_root:
        return os.path.join(project_root, ".env")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), ".env")


# ──────────────────────────────────────────────────────────────────────────
# mykey synthesis from env vars
#
# Mapping rules:
#   ANTHROPIC_API_KEY  → native_claude_config_anthropic
#       — auto-detects sk-ant- prefix (real Anthropic) vs anything else
#         (relay/proxy → fake_cc_system_prompt=True)
#   OPENAI_API_KEY     → native_oai_config
#   ``WLWL_*`` prefixed variants take priority for users who don't want their
#   wlwl-ass config to leak into other tools.
#
# We always emit a top-level ``mixin_config`` so the launcher can pick a
# session even when the user only set one key.


def _pick(env: dict[str, str], *names: str) -> str:
    """Return the first non-empty value among ``names`` in ``env``."""
    for n in names:
        v = env.get(n, "")
        if v:
            return v
    return ""


def synthesize_mykeys(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a mykey-shaped dict from recognized env vars.

    Returns ``{}`` when no recognized API key is set — the caller treats
    that as "no fallback available" and falls through to the next layer.
    """
    if env is None:
        env = dict(os.environ)
    out: dict[str, Any] = {}
    names: list[str] = []

    anth_key = _pick(env, "WLWL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    if anth_key:
        anth_base = _pick(env, "WLWL_ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        anth_model = _pick(env, "WLWL_ANTHROPIC_MODEL", "ANTHROPIC_MODEL") or "claude-opus-4-7"
        cfg = {
            "name": "anthropic-env",
            "apikey": anth_key,
            "apibase": anth_base,
            "model": anth_model,
        }
        # Real Anthropic endpoints use sk-ant-; anything else is a relay that
        # almost certainly needs the CC fingerprint to pass auth.
        if not anth_key.startswith("sk-ant-"):
            cfg["fake_cc_system_prompt"] = True
        out["native_claude_config_env"] = cfg
        names.append("anthropic-env")

    oai_key = _pick(env, "WLWL_OPENAI_API_KEY", "OPENAI_API_KEY")
    if oai_key:
        oai_base = _pick(env, "WLWL_OPENAI_BASE_URL", "OPENAI_BASE_URL") or "https://api.openai.com/v1"
        oai_model = _pick(env, "WLWL_OPENAI_MODEL", "OPENAI_MODEL") or "gpt-5.4"
        out["native_oai_config_env"] = {
            "name": "openai-env",
            "apikey": oai_key,
            "apibase": oai_base,
            "model": oai_model,
        }
        names.append("openai-env")

    if not names:
        return {}

    out["mixin_config"] = {
        "llm_nos": names,
        "max_retries": 5,
        "base_delay": 0.5,
    }
    return out


def bootstrap(*, override: bool = False) -> tuple[int, str]:
    """One-shot ``.env`` loader for use early in the launcher path.

    Returns ``(vars_loaded, path)``. Safe to call repeatedly — re-loading
    only re-applies values the shell hasn't overridden.
    """
    path = _default_env_path()
    n = load_env(path, override=override)
    return n, path
