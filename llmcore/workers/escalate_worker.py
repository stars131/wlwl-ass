"""escalate_worker — concierge → owner approval channel.

Capability offered:
  - concierge.escalate.v1

Two modes, selected at factory build-time by config:

  * **Phase-1 stub** (default, when no owner_app_id/secret given): every
    escalation is appended to ``temp/concierge_escalations.jsonl`` along
    with the rendered Feishu interactive card JSON. The owner can ``cat``
    the file or watch it manually. Useful for tests and for environments
    where the owner Feishu app isn't set up yet.

  * **Phase-2 live** (when ``owner_app_id`` + ``owner_app_secret`` +
    ``owner_open_id`` are all present in the worker config): builds a
    ``lark_oapi.Client`` from the OWNER app's credentials and sends the
    rendered card to the owner's open_id via ``im.v1.message.create``.
    Falls back to the JSONL log on any send failure so the escalation
    is never silently lost.

Privilege boundary: the lark client is built INSIDE this worker, from
config injected at ``kernel.add_worker(...)`` time. The concierge process
does see those credentials in its config_store (single-process Phase 2),
but the agent code never imports lark_oapi or holds the client. Future
out-of-process workers (ADR-0008 transport=subprocess) will close the
gap fully.

The card payload + button schema follow Feishu Card 2.0 spec and match
the existing ``frontends/fsapp.py:_card_raw`` pattern. The button
``value`` carries the escalation_id so the owner-bot's card-action
handler can route the decision back to ``concierge.escalate.resolve.v1``
(Phase 3 / deferred).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

from llmcore.worker import (
    API_VERSION, ErrorCodes, FactoryDescription, HealthReport,
    InvokeRequest, InvokeResponse, KernelHandle, Worker,
    WorkerFactory, WorkerMetadata, make_error,
)

CAPS = ("concierge.escalate.v1",)

VALID_KINDS = ("schedule_request", "qa_unsure", "manual_relay", "off_topic_flag")


def _new_escalation_id() -> str:
    return "esc_" + secrets.token_hex(6)


def _render_schedule_card(*, friend_alias: str, summary: str,
                          proposed_slot: dict, escalation_id: str) -> dict:
    """Owner-facing approval card. The button ``value`` carries the
    escalation_id so the owner-bot's card-action handler can route the
    decision back to ``concierge.escalate.resolve.v1`` (Phase 2)."""
    start = str(proposed_slot.get("start", "")) if proposed_slot else ""
    end = str(proposed_slot.get("end", "")) if proposed_slot else ""
    slot_line = f"**{start}** → **{end}**" if start or end else "(待定时间)"
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📅 {friend_alias} 提议: {summary[:40]}"},
        },
        "body": {
            "elements": [
                {"tag": "markdown",
                 "content": f"**{friend_alias}** 想约你：\n\n{summary}\n\n时段：{slot_line}"},
                {"tag": "action", "actions": [
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "✅ 确认"},
                     "value": {"esc_id": escalation_id, "decision": "confirm"},
                     "type": "primary"},
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                     "value": {"esc_id": escalation_id, "decision": "decline"},
                     "type": "danger"},
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "🕐 改时间"},
                     "value": {"esc_id": escalation_id, "decision": "reschedule"}},
                ]},
            ]
        },
    }


def _render_qa_card(*, friend_alias: str, summary: str, escalation_id: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"❓ {friend_alias} 问你: {summary[:40]}"},
        },
        "body": {
            "elements": [
                {"tag": "markdown",
                 "content": f"**{friend_alias}** 问了一个我不确定该不该答的问题：\n\n> {summary}"},
                {"tag": "markdown",
                 "content": f"_escalation: {escalation_id}_"},
            ]
        },
    }


def _render_generic_card(*, friend_alias: str, kind: str, summary: str,
                          escalation_id: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": f"📨 {friend_alias} · {kind}"},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": summary},
                {"tag": "markdown", "content": f"_escalation: {escalation_id}_"},
            ]
        },
    }


class EscalationLog:
    """Append-only JSONL log of every escalation issued."""

    def __init__(self, log_path: str) -> None:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        self._path = log_path
        self._lock = threading.Lock()

    def append(self, row: dict) -> None:
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        with self._lock:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return [json.loads(line) for line in f if line.strip()]
            except FileNotFoundError:
                return []


class EscalateWorker:
    """Sends an approval card to the owner. Two transports:

    * If ``owner_app_id`` / ``owner_app_secret`` / ``owner_open_id`` are
      all configured, builds a lark client (lazy on first use) and
      ``im.v1.message.create``s the card to the owner.
    * Otherwise, writes the card JSON to JSONL (Phase-1 stub).

    In either case the escalation row is also appended to the JSONL log
    so the owner has a durable record.
    """

    def __init__(self, *, name: str, log: EscalationLog,
                 owner_open_id: str = "",
                 owner_app_id: str = "",
                 owner_app_secret: str = "") -> None:
        self._meta = WorkerMetadata(
            name=name, kind="concierge_escalate", capabilities=CAPS,
            api_version=API_VERSION, owner_plugin="wlwl-ass-builtin",
            description="Concierge → owner approval channel (Feishu card or JSONL stub).",
        )
        self._log = log
        self._owner_open_id = owner_open_id
        self._owner_app_id = owner_app_id
        self._owner_app_secret = owner_app_secret
        self._lark_client = None  # built lazily; None when stubbing
        self._in_flight = 0
        self._last_error: str | None = None
        self._sent_count = 0

    def _live_mode(self) -> bool:
        return bool(self._owner_app_id and self._owner_app_secret and self._owner_open_id)

    def _build_lark(self):
        """Lazy lark client builder. Returns None on import failure (which
        is fine — we fall back to JSONL stub)."""
        if self._lark_client is not None:
            return self._lark_client
        try:
            import lark_oapi as lark  # type: ignore
        except ImportError:
            return None
        try:
            self._lark_client = (
                lark.Client.builder()
                .app_id(self._owner_app_id)
                .app_secret(self._owner_app_secret)
                .log_level(lark.LogLevel.WARN)
                .build()
            )
        except Exception:
            self._lark_client = None
        return self._lark_client

    def _send_card_to_owner(self, card: dict) -> str:
        """Returns the message_id on success, empty string on failure.

        Failure modes:
          * lark_oapi not installed → lazy build returns None
          * Network / API error → caught; returns ""
          * Owner app not yet published / scope missing → API returns
            non-success; returns "" and the error is logged.
        """
        client = self._build_lark()
        if client is None:
            return ""
        try:
            from lark_oapi.api.im.v1 import (  # type: ignore
                CreateMessageRequest, CreateMessageRequestBody,
            )
        except ImportError:
            return ""
        try:
            payload = json.dumps(card, ensure_ascii=False)
            body = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self._owner_open_id)
                    .msg_type("interactive")
                    .content(payload)
                    .build()
                )
                .build()
            )
            r = client.im.v1.message.create(body)
        except Exception as exc:  # noqa: BLE001 — third-party SDK
            self._last_error = f"lark send raised: {exc!r}"
            return ""
        if r and r.success():
            return getattr(r.data, "message_id", "") or ""
        self._last_error = f"lark send failed: code={getattr(r, 'code', '?')} msg={getattr(r, 'msg', '?')}"
        return ""

    @property
    def metadata(self) -> WorkerMetadata:
        return self._meta

    def health(self) -> HealthReport:
        return HealthReport(state="ready", in_flight=self._in_flight, last_error=self._last_error)

    def invoke(self, req: InvokeRequest) -> InvokeResponse:
        self._in_flight += 1
        try:
            if req.capability != "concierge.escalate.v1":
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.NOT_IMPLEMENTED, req.capability))
            p = req.payload
            kind = str(p.get("kind", ""))
            if kind not in VALID_KINDS:
                return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                    ErrorCodes.PAYLOAD_INVALID,
                    f"kind must be one of {VALID_KINDS}, got {kind!r}"))
            friend_open_id = str(p.get("friend_open_id", ""))
            friend_alias = str(p.get("friend_alias", "") or friend_open_id[:8] or "朋友")
            summary = str(p.get("summary", ""))
            proposed_slot = p.get("proposed_slot") or {}

            escalation_id = _new_escalation_id()
            if kind == "schedule_request":
                card = _render_schedule_card(
                    friend_alias=friend_alias, summary=summary,
                    proposed_slot=proposed_slot, escalation_id=escalation_id,
                )
            elif kind == "qa_unsure":
                card = _render_qa_card(
                    friend_alias=friend_alias, summary=summary,
                    escalation_id=escalation_id,
                )
            else:
                card = _render_generic_card(
                    friend_alias=friend_alias, kind=kind, summary=summary,
                    escalation_id=escalation_id,
                )

            row = {
                "escalation_id": escalation_id,
                "t": time.time(),
                "kind": kind,
                "friend_open_id": friend_open_id,
                "friend_alias": friend_alias,
                "summary": summary,
                "proposed_slot": proposed_slot or None,
                "owner_open_id": self._owner_open_id,
                "card": card,
                "transport": "live" if self._live_mode() else "phase1_stub",
                "extras": p.get("payload") or {},
            }

            card_message_id = ""
            if self._live_mode():
                card_message_id = self._send_card_to_owner(card)
                row["card_message_id"] = card_message_id
                row["sent_to_owner"] = bool(card_message_id)
            else:
                row["card_message_id"] = ""
                row["sent_to_owner"] = False
            # Always append to JSONL — even when live-send succeeded, the
            # log is the canonical history of what was escalated.
            self._log.append(row)
            self._sent_count += 1
            return InvokeResponse(
                call_id=req.call_id, ok=True,
                result={
                    "escalation_id": escalation_id,
                    "card_message_id": card_message_id,
                    "sent": True,  # True = either delivered to owner OR queued to JSONL
                },
            )
        except Exception as exc:
            self._last_error = repr(exc)
            return InvokeResponse(call_id=req.call_id, ok=False, error=make_error(
                ErrorCodes.INTERNAL, str(exc)))
        finally:
            self._in_flight = max(0, self._in_flight - 1)

    def shutdown(self, *, drain_timeout_ms: int = 5_000) -> None:
        return


class EscalateFactory:
    def describe(self) -> FactoryDescription:
        return FactoryDescription(
            factory_id="wlwl_ass.workers.concierge_escalate",
            api_version=API_VERSION,
            capabilities_offered=CAPS,
            transport="in_process",
        )

    def build(self, config: dict, kernel: KernelHandle) -> Worker:
        name = config.get("name") or "concierge_escalate"
        log_path = config.get("log_path") or os.path.join(
            _default_temp(), "concierge_escalations.jsonl"
        )
        owner_open_id = str(config.get("owner_open_id", "") or "")
        owner_app_id = str(config.get("owner_app_id", "") or "")
        owner_app_secret = str(config.get("owner_app_secret", "") or "")
        log = EscalationLog(log_path)
        return EscalateWorker(
            name=name, log=log,
            owner_open_id=owner_open_id,
            owner_app_id=owner_app_id,
            owner_app_secret=owner_app_secret,
        )


def _default_temp() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "temp")
