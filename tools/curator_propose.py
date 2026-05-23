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


_VALID_TARGETS = ("L1", "L2", "user_profile", "playbook")
_PROPOSALS_REL_PATH = os.path.join("memory", "curator_proposals.jsonl")


def _project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def proposals_path() -> str:
    override = os.environ.get("WLWL_CURATOR_PROPOSALS_PATH")
    if override:
        return override
    return os.path.join(_project_root(), _PROPOSALS_REL_PATH)


def curator_propose(
    insight: str,
    target: Literal["L1", "L2", "user_profile", "playbook"] = "L1",
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

    path = proposals_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Append-only — one JSON object per line, newline-terminated.
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        return f"[curator_propose error] could not write {path}: {exc}"
    if target == "playbook":
        try:
            from launcher import playbook

            entry = playbook.propose(
                insight,
                category="curator",
                rationale=rationale,
                source="curator_propose",
                source_turn=record.get("source_turn"),
                source_session=str(record.get("source_session") or ""),
            )
            return (
                f"Playbook proposal {entry['id']} recorded. It will be injected "
                "only after user approval."
            )
        except Exception as exc:
            return (
                f"Proposal recorded for {target}, but playbook queue failed: {exc}. "
                f"The user can still review {_PROPOSALS_REL_PATH}."
            )
    return (
        f"Proposal recorded for {target}. The user will review proposals "
        f"in {_PROPOSALS_REL_PATH} before any change to canonical memory."
    )


def list_proposals(*, limit: int = 50) -> list[dict]:
    """Read recent proposals (newest last). Returns ``[]`` when the
    ledger is missing / corrupt — never raises."""
    path = proposals_path()
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
