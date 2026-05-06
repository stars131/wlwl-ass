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
    yield CalendarFactory()
    yield InspirationFactory()
    yield MockSTTFactory()
    yield MockTTSFactory()
    # llm_worker is opt-in (needs an existing BaseSession); see workers/llm_worker.py
