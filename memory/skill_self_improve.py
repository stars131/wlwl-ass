"""Skills self-improvement (#6).

When the agent finishes using a skill (SOP) and notices a better path —
extra precondition, missing tool call, wrong example — it can call
``propose_patch(skill_id, diff, reason=...)`` to record the improvement
proposal. Proposals are saved to ``memory/skills_proposals.jsonl`` and
exposed via /api/skills/proposals so the user can review/accept/reject.

Why proposals (not direct writes):
  * SOPs are committed to git; agents auto-patching them risks corrupting
    the L2 layer that other agents rely on.
  * The user has final say. The proposal queue gives a one-glance review
    surface.
  * Accepted proposals apply the patch atomically and back-up the original
    to ``memory/skill_backups/<skill>_<ts>.md``.

Diff format: standard unified diff (the agent generates it via tooling
already in use). We do *not* try to be smart about merging; if a diff
fails to apply, the proposal goes into ``status: rejected`` with the
reason logged.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
from datetime import datetime
from typing import Any

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROPOSALS_PATH = os.path.join(_BASE, "memory", "skills_proposals.jsonl")
_BACKUP_DIR = os.path.join(_BASE, "memory", "skill_backups")
_SKILLS_DIR = os.path.join(_BASE, "memory")  # SOPs live as memory/<name>_sop.md

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gen_id() -> str:
    return f"prop_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"


def _resolve_skill_path(skill_id: str) -> str | None:
    """Map a skill_id to an existing SOP file. Accepts:

      * "plan" → memory/plan_sop.md
      * "plan_sop" → memory/plan_sop.md
      * "plan_sop.md" → memory/plan_sop.md
      * "memory/plan_sop.md" → unchanged
    """
    sid = (skill_id or "").strip()
    if not sid:
        return None
    candidates = []
    if sid.endswith(".md"):
        candidates.append(sid if os.path.isabs(sid) else os.path.join(_BASE, sid))
        candidates.append(os.path.join(_SKILLS_DIR, sid))
    else:
        candidates.append(os.path.join(_SKILLS_DIR, sid + ".md"))
        if not sid.endswith("_sop"):
            candidates.append(os.path.join(_SKILLS_DIR, sid + "_sop.md"))
    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


# ── persistence ──────────────────────────────────────────────────────


def _read_all() -> list[dict[str, Any]]:
    if not os.path.isfile(_PROPOSALS_PATH):
        return []
    out = []
    with open(_PROPOSALS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_all(rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_PROPOSALS_PATH), exist_ok=True)
    tmp = _PROPOSALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, _PROPOSALS_PATH)


def _normalize_revision_meta(
    *,
    evidence: list[str] | tuple[str, ...] | str | None = None,
    change_type: str = "unspecified",
    reflection: str = "",
    execution_lapse: bool = False,
    skill_followed: bool | None = None,
) -> dict[str, Any]:
    """Normalize optional audit metadata for skill-evolution proposals.

    These advisory fields keep the old proposal flow intact while capturing
    SkillEvolver-style online refinement evidence and EmbodiSkill-style
    separation between invalid skill content and execution lapses.
    """
    if evidence is None:
        evidence_items: list[str] = []
    elif isinstance(evidence, str):
        evidence_items = [evidence]
    else:
        evidence_items = [str(item) for item in evidence if str(item).strip()]
    return {
        "change_type": (change_type or "unspecified").strip() or "unspecified",
        "evidence": evidence_items,
        "reflection": reflection or "",
        "execution_lapse": bool(execution_lapse),
        "skill_followed": skill_followed,
    }


# ── public API ───────────────────────────────────────────────────────


def propose_patch(
    skill_id: str,
    diff: str,
    *,
    reason: str = "",
    evidence: list[str] | tuple[str, ...] | str | None = None,
    change_type: str = "unspecified",
    reflection: str = "",
    execution_lapse: bool = False,
    skill_followed: bool | None = None,
) -> dict[str, Any]:
    """Record a proposed patch to a skill. Returns the proposal record.

    Does NOT apply the diff — only logs it. ``accept(proposal_id)`` is the
    apply path.
    """
    if not skill_id or not diff:
        raise ValueError("skill_id and diff are both required")
    skill_path = _resolve_skill_path(skill_id)
    revision_meta = _normalize_revision_meta(
        evidence=evidence,
        change_type=change_type,
        reflection=reflection,
        execution_lapse=execution_lapse,
        skill_followed=skill_followed,
    )
    proposal = {
        "id": _gen_id(),
        "skill_id": skill_id,
        "skill_path": skill_path,
        "diff": diff,
        "reason": reason or "",
        "status": "open",
        "proposed_at": _now_iso(),
        "decided_at": None,
        "decision_note": None,
        "revision_meta": revision_meta,
    }
    with _lock:
        rows = _read_all()
        rows.append(proposal)
        _write_all(rows)
    return proposal


def list_proposals(*, status: str | None = None) -> list[dict[str, Any]]:
    rows = _read_all()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: r.get("proposed_at", ""), reverse=True)
    return rows


def _backup_skill(skill_path: str) -> str:
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    name = os.path.basename(skill_path)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = os.path.join(_BACKUP_DIR, f"{name}.{stamp}.bak")
    shutil.copy2(skill_path, dest)
    return dest


def _apply_unified_diff(original: str, diff: str) -> tuple[bool, str, str]:
    """Apply a unified diff to ``original`` text in pure Python.

    Returns ``(ok, new_text, message)``. On failure ``new_text`` equals
    ``original`` and ``message`` describes which hunk failed and why.

    We intentionally don't use the optional ``unidiff`` package — staying
    stdlib-only means this works on any user's machine without ``pip
    install``. The trade-off is: we accept *strict* unified diffs (the
    output of ``diff -u``) but reject anything weird like word-diff or
    git binary patches.
    """
    lines = diff.splitlines()
    i = 0
    # Skip pre-amble (--- a/foo / +++ b/foo / "diff --git" / etc.)
    while i < len(lines) and not lines[i].startswith("@@"):
        i += 1
    if i == len(lines):
        return False, original, "no @@ hunk headers found"

    # Walk hunks; after each, splice in the new content.
    src_lines = original.splitlines(keepends=False)
    out_lines: list[str] = []
    cursor = 0  # next index in src_lines we haven't copied yet
    hunk_no = 0

    while i < len(lines):
        header = lines[i]
        i += 1
        if not header.startswith("@@"):
            return False, original, f"expected @@ at line {i}, got: {header!r}"
        hunk_no += 1
        # Parse "@@ -a,b +c,d @@"
        try:
            parts = header.split("@@")[1].strip().split()
            old_spec = parts[0]  # "-a,b" or "-a"
            old_start = int(old_spec.lstrip("-").split(",")[0])
        except Exception as exc:
            return False, original, f"hunk {hunk_no} header malformed: {header!r} ({exc})"

        # Collect hunk body until next @@ or end.
        body: list[str] = []
        while i < len(lines) and not lines[i].startswith("@@"):
            body.append(lines[i])
            i += 1

        # Apply: copy untouched src_lines up to old_start-1, then walk body.
        target_idx = max(0, old_start - 1)
        if target_idx > len(src_lines):
            return False, original, (
                f"hunk {hunk_no}: target line {old_start} past end "
                f"of file ({len(src_lines)} lines)"
            )
        # Carry over context before the hunk verbatim.
        if cursor < target_idx:
            out_lines.extend(src_lines[cursor:target_idx])
            cursor = target_idx

        for body_idx, raw in enumerate(body):
            if not raw:
                # Empty hunk line (very last line of patch sometimes) —
                # treat as a context blank.
                if cursor < len(src_lines) and src_lines[cursor] == "":
                    out_lines.append("")
                    cursor += 1
                continue
            tag, content = raw[0], raw[1:]
            if tag == " ":  # context line
                if cursor >= len(src_lines):
                    return False, original, (
                        f"hunk {hunk_no} body line {body_idx}: ran past file end "
                        f"on context {content!r}"
                    )
                if src_lines[cursor] != content:
                    return False, original, (
                        f"hunk {hunk_no} body line {body_idx}: context mismatch at "
                        f"src line {cursor + 1}\n"
                        f"  expected: {content!r}\n"
                        f"  actual:   {src_lines[cursor]!r}"
                    )
                out_lines.append(content)
                cursor += 1
            elif tag == "-":  # deletion
                if cursor >= len(src_lines):
                    return False, original, (
                        f"hunk {hunk_no} body line {body_idx}: deletion past file end "
                        f"({content!r})"
                    )
                if src_lines[cursor] != content:
                    return False, original, (
                        f"hunk {hunk_no} body line {body_idx}: deletion mismatch at "
                        f"src line {cursor + 1}\n"
                        f"  expected: {content!r}\n"
                        f"  actual:   {src_lines[cursor]!r}"
                    )
                cursor += 1
            elif tag == "+":  # insertion
                out_lines.append(content)
            elif tag == "\\":
                # "\ No newline at end of file" — diff's trailing-newline marker.
                continue
            else:
                # Sometimes patches have a stray empty-prefix line meant as " "
                # (whitespace-only context). Be lenient.
                if cursor < len(src_lines) and src_lines[cursor] == raw:
                    out_lines.append(raw)
                    cursor += 1
                else:
                    return False, original, (
                        f"hunk {hunk_no} body line {body_idx}: unknown tag {tag!r} "
                        f"in line {raw!r}"
                    )

    # Tail of the file beyond the last hunk
    if cursor < len(src_lines):
        out_lines.extend(src_lines[cursor:])

    # Preserve trailing newline behaviour of the original.
    new_text = "\n".join(out_lines)
    if original.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    return True, new_text, f"applied {hunk_no} hunk(s) in pure Python"


def _apply_diff(skill_path: str, diff: str) -> tuple[bool, str]:
    """Apply ``diff`` to ``skill_path``.

    Strategy:
      1. If the input doesn't look like a unified diff (no ``---`` /
         ``@@`` markers), treat it as a whole-file replacement.
      2. Otherwise apply the diff in-process (``_apply_unified_diff``).
         No external ``patch`` binary needed, so this works on bare
         Windows / Docker without git.
    """
    diff_stripped = diff.lstrip()
    if not (diff_stripped.startswith("---") or diff_stripped.startswith("diff ") or "@@" in diff_stripped):
        try:
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(diff)
            return True, "applied as whole-file replacement"
        except Exception as exc:
            return False, f"whole-file write failed: {exc}"

    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            original = f.read()
    except Exception as exc:
        return False, f"read failed: {exc}"

    ok, new_text, msg = _apply_unified_diff(original, diff)
    if not ok:
        return False, msg
    try:
        # Atomic write — keep an .tmp until the rename succeeds so a crash
        # during write doesn't leave a half-patched SOP behind.
        tmp = skill_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, skill_path)
    except Exception as exc:
        return False, f"write failed: {exc}"
    return True, msg


def preview_patch(proposal_id: str) -> dict[str, Any]:
    """Dry-run an ``accept`` — return what the file would look like after
    the diff applies, without actually writing. Used by GUI to show users
    the post-patch file before they click "采纳"."""
    rows = _read_all()
    prop = next((r for r in rows if r.get("id") == proposal_id), None)
    if prop is None:
        return {"ok": False, "error": "proposal_not_found"}
    skill_path = prop.get("skill_path") or _resolve_skill_path(prop.get("skill_id", ""))
    if not skill_path or not os.path.isfile(skill_path):
        return {"ok": False, "error": "skill_file_missing", "skill_path": skill_path}
    diff = prop.get("diff", "")
    diff_stripped = diff.lstrip()
    if not (diff_stripped.startswith("---") or diff_stripped.startswith("diff ") or "@@" in diff_stripped):
        return {
            "ok": True,
            "mode": "whole-file",
            "before": _read_text(skill_path),
            "after": diff,
            "message": "whole-file replacement",
        }
    original = _read_text(skill_path)
    ok, new_text, msg = _apply_unified_diff(original, diff)
    return {
        "ok": ok,
        "mode": "unified-diff",
        "before": original,
        "after": new_text if ok else original,
        "message": msg,
    }


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def accept(proposal_id: str) -> tuple[bool, str]:
    """Apply the proposal's diff to the skill file. On success, update the
    proposal's status to ``accepted`` and persist the backup path.

    Refuses to re-decide a proposal that's already in a terminal state."""
    with _lock:
        rows = _read_all()
        prop = next((r for r in rows if r.get("id") == proposal_id), None)
        if prop is None:
            return False, f"proposal {proposal_id!r} not found"
        if prop.get("status") != "open":
            return False, f"proposal already {prop.get('status')}"
        skill_path = prop.get("skill_path") or _resolve_skill_path(prop.get("skill_id", ""))
        if not skill_path or not os.path.isfile(skill_path):
            prop["status"] = "rejected"
            prop["decided_at"] = _now_iso()
            prop["decision_note"] = f"skill file missing: {skill_path}"
            _write_all(rows)
            return False, prop["decision_note"]
        backup = _backup_skill(skill_path)
        ok, msg = _apply_diff(skill_path, prop["diff"])
        if not ok:
            # Restore from backup defensively.
            try:
                shutil.copy2(backup, skill_path)
            except Exception:
                pass
            prop["status"] = "rejected"
            prop["decided_at"] = _now_iso()
            prop["decision_note"] = f"diff failed: {msg}"
            _write_all(rows)
            return False, prop["decision_note"]
        prop["status"] = "accepted"
        prop["decided_at"] = _now_iso()
        prop["decision_note"] = msg
        prop["backup_path"] = backup
        _write_all(rows)
    return True, msg


def reject(proposal_id: str, *, note: str = "") -> tuple[bool, str]:
    with _lock:
        rows = _read_all()
        prop = next((r for r in rows if r.get("id") == proposal_id), None)
        if prop is None:
            return False, f"proposal {proposal_id!r} not found"
        if prop.get("status") != "open":
            return False, f"proposal already {prop.get('status')}"
        prop["status"] = "rejected"
        prop["decided_at"] = _now_iso()
        prop["decision_note"] = note or "rejected by user"
        _write_all(rows)
    return True, "rejected"
