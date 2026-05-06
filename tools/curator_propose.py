"""``curator_propose`` — agent-callable tool for submitting curation proposals.

Append-only JSONL ledger at ``memory/curator_proposals.jsonl``. Each entry:
  {ts, target, insight, rationale, source_turn?, source_session?}

Why JSONL:
  * Append-only by design → no race between concurrent writes (single
    fsync per line).
  * Trivially greppable / loadable into the GUI's review tab.
  * No fancy DB needed.

Why not auto-apply:
  L1 (``memory/global_mem_insight.txt``) is 22 lines hand-curated to be
  the project's terse top-level index. A misaligned auto-patch could
  silently degrade every future agent run. Manual review keeps the
  bar high.
"""
from __future__ import annotations

import json
import os
import time
from typing import Literal


_VALID_TARGETS = ("L1", "L2", "user_profile")
_PROPOSALS_REL_PATH = os.path.join("memory", "curator_proposals.jsonl")


def _project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def curator_propose(
    insight: str,
    target: Literal["L1", "L2", "user_profile"] = "L1",
    rationale: str = "",
    source_turn: int | None = None,
    source_session: str = "",
) -> str:
    """Append a curation proposal to ``memory/curator_proposals.jsonl``.

    Returns a short status string suitable for use as a ``tool_result``.
    Validates the target name (``L1`` / ``L2`` / ``user_profile``) and
    requires a non-empty ``insight`` — anything else is rejected with
    ``[curator_propose error] ...``.
    """
    insight = (insight or "").strip()
    if not insight:
        return "[curator_propose error] insight is required (one-line)"
    if target not in _VALID_TARGETS:
        return f"[curator_propose error] target must be one of {_VALID_TARGETS}, got {target!r}"
    if len(insight) > 500:
        return "[curator_propose error] insight must be ≤ 500 characters; keep it concise"

    record = {
        "ts": time.time(),
        "target": target,
        "insight": insight,
        "rationale": (rationale or "").strip(),
    }
    if source_turn is not None:
        try:
            record["source_turn"] = int(source_turn)
        except (TypeError, ValueError):
            pass
    if source_session:
        record["source_session"] = str(source_session)[:200]

    path = os.path.join(_project_root(), _PROPOSALS_REL_PATH)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Append-only — one JSON object per line, newline-terminated.
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        return f"[curator_propose error] could not write {path}: {exc}"
    return (
        f"Proposal recorded for {target}. The user will review proposals "
        f"in {_PROPOSALS_REL_PATH} before any change to canonical memory."
    )


def list_proposals(*, limit: int = 50) -> list[dict]:
    """Read recent proposals (newest last). Returns ``[]`` when the
    ledger is missing / corrupt — never raises."""
    path = os.path.join(_project_root(), _PROPOSALS_REL_PATH)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip corrupt rows; never blow up the agent
    except OSError:
        return []
    return out[-max(1, int(limit)):]
