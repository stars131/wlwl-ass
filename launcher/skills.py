"""Skill (SOP) catalogue scanner.

A "skill" in this codebase is an SOP markdown file under ``<repo>/memory/`` —
plus the special ``subagent.md`` reference that the agent treats the same way.
This module enumerates those files and joins them with the runtime outcome
counts emitted by :mod:`launcher.activity_log`.

Why a dedicated module? :mod:`launcher.activity_log` deals with append-only
event logs — ephemeral, daily-rotated, reset on git clean. Skills are the
persistent definitions that *produce* those events; conflating the two would
muddle the responsibility and tests.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
from typing import Any

from launcher import activity_log

_TITLE_MAX = 120
_SUBTITLE_MAX = 240


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def memory_dir() -> str:
    """Resolve the SOP directory. Override via ``WLWL_MEMORY_DIR`` for tests."""
    override = os.environ.get("WLWL_MEMORY_DIR")
    if override:
        return override
    return os.path.join(_project_root(), "memory")


def _is_skill_file(name: str) -> bool:
    """SOP files end in ``_sop.md``. ``subagent.md`` is included by convention
    (it is referenced from RULES the same way and has its own outcome bucket
    when the agent dispatches subagent calls)."""
    if not name.endswith(".md"):
        return False
    return name.endswith("_sop.md") or name == "subagent.md"


def _extract_header(path: str) -> tuple[str, str]:
    """Read the first H1 (``# Title``) and the first paragraph after it.

    We never read more than ~4 KB — SOPs are big and we only need the lede.
    Returns ``(title, subtitle)``. Either may be empty.
    """
    title = ""
    subtitle_parts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return "", ""
    for raw in io.StringIO(head):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not title:
            if stripped.startswith("# "):
                title = stripped[2:].strip()
            elif stripped.startswith("#"):
                # Some SOPs start with `## …` if they were extracted from a
                # bigger doc. Be lenient.
                title = stripped.lstrip("#").strip()
            continue
        if not stripped:
            if subtitle_parts:
                break
            continue
        if stripped.startswith("#"):
            break  # next heading — stop accumulating.
        subtitle_parts.append(stripped)
        if sum(len(p) for p in subtitle_parts) > _SUBTITLE_MAX:
            break
    subtitle = " ".join(subtitle_parts).strip()
    if len(title) > _TITLE_MAX:
        title = title[:_TITLE_MAX].rstrip() + "…"
    if len(subtitle) > _SUBTITLE_MAX:
        subtitle = subtitle[:_SUBTITLE_MAX].rstrip() + "…"
    return title, subtitle


def _relative_path(path: str) -> str:
    """Repo-relative path with forward slashes. Falls back to the bare name
    when the file lives on a different drive (Windows tmp_path on a different
    drive from the repo)."""
    try:
        rel = os.path.relpath(path, _project_root())
    except ValueError:
        rel = os.path.basename(path)
    return rel.replace("\\", "/")


def _iso_utc(ts: float) -> str:
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def list_skills() -> list[dict[str, Any]]:
    """Return all known skills with metadata + outcome stats merged in.

    Sort: highest ``total`` invocations first, then alphabetical by name.
    Skills with no recorded turns appear at the end (``outcomes`` is None).
    The synthetic ``_unattributed`` bucket from
    :func:`activity_log.summarize_outcomes` is appended last when present —
    it represents turn_end events whose ``related_sop`` was empty, which is
    valuable for noticing skill-attribution gaps but is not a real SOP.
    """
    md = memory_dir()
    outcomes = activity_log.summarize_outcomes()
    items: list[dict[str, Any]] = []

    if os.path.isdir(md):
        for name in sorted(os.listdir(md)):
            if not _is_skill_file(name):
                continue
            path = os.path.join(md, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            stem = name[:-3]  # strip ``.md``
            title, subtitle = _extract_header(path)
            items.append({
                "name": stem,
                "title": title or stem,
                "subtitle": subtitle,
                "path": _relative_path(path),
                "size_bytes": stat.st_size,
                "mtime": _iso_utc(stat.st_mtime),
                "outcomes": outcomes.get(stem),
            })

    items.sort(
        key=lambda it: (
            -((it.get("outcomes") or {}).get("total", 0)),
            it["name"],
        )
    )

    if "_unattributed" in outcomes:
        items.append({
            "name": "_unattributed",
            "title": "(no skill attributed)",
            "subtitle": (
                "Turns that ended without a related_sop set. Rising counts "
                "here mean the agent is solving tasks without recording "
                "which playbook it followed."
            ),
            "path": "",
            "size_bytes": 0,
            "mtime": "",
            "outcomes": outcomes["_unattributed"],
        })
    return items
