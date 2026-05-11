"""WeChat outbound messaging via wxauto (UI automation).

Drives the running PC WeChat client by simulating user actions: searching for
the contact, switching to that chat, pasting text, optionally sending files,
and pressing enter. Single-direction: outbound only — no history reads, no
listeners. Failure modes (WeChat not running, contact not found, version
incompatibility) all surface as ``[wechat_send error] ...`` strings rather
than exceptions, so the agent loop can recover via the ljqCtrl fallback SOP.

Prerequisites:
  * ``pip install wxauto`` (declared as the ``[wechat]`` optional-dependency)
  * PC WeChat (Windows) installed, logged in, window foregroundable
  * Tested against WeChat 3.9.x. WeChat 4.x may require the ljqCtrl fallback
    described in ``memory/wechat_ljqctrl_sop.md``.
"""
from __future__ import annotations

import os
from typing import Any


def wechat_send(to: str, text: str, *, files: list[str] | None = None) -> str:
    to = (to or "").strip()
    text = (text or "").strip()
    if not to or not text:
        return "[wechat_send error] to + text required"

    try:
        import wxauto
    except ImportError:
        return ("[wechat_send error] wxauto not installed. "
                "Run: pip install wxauto  (or: pip install -e .[wechat])")
    except Exception as exc:
        return f"[wechat_send error] wxauto import failed: {exc!r}"

    try:
        wx = wxauto.WeChat()
    except Exception as exc:
        return ("[wechat_send error] WeChat client not reachable. "
                f"Make sure PC WeChat is running and logged in. ({exc!r})")

    try:
        wx.SendMsg(text, who=to)
    except Exception as exc:
        return ("[wechat_send error] SendMsg failed; the contact may not be "
                "in your address book, or this WeChat version is unsupported "
                f"by wxauto. Consider sop_read wechat_ljqctrl_sop. ({exc!r})")

    sent_files: list[str] = []
    if files:
        for path in files:
            if not isinstance(path, str) or not path.strip():
                continue
            abspath = os.path.abspath(path)
            if not os.path.exists(abspath):
                return (f"[wechat_send partial] text sent to {to!r} but file "
                        f"missing: {abspath}")
            try:
                wx.SendFiles(filepath=abspath, who=to)
                sent_files.append(abspath)
            except Exception as exc:
                return (f"[wechat_send partial] text sent to {to!r}; file "
                        f"{abspath} failed: {exc!r}")

    suffix = f" + {len(sent_files)} file(s)" if sent_files else ""
    return f"[wechat_send] -> {to} ({len(text)} chars{suffix})"
