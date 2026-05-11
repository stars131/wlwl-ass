"""Automatic checkpoint hook for GeneraticAgent (#K).

Registers a ``turn_end`` hook on a GeneraticAgent so its working state is
periodically snapshotted to ``temp/checkpoints/<task_id>/`` via the
``CheckpointManager`` (#20). Goal: a context-flush / crash / kill mid-task
no longer wipes the agent's progress.

Wiring point: ``wlwl_ass.WlwlAssHandler.turn_end_callback`` already
iterates ``self.parent._turn_end_hooks``. We add one entry there.

State shape (small on purpose — checkpoints aren't training data):
    {
        "turn": int,
        "last_summary": str,
        "key_info": str,
        "related_sop": str,
        "history_tail": [str, ...],   # last 40 lines
        "exit_reason": dict | None,
    }

What we *don't* save:
    - Full LLM history (would be MB-scale)
    - File contents (re-read from disk on resume)
    - Tool call args / responses (the activity_log already has those)

The principle: a checkpoint should be just enough to get the agent
oriented after restart. Anything bulky comes back from the world.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Any

_DEFAULT_INTERVAL = 5
_HISTORY_TAIL = 40
_AUTO_TASK_ENV = "WLWL_AUTO_CHECKPOINT_TASK_ID"


def _safe_slug(s: str) -> str:
    """Clamp a free-text identifier to a filesystem-safe slug."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or ""))
    return out[:60].strip("_") or "task"


def _generate_task_id(project_id: str | None = None) -> str:
    """Build a stable per-agent-instance task id.

    Honors ``WLWL_AUTO_CHECKPOINT_TASK_ID`` if the launcher set one (so the
    user can wire ``--resume`` later), otherwise builds from process id +
    start timestamp + a short random tail to keep tests reproducible-ish.
    """
    explicit = os.environ.get(_AUTO_TASK_ENV, "").strip()
    if explicit:
        return _safe_slug(explicit)
    project_id = _safe_slug(project_id or os.environ.get("WLWL_PROJECT_ID", "") or "")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(2)
    if project_id:
        return f"auto-{project_id}-{stamp}-{suffix}"
    return f"auto-{stamp}-{suffix}"


def install_auto_checkpoint(
    agent: Any,
    *,
    every_n_turns: int = _DEFAULT_INTERVAL,
    task_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Attach an auto-save hook to ``agent``.

    Returns the resolved ``task_id`` so callers (tests; future ``--resume``
    flag) can fetch the checkpoint by name. Idempotent — calling twice on
    the same agent only registers one hook (we key by hook name).
    """
    resolved = _safe_slug(task_id) if task_id else _generate_task_id(project_id)
    state_lock = threading.Lock()

    if not hasattr(agent, "_turn_end_hooks") or agent._turn_end_hooks is None:
        agent._turn_end_hooks = {}

    if "auto_checkpoint" in agent._turn_end_hooks:
        # Already installed; don't double up. But keep the prior task_id —
        # restarting the hook with a new id mid-run would orphan saves.
        return getattr(agent, "_auto_checkpoint_task_id", resolved)

    agent._auto_checkpoint_task_id = resolved
    agent._auto_checkpoint_last_save_at = 0.0

    def _hook(local_vars: dict[str, Any]) -> None:
        # local_vars is the whole turn_end_callback frame — read-only by
        # contract. We pull the bits we care about.
        turn = local_vars.get("turn")
        if not isinstance(turn, int) or turn <= 0:
            return
        exit_reason = local_vars.get("exit_reason")
        # Save criteria: every Nth turn OR on exit (any exit reason).
        if (turn % every_n_turns != 0) and not exit_reason:
            return
        # Throttle: never more than once per 2 seconds — guards against
        # pathological loops where turn counts increment unusually fast.
        with state_lock:
            now = time.monotonic()
            if now - agent._auto_checkpoint_last_save_at < 2.0 and not exit_reason:
                return
            agent._auto_checkpoint_last_save_at = now

        try:
            handler = local_vars.get("self") or getattr(agent, "handler", None)
            history_info = list(getattr(handler, "history_info", [])[-_HISTORY_TAIL:])
            working = dict(getattr(handler, "working", {}) or {})
        except Exception:
            history_info, working = [], {}

        state = {
            "turn": turn,
            "last_summary": str(local_vars.get("next_prompt", ""))[:500],
            "key_info": str(working.get("key_info", "") or "")[:2000],
            "related_sop": str(working.get("related_sop", "") or "")[:200],
            "history_tail": history_info,
            "exit_reason": exit_reason if isinstance(exit_reason, dict) else None,
        }
        try:
            from launcher.checkpoint_manager import get_manager

            note = (
                f"auto @ turn {turn}"
                + (f" [{exit_reason.get('result', 'exit')}]" if exit_reason else "")
            )
            get_manager().save(resolved, state, note=note)
        except Exception:
            # Never propagate — auto-checkpoint failures must not abort
            # the turn loop. The user has logs to debug if persistence
            # is broken.
            pass

    agent._turn_end_hooks["auto_checkpoint"] = _hook
    return resolved


def load_resume_state(task_id: str) -> dict[str, Any] | None:
    """Return the most-recent saved state for ``task_id``, or None.

    Caller (typically agentmain on startup if ``WLWL_AUTO_CHECKPOINT_TASK_ID``
    is set) is responsible for actually applying the state — we don't
    decide policy on what to restore.
    """
    try:
        from launcher.checkpoint_manager import get_manager

        cp = get_manager().load(task_id)
    except Exception:
        return None
    if cp is None:
        return None
    state = cp.get("state")
    return state if isinstance(state, dict) else None


def maybe_apply_resume(agent: Any, *, task_id: str | None = None) -> dict[str, Any] | None:
    """If the env points at a resumable task and a checkpoint exists,
    restore working memory + a marker into history. Otherwise no-op.

    Returns the loaded state (for tests / logging) or None.
    """
    task_id = (task_id or os.environ.get(_AUTO_TASK_ENV, "")).strip()
    if not task_id:
        return None
    state = load_resume_state(_safe_slug(task_id))
    if state is None:
        return None
    # Best-effort restore — agent.handler may not exist yet at __init__ time;
    # we attach to the agent object itself and let the next turn_end_callback
    # propagate. This keeps coupling to wlwl_ass.WlwlAssHandler internals
    # minimal.
    try:
        agent._pending_resume_state = state
    except Exception:
        pass
    return state
