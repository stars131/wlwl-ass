"""Parse YAML-style frontmatter at the top of skill / SOP markdown files.

Conforms to the agentskills.io (https://agentskills.io) convention used by
Hermes-style skill packs, but kept stdlib-only — we don't pull a yaml
dependency in for what's essentially flat key:value parsing. The supported
subset:

  ---
  version: 0.3.1
  authors: ["alice@example.com", "bob"]
  requires: [bottle, requests]
  tags:
    - automation
    - browser
  description: |
    A multi-line description.
    Folds whitespace at line breaks.
  summary: One-line single-quoted or unquoted string.
  ---
  # rest of the markdown body...

Parser rules (intentionally narrow):
  * The block must START at line 1 with the literal ``---`` (no leading
    blank lines, no BOM tolerance — that's a real-world SOP smell).
  * Block ends at the next standalone ``---`` line.
  * Each entry is one of:
    - ``key: value``                 — scalar
    - ``key: [a, b, c]``             — inline list
    - ``key:`` then ``  - item``×N  — block list
    - ``key: |`` then indented text — folded scalar (preserves internal newlines)
  * Quotes around scalars: optional. ``"x"``, ``'x'``, and ``x`` all parse
    to the string ``x``. ``"a,b"`` stays as the literal ``a,b`` (commas
    aren't list separators inside quoted strings).

Anything fancier (nested maps, anchors, multi-doc, !tag) is rejected — we
return ``({}, original_text)`` and let the caller treat the file as
metadata-less. Better to under-promise than ship a broken parser users
might trust.
"""
from __future__ import annotations

import re
from typing import Any


_FENCE = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into ``(metadata_dict, body_without_frontmatter)``.

    Returns ``({}, text)`` unchanged if the document doesn't open with a
    fence. Best-effort: malformed frontmatter falls back to the same
    "no metadata" path rather than raising — callers shouldn't have to
    wrap every read in try/except just to handle a stray colon.
    """
    if not text or not text.startswith(_FENCE):
        return {}, text
    # Frontmatter must be on its own line — guard against `---hr` etc.
    head, _, rest = text.partition("\n")
    if head.strip() != _FENCE:
        return {}, text
    end_match = re.search(r"^---[ \t]*$", rest, flags=re.MULTILINE)
    if not end_match:
        return {}, text
    block = rest[: end_match.start()]
    body = rest[end_match.end():]
    # Body's leading newline (after the closing fence) is conventional;
    # strip a single one so callers don't get an empty first line.
    if body.startswith("\n"):
        body = body[1:]
    try:
        meta = _parse_block(block)
    except _MalformedFrontmatter:
        return {}, text
    return meta, body


class _MalformedFrontmatter(Exception):
    """Internal sentinel — raised only inside ``_parse_block`` to escape to
    the caller's "treat as metadata-less" fallback."""


def _parse_block(block: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # Top-level entry: must not be indented.
        if line[:1] in (" ", "\t"):
            raise _MalformedFrontmatter(f"unexpected indentation at line {i+1}: {line!r}")
        if ":" not in line:
            raise _MalformedFrontmatter(f"missing ':' at line {i+1}: {line!r}")
        key, _, raw_val = line.partition(":")
        key = key.strip()
        raw_val = raw_val.rstrip()
        if not key:
            raise _MalformedFrontmatter(f"empty key at line {i+1}")
        # Folded scalar: ``key: |`` then indented continuation lines.
        if raw_val.strip() == "|":
            i += 1
            collected: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                # Strip the smallest common indent — we accept any indent
                # depth, real-world SOPs sometimes use 2 spaces, sometimes 4.
                stripped = lines[i].lstrip(" \t")
                collected.append(stripped)
                i += 1
            out[key] = "\n".join(collected).rstrip("\n")
            continue
        # Block list: ``key:`` then ``  - item`` lines.
        if raw_val.strip() == "":
            i += 1
            items: list[Any] = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(_scalar(lines[i].lstrip()[2:].strip()))
                i += 1
            out[key] = items
            continue
        # Inline list ``key: [a, b]``
        v = raw_val.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            if not inner:
                out[key] = []
            else:
                out[key] = [_scalar(p.strip()) for p in _split_csv_respecting_quotes(inner)]
            i += 1
            continue
        # Plain scalar.
        out[key] = _scalar(v)
        i += 1
    return out


def _scalar(raw: str) -> Any:
    """Coerce a raw scalar token to bool / int / float / str. Quotes are
    stripped *only* when they fully wrap the value — embedded quotes stay
    as literal characters."""
    s = raw.strip()
    if (len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"')):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    # Number? Try int first, then float — bare ``1.0`` should stay float.
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+([eE][-+]?\d+)?", s):
        return float(s)
    return s


def _split_csv_respecting_quotes(s: str) -> list[str]:
    """Tiny CSV split that won't break on ``"a,b", c`` — ``,`` inside
    matched quotes doesn't count as a separator."""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def format_metadata_summary(meta: dict[str, Any]) -> str:
    """One-line human-readable summary of the parsed metadata, intended for
    inline display next to a SOP search result. Returns ``""`` if the dict
    is empty / has no recognized keys (caller decides whether to render)."""
    if not meta:
        return ""
    bits: list[str] = []
    if "version" in meta:
        bits.append(f"v{meta['version']}")
    if meta.get("authors"):
        a = meta["authors"]
        author_str = a if isinstance(a, str) else ", ".join(str(x) for x in a)
        bits.append(f"by {author_str}")
    if meta.get("tags"):
        t = meta["tags"]
        tag_str = t if isinstance(t, str) else " ".join(f"#{x}" for x in t)
        bits.append(tag_str if isinstance(t, str) else tag_str)
    return " · ".join(bits)
