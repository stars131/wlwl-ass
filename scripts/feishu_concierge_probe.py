"""Quick smoke test for the Feishu concierge bot's WS event subscription.

Run AFTER you've configured the app on open.feishu.cn:
  - Bot capability added
  - im:message + im:message:send_as_bot + contact:user.id:readonly permissions
  - Long-connection event mode enabled
  - im.message.receive_v1 subscribed
  - Version published & approved

Then run this script and DM the bot from your Feishu client. It will:
  1. Connect to Feishu cloud via WS using bots.feishu_concierge credentials
  2. Print every inbound message + the sender's open_id (so you can copy it
     into bots.feishu_concierge.owner_open_id_on_owner_app for the real loop)
  3. NOT reply — this is just a passive listener. Ctrl+C to stop.

This is a one-off probe. The real frontend (fsapp_concierge.py) lands in
Phase 2 of ADR-0011.
"""
import json
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import lark_oapi as lark
from llmcore import mykeys

APP_ID = str(mykeys.get("fs_concierge_app_id", "") or "").strip()
APP_SECRET = str(mykeys.get("fs_concierge_app_secret", "") or "").strip()

if not APP_ID or not APP_SECRET:
    print("ERROR: fs_concierge_app_id / fs_concierge_app_secret not set in config.")
    print("Run: python -m launcher.config set bots.feishu_concierge.app_id ...")
    sys.exit(1)


def on_message(data):
    ev = data.event
    msg = ev.message
    sender = ev.sender
    open_id = sender.sender_id.open_id if sender and sender.sender_id else "?"
    try:
        text = json.loads(msg.content or "{}").get("text", "")
    except Exception:
        text = msg.content
    print(f"\n=== inbound message ===")
    print(f"sender open_id:  {open_id}")
    print(f"message_type:    {msg.message_type}")
    print(f"chat_id:         {getattr(msg, 'chat_id', '?')}")
    print(f"text:            {text!r}")
    print(f"=======================\n")


def main():
    print(f"connecting to Feishu cloud as concierge bot")
    print(f"  app_id: {APP_ID}")
    print(f"  press Ctrl+C to stop\n")
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    cli = lark.ws.Client(APP_ID, APP_SECRET,
                          event_handler=handler,
                          log_level=lark.LogLevel.INFO)
    cli.start()


if __name__ == "__main__":
    main()
