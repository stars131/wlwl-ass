"""Unified IM gateway skeleton (#29).

GA today launches one Python process per IM platform (fsapp.py / qqapp.py /
tgapp.py …). Each spawns its own agent loop, holds its own memory, and
can't easily participate in cross-platform conversation continuity ("send
the rest on Telegram"). The unified gateway is a single process that:

  1. Receives webhooks / polls from each enabled platform via small
     adapter modules.
  2. Normalises each incoming message to ``InboundMessage`` (platform,
     user_id, text, attachments, ts).
  3. Pushes it onto a thread-safe queue.
  4. A worker pool consumes the queue, runs the agent, and dispatches the
     reply back through the originating platform adapter.

This MVP gives the dispatcher + the message normaliser + a stub adapter
registry. It does NOT replace the existing per-platform frontends — it's a
parallel runtime that the user can opt into, and we keep the originals
working unchanged. Migrating each adapter (fs/qq/tg/wecom/dingtalk) is a
follow-up task per platform; the contract is small enough (1 class) that
each migration is a 1-day effort.

Run: ``python -m frontends.gateway --port 19599`` — exposes:

  * POST /webhook/<platform>     incoming payload (platform-specific shape)
  * POST /send                   {platform, user_id, text} for tests
  * GET  /status                 worker counts + queue length

Wire your IM platform's webhook to the gateway URL. For pull-based bots
(QQ/Telegram bot SDK that spawn their own thread), the adapter calls
``Gateway.ingest(...)`` directly; no webhook needed.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── data shapes ──────────────────────────────────────────────────────


@dataclass
class InboundMessage:
    platform: str  # "feishu" / "tg" / "qq" / "wecom" / "dingtalk" / "wechat"
    user_id: str
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    ts: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def conversation_key(self) -> str:
        """Canonical id used to look up shared agent state across platforms.

        For now this is just ``platform:user_id`` — but the design point is
        that an upstream user-mapping layer can rewrite this to fold the
        same human across platforms. ``map_user("tg:42") == "u_alice"`` →
        same ``conversation_key`` everywhere.
        """
        return f"{self.platform}:{self.user_id}"


@dataclass
class OutboundMessage:
    platform: str
    user_id: str
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)


# ── adapters ────────────────────────────────────────────────────────


class PlatformAdapter:
    """Implement once per platform. Two methods to fill in:

      * ``parse_webhook(body, headers) -> InboundMessage | None``
      * ``send(msg: OutboundMessage) -> bool``
    """

    name: str = "base"

    def parse_webhook(self, body: dict[str, Any], headers: dict[str, str]) -> InboundMessage | None:
        return None

    def send(self, msg: OutboundMessage) -> bool:
        raise NotImplementedError


class EchoAdapter(PlatformAdapter):
    """Reference adapter for tests + manual /send → /webhook smoke tests.

    parse_webhook expects ``{"user_id": str, "text": str}``.
    send appends to ``_outbox`` so tests can assert on dispatched replies.
    """

    name = "echo"

    def __init__(self) -> None:
        self.outbox: list[OutboundMessage] = []

    def parse_webhook(self, body: dict[str, Any], headers: dict[str, str]) -> InboundMessage | None:
        text = body.get("text")
        user_id = body.get("user_id")
        if not isinstance(text, str) or not isinstance(user_id, str):
            return None
        return InboundMessage(
            platform=self.name,
            user_id=user_id,
            text=text,
            ts=time.time(),
            raw=body,
        )

    def send(self, msg: OutboundMessage) -> bool:
        self.outbox.append(msg)
        return True


# ── gateway core ────────────────────────────────────────────────────


AgentHandler = Callable[[InboundMessage], str]
"""Callable that takes an InboundMessage and returns the reply text. The
default handler echoes the input — replace via ``Gateway(handler=...)`` to
plug in your own agent loop (e.g. ``agent_loop.run_one_turn``)."""


def _default_handler(msg: InboundMessage) -> str:
    return f"[gateway] received on {msg.platform} from {msg.user_id}: {msg.text}"


class Gateway:
    def __init__(
        self,
        *,
        handler: AgentHandler | None = None,
        worker_count: int = 2,
    ):
        self.handler = handler or _default_handler
        self._adapters: dict[str, PlatformAdapter] = {}
        self._queue: "queue.Queue[InboundMessage]" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.worker_count = max(1, int(worker_count))

    # ── adapter registry ──────────────────────────────────────────

    def register_adapter(self, adapter: PlatformAdapter) -> None:
        with self._lock:
            self._adapters[adapter.name] = adapter

    def adapter(self, name: str) -> PlatformAdapter | None:
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        return sorted(self._adapters)

    # ── ingestion ─────────────────────────────────────────────────

    def ingest(self, msg: InboundMessage) -> None:
        """Push an already-parsed message onto the queue. Used by pull-based
        adapters (QQ/Telegram bot SDKs in their own threads)."""
        self._queue.put(msg)

    def ingest_webhook(self, platform: str, body: dict[str, Any], headers: dict[str, str]) -> bool:
        adapter = self._adapters.get(platform)
        if adapter is None:
            return False
        msg = adapter.parse_webhook(body, headers)
        if msg is None:
            return False
        self._queue.put(msg)
        return True

    def queue_size(self) -> int:
        return self._queue.qsize()

    # ── workers ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._workers:
            return
        for i in range(self.worker_count):
            t = threading.Thread(target=self._worker_loop, name=f"ga-gateway-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        # Wake up workers blocked on queue.get with sentinel messages.
        for _ in self._workers:
            self._queue.put(InboundMessage(platform="__stop__", user_id="", text=""))
        for t in self._workers:
            t.join(timeout=timeout)
        self._workers.clear()
        self._stop.clear()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if msg.platform == "__stop__":
                return
            try:
                reply = self.handler(msg)
            except Exception as exc:
                reply = f"[gateway] handler error: {type(exc).__name__}: {exc}"
            adapter = self._adapters.get(msg.platform)
            if adapter is None or not reply:
                continue
            try:
                adapter.send(OutboundMessage(
                    platform=msg.platform,
                    user_id=msg.user_id,
                    text=reply,
                ))
            except Exception:
                # Swallow — losing a single reply shouldn't kill the worker.
                pass


# ── HTTP server ─────────────────────────────────────────────────────


def make_http_server(gateway: Gateway, port: int = 19599) -> "_GwServer":
    handler_cls = _make_handler(gateway)
    return _GwServer(("127.0.0.1", port), handler_cls)


class _GwServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(gateway: Gateway):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence
            return

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self):
            if self.path == "/status":
                self._send(200, {
                    "queue_size": gateway.queue_size(),
                    "adapters": gateway.list_adapters(),
                    "workers": len(gateway._workers),
                })
                return
            self._send(404, {"error": "not_found", "path": self.path})

        def do_POST(self):
            if self.path == "/send":
                body = self._read_json()
                platform = str(body.get("platform") or "")
                user_id = str(body.get("user_id") or "")
                text = str(body.get("text") or "")
                if not platform or not user_id or not text:
                    self._send(400, {"error": "missing_field"})
                    return
                msg = InboundMessage(platform=platform, user_id=user_id, text=text, ts=time.time())
                gateway.ingest(msg)
                self._send(200, {"ok": True})
                return
            if self.path.startswith("/webhook/"):
                platform = self.path[len("/webhook/"):].strip("/")
                body = self._read_json()
                headers = {k: v for k, v in self.headers.items()}
                ok = gateway.ingest_webhook(platform, body, headers)
                if not ok:
                    self._send(400, {"error": "unparseable_or_unknown_platform", "platform": platform})
                    return
                self._send(200, {"ok": True})
                return
            self._send(404, {"error": "not_found", "path": self.path})

    return _Handler


def run_gateway(port: int = 19599, *, enable_feishu: bool = True, real_agent: bool = True) -> None:
    """Stand up a gateway with the EchoAdapter (always) plus any real
    adapter whose deps are present (Feishu today; QQ/TG/wecom follow).

    By default ``real_agent=True`` wires the real ``GeneraticAgent`` per
    conversation via ``frontends.gateway_handler.AgentBridge``. Pass
    ``real_agent=False`` to keep the echo handler — useful for transport-
    only smoke tests where you don't want the LLM cost.
    """
    handler = None
    if real_agent:
        try:
            from frontends.gateway_handler import get_bridge
            handler = get_bridge().handle
        except Exception as exc:
            print(f"[gateway] AgentBridge unavailable, falling back to echo: {exc}")
    gw = Gateway(handler=handler) if handler else Gateway()
    gw.register_adapter(EchoAdapter())
    started: list[str] = ["echo"]
    if enable_feishu:
        try:
            from frontends.gateway_adapters.feishu import FeishuAdapter
            fs = FeishuAdapter()
            gw.register_adapter(fs)
            if fs.start(gw):
                started.append("feishu (live)")
            else:
                started.append("feishu (webhook-only — WS unavailable)")
        except Exception as exc:
            print(f"[gateway] feishu adapter unavailable: {exc}")
    gw.start()
    httpd = make_http_server(gw, port=port)
    handler_label = "agent" if handler else "echo"
    print(f"[gateway] listening on 127.0.0.1:{port}; handler={handler_label}; adapters={started}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        gw.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="wlwl-ass unified IM gateway")
    parser.add_argument("--port", type=int, default=19599)
    parser.add_argument("--no-feishu", action="store_true", help="Skip the Feishu adapter")
    parser.add_argument("--no-agent", action="store_true",
                        help="Use the echo handler instead of a real GeneraticAgent")
    args = parser.parse_args()
    run_gateway(args.port, enable_feishu=not args.no_feishu, real_agent=not args.no_agent)
