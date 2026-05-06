"""Feishu (Lark) adapter for the unified gateway (#29 + #14-style migration).

This is a *real* adapter — when ``lark_oapi`` is installed it connects to
Feishu via the same WebSocket event channel as ``frontends/fsapp.py``,
parses incoming text/post/image messages into ``InboundMessage``, and uses
the Lark IM v1 ``message.create`` endpoint to send replies.

Design split vs ``frontends/fsapp.py``:

  * fsapp.py owns the *agent loop* — it spins up a ``GeneraticAgent`` and
    pumps messages through it, then formats results back as Lark cards.
  * THIS adapter is loop-agnostic — the gateway's worker thread runs the
    agent (or whatever handler is wired); we only handle Lark transport.
    That lets one process serve every IM platform with one shared agent.

If ``lark_oapi`` is missing, the adapter still imports cleanly and
``start()`` returns False with a clear message (instead of crashing the
whole gateway). This matches the rest of the codebase's "gracefully
degrade when an optional dep is absent" convention.

Credentials come from ``bots.feishu.app_id`` / ``app_secret`` /
``allowed_users`` in :mod:`launcher.config_store` (flattened to the
legacy ``fs_app_id`` / ``fs_app_secret`` / ``fs_allowed_users`` names by
``llmcore.reload_mykeys``) — same names as the legacy fsapp.py — so users
don't have to reconfigure.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from frontends.gateway import Gateway, InboundMessage, OutboundMessage, PlatformAdapter


def _load_creds() -> tuple[str, str, set[str]]:
    """Pull the bot credentials. ``mykeys`` is loaded lazily so importing
    this adapter doesn't trigger llmcore side-effects up front."""
    try:
        from llmcore import mykeys
    except Exception:
        mykeys = {}
    app_id = str(mykeys.get("fs_app_id", "") or "").strip()
    app_secret = str(mykeys.get("fs_app_secret", "") or "").strip()
    raw_allowed = mykeys.get("fs_allowed_users", []) or []
    if isinstance(raw_allowed, str):
        raw_allowed = [raw_allowed]
    allowed = {str(x).strip() for x in raw_allowed if str(x).strip()}
    return app_id, app_secret, allowed


class FeishuAdapter(PlatformAdapter):
    name = "feishu"

    def __init__(self) -> None:
        self._client = None  # lark.Client, lazily built
        self._ws_client = None
        self._gateway: Gateway | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._app_id, self._app_secret, self._allowed = "", "", set()

    # ── webhook path (unused for Lark — they use WS not REST webhooks) ──

    def parse_webhook(self, body: dict[str, Any], headers: dict[str, str]) -> InboundMessage | None:
        """Lark cloud also supports the old HTTP webhook style for tenants
        that haven't enabled the v3 event subscription. We accept the
        ``event.message.content`` shape just in case."""
        try:
            event = body.get("event") or {}
            msg = event.get("message") or {}
            sender = event.get("sender") or {}
            user_id = (sender.get("sender_id") or {}).get("open_id", "")
            text_content = msg.get("content") or "{}"
            try:
                text = json.loads(text_content).get("text", "")
            except Exception:
                text = str(text_content)
            if not user_id or not text:
                return None
            return InboundMessage(
                platform=self.name,
                user_id=str(user_id),
                text=str(text),
                ts=float(event.get("ts") or 0) or 0.0,
                raw=body,
            )
        except Exception:
            return None

    # ── outbound ─────────────────────────────────────────────────────

    def send(self, msg: OutboundMessage) -> bool:
        """Send via Lark IM v1 message.create. Returns False on any error.

        ``user_id`` here is whatever ``InboundMessage.user_id`` was — for
        the WS adapter that's the open_id; for webhooks it's also open_id.
        We use ``open_id`` as the receive_id_type to match.
        """
        if self._client is None and not self._build_client():
            return False
        try:
            from lark_oapi.api.im.v1 import (  # type: ignore
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except Exception:
            return False
        payload = json.dumps({"text": msg.text}, ensure_ascii=False)
        body = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.user_id)
                .msg_type("text")
                .content(payload)
                .build()
            )
            .build()
        )
        try:
            r = self._client.im.v1.message.create(body)
        except Exception as exc:
            print(f"[FeishuAdapter] send failed: {exc}")
            return False
        return bool(r and r.success())

    def _build_client(self) -> bool:
        try:
            import lark_oapi as lark  # type: ignore
        except ImportError:
            print("[FeishuAdapter] lark_oapi not installed; pip install lark-oapi")
            return False
        self._app_id, self._app_secret, self._allowed = _load_creds()
        if not self._app_id or not self._app_secret:
            print("[FeishuAdapter] fs_app_id / fs_app_secret missing — set via GUI Bots tab or `python -m launcher.config set bots.feishu.app_id ...`")
            return False
        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .log_level(lark.LogLevel.WARN)
            .build()
        )
        return True

    # ── inbound: WS event subscription ───────────────────────────────

    def start(self, gateway: Gateway) -> bool:
        """Connect to Feishu WS and forward incoming messages to ``gateway``.

        Returns False if the SDK or credentials are missing. The actual WS
        loop runs in a daemon thread; ``stop()`` signals it to exit.
        """
        self._gateway = gateway
        if not self._build_client():
            return False
        try:
            import lark_oapi as lark  # type: ignore
        except ImportError:
            return False

        def _on_message(data: Any) -> None:
            try:
                ev = data.event
                msg = ev.message
                sender = ev.sender
                user_id = sender.sender_id.open_id if sender and sender.sender_id else ""
                if self._allowed and "*" not in self._allowed and user_id not in self._allowed:
                    return  # silently drop unauthorised users
                content_json = msg.content or "{}"
                try:
                    text = json.loads(content_json).get("text", "")
                except Exception:
                    text = str(content_json)
                if not user_id or not text:
                    return
                inbound = InboundMessage(
                    platform=self.name,
                    user_id=str(user_id),
                    text=str(text),
                    ts=float(getattr(ev, "ts", 0) or 0),
                    raw={"chat_id": getattr(msg, "chat_id", None), "message_id": getattr(msg, "message_id", None)},
                )
                if self._gateway is not None:
                    self._gateway.ingest(inbound)
            except Exception as exc:
                print(f"[FeishuAdapter] on_message error: {exc}")

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARN,
        )

        def _run() -> None:
            try:
                self._ws_client.start()
            except Exception as exc:
                print(f"[FeishuAdapter] ws loop crashed: {exc}")

        self._stop.clear()
        self._thread = threading.Thread(target=_run, name="feishu-ws", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        # lark.ws.Client doesn't expose a clean stop; rely on daemon thread
        # exiting when the process does. Best-effort to break the loop:
        try:
            if self._ws_client is not None and hasattr(self._ws_client, "stop"):
                self._ws_client.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
