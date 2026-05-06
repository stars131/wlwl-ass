"""Bridge between the unified gateway and the actual GeneraticAgent loop.

The gateway's default handler is a stub echo. This module gives a real
agent handler that:

  1. Lazily creates one ``GeneraticAgent`` per ``conversation_key`` (so
     each user has their own working memory + history).
  2. Puts each inbound text onto that agent's task queue and drains the
     resulting ``display_queue`` until ``'done'`` arrives.
  3. Returns the polished reply text as the handler result, which the
     gateway's worker then dispatches back through the originating
     PlatformAdapter.send().
  4. Caps the per-agent idle time — agents idle for more than
     ``idle_timeout_s`` get reaped to free memory.

Why per-conversation agents (rather than one shared): the agent's history
+ working memory + task_dir are user-specific. A shared agent would mix
contexts across users on different IM platforms — security and quality
disaster. The cost is one Python thread + a few MB per active conversation.

This bridge is intentionally optional — ``frontends.gateway.run_gateway``
keeps working with the echo handler if you don't wire this in. To enable:

    from frontends.gateway import Gateway
    from frontends.gateway_handler import AgentBridge

    gw = Gateway(handler=AgentBridge().handle)
    ...
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from frontends.gateway import InboundMessage


@dataclass
class _AgentSlot:
    agent: Any  # GeneraticAgent — typed as Any so this module imports cheaply
    thread: threading.Thread
    last_used_ts: float = field(default_factory=time.time)


class AgentBridge:
    """Maps ``conversation_key → GeneraticAgent`` and drains replies."""

    def __init__(
        self,
        *,
        idle_timeout_s: float = 3600.0,  # 1 h
        reply_timeout_s: float = 600.0,  # 10 min cap on a single turn
        cleanup_interval_s: float = 300.0,
    ):
        self.idle_timeout_s = idle_timeout_s
        self.reply_timeout_s = reply_timeout_s
        self.cleanup_interval_s = cleanup_interval_s
        self._slots: dict[str, _AgentSlot] = {}
        self._lock = threading.Lock()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    # ── public ───────────────────────────────────────────────────────

    def handle(self, msg: InboundMessage) -> str:
        """Gateway-compatible handler. Returns the agent's polished reply
        text (or an error string starting with ``[error]``)."""
        slot = self._get_or_create_slot(msg.conversation_key())
        slot.last_used_ts = time.time()
        try:
            display_q = slot.agent.put_task(msg.text, source=f"gateway:{msg.platform}")
        except Exception as exc:
            return f"[error] put_task failed: {type(exc).__name__}: {exc}"
        return self._drain_reply(display_q, deadline=time.time() + self.reply_timeout_s)

    def shutdown(self) -> None:
        with self._lock:
            for slot in list(self._slots.values()):
                self._kill_slot(slot)
            self._slots.clear()

    def active_conversations(self) -> int:
        with self._lock:
            return len(self._slots)

    # ── internals ────────────────────────────────────────────────────

    def _get_or_create_slot(self, key: str) -> _AgentSlot:
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None and slot.thread.is_alive():
                return slot
            # Lazy import — agentmain pulls in llmcore which is heavy.
            from agentmain import GeneraticAgent

            agent = GeneraticAgent()
            t = threading.Thread(target=agent.run, name=f"ga-bridge-{key}", daemon=True)
            t.start()
            slot = _AgentSlot(agent=agent, thread=t)
            self._slots[key] = slot
            return slot

    def _drain_reply(self, display_q: "queue.Queue[dict[str, Any]]", *, deadline: float) -> str:
        """Pull from the agent's display_queue until we see a ``done`` entry.

        Returns the full ``done`` payload. Times out at ``deadline`` and
        returns whatever partial text we've accumulated, prefixed with a
        warning so callers can spot it."""
        partial = ""
        while time.time() < deadline:
            try:
                item = display_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if not isinstance(item, dict):
                continue
            if "done" in item:
                return str(item["done"] or partial or "(empty reply)")
            if "next" in item:
                partial = str(item["next"])
        return f"[timeout after {self.reply_timeout_s:.0f}s]\n\n{partial}"

    def _kill_slot(self, slot: _AgentSlot) -> None:
        try:
            # GeneraticAgent.abort signals the current task; the thread
            # itself will keep waiting on task_queue.get() but as a daemon
            # it dies with the process. We at least zero our reference so
            # the next get_or_create rebuilds.
            slot.agent.abort()
        except Exception:
            pass

    def _reap_loop(self) -> None:
        while True:
            time.sleep(self.cleanup_interval_s)
            now = time.time()
            with self._lock:
                stale = [k for k, s in self._slots.items()
                         if now - s.last_used_ts > self.idle_timeout_s]
                for k in stale:
                    self._kill_slot(self._slots[k])
                    self._slots.pop(k, None)


# ── module-level singleton for the common case ───────────────────────

_singleton: AgentBridge | None = None
_singleton_lock = threading.Lock()


def get_bridge() -> AgentBridge:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = AgentBridge()
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.shutdown()
        _singleton = None
