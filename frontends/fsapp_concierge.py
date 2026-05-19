"""Feishu concierge bot frontend — friend-facing restricted agent.

Companion to ``frontends/fsapp.py`` (the owner-facing full-power agent).
See ``docs/specs/feishu-concierge-bot.md`` + ``docs/adr/0011-*`` for the
full design.

What this script does (top to bottom):

1. Acquire a single-instance TCP lock on port 19533 — prevents two
   concierge processes from racing on the same Feishu WS event stream.
2. Read credentials + config from ``llmcore.mykeys`` (which is itself
   sourced from ``~/.wlwl-ass/config.json`` + project ``.wlwl-ass/`` +
   ``.env`` — see ``launcher/config_store.py``).
3. Boot a kernel and register five workers:
     - calendar (read-only for the concierge; writes go through the
       owner-approval gate)
     - concierge.kb  / concierge.slot
     - concierge.escalate (owner-app credentials wired here, NOT in the
       agent — see ADR-0011 § 6.3)
     - concierge.audit
4. Open a Feishu WebSocket (lark_oapi long-connection mode) using the
   **concierge** app's credentials.
5. For every inbound message:
     - drop owner messages silently (owner uses ``fsapp.py`` for its
       own agent — friend bot ignores owner)
     - check allowed_friends whitelist (default deny, same posture as
       fsapp.py)
     - feed the text into ``ConciergeAgent.handle()``
     - send the returned reply via Lark IM v1 ``message.create``

Differences vs ``fsapp.py``:

  * No multimodal inbound — images / audio / files acknowledged with a
    placeholder; concierge doesn't try to OCR them in v1.
  * No multi-step task card — concierge returns one reply per turn.
  * No ``/new`` / ``/stop`` / ``/restore`` slash commands — those are
    owner-only tools. Friend ``/`` messages get the normal smalltalk
    fallback.
  * One ``ConciergeAgent`` for ALL friends (sessions are isolated
    inside ``SessionStore`` file-per-friend); fsapp.py spins one
    ``GeneraticAgent`` per open_id because each agent has stateful
    history. ConciergeAgent is stateless except for the file-backed
    session store.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from llmcore import mykeys, reload_mykeys
from llmcore.concierge_agent import (
    ConciergeAgent, ConciergeConfig, LLMHooks, SessionStore, config_from_mykeys,
)
from llmcore.kernel import get_kernel, reset_kernel
from launcher.llm_binding import parse_binding, resolve_to_config_names
from launcher.api_config import list_api_configs, safe_config_var_name
from launcher.profiles import load_profiles

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
)


def _instantiate_session_for_config(cfg: dict, var_name: str):
    """Build the matching ``llmcore.*Session`` for a single config payload.

    Mirrors the ``if/elif`` ladder in ``_build_llm_session`` and
    ``agentmain.load_llm_sessions`` so concierge picks the same shape that
    the owner bot would. Returns None on import or init failure."""
    try:
        from llmcore import (
            LLMSession, ClaudeSession,
            NativeClaudeSession, NativeOAISession,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[concierge] LLM imports unavailable: {exc!r}")
        return None
    k = var_name.lower()
    try:
        if "native" in k and "claude" in k:
            return NativeClaudeSession(cfg=cfg)
        if "native" in k and "oai" in k:
            return NativeOAISession(cfg=cfg)
        if "claude" in k:
            return ClaudeSession(cfg=cfg)
        return LLMSession(cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[concierge] LLM session init for {var_name!r} failed: {exc!r}")
        return None


def _build_llm_session(binding: str = ""):
    """Pick an LLM session honouring the WLWL_BOT_LLM_BINDING contract.

    ``binding="config:<name>"`` pins to one specific config.
    ``binding="profile:<name>"`` wraps the profile's ordered members in a
    ``MixinSession`` (primary-then-fallback with spring-back to primary
    after 300s — see ``llmcore/mixin.py:21``). Empty binding falls back to
    the legacy "first usable session from mykeys" behaviour.

    Returns the session instance (with ``.ask(user_text, stream=False)``
    interface) or None when no LLM is configured. Caller decides which hooks
    to wire."""
    try:
        from llmcore import (
            LLMSession, ClaudeSession, MixinSession,
            NativeClaudeSession, NativeOAISession,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[concierge] LLM imports unavailable: {exc!r}")
        return None

    kind, name = parse_binding(binding)
    if kind:
        try:
            profiles = load_profiles(PROJECT_ROOT)
            configs = list_api_configs(PROJECT_ROOT)
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge] binding resolve failed: {exc!r} — falling back to default")
            kind = ""
        else:
            names = resolve_to_config_names(kind, name, profiles, configs)
            if not names:
                kind = ""  # fall back
            elif kind == "config":
                # Single-config pin: look up its var_name and use the cfg dict
                # from mykeys (which has the unmasked apikey).
                target_var = safe_config_var_name(
                    next((c.get("kind") for c in configs if c.get("name") == names[0]), "native_oai"),
                    names[0],
                )
                cfg = mykeys.get(target_var)
                if not isinstance(cfg, dict):
                    print(f"[concierge] binding config:{name!r} not in mykeys — fallback")
                    kind = ""
                else:
                    print(f"[concierge] LLM via config:{names[0]!r}")
                    return _instantiate_session_for_config(cfg, target_var)
            else:  # profile
                inner_sessions = []
                for cn in names:
                    entry = next((c for c in configs if c.get("name") == cn), None)
                    if entry is None:
                        continue
                    target_var = safe_config_var_name(entry.get("kind"), cn)
                    cfg = mykeys.get(target_var)
                    if not isinstance(cfg, dict):
                        print(f"[concierge] mixin member {cn!r} missing cfg — skipped")
                        continue
                    s = _instantiate_session_for_config(cfg, target_var)
                    if s is None:
                        continue
                    # MixinSession expects objects with `.backend` (set by
                    # ToolClient / NativeToolClient in the owner-bot path).
                    # Here we wrap inline with a tiny shim that exposes
                    # `.backend` pointing at the session itself, matching
                    # the shape mixin.py:27 expects.
                    shim = type("_Shim", (), {"backend": s})
                    inner_sessions.append(shim)
                if not inner_sessions:
                    print(f"[concierge] profile:{name!r} resolved to zero sessions — fallback")
                    kind = ""
                else:
                    cfg = {
                        "llm_nos": [s.backend.name for s in inner_sessions],
                        "max_retries": max(3, len(inner_sessions) + 1),
                        "base_delay": 0.5,
                        "spring_back": 300,
                    }
                    try:
                        mx = MixinSession(inner_sessions, cfg)
                        print(f"[concierge] LLM via profile:{name!r} → {cfg['llm_nos']}")
                        return mx
                    except Exception as exc:  # noqa: BLE001
                        print(f"[concierge] MixinSession init failed: {exc!r} — fallback")
                        kind = ""

    # Default / fallback path: pick the first usable session from mykeys.
    for k, cfg in mykeys.items():
        if not isinstance(cfg, dict):
            continue
        if not any(x in k for x in ("api", "config")):
            continue
        if "mixin" in k:
            continue
        try:
            if "native" in k and "claude" in k:
                return NativeClaudeSession(cfg=cfg)
            if "native" in k and "oai" in k:
                return NativeOAISession(cfg=cfg)
            if "claude" in k:
                return ClaudeSession(cfg=cfg)
            if "oai" in k:
                return LLMSession(cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge] LLM session init {k!r} failed: {exc!r}")
            continue
    return None


def _ask_llm(session, system_prompt: str, user_text: str) -> str | None:
    """One-shot: clear history, set system, ask, clear. Returns the raw
    string reply or None on any failure. Each call is stateless — multi-
    turn state lives in concierge SessionStore on disk.

    Handles both session protocols:
      * ``BaseSession.ask(prompt, stream=False)`` (text protocol used by
        ``ClaudeSession`` / ``LLMSession``) — string in, string out.
      * ``NativeClaudeSession.ask(msg)`` (tool-use protocol used by
        ``NativeClaudeSession`` / ``NativeOAISession``) — dict in, the
        function is a generator that yields chunks and returns a
        ``MockResponse`` via ``StopIteration.value``. We exhaust the
        generator and extract ``.content``.
    """
    try:
        session.history = []
        session.system = system_prompt
        try:
            from llmcore.adapters.anthropic import NativeClaudeSession
            is_native = isinstance(session, NativeClaudeSession)
        except Exception:
            is_native = False
        if is_native:
            msg = {"role": "user",
                   "content": [{"type": "text", "text": user_text}]}
            gen = session.ask(msg)
            mock_resp = None
            try:
                while True:
                    next(gen)
            except StopIteration as exc:
                mock_resp = exc.value
            reply = getattr(mock_resp, "content", None) if mock_resp else None
        else:
            reply = session.ask(user_text, stream=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[concierge] LLM ask failed: {exc!r}")
        return None
    finally:
        try:
            session.history = []
        except Exception:
            pass
    if isinstance(reply, str):
        return reply
    if reply is None:
        return None
    return str(reply) or None


def _strip_json_fence(text: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ``` fences. Strip them."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_]*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def _parse_llm_json(raw: str) -> "dict | None":
    """Lenient JSON extractor for LLM-emitted dicts.

    Real LLMs do all of these:
      * wrap output in ```json ... ``` or ``` ... ``` fences
      * prepend natural-language preamble: "Sure, here it is: {...}"
      * append a trailing "Let me know if..." or trailing dot
      * mix single quotes with double quotes
      * include trailing commas
      * emit reasoning-model `<think>` tags

    We try increasingly forgiving strategies until one yields a dict.
    Returns None when no strategy parses — caller falls back to rules.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # 1. Strip reasoning tags (Phase 3 LLMs sometimes leak <think>...</think>)
    text = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", text, flags=re.IGNORECASE)
    # 2. Strip code fences
    text = _strip_json_fence(text)

    # 3. Direct parse — happy path
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except (ValueError, TypeError):
        pass

    # 4. Extract the first {...} balanced block. Catches the
    # "preamble + JSON + postamble" case.
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        v = json.loads(candidate)
                        if isinstance(v, dict):
                            return v
                    except (ValueError, TypeError):
                        pass
                    # 5. Tolerate single quotes / trailing commas — common
                    # in models that learned from Python dict reprs.
                    try:
                        relaxed = candidate.replace("'", '"')
                        relaxed = re.sub(r",(\s*[}\]])", r"\1", relaxed)
                        v = json.loads(relaxed)
                        if isinstance(v, dict):
                            return v
                    except (ValueError, TypeError):
                        pass
                    break  # don't try smaller blocks if biggest failed
    return None


def _build_llm_hooks() -> "LLMHooks | None":
    """Build the full LLM hook bundle (chat + classify + extract_schedule +
    render_slots + render_qa) sharing one session.

    Returns None when no LLM session is available — caller skips LLM and
    the agent runs in pure rule-based mode (Phase 1 behaviour).
    """
    session = _build_llm_session(os.environ.get("WLWL_BOT_LLM_BINDING", ""))
    if session is None:
        return None
    print(f"[concierge] LLM hooks via {getattr(session, 'name', '<?>')}")

    def chat(user_text: str, system_prompt: str) -> str | None:
        return _ask_llm(session, system_prompt, user_text)

    classify_system = (
        "你是一个中文消息意图分类器。把朋友给 Alice 的小秘书的消息分到下列类别之一：\n"
        "- schedule: 朋友想跟 Alice 约时间见面/吃饭/通话/喝咖啡等\n"
        "- qa: 朋友打听 Alice 的近况（在哪上班、住哪儿、最近忙啥之类）\n"
        "- escalate_now: 朋友让你向 Alice 转告紧急信息\n"
        "- cancel: 朋友想取消之前提到的约会\n"
        "- smalltalk: 上面都不是（问候、闲聊、emoji 等）\n\n"
        "**只输出 JSON**，不要任何解释、markdown、代码块标记。格式：\n"
        '{"intent": "<intent>", "payload": {"topic_summary": "<≤60字摘要>"}}'
    )

    def classify(user_text: str, session_snapshot: dict) -> dict | None:
        raw = _ask_llm(session, classify_system, user_text)
        if not raw:
            return None
        return _parse_llm_json(raw)

    extract_system = (
        "从中文消息里提取约时间需要的结构化字段。**只输出 JSON**，无解释。\n"
        "字段：\n"
        '- duration_minutes (int 5..480, 默认 60)\n'
        '- preferred_window: "morning" / "afternoon" / "evening" / "any"\n'
        '- topic_summary: ≤60 字短句概述约什么\n'
        '示例: {"duration_minutes": 60, "preferred_window": "evening", "topic_summary": "晚上一起吃饭"}'
    )

    def extract_schedule(user_text: str, session_snapshot: dict) -> dict | None:
        raw = _ask_llm(session, extract_system, user_text)
        if not raw:
            return None
        return _parse_llm_json(raw)

    def render_slots(slots, topic, friend_alias):
        slot_lines = "\n".join(
            f"{i+1}. {s.get('start','')} ~ {s.get('end','')}"
            for i, s in enumerate(slots)
        )
        sys_p = (
            "你是 Alice 的小助理，用自然温和的中文向朋友提议会面时段。"
            "硬要求：把每一个时段的具体时间都说出来（保留 HH:MM 数字），"
            "不要省略任何选项，不要加 markdown、不要 emoji 罗列。"
            "保持 2-4 句话。"
        )
        user_p = (
            f"朋友（{friend_alias or '朋友'}）想约：{topic or '碰个面'}。"
            f"我帮她看过日历，这几个时段她可以：\n{slot_lines}\n"
            f"请把它说得自然点，让朋友挑一个。"
        )
        return _ask_llm(session, sys_p, user_p)

    def render_qa(question, kb_text, topic):
        sys_p = (
            "你是 Alice 的小助理，用自然简短的中文回答朋友关于 Alice 的提问。"
            "硬要求：只能基于下面给你的事实，不允许虚构、不允许添加未在事实里出现的信息；"
            "1-2 句话，口语化，不要 markdown。"
        )
        user_p = (
            f"朋友的问题：{question}\n"
            f"你掌握的事实（这是你唯一可用的信息源）：{kb_text}\n"
            "用 1-2 句话告诉朋友。"
        )
        return _ask_llm(session, sys_p, user_p)

    return LLMHooks(
        chat=chat,
        classify=classify,
        extract_schedule=extract_schedule,
        render_slots=render_slots,
        render_qa=render_qa,
    )


CONCIERGE_LOCK_PORT = 19533
_INSTANCE_LOCK: socket.socket | None = None

TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
DEFAULT_KB_PATH = os.path.join(TEMP_DIR, "concierge_kb.jsonl")
DEFAULT_AUDIT_PATH = os.path.join(TEMP_DIR, "concierge_audit.jsonl")
DEFAULT_ESCALATION_LOG = os.path.join(TEMP_DIR, "concierge_escalations.jsonl")
DEFAULT_SESSION_DIR = os.path.join(TEMP_DIR, "concierge_state")
DEFAULT_CALENDAR_DB = os.path.join(TEMP_DIR, "calendar.db")


def _acquire_single_instance() -> bool:
    """Hold a localhost TCP port for the process lifetime — duplicates
    the pattern from ``fsapp.py:_acquire_single_instance``. If another
    concierge process already holds the port, abort cleanly."""
    global _INSTANCE_LOCK
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", CONCIERGE_LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _INSTANCE_LOCK = sock
    return True


def _to_allowed_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def _parse_text(msg) -> str:
    """Pull the text payload out of an inbound IM event. v1 only supports
    text — images/audio/files trigger an explicit placeholder so the
    intent classifier doesn't blank-route them."""
    msg_type = msg.message_type
    content_json = msg.content or "{}"
    if msg_type == "text":
        try:
            return str(json.loads(content_json).get("text", "") or "").strip()
        except (ValueError, TypeError):
            return ""
    if msg_type in ("image", "audio", "file", "media"):
        return f"[{msg_type}]"
    if msg_type == "post":
        # crude: surface the title + textual nodes; concierge v1 doesn't
        # OCR embedded images
        try:
            post = json.loads(content_json)
        except ValueError:
            return ""
        root = post.get("post") or post
        if not isinstance(root, dict):
            return ""
        for locale in ("zh_cn", "en_us", "ja_jp"):
            block = root.get(locale)
            if isinstance(block, dict):
                texts = []
                if block.get("title"):
                    texts.append(str(block["title"]))
                for row in block.get("content", []) or []:
                    if isinstance(row, list):
                        for el in row:
                            if isinstance(el, dict) and el.get("tag") == "text":
                                texts.append(str(el.get("text", "")))
                return " ".join(t for t in texts if t).strip()
        return ""
    return ""


class ConciergeApp:
    """The long-lived process. ``run()`` opens the WS and blocks."""

    def __init__(self) -> None:
        self.app_id = str(mykeys.get("fs_concierge_app_id", "") or "").strip()
        self.app_secret = str(mykeys.get("fs_concierge_app_secret", "") or "").strip()
        # `owner_open_id` here is the owner's open_id under the OWNER app
        # — used by escalate worker to push cards back to owner via the
        # owner-app credentials. Do NOT use this for inbound access_check;
        # inbound events carry concierge-app-view IDs which are different.
        self.owner_open_id = str(mykeys.get("fs_concierge_owner_open_id", "") or "").strip()
        # Concierge-view open_id of the owner — used ONLY by access_check
        # to silently ignore the case where the owner accidentally DMs the
        # friend bot. Empty by default so the owner CAN test-DM their own
        # concierge bot. Set this if you want the friend bot to ignore you
        # when you message it directly.
        self.owner_open_id_on_concierge = str(
            mykeys.get("fs_concierge_owner_open_id_on_concierge", "") or ""
        ).strip()
        self.allowed_friends = _to_allowed_set(mykeys.get("fs_concierge_allowed_friends"))
        # Owner-app credentials — escalate worker needs these to send cards
        # back to the owner. Fine if missing — escalate falls back to JSONL.
        self.owner_app_id = str(mykeys.get("fs_app_id", "") or "").strip()
        self.owner_app_secret = str(mykeys.get("fs_app_secret", "") or "").strip()

        self._public_access = "*" in self.allowed_friends
        if not self.allowed_friends:
            print(
                "⚠️  bots.feishu_concierge.allowed_friends 未配置 —— 默认拒绝所有用户。\n"
                "   单个朋友：python -m launcher.config set bots.feishu_concierge.allowed_friends '[\"ou_xxx\"]'\n"
                "   公开使用（朋友 free/busy 状态会泄漏给任何陌生人）：'[\"*\"]'"
            )

        self.kernel = None
        self.agent: ConciergeAgent | None = None
        self.send_client = None      # lark client for replies (concierge app)
        self._lock = threading.Lock()

    # ── kernel + workers ──────────────────────────────────────────────

    def boot_kernel(self) -> None:
        """Spin a fresh kernel and register the five workers the
        concierge needs. Uses a fresh kernel (reset_kernel) so this
        process doesn't inherit any stray workers from earlier imports.
        """
        reset_kernel()
        self.kernel = get_kernel()
        # Read paths from config with sensible defaults so the user
        # doesn't have to set every single one.
        kb_path = (mykeys.get("fs_concierge_kb_path") or DEFAULT_KB_PATH).strip() \
            if isinstance(mykeys.get("fs_concierge_kb_path"), str) else DEFAULT_KB_PATH
        audit_path = (mykeys.get("fs_concierge_audit_path") or DEFAULT_AUDIT_PATH).strip() \
            if isinstance(mykeys.get("fs_concierge_audit_path"), str) else DEFAULT_AUDIT_PATH
        esc_path = (mykeys.get("fs_concierge_escalation_log_path") or DEFAULT_ESCALATION_LOG).strip() \
            if isinstance(mykeys.get("fs_concierge_escalation_log_path"), str) else DEFAULT_ESCALATION_LOG

        self.kernel.add_worker({
            "name": "cal", "kind": "calendar",
            "db_path": DEFAULT_CALENDAR_DB,
        })
        self.kernel.add_worker({
            "name": "concierge_kb", "kind": "concierge_kb",
            "kb_path": kb_path,
        })
        self.kernel.add_worker({
            "name": "concierge_slot", "kind": "concierge_slot",
        })
        self.kernel.add_worker({
            "name": "concierge_escalate", "kind": "concierge_escalate",
            "log_path": esc_path,
            "owner_open_id": self.owner_open_id,
            "owner_app_id": self.owner_app_id,
            "owner_app_secret": self.owner_app_secret,
        })
        self.kernel.add_worker({
            "name": "concierge_audit", "kind": "concierge_audit",
            "audit_path": audit_path,
        })

    def build_agent(self) -> ConciergeAgent:
        config = config_from_mykeys(dict(mykeys))
        sessions = SessionStore(DEFAULT_SESSION_DIR)
        # LLM augmentation. Opt-in via bots.feishu_concierge.llm_enabled.
        # Disabled by default so concierge runs free of LLM cost even when
        # the user has API keys configured for the owner bot.
        # Phase 3 (2026-05-17): when enabled, the LLM bundle is wired into
        # five hooks — smalltalk reply, intent classify (smalltalk→upgrade
        # only), schedule hint extraction, slot rendering, QA rephrasing.
        # Out_of_scope / capability gating stay rule-based; LLM never
        # bypasses the privilege boundary.
        llm_hooks = None
        llm_enabled = bool(mykeys.get("fs_concierge_llm_enabled"))
        if llm_enabled:
            llm_hooks = _build_llm_hooks()
            if llm_hooks is None:
                print("[concierge] llm_enabled=true but no LLM session built; "
                      "agent falls back to rule-based behaviour for all paths.")
            else:
                print("[concierge] LLM hooks active: chat + classify + extract_schedule "
                      "+ render_slots + render_qa")
        else:
            print("[concierge] LLM disabled (set bots.feishu_concierge.llm_enabled true to enable).")
        return ConciergeAgent(
            kernel=self.kernel,
            config=config,
            sessions=sessions,
            llm_hooks=llm_hooks,
        )

    # ── Lark send path ────────────────────────────────────────────────

    def _build_send_client(self):
        if self.send_client is not None:
            return self.send_client
        try:
            self.send_client = (
                lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge] failed to build send client: {exc!r}")
            self.send_client = None
        return self.send_client

    def send_text(self, receive_id: str, text: str,
                  receive_id_type: str = "open_id") -> bool:
        """Send a plain-text message. When the inbound message arrived in
        a group chat (``chat_type == "group"``), pass ``receive_id_type=
        "chat_id"`` so the reply lands in the same group; passing the
        sender's open_id would spawn a NEW p2p chat alongside the group,
        which is what we'd been doing pre-fix and what looked to the user
        like "bot ignored my message in the group"."""
        client = self._build_send_client()
        if client is None or not text:
            return False
        try:
            payload = json.dumps({"text": text}, ensure_ascii=False)
            body = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("text")
                    .content(payload)
                    .build()
                )
                .build()
            )
            r = client.im.v1.message.create(body)
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge] send_text raised: {exc!r}")
            return False
        if r and r.success():
            return True
        print(f"[concierge] send_text failed: code={getattr(r,'code','?')} msg={getattr(r,'msg','?')}")
        return False

    # ── inbound dispatch ──────────────────────────────────────────────

    def access_check(self, open_id: str) -> str:
        """Return reason-to-drop ("" if allowed)."""
        if not open_id:
            return "empty sender"
        if (
            self.owner_open_id_on_concierge
            and open_id == self.owner_open_id_on_concierge
        ):
            # Owner DM'd the concierge by accident — silently ignore.
            # The owner has their own (full-power) bot via fsapp.py.
            # Compares concierge-app-view IDs (matches inbound event shape).
            return "owner self-DM (ignored)"
        if not self._public_access and open_id not in self.allowed_friends:
            return "not in allowed_friends"
        return ""

    def handle_event(self, data) -> None:
        """Lark WS event callback. Runs on the SDK's event-loop thread.
        We do the heavy lifting on a worker thread so the SDK isn't
        blocked while ConciergeAgent.handle() runs.
        """
        try:
            ev = data.event
            msg = ev.message
            sender = ev.sender
            open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
            text = _parse_text(msg)
            chat_id = getattr(msg, "chat_id", None)
        except Exception as exc:  # noqa: BLE001 — SDK shape changes shouldn't kill us
            print(f"[concierge] event parse failed: {exc!r}")
            return

        reason = self.access_check(open_id)
        if reason:
            # Drop path prints the FULL open_id — owner needs to see it to
            # know who to add to allowed_friends. (This is the owner's own
            # log on the owner's own machine — not a PII leak.)
            if reason == "not in allowed_friends":
                print(
                    f"[concierge] drop {open_id} — {reason}\n"
                    f"           → 要让 ta 跟 bot 聊：\n"
                    f"             python -m launcher.config set "
                    f"bots.feishu_concierge.allowed_friends '[\"{open_id}\"]'\n"
                    f"             （多个朋友: '[\"ou_a\",\"ou_b\"]'；公开: '[\"*\"]'）\n"
                    f"           改完到 GUI Bots tab 重启「飞书·小秘书」或 taskkill 19533 端口的进程"
                )
            else:
                print(f"[concierge] drop {open_id[:12]}… — {reason}")
            return
        if not text:
            print(f"[concierge] drop {open_id[:12]}… — empty/unsupported message")
            return

        print(f"[concierge] inbound {open_id[:12]}…: {text[:60]!r}")
        threading.Thread(
            target=self._dispatch_one,
            args=(open_id, text, chat_id),
            name=f"concierge-handle-{open_id[:8]}",
            daemon=True,
        ).start()

    def _dispatch_one(self, open_id: str, text: str, chat_id: str | None) -> None:
        with self._lock:
            agent = self.agent
        if agent is None:
            print(f"[concierge] no agent; dropping {open_id[:12]}")
            return
        try:
            reply = agent.handle(open_id, text)
        except Exception as exc:  # noqa: BLE001 — never crash the WS loop
            print(f"[concierge] agent.handle crashed: {exc!r}")
            reply = "我先告诉她，回头再说。"
        if not reply:
            return
        # Reply in the same chat the message came from. Group → chat_id;
        # bare DM → open_id. Sending to open_id from a group would spawn
        # a NEW p2p chat alongside, which looks broken to the user.
        if chat_id:
            self.send_text(chat_id, reply, receive_id_type="chat_id")
        else:
            self.send_text(open_id, reply)

    # ── lifecycle ─────────────────────────────────────────────────────

    def run(self) -> None:
        if not self.app_id or not self.app_secret:
            print("ERROR: bots.feishu_concierge.app_id / app_secret 未配置。")
            print("       python -m launcher.config set bots.feishu_concierge.app_id ...")
            sys.exit(1)
        self.boot_kernel()
        self.agent = self.build_agent()
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.handle_event)
            .build()
        )
        ws = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        banner = (
            "=" * 60 + "\n"
            "飞书 Concierge 已启动（长连接模式）\n"
            f"  app_id:        {self.app_id}\n"
            f"  owner_open_id: {self.owner_open_id or '<未配置 — escalate 会降级到 JSONL>'}\n"
            f"  allowed_friends: {sorted(self.allowed_friends) if self.allowed_friends else '<deny all — 没人能聊到 bot>'}\n"
            f"  escalate live: {'yes' if (self.owner_app_id and self.owner_app_secret) else 'no (stub → JSONL)'}\n"
            "  press Ctrl+C to stop\n"
            + "=" * 60
        )
        print(banner)
        # Surface the "configured but not usable" state loudly so the user
        # doesn't watch in puzzlement while friends DM the bot and get nothing.
        if not self.allowed_friends:
            print(
                "\n⚠️  allowed_friends 为空 —— 所有朋友消息将被静默丢弃。\n"
                "    `python -m launcher.config set bots.feishu_concierge.allowed_friends '[\"ou_friend1\"]'`\n"
                "    （或 '[\"*\"]' 公开访问；改完重启此进程）\n"
            )
        if not self.owner_open_id:
            print(
                "ℹ️   owner_open_id_on_owner_app 未配 —— 朋友约时间的卡片会落到\n"
                "    temp/concierge_escalations.jsonl 而非直接弹到你飞书。\n"
                "    配完后 escalate live 模式自动启用（重启此进程）。\n"
            )
        ws.start()


def main() -> None:
    if not _acquire_single_instance():
        print("[concierge] Another instance is already running; exiting.")
        return
    # Force a fresh mykeys read in case launcher wrote new config between
    # GUI-spawn and our cwd change.
    try:
        reload_mykeys()
    except Exception:
        pass
    ConciergeApp().run()


if __name__ == "__main__":
    main()
