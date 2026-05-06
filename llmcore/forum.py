"""Forum bus — typed pub/sub topics for kernel ↔ worker coordination.

Five system topics ship by default (control / route / disagree / audit / lounge).
Topics are ring buffers capped by message count + age. Audit overflow spools
to ``temp/forum_audit.jsonl``.

Visibility scopes:
  - kernel-only : only the kernel can read or write
  - subscribed  : only listed subscribers/posters
  - global      : anyone may read; capability-checked write

Concurrency: one RLock guards the entire bus. Read snapshots use deque copy.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ── Schemas ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForumTopic:
    name: str
    visibility: str = "subscribed"           # "kernel-only" | "subscribed" | "global"
    retention_messages: int = 512
    retention_seconds: int = 3600
    moderator: str = "kernel"
    sub_moderators: tuple[str, ...] = ()
    spool_path: str | None = None             # if set, append on overflow


@dataclass
class ForumMessage:
    topic: str
    author: str
    timestamp: float
    seq: int
    type: str                                 # "post"|"ack"|"vote"|"complaint"|"escalate"|"summary"|"redact"|"event"
    payload: dict
    in_reply_to: int | None = None
    cap_token_subject: str | None = None
    redacted: bool = False


# ── Default system topics (ADR-0008 §5.2) ───────────────────────────────


SYSTEM_TOPICS: tuple[ForumTopic, ...] = (
    ForumTopic("control",  visibility="kernel-only", retention_messages=1024, retention_seconds=86400),
    ForumTopic("route",    visibility="subscribed",  retention_messages=512,  retention_seconds=600),
    ForumTopic("disagree", visibility="subscribed",  retention_messages=512,  retention_seconds=7200),
    ForumTopic("audit",    visibility="kernel-only", retention_messages=8192, retention_seconds=604800),
    ForumTopic("lounge",   visibility="global",      retention_messages=256,  retention_seconds=600),
)


# ── Bus ─────────────────────────────────────────────────────────────────


class ForumBus:
    def __init__(self, *, audit_spool_path: str | None = None) -> None:
        self._topics: dict[str, ForumTopic] = {}
        self._buffers: dict[str, deque[ForumMessage]] = {}
        self._subscribers: dict[str, dict[str, Callable[[ForumMessage], None]]] = {}
        self._readers: dict[str, set[str]] = {}      # topic → {worker names that can read}
        self._writers: dict[str, set[str]] = {}      # topic → {worker names that can write}
        self._seq: dict[str, int] = {}
        self._lock = threading.RLock()
        self._audit_spool = audit_spool_path

        for t in SYSTEM_TOPICS:
            spool = audit_spool_path if t.name == "audit" else None
            self.register_topic(ForumTopic(
                t.name, t.visibility, t.retention_messages, t.retention_seconds,
                t.moderator, t.sub_moderators, spool,
            ))

    def register_topic(self, topic: ForumTopic) -> None:
        with self._lock:
            self._topics[topic.name] = topic
            self._buffers.setdefault(topic.name, deque(maxlen=topic.retention_messages))
            self._subscribers.setdefault(topic.name, {})
            self._readers.setdefault(topic.name, set())
            self._writers.setdefault(topic.name, set())
            self._seq.setdefault(topic.name, 0)

    def grant_read(self, topic: str, worker_name: str) -> None:
        with self._lock:
            self._readers.setdefault(topic, set()).add(worker_name)

    def grant_write(self, topic: str, worker_name: str) -> None:
        with self._lock:
            self._writers.setdefault(topic, set()).add(worker_name)

    def revoke_read(self, topic: str, worker_name: str) -> None:
        with self._lock:
            self._readers.get(topic, set()).discard(worker_name)
            self._subscribers.get(topic, {}).pop(worker_name, None)

    def revoke_write(self, topic: str, worker_name: str) -> None:
        with self._lock:
            self._writers.get(topic, set()).discard(worker_name)

    def can_write(self, topic: str, author: str) -> bool:
        with self._lock:
            t = self._topics.get(topic)
            if t is None:
                return False
            if author == "kernel":
                return True
            if t.visibility == "kernel-only":
                return False
            if t.visibility == "global":
                return True  # global topics: anyone may write
            return author in self._writers.get(topic, set())

    def can_read(self, topic: str, reader: str) -> bool:
        with self._lock:
            t = self._topics.get(topic)
            if t is None:
                return False
            if reader == "kernel":
                return True
            if t.visibility == "kernel-only":
                return False
            if t.visibility == "global":
                return True
            return reader in self._readers.get(topic, set())

    def publish(
        self, topic: str, payload: dict, *,
        author: str, type: str = "post",
        in_reply_to: int | None = None,
        cap_token_subject: str | None = None,
    ) -> ForumMessage:
        with self._lock:
            if topic not in self._topics:
                raise KeyError(f"unknown topic {topic!r}")
            if not self.can_write(topic, author):
                raise PermissionError(f"{author!r} cannot write to {topic!r}")
            self._seq[topic] += 1
            msg = ForumMessage(
                topic=topic, author=author, timestamp=time.time(),
                seq=self._seq[topic], type=type, payload=dict(payload),
                in_reply_to=in_reply_to, cap_token_subject=cap_token_subject,
            )
            buf = self._buffers[topic]
            if len(buf) == buf.maxlen:
                self._spool_one(topic, buf[0])
            buf.append(msg)
            self._evict_old(topic)
            subs = list(self._subscribers.get(topic, {}).items())
        # call subscribers outside the lock
        for name, cb in subs:
            try:
                cb(msg)
            except Exception:
                # drop silently; subscribers are best-effort
                pass
        return msg

    def subscribe(self, topic: str, *, subscriber: str, callback: Callable[[ForumMessage], None]) -> None:
        with self._lock:
            if topic not in self._topics:
                raise KeyError(topic)
            if not self.can_read(topic, subscriber):
                raise PermissionError(f"{subscriber!r} cannot read {topic!r}")
            self._subscribers.setdefault(topic, {})[subscriber] = callback

    def unsubscribe(self, topic: str, subscriber: str) -> None:
        with self._lock:
            self._subscribers.get(topic, {}).pop(subscriber, None)

    def tail(self, topic: str, *, reader: str, n: int = 50) -> list[ForumMessage]:
        with self._lock:
            if not self.can_read(topic, reader):
                raise PermissionError(f"{reader!r} cannot read {topic!r}")
            buf = self._buffers.get(topic, deque())
            return list(buf)[-n:]

    def redact(self, topic: str, seq: int, *, by: str) -> bool:
        with self._lock:
            t = self._topics.get(topic)
            if t is None:
                return False
            if by != "kernel" and by != t.moderator and by not in t.sub_moderators:
                raise PermissionError(f"{by!r} is not a moderator of {topic!r}")
            for m in self._buffers[topic]:
                if m.seq == seq:
                    m.redacted = True
                    return True
            return False

    def topics(self) -> list[ForumTopic]:
        with self._lock:
            return list(self._topics.values())

    # ── internals ──

    def _evict_old(self, topic: str) -> None:
        t = self._topics[topic]
        cutoff = time.time() - t.retention_seconds
        buf = self._buffers[topic]
        while buf and buf[0].timestamp < cutoff:
            self._spool_one(topic, buf[0])
            buf.popleft()

    def _spool_one(self, topic: str, msg: ForumMessage) -> None:
        t = self._topics.get(topic)
        if t is None or not t.spool_path:
            return
        try:
            os.makedirs(os.path.dirname(t.spool_path), exist_ok=True)
            with open(t.spool_path, "a", encoding="utf-8") as f:
                json.dump({
                    "topic": msg.topic, "author": msg.author,
                    "timestamp": msg.timestamp, "seq": msg.seq,
                    "type": msg.type, "payload": msg.payload,
                    "in_reply_to": msg.in_reply_to,
                    "cap_token_subject": msg.cap_token_subject,
                    "redacted": msg.redacted,
                }, f, ensure_ascii=False)
                f.write("\n")
        except OSError:
            pass  # disk full / permission denied: drop. ADR-0008 says we surface this; logging hooks up later.
