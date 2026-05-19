"""Deep-link parser + endpoint URL probe.

Two small helpers borrowed in spirit from cc-switch:

1. **Deep-link import** — parse a ``wlwl-config://...`` (or ``ccswitch://``
   compatibility alias) URL into a config dict. Mirrors cc-switch's
   one-click provider import, but as plain text/copy-paste — no OS-level
   protocol handler required. Sample:

       wlwl-config://provider?preset=kimi&apikey=sk-...
       wlwl-config://provider?name=my-mix&apibase=https://x.y/v1&model=gpt-4.1&kind=native_oai

2. **Endpoint probe** — given a list of candidate URLs (cc-switch's
   ``endpointCandidates``), run a quick GET / HEAD against each and
   return their latency in ms, so the user can pick the fastest one.
   Used by ``wlwl config probe NAME``.

Both functions live in ``launcher`` so they're reusable from the CLI
subcommand, the REPL slash command, and (eventually) the GUI.
"""
from __future__ import annotations

import socket
import time
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

from launcher import api_presets


SCHEMES = ("wlwl-config", "wlwl", "ccswitch")
PROBE_TIMEOUT_S = 4.0


def parse_deep_link(url: str) -> dict:
    """Convert a ``wlwl-config://...`` URL into a config dict.

    Recognised query params:
      ``preset``   – preset id; the preset is expanded first, then overrides
                     below apply on top
      ``name``     – display name for the config (defaults to preset id or
                     a "imported" fallback)
      ``apikey``   – API key (left empty when omitted; user can fill later)
      ``apibase``  – overrides preset's apibase
      ``model``    – overrides preset's model
      ``kind``     – overrides preset's kind (``native_oai`` / ``native_claude``)

    Raises ``ValueError`` on unsupported schemes or malformed inputs."""
    if not isinstance(url, str) or "://" not in url:
        raise ValueError(f"not a deep link: {url!r}")
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEMES:
        raise ValueError(
            f"unsupported scheme {scheme!r}; expected one of "
            f"{', '.join(SCHEMES)}"
        )
    # Both ``wlwl-config://provider?...`` and ``wlwl-config:provider?...``
    # (no //) should work — urlsplit treats them slightly differently.
    raw_query = parts.query or ""
    if not raw_query and "?" in parts.path:
        raw_query = parts.path.split("?", 1)[1]
    params = {k: (v[0] if v else "") for k, v in parse_qs(raw_query).items()}

    preset_id = params.pop("preset", "").strip().lower()
    config = {}
    if preset_id:
        preset = api_presets.get_preset(preset_id)
        if not preset:
            raise ValueError(
                f"unknown preset id {preset_id!r}; "
                "run `wlwl config presets` to see the list"
            )
        config = api_presets.preset_to_config(preset)

    # Apply explicit overrides (these win over preset defaults).
    for field in ("name", "apikey", "apibase", "model", "kind"):
        if field in params and params[field]:
            config[field] = params[field].strip()

    config.setdefault("kind", "native_oai")
    config.setdefault("name", preset_id or "imported")

    if config["kind"] not in ("native_oai", "native_claude"):
        raise ValueError(
            f"unsupported kind {config['kind']!r}; "
            "deep-link import only handles native_oai / native_claude"
        )
    if not config.get("apibase"):
        raise ValueError(
            "deep link missing required field: apibase "
            "(provide ?apibase=... or ?preset=...)"
        )
    if not config.get("model"):
        raise ValueError(
            "deep link missing required field: model "
            "(provide ?model=... or ?preset=...)"
        )
    return config


def probe_endpoint(url: str, timeout_s: float = PROBE_TIMEOUT_S) -> float | None:
    """Return latency in milliseconds for a TCP connect to ``url``'s host,
    or ``None`` on failure.

    Why TCP and not HTTP? Many of these endpoints expect signed headers,
    OAuth tokens, or per-account paths; a bare HEAD often returns 401/403
    even on a perfectly healthy host. A TCP connect time is enough to
    rank "reachable + fast" vs "blocked / slow", which is the question
    ``wlwl config probe`` is actually asking."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    addr_family = socket.AF_INET
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            return None
        addr_family, _, _, _, sockaddr = infos[0]
    except (socket.gaierror, OSError):
        return None
    start = time.monotonic()
    sock = socket.socket(addr_family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_s)
        sock.connect(sockaddr)
    except (socket.timeout, OSError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return round((time.monotonic() - start) * 1000.0, 1)


def probe_all(urls: Iterable[str], timeout_s: float = PROBE_TIMEOUT_S) -> list[tuple[str, float | None]]:
    """Probe each URL once, sequentially, returning ``(url, latency_ms_or_None)``.

    Sequential is fine for the typical 1-5 candidate URLs per preset;
    parallelism would just add complexity without changing the answer."""
    return [(u, probe_endpoint(u, timeout_s=timeout_s)) for u in urls]
