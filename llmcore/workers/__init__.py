"""In-tree worker factories. The kernel auto-discovers everything yielded
by ``iter_builtin_factories``.
"""
from __future__ import annotations

from typing import Iterator

from llmcore.worker import WorkerFactory


def iter_builtin_factories() -> Iterator[WorkerFactory]:
    from llmcore.workers.calendar_worker import CalendarFactory
    from llmcore.workers.inspiration_worker import InspirationFactory
    from llmcore.workers.mock_voice_worker import MockSTTFactory, MockTTSFactory
    from llmcore.workers.xiaomi_voice_worker import XiaomiSTTFactory, XiaomiTTSFactory
    from llmcore.workers.local_whisper_worker import LocalWhisperSTTFactory
    from llmcore.workers.kb_worker import KBFactory
    from llmcore.workers.slot_worker import SlotFactory
    from llmcore.workers.escalate_worker import EscalateFactory
    from llmcore.workers.audit_worker import AuditFactory
    yield CalendarFactory()
    yield InspirationFactory()
    yield MockSTTFactory()
    yield MockTTSFactory()
    yield XiaomiSTTFactory()
    yield XiaomiTTSFactory()
    yield LocalWhisperSTTFactory()
    yield KBFactory()
    yield SlotFactory()
    yield EscalateFactory()
    yield AuditFactory()
    # llm_worker is opt-in (needs an existing BaseSession); see workers/llm_worker.py
