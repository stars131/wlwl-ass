"""audit_worker — append-only JSONL audit log for the concierge bot.

Capability offered:
  - concierge.audit.v1

Storage: one JSONL row per accepted invocation. PII redaction:

  * friend_open_id is replaced with a SHA-256 hash (first 12 hex chars).
    The mapping ``hash -> open_id`` is cached in ``.id_map.json`` next to
    the audit log so the owner can de-anonymize for debugging without the
    audit log itself leaking identifiers.
  * Phone numbers (``\\b1\\d{10}\\b`` style — Chinese mobile) and email
    addresses inside ``in_text`` / ``out_text`` are replaced with
    ``[redacted-phone]`` / ``[redacted-email]`` before append.

Why a worker (not just an open file): putting audit through the kernel
gives us schema validation, capability tokens, latency/cost metrics, and
circuit-breaker protection if the disk fills up — all free of charge.

Design note: this worker is the **only** place that touches the audit
file. ConciergeAgent calls ``concierge.audit.v1`` for every action; the
file format is otherwise an internal contract.
"""
from __future__ import annotations

import hashlib
import json
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

CAPS = ("concierge.audit.v1",)

_PHONE_RE = re.compile(r"\b1\d{10}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def _hash_open_id(open_id: str) -> str:
    if not open_id:
        return ""
    return hashlib.sha256(open_id.encode("utf-8")).hexdigest()[:12]


def _redact(text: str) -> str:
    if not text:
        return text
    text = _PHONE_RE.sub("[redacted-phone]", text)
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return text


class AuditStorage:
    """File-backed JSONL appender + identifier-hash side table."""

    def __init__(self, audit_path: str, id_map_path: str | None = None) -> None:
        self._path = audit_path
        self._id_map_path = id_map_path or os.path.join(
            os.path.dirname(audit_path), ".id_map.json"
        )
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._id_map: dict[str, str] = self._load_id_map()

    def _load_id_map(self) -> dict[str, str]:
        try:
            with open(self._id_map_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_id_map(self) -> None:
        tmp = self._id_map_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._id_map, f, ensure_ascii=False)
            os.replace(tmp, self._id_map_path)
        except OSError:
            pass

    def append(self, row: dict) -> dict:
        with self._lock:
            # Strip the side-table key before writing to disk — _raw_open_id
            # must NEVER land in the JSONL audit log.
            raw_open_id = row.pop("_raw_open_id", "")
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if raw_open_id and row.get("friend_open_id"):
                self._id_map[row["friend_open_id"]] = raw_open_id
                self._save_id_map()
        return row

    def reverse_lookup(self, hashed: str) -> str | None:
        return self._id_map.get(hashed)

    def read_all(self) -> list[dict]:
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return [json.loads(line) for line in f if line.strip()]
            except FileNotFoundError:
                return []


class AuditWorker:
    def __init__(self, *, name: str, storage: AuditStorage) -> None:
        self._meta = WorkerMetadata(
            name=name, kind="audit", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Append-only audit log for concierge bot, with PII redaction.",
        )
        self._storage = storage
        self._in_flight = 0
        self._last_error: str | None = None
        self._written_count = 0

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready", in_flight=self._in_flight, last_error=self._last_error)

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        self._in_flight += 1
        try:
            if req.capability != "concierge.audit.v1":
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.NOT_IMPLEMENTED, req.capability))
            p = req.payload
            row = {
                "t": time.time(),
                "kind": str(p.get("kind", "unknown")),
                "friend_open_id": _hash_open_id(str(p.get("friend_open_id", ""))),
                "_raw_open_id": str(p.get("friend_open_id", "")),
                "intent": p.get("intent"),
                "state_before": p.get("state_before"),
                "state_after": p.get("state_after"),
                "in_text": _redact(str(p.get("in_text", "") or "")) or None,
                "out_text": _redact(str(p.get("out_text", "") or "")) or None,
                "capabilities_used": list(p.get("capabilities_used") or []),
                "latency_ms": float(p.get("latency_ms") or 0.0),
                "extras": p.get("extras") or {},
            }
            # Drop None-valued keys to keep rows tight; keep _raw_open_id for
            # the id_map side table but never write it to JSONL.
            row_to_write = {k: v for k, v in row.items()
                            if v is not None and k != "_raw_open_id"}
            written = dict(row_to_write)
            written["_raw_open_id"] = row["_raw_open_id"]  # consumed by storage
            self._storage.append(written)
            self._written_count += 1
            return InvokeResponse(
                call_id=req.call_id, ok=True,
                result={"written": True, "row": row_to_write},
            )
        except Exception as exc:
            self._last_error = repr(exc)
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.INTERNAL, str(exc)))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        return


class AuditFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.concierge_audit",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "concierge_audit"
        audit_path = config.get("audit_path") or os.path.join(
            _default_temp(), "concierge_audit.jsonl"
        )
        id_map_path = config.get("id_map_path")  # optional override for tests
        storage = AuditStorage(audit_path, id_map_path=id_map_path)
        return AuditWorker(name=name, storage=storage)


def _default_temp() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "temp")
