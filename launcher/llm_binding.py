"""Per-bot LLM binding parser and resolver.

A "binding" is a short string carried in ``config_store.bots.<key>.llm_binding``
and propagated to bot subprocesses via the ``WLWL_BOT_LLM_BINDING`` env var.

Three forms::

    ""                  → empty / default behaviour (use whatever the agent
                          picks today — typically the first config in
                          ``launcher_api_configs.json``).
    "config:<name>"     → pin the bot to a single API config.
    "profile:<name>"    → use the named profile (a list of config names from
                          ``launcher/profiles.py``) as a fallback chain. The
                          first config is primary; on errors the bot falls
                          through to the next; ``MixinSession`` reuses this.

Helpers here are pure functions so they're trivial to unit-test. The actual
runtime injection (mutating ``llmcore.mykeys`` / calling
``agent.select_llm_by_name``) lives in ``frontends/fsapp.py`` and
``frontends/fsapp_concierge.py``.

See ``docs/adr/0011-feishu-concierge-bot.md`` for the broader concierge-bot
architecture; this module was added on 2026-05-19 when the owner Feishu bot
spent a full day hitting HTTP 402 ``Insufficient Balance`` against DeepSeek
because the global "first config wins" policy had no per-bot override.
"""
from __future__ import annotations

from typing import Any, Literal


BindingKind = Literal["", "config", "profile"]


def parse_binding(s: str | None) -> tuple[BindingKind, str]:
    """Parse a binding string. Unknown prefixes return ``("", "")`` so the
    caller can fall back to default behaviour without raising."""
    if not s:
        return "", ""
    text = str(s).strip()
    if not text:
        return "", ""
    if ":" not in text:
        return "", ""
    prefix, _, rest = text.partition(":")
    kind = prefix.strip().lower()
    name = rest.strip()
    if not name:
        return "", ""
    if kind == "config":
        return "config", name
    if kind == "profile":
        return "profile", name
    return "", ""


def resolve_to_config_names(
    kind: BindingKind,
    name: str,
    profiles_state: dict[str, Any],
    configs_list: list[dict[str, Any]],
) -> list[str]:
    """Return the ordered list of config ``name`` strings this binding maps to.

    Empty list means "nothing usable — fall back to default behaviour".
    Members of a profile that don't appear in ``configs_list`` are dropped
    with a stderr warning (rather than raising) so a stale profile from a
    deleted config doesn't take the whole bot down. ``mixin`` configs are
    also dropped from profile expansion — wrapping a mixin inside another
    mixin would just stack ``MixinSession`` on top of itself, which
    ``MixinSession.__init__`` would reject at the ``len(groups)==1`` assert.
    """
    if not kind or not name:
        return []
    existing = {str(c.get("name") or ""): c for c in (configs_list or [])
                if isinstance(c, dict)}
    if kind == "config":
        if name in existing:
            return [name]
        print(f"[llm_binding] config {name!r} not found in launcher_api_configs.json — falling back to default")
        return []
    # kind == "profile"
    profiles = (profiles_state or {}).get("profiles") or {}
    members = profiles.get(name)
    if not members:
        print(f"[llm_binding] profile {name!r} not found or empty — falling back to default")
        return []
    resolved: list[str] = []
    for member in members:
        m = str(member or "").strip()
        if not m:
            continue
        entry = existing.get(m)
        if entry is None:
            print(f"[llm_binding] profile {name!r}: config {m!r} missing — skipped")
            continue
        if str(entry.get("kind") or "") == "mixin":
            print(f"[llm_binding] profile {name!r}: skipped mixin member {m!r} (no nested mixins)")
            continue
        resolved.append(m)
    if not resolved:
        print(f"[llm_binding] profile {name!r} resolved to nothing — falling back to default")
    return resolved


def sort_config_names_by_priority(
    config_names: list[str],
    configs_list: list[dict[str, Any]],
) -> list[str]:
    """Sort resolved config names by the API config page ordering.

    The API config page groups by category and sorts each category by
    descending numeric priority. Runtime profile bindings need the same
    ordering so selecting a profile in Sessions means "use the best member
    first", not "use whatever checkbox order happened to be saved".
    """
    if not config_names:
        return []
    try:
        from launcher.api_config import API_CONFIG_CATEGORY_ORDER, normalize_priority
    except Exception:  # pragma: no cover - import failure fallback is trivial
        API_CONFIG_CATEGORY_ORDER = {
            "language": 0,
            "multimodal": 1,
            "voice": 2,
            "utility": 3,
        }

        def normalize_priority(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

    wanted = {name: idx for idx, name in enumerate(config_names)}
    by_name = {
        str(c.get("name") or ""): c
        for c in (configs_list or [])
        if isinstance(c, dict)
    }

    def key(name: str):
        cfg = by_name.get(name) or {}
        category = str(cfg.get("category") or "language")
        return (
            API_CONFIG_CATEGORY_ORDER.get(category, len(API_CONFIG_CATEGORY_ORDER)),
            -normalize_priority(cfg.get("priority")),
            wanted.get(name, len(wanted)),
        )

    return sorted([name for name in config_names if name in wanted], key=key)


def resolve_to_config_names_by_priority(
    kind: BindingKind,
    name: str,
    profiles_state: dict[str, Any],
    configs_list: list[dict[str, Any]],
) -> list[str]:
    """Resolve a binding, then order profile members by category/priority.

    Single-config bindings are unchanged. Profile bindings still drop missing
    and nested mixin members via :func:`resolve_to_config_names`, then sort the
    remaining members to match the API config page's category + priority
    ordering.
    """
    resolved = resolve_to_config_names(kind, name, profiles_state, configs_list)
    if kind == "profile":
        return sort_config_names_by_priority(resolved, configs_list)
    return resolved


def synthesize_mixin_entry(
    bot_key: str,
    config_names: list[str],
) -> tuple[str, dict[str, Any]]:
    """Build a virtual mixin config dict that ``agentmain.load_llm_sessions``
    will assemble into a ``MixinSession`` covering ``config_names`` in order.

    Returns ``(mykeys_key, payload)``. ``payload['llm_nos']`` uses the public
    config ``name`` strings (not ``var_name``) because
    ``MixinSession.__init__`` looks up sub-sessions by ``backend.name``
    (mixin.py:28), which matches the ``name`` field of each underlying
    config.
    """
    if not config_names:
        raise ValueError("synthesize_mixin_entry requires at least one config name")
    safe_key = str(bot_key or "anon").strip().lower() or "anon"
    mykeys_key = f"mixin_config_bot_{safe_key}"
    payload = {
        "kind": "mixin",
        "name": f"bot_{safe_key}",
        "llm_nos": list(config_names),
        "max_retries": max(3, len(config_names) + 1),
        "base_delay": 0.5,
        "spring_back": 300,
    }
    return mykeys_key, payload
