"""DEBUG-level WS probe for the Feishu concierge bot.

Same as feishu_concierge_probe.py but with lark.LogLevel.DEBUG so we
capture the raw WS handshake + every inbound frame, not just events
that match a registered handler. Use this when the bot connects but
no inbound events seem to arrive — DEBUG shows whether the WS is
empty (event subscription mis-configured on Feishu side) or has
frames that fail to route (handler shape mismatch).

Sets a default 5-minute timeout so we don't leave a dangling WS — DM
the bot once during that window and watch the stdout.

Single-purpose diagnostic; not loaded by anything else.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import lark_oapi as lark
from llmcore import mykeys


APP_ID = str(mykeys.get("fs_concierge_app_id", "") or "").strip()
APP_SECRET = str(mykeys.get("fs_concierge_app_secret", "") or "").strip()
RUN_SECONDS = int(os.environ.get("PROBE_SECONDS", "300"))


def on_message(data):
    """Fires only for im.message.receive_v1 frames the SDK could dispatch."""
    try:
        ev = data.event
        msg = ev.message
        sender = ev.sender
        open_id = sender.sender_id.open_id if sender and sender.sender_id else "?"
        print(f"[probe] HANDLER FIRED open_id={open_id} type={msg.message_type} content={msg.content!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] handler crashed: {exc!r}")


def _acquire_lock() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 19533))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    # Hold it for the process lifetime
    threading._sock_keepalive = sock  # type: ignore[attr-defined]
    return True


def main():
    if not APP_ID or not APP_SECRET:
        print("ERROR: fs_concierge_app_id / fs_concierge_app_secret not set in mykeys.")
        sys.exit(1)
    if not _acquire_lock():
        print("ERROR: port 19533 busy — kill the running concierge first")
        print("       netstat -ano | grep :19533    →    taskkill /PID <pid> /F")
        sys.exit(1)

    print("=" * 60)
    print(f"DEBUG probe for app_id={APP_ID}")
    print(f"  staying up for {RUN_SECONDS}s — DM the bot now")
    print(f"  watching for ANY frame on the WS (handshake + events)")
    print(f"  if no frames at all → event subscription mis-configured")
    print("=" * 60)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    cli = lark.ws.Client(
        APP_ID, APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,
    )

    # Auto-quit after RUN_SECONDS so we don't leak the process
    def _kill():
        import time
        time.sleep(RUN_SECONDS)
        print(f"\n[probe] {RUN_SECONDS}s elapsed — exiting")
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()

    cli.start()


if __name__ == "__main__":
    main()
