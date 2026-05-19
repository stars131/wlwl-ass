"""kb_worker — owner-curated friend-facing knowledge base for the concierge.

Capability offered:
  - concierge.kb_answer.v1

Storage: ``temp/concierge_kb.jsonl`` — one JSON object per line:

    {"topic": "employer",
     "summary": "她现在在 ABC 公司做 ML，2025 年开始的。",
     "long":    "Alice 自 2025-08 起在 ABC ...",
     "visibility": "friends",
     "updated_at": "2026-05-12T10:00:00+08:00"}

Lookup: BM25-lite over ``topic + summary + long``. We deliberately don't
pull a vector DB into the dependency graph for a KB that's typically <100
rows; BM25 with a unicode-friendly tokenizer is more than enough.

Topic-allowlist gating: callers pass ``topics_allowed=[...]`` (sourced
from ``bots.feishu_concierge.topics_allowed``). A row with a matching
``topic`` that is NOT in the allowlist still surfaces as ``matched=true``
but with ``in_allowlist=false`` — so the agent can decide to escalate
("she didn't tell me to share this") instead of pretending the row
doesn't exist.

Why BM25 instead of substring search: a friend who asks "她现在在哪上班"
should find the ``employer`` row even though "上班" is in neither the
topic nor the summary; matching against the ``long`` field with proper
term weighting handles those near-miss queries. Substring would miss
them silently.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from typing import Any

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)

CAPS = ("concierge.kb_answer.v1",)

VISIBILITY_LEVELS = ("friends", "close_friends", "family")
DEFAULT_VISIBILITY = "friends"

# Unicode-aware token splitter: keeps Chinese characters as single tokens
# while splitting ASCII on word boundaries. ``\w`` in Python regex is
# unicode-aware by default, so 中文 is included in `\w+`.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CJK_RE = re.compile(r"[一-鿿]")


def _tokenize(text: str) -> list[str]:
    """Tokenize for BM25. Latin words stay as-is; CJK gets per-char unigrams
    *and* bigrams so short queries like "上班" still match documents
    containing "公司上班"."""
    if not text:
        return []
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        word = m.group(0)
        if _CJK_RE.search(word):
            chars = list(word)
            tokens.extend(chars)
            tokens.extend(a + b for a, b in zip(chars, chars[1:]))
        else:
            tokens.append(word)
    return tokens


class KBStorage:
    """File-backed JSONL KB with hot-path BM25 cache.

    On every ``load()`` we re-read the JSONL and re-compute df + lengths.
    KB size is small (<1000 rows in any realistic deployment) so this
    is cheap.
    """

    def __init__(self, kb_path: str) -> None:
        self._path = kb_path
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._rows: list[dict] = []
        self._tokens: list[list[str]] = []
        self._doc_len: list[int] = []
        self._avgdl: float = 0.0
        self._df: dict[str, int] = {}
        self._mtime: float | None = None
        self.load(force=True)

    def load(self, *, force: bool = False) -> None:
        with self._lock:
            try:
                cur_mtime = os.path.getmtime(self._path)
            except OSError:
                cur_mtime = None
            if not force and cur_mtime == self._mtime:
                return
            rows: list[dict] = []
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and obj.get("topic"):
                            rows.append(obj)
            except FileNotFoundError:
                pass
            self._rows = rows
            self._tokens = [
                _tokenize(" ".join([
                    str(r.get("topic", "")),
                    str(r.get("summary", "")),
                    str(r.get("long", "")),
                ]))
                for r in rows
            ]
            self._doc_len = [len(t) for t in self._tokens]
            self._avgdl = (sum(self._doc_len) / len(self._doc_len)) if self._doc_len else 0.0
            df: dict[str, int] = {}
            for tokens in self._tokens:
                for tok in set(tokens):
                    df[tok] = df.get(tok, 0) + 1
            self._df = df
            self._mtime = cur_mtime

    def search(self, q: str, *, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        """Return list of (row_index, score) ranked desc. Empty if KB empty."""
        self.load()  # cheap mtime check; reloads on file change
        if not self._rows:
            return []
        q_tokens = _tokenize(q)
        if not q_tokens:
            return []
        n = len(self._rows)
        scores: list[float] = [0.0] * n
        for tok in q_tokens:
            df = self._df.get(tok, 0)
            if df == 0:
                continue
            # BM25 idf with the standard +0.5 smoothing; clip to non-negative
            # so very-common tokens don't pull scores below zero.
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for i, doc_tokens in enumerate(self._tokens):
                tf = doc_tokens.count(tok)
                if tf == 0:
                    continue
                denom = tf + k1 * (1 - b + b * (self._doc_len[i] / (self._avgdl or 1)))
                scores[i] += idf * (tf * (k1 + 1)) / denom
        ranked = sorted(
            (i for i in range(n) if scores[i] > 0),
            key=lambda i: scores[i], reverse=True,
        )
        return [(i, scores[i]) for i in ranked]

    def get_row(self, idx: int) -> dict:
        with self._lock:
            return dict(self._rows[idx])

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]

    def upsert(self, row: dict) -> dict:
        """Append a new row (or replace one with the same topic). Returns the
        stored row. Used by cli_kb; not exposed through the worker."""
        with self._lock:
            topic = str(row.get("topic", "")).strip()
            if not topic:
                raise ValueError("topic is required")
            existing = [r for r in self._rows if r.get("topic") != topic]
            row = dict(row)
            row.setdefault("visibility", DEFAULT_VISIBILITY)
            row.setdefault("updated_at", _now_iso())
            existing.append(row)
            self._write_all(existing)
            self.load(force=True)
            return row

    def delete(self, topic: str) -> bool:
        with self._lock:
            kept = [r for r in self._rows if r.get("topic") != topic]
            if len(kept) == len(self._rows):
                return False
            self._write_all(kept)
            self.load(force=True)
            return True

    def _write_all(self, rows: list[dict]) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self._path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()) or time.strftime("%Y-%m-%dT%H:%M:%S")


class KBWorker:
    def __init__(self, *, name: str, storage: KBStorage, score_floor: float = 0.5) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="concierge_kb", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Owner-curated friend-facing KB with topic-allowlist gating.",
        )
        self._storage = storage
        self._score_floor = score_floor
        self._in_flight = 0
        self._last_error: str | None = None

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready", in_flight=self._in_flight, last_error=self._last_error)

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        self._in_flight += 1
        try:
            if req.capability != "concierge.kb_answer.v1":
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.NOT_IMPLEMENTED, req.capability))
            p = req.payload
            q = str(p.get("q", "") or "").strip()
            if not q:
                return InvokeResponse(call_id=req.call_id, ok=True, result={
                    "matched": False, "in_allowlist": False,
                })
            allowlist = p.get("topics_allowed")
            if allowlist is not None and not isinstance(allowlist, (list, tuple, set)):
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.PAYLOAD_INVALID, "topics_allowed must be a list"))
            allow_set = set(allowlist) if allowlist is not None else None
            ranked = self._storage.search(q)
            if not ranked or ranked[0][1] < self._score_floor:
                return InvokeResponse(call_id=req.call_id, ok=True, result={
                    "matched": False, "in_allowlist": False,
                })
            top_idx, top_score = ranked[0]
            row = self._storage.get_row(top_idx)
            topic = str(row.get("topic", ""))
            in_allow = (allow_set is None) or (topic in allow_set)
            text = str(row.get("summary", "") or row.get("long", ""))
            return InvokeResponse(call_id=req.call_id, ok=True, result={
                "matched": True,
                "topic": topic,
                "text": text,
                "score": float(top_score),
                "in_allowlist": bool(in_allow),
            })
        except Exception as exc:
            self._last_error = repr(exc)
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.INTERNAL, str(exc)))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        return


class KBFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.concierge_kb",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "concierge_kb"
        kb_path = config.get("kb_path") or os.path.join(
            _default_temp(), "concierge_kb.jsonl"
        )
        score_floor = float(config.get("score_floor", 0.5))
        storage = KBStorage(kb_path)
        return KBWorker(name=name, storage=storage, score_floor=score_floor)


def _default_temp() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "temp")
