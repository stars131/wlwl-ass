"""``curator`` — periodic memory-curation nudges for the agent.

Closes the "self-evolving" loop without giving the agent direct write
access to L1 (``memory/global_mem_insight.txt``). The protocol:

  1. **Curator decides when to nudge** (this module). Every N turns, if
     enough new ``history_info`` has accumulated, the curator computes a
     nudge prompt that asks the agent to reflect: *which shortcuts /
     learnings from the last N turns deserve a permanent index entry?*

  2. **Agent submits proposals**, not patches. The agent calls
     ``tools/curator_propose.curator_propose(...)`` with a one-line
     insight and a target layer (L1 / L2 / user_profile / playbook). Proposals
     accumulate in ``memory/curator_proposals.jsonl`` — append-only,
     auditable, never auto-applied.

  3. **User reviews + applies**. A separate (future) review pass can
     surface top-voted proposals in the GUI. Manual application keeps
     L1 — the project's most precious 22-line index — safe from a
     misaligned auto-patch.

This split honors the "self-evolving" spirit while preserving the
"極簡種子" rule: no automatic writes to canonical memory. The agent
proposes; the user disposes.

Why pure functions:
  Nudge timing has two pieces of state — ``last_nudge_turn`` and the
  cumulative count of history_info entries since then. Both can be
  stored in the existing ``self.working`` dict; this module just gates
  the decision. Easier to test, easier to reason about, easier to
  unwire if it turns out to be the wrong cadence.

How it integrates without touching wlwl_ass.py:
  Register a hook on ``self.parent._turn_end_hooks`` that calls
  ``decide_nudge(...)`` with the current turn / history_info / last
  nudge state, and writes the result to the ``_intervene`` file under
  ``task_dir``. The existing ``turn_end_callback`` already consumes
  that file and prepends it to ``next_prompt`` — zero-line wlwl_ass.py diff.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# Default cadence: every 30 turns the curator gets a chance to nudge.
# Tuned so that:
#   * Short tasks (≤ 30 turns) never trigger curation — there's nothing
#     to extract from a single-shot conversation.
#   * Long autonomous runs get a chance to consolidate every ~5 minutes
#     of agent-time (turns are ~10s each on a normal model).
_DEFAULT_PERIOD = 30
# Minimum new history entries between nudges. Prevents back-to-back
# nudges when ``turn`` jumps non-monotonically (e.g. after a checkpoint
# resume that bumps the turn counter without producing real activity).
_DEFAULT_MIN_NEW_HISTORY = 8


@dataclass
class NudgeDecision:
    """What ``decide_nudge`` decides on a single turn-end."""
    should_nudge: bool
    next_last_nudge_turn: int
    prompt: str = ""

    @property
    def updates_state(self) -> bool:
        """Whether the caller should persist ``next_last_nudge_turn``."""
        return self.should_nudge


def decide_nudge(
    *,
    turn: int,
    last_nudge_turn: int,
    history_info: list[str],
    period: int = _DEFAULT_PERIOD,
    min_new_history: int = _DEFAULT_MIN_NEW_HISTORY,
    lang: str = "zh",
) -> NudgeDecision:
    """Decide whether to surface a curation nudge this turn.

    Pure function — no I/O, no time. State (``last_nudge_turn``) is
    passed in and updated via the returned ``NudgeDecision``. Callers
    persist the new ``last_nudge_turn`` in their own working memory.

    Triggers when ALL of:
      * ``turn`` is a positive multiple of ``period`` (turn 30, 60, …).
      * At least ``min_new_history`` new entries arrived since the last
        nudge (gates against turn-counter inflation).
    """
    if turn <= 0 or period <= 0:
        return NudgeDecision(False, last_nudge_turn)
    if turn % period != 0:
        return NudgeDecision(False, last_nudge_turn)
    if len(history_info) < min_new_history:
        return NudgeDecision(False, last_nudge_turn)
    if turn <= last_nudge_turn:
        return NudgeDecision(False, last_nudge_turn)
    # Build the nudge body. Bilingual; keep the actionable instruction
    # near the top so an agent skimming a long ``next_prompt`` sees it.
    prompt = _render_prompt(turn, history_info, lang=lang)
    return NudgeDecision(should_nudge=True, next_last_nudge_turn=turn, prompt=prompt)


def _render_prompt(turn: int, history_info: list[str], *, lang: str) -> str:
    # Recent entries — last 12 are usually enough context to spot a pattern.
    recent = history_info[-12:]
    sample_block = "\n".join(f"  {i+1}. {line}" for i, line in enumerate(recent))
    if lang == "en":
        return (
            f"\n\n[Curator @ turn {turn}] Reflection prompt — "
            "review the last several turns and decide whether anything "
            "deserves a permanent memory entry.\n"
            "Recent activity:\n"
            f"{sample_block}\n\n"
            "If you spot a *durable shortcut* (a pattern, a SOP gap, a "
            "user preference, a tool quirk worth remembering), call:\n"
            "  curator_propose(insight='one-line insight', target='L1' | 'L2' | 'user_profile' | 'playbook', rationale='why this matters')\n"
            "Use target='playbook' for reusable execution strategies; accepted playbook entries are injected into future prompts.\n"
            "Proposals are stored in memory/curator_proposals.jsonl — they are "
            "NOT auto-applied. The user reviews + applies them out of band.\n"
            "If nothing stands out, ignore this nudge and continue your "
            "current task. Do not propose for the sake of proposing."
        )
    # zh (default)
    return (
        f"\n\n[Curator @ 第 {turn} 轮] 反思提示——回顾最近的工作，判断是否有内容值得写入永久记忆。\n"
        "最近活动：\n"
        f"{sample_block}\n\n"
        "如果你发现了*可复用的捷径*（模式、SOP 缺口、用户偏好、值得记住的工具特性），调用：\n"
        "  curator_propose(insight='一句话洞见', target='L1' | 'L2' | 'user_profile' | 'playbook', rationale='为什么值得记')\n"
        "可复用执行策略用 target='playbook'；只有用户采纳后的 Playbook 条目才会注入未来提示。\n"
        "Proposal 写入 memory/curator_proposals.jsonl —— **不会自动应用**，用户线下审查再生效。\n"
        "如果没有特别可记的，忽略此提示继续当前任务。不要为了 propose 而 propose。"
    )


# ─── Hook helper (optional) ──────────────────────────────────────────────


def write_nudge_via_intervene(task_dir: str, nudge: str) -> bool:
    """Write a nudge into the ``_intervene`` file the existing
    ``turn_end_callback`` already consumes. Returns ``True`` if written.

    This is the integration path that requires zero wlwl_ass.py changes —
    a curator hook calls ``decide_nudge(...)`` and pipes the result here.
    """
    if not nudge or not task_dir:
        return False
    target = os.path.join(task_dir, "_intervene")
    try:
        os.makedirs(task_dir, exist_ok=True)
        # Append rather than overwrite so multiple sources of intervene
        # text (master injection + curator) can coexist on the same turn.
        with open(target, "a", encoding="utf-8") as f:
            f.write(nudge.rstrip() + "\n")
        return True
    except OSError:
        return False
