"""ConciergeAgent — friend-facing restricted agent for the Feishu concierge bot.

The agent is **not** a ReAct/tool-using LLM agent. It is a deterministic
state machine with an LLM-optional intent classifier and an LLM-optional
reply renderer. Both default to rule-based behavior so the agent can run
fully offline for tests.

Privilege model: ``CONCIERGE_CAPABILITIES`` is a hard 6-item tuple.
``handle(...)`` dispatches **only** through capabilities in this set. The
absence of ``code_run`` / ``file_write`` etc. is not a prompt rule — it
is the lack of a worker behind those names. Even if a jailbreaking
friend convinces the LLM to "use the file_write tool", the agent's code
path never makes that dispatch.

See `docs/specs/feishu-concierge-bot.md` for the full design.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

log = logging.getLogger("wlwl_ass.concierge")


# ── Allowlisted capabilities ───────────────────────────────────────────

CONCIERGE_CAPABILITIES: tuple[str, ...] = (
    "calendar.query_events.v1",
    "concierge.propose_slot.v1",
    "calendar.create_event.v1",     # gated by owner approval (Phase 2)
    "concierge.kb_answer.v1",
    "concierge.escalate.v1",
    "concierge.audit.v1",
)


# ── Intent classifier (rule-based, no LLM in v1) ───────────────────────

INTENT_SCHEDULE = "schedule"
INTENT_SCHEDULE_PICK = "schedule_pick"
INTENT_QA = "qa"
INTENT_SMALLTALK = "smalltalk"
INTENT_OUT_OF_SCOPE = "out_of_scope"
INTENT_ESCALATE_NOW = "escalate_now"
INTENT_CONFIRM = "confirm"
INTENT_CANCEL = "cancel"


_SCHEDULE_RE = re.compile(
    r"(?:见个面|约|约个|约一下|聚|吃饭|喝咖啡|聊聊|聊一下|meet|coffee|lunch|dinner|"
    r"碰个面|碰面|有空吗|方便吗|有时间吗|能不能见|什么时候有空|什么时候方便|"
    r"周[一二三四五六日天]|下周|这周|本周|今晚|明天|后天)",
    re.IGNORECASE,
)
_PICK_RE = re.compile(
    r"^\s*(?:第?[123一二三]个?|第一|第二|第三|"
    r"周[一二三四五六日天].{0,8}行|周[一二三四五六日天].{0,8}可以|"
    r"\d{1,2}[点:：]\d{0,2}\s*(?:行|可以|OK|ok|好|好的)|"
    r"行|可以|好的|OK|ok)\s*$"
)
_DURATION_RE = re.compile(r"(\d+)\s*(分钟|小时|min|hour|hours?)", re.IGNORECASE)
_TIME_OF_DAY_RE = re.compile(
    r"(早上|早晨|上午|中午|下午|傍晚|晚上|半夜|morning|afternoon|evening|night)",
    re.IGNORECASE,
)
_QA_QUERY_RE = re.compile(
    r"(?:在哪|哪里|哪个|哪家|怎么|什么时候|多少|多大|几点|几号|"
    r"是谁|是不是|是吗|有没有|有吗|工作|公司|学校|住哪|哪上班|什么职业)",
    re.IGNORECASE,
)
_ESCALATE_NOW_RE = re.compile(
    r"(?:告诉她|告诉他|跟她说|跟他说|帮我转告|转达|让她联系|让他联系|"
    r"急事|紧急|emergency)",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(r"^\s*(?:确认|定了|就这样|OK|ok|好|好的|可以|行|对)\s*$")
_CANCEL_RE = re.compile(r"(?:算了|不约了|取消|不用了|改天|下次)")
_OUT_OF_SCOPE_RE = re.compile(
    r"(?:写代码|爬虫|python|代码|帮我.{0,5}查|帮我.{0,5}下载|破解|"
    r"密码|身份证|银行卡|银行账号|信用卡|余额|工资|薪水|彩礼|彩礼钱)",
    re.IGNORECASE,
)
_CHINESE_DIGITS = {"一": 1, "二": 2, "三": 3}


@dataclass
class IntentClassification:
    kind: str
    payload: dict = field(default_factory=dict)


# ── LLM hook bundle ────────────────────────────────────────────────────
#
# Phase 3 (2026-05-17): the agent can call LLMs in four places beyond
# smalltalk. Every hook is optional; every hook gracefully degrades to
# the rule-based path on failure. The privilege boundary stays the same:
# the LLM **never** decides whether something is out_of_scope, and the
# LLM **never** dispatches outside ``CONCIERGE_CAPABILITIES``. The LLM
# can only enrich a path the rules already opened.

# llm_chat: (user_text, system_prompt) -> reply_text | None
LLMChat = Callable[[str, str], "str | None"]
# llm_classify: (text, session_snapshot) -> {"intent": str, "payload": dict} | None
LLMClassify = Callable[[str, dict], "dict | None"]
# llm_extract_schedule: (text, session_snapshot) -> {"duration_minutes": int, "preferred_window": str, "topic_summary": str} | None
LLMExtractSchedule = Callable[[str, dict], "dict | None"]
# llm_render_slots: (slots, topic_summary, friend_alias) -> reply_text | None
LLMRenderSlots = Callable[[list, str, str], "str | None"]
# llm_render_qa: (question, kb_text, topic) -> reply_text | None
LLMRenderQA = Callable[[str, str, str], "str | None"]


@dataclass
class LLMHooks:
    """Optional LLM callables. ``None`` = unavailable → fall back to rules.

    Each hook is independent; a deployment can wire any subset. Production
    wiring lives in ``frontends/fsapp_concierge.py:_build_llm_hooks``.
    """
    chat: LLMChat | None = None
    classify: LLMClassify | None = None
    extract_schedule: LLMExtractSchedule | None = None
    render_slots: LLMRenderSlots | None = None
    render_qa: LLMRenderQA | None = None


# ── Circuit breaker for LLM hooks ─────────────────────────────────────
#
# Symptom we're guarding against: the LLM endpoint goes down (network
# blip, quota hit, model deprecated). Each hook call then waits the full
# request timeout (10-60s) before falling back to rules. For a friend
# typing in Feishu, this turns a snappy bot into a frozen one.
#
# Breaker behaviour (per-hook):
#   - ``failures`` counts consecutive exceptions / None returns.
#   - At ``FAILURE_THRESHOLD`` consecutive failures, breaker OPENS:
#     subsequent calls within ``COOLDOWN_S`` short-circuit to None
#     (caller falls back to rules immediately, no LLM round-trip).
#   - After ``COOLDOWN_S``, one trial call is allowed (half-open). If
#     it succeeds, breaker CLOSES and resets failures. If it fails,
#     breaker stays OPEN for another ``COOLDOWN_S``.

_BREAKER_FAILURE_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 60.0


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float = 0.0     # 0 = closed

    def is_open(self, now: float) -> bool:
        if self.opened_at == 0.0:
            return False
        return (now - self.opened_at) < _BREAKER_COOLDOWN_S

    def record_failure(self, now: float) -> None:
        self.failures += 1
        # Once we hit the threshold, refresh ``opened_at`` on every
        # subsequent failure too — otherwise a failed half-open trial
        # leaves opened_at pointing at the original failure, and the
        # next call (still within cooldown of the trial, but past
        # cooldown of the original open) would slip through.
        if self.failures >= _BREAKER_FAILURE_THRESHOLD:
            self.opened_at = now

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0


# LLM may upgrade a rule-based SMALLTALK to one of these; anything else
# is discarded (including OUT_OF_SCOPE — that path stays rule-only).
_LLM_ALLOWED_UPGRADES: frozenset[str] = frozenset({
    "schedule", "qa", "escalate_now", "cancel",
})
_LLM_ALLOWED_WINDOWS: frozenset[str] = frozenset({
    "morning", "afternoon", "evening", "any",
})


def classify_intent(text: str, session: "FriendSession") -> IntentClassification:
    """Deterministic rule-based intent classifier. Returns one of the
    INTENT_* constants. Order matters — out_of_scope checked FIRST so
    "帮我写代码" doesn't drift into smalltalk.
    """
    text = (text or "").strip()
    if not text:
        return IntentClassification(INTENT_SMALLTALK)
    if _OUT_OF_SCOPE_RE.search(text):
        return IntentClassification(INTENT_OUT_OF_SCOPE)
    if session.in_flight_intent == INTENT_SCHEDULE:
        # Currently negotiating a schedule — look for picks / confirms /
        # cancels before treating new text as a fresh intent.
        if _CANCEL_RE.search(text):
            return IntentClassification(INTENT_CANCEL)
        pick = _parse_pick(text, session)
        if pick is not None:
            return IntentClassification(INTENT_SCHEDULE_PICK, {"pick_index": pick})
    if _ESCALATE_NOW_RE.search(text):
        return IntentClassification(INTENT_ESCALATE_NOW)
    if _SCHEDULE_RE.search(text):
        return IntentClassification(INTENT_SCHEDULE, _parse_schedule_hints(text))
    if _CONFIRM_RE.match(text):
        # bare "ok" / "好的" outside a schedule flow — treat as smalltalk
        return IntentClassification(INTENT_SMALLTALK)
    if _QA_QUERY_RE.search(text):
        return IntentClassification(INTENT_QA)
    return IntentClassification(INTENT_SMALLTALK)


def _parse_pick(text: str, session: "FriendSession") -> int | None:
    m = _PICK_RE.match(text)
    if m is None:
        return None
    raw = m.group(0).strip()
    # explicit ordinal
    for word, idx in [("第一", 0), ("第二", 1), ("第三", 2),
                      ("1", 0), ("2", 1), ("3", 2)]:
        if word in raw:
            if idx < len(session.proposed_slots):
                return idx
    for ch, n in _CHINESE_DIGITS.items():
        if ch in raw and n - 1 < len(session.proposed_slots):
            return n - 1
    # bare "可以"/"行" → first option
    if session.proposed_slots:
        return 0
    return None


def _parse_schedule_hints(text: str) -> dict:
    out: dict[str, Any] = {}
    m = _DURATION_RE.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("小时") or unit.startswith("hour"):
            out["duration_minutes"] = n * 60
        else:
            out["duration_minutes"] = n
    m = _TIME_OF_DAY_RE.search(text)
    if m:
        word = m.group(1).lower()
        if word in ("早上", "早晨", "上午", "morning"):
            out["preferred_window"] = "morning"
        elif word in ("中午", "下午", "afternoon"):
            out["preferred_window"] = "afternoon"
        elif word in ("傍晚", "晚上", "半夜", "evening", "night"):
            out["preferred_window"] = "evening"
    out["topic_summary"] = text.strip()[:60]
    return out


# ── Per-friend session ────────────────────────────────────────────────

@dataclass
class FriendSession:
    open_id: str
    friend_alias: str = ""
    in_flight_intent: str = ""
    proposed_slots: list[dict] = field(default_factory=list)
    topic_summary: str = ""
    escalation_id: str = ""
    last_msg_ts: float = 0.0
    msg_count_window: list[float] = field(default_factory=list)
    state: str = "NEW"

    def to_dict(self) -> dict:
        return {
            "open_id": self.open_id,
            "friend_alias": self.friend_alias,
            "in_flight_intent": self.in_flight_intent,
            "proposed_slots": list(self.proposed_slots),
            "topic_summary": self.topic_summary,
            "escalation_id": self.escalation_id,
            "last_msg_ts": self.last_msg_ts,
            "msg_count_window": list(self.msg_count_window),
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FriendSession":
        return cls(
            open_id=d.get("open_id", ""),
            friend_alias=d.get("friend_alias", ""),
            in_flight_intent=d.get("in_flight_intent", ""),
            proposed_slots=list(d.get("proposed_slots") or []),
            topic_summary=d.get("topic_summary", ""),
            escalation_id=d.get("escalation_id", ""),
            last_msg_ts=float(d.get("last_msg_ts") or 0.0),
            msg_count_window=list(d.get("msg_count_window") or []),
            state=d.get("state", "NEW"),
        )


class SessionStore:
    """One JSON file per friend under ``state_dir/<open_id>.json``.

    File-per-friend instead of a single SQLite — most friends will send
    <5 messages a week and the hot set is tiny. Per-friend files also
    make debugging trivial (cat one to see the state).
    """

    def __init__(self, state_dir: str) -> None:
        self._dir = state_dir
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.RLock()

    def _path_for(self, open_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", open_id)[:64] or "unknown"
        return os.path.join(self._dir, f"{safe}.json")

    def load(self, open_id: str) -> FriendSession:
        with self._lock:
            try:
                with open(self._path_for(open_id), "r", encoding="utf-8") as f:
                    return FriendSession.from_dict(json.load(f))
            except FileNotFoundError:
                return FriendSession(open_id=open_id)
            except (ValueError, OSError) as exc:
                # JSON corruption, permission flip, file truncated mid-write,
                # filesystem error — never crash inbound message handling
                # on a bad session file. Treat as fresh session and continue.
                log.warning("SessionStore.load(%s) failed (%r) — using fresh session",
                            open_id, exc)
                return FriendSession(open_id=open_id)

    def save(self, sess: FriendSession) -> bool:
        """Persist the session. Returns True on success, False on failure.

        Failures (disk full, permission, transient FS hiccup) are logged
        but never raised — losing one session save is preferable to
        crashing the reply path mid-conversation. The next inbound message
        will see the LAST successfully-saved state, which is at worst one
        turn out of date.
        """
        with self._lock:
            tmp = self._path_for(sess.open_id) + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(sess.to_dict(), f, ensure_ascii=False)
                os.replace(tmp, self._path_for(sess.open_id))
                return True
            except OSError as exc:
                log.warning("SessionStore.save(%s) failed: %r", sess.open_id, exc)
                # Clean up the tmp file if it exists — otherwise it leaks.
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False


# ── Per-bot structured persona ────────────────────────────────────────
#
# Each LLM-augmented bot has a structured persona that composes the
# system prompt sent to its LLM. Four optional fields, each a free-form
# string; if any is empty it's skipped. The order is stable so prompts
# stay cache-friendly.
#
# Default values give a usable concierge persona out of the box. The
# user overrides any field via ``bots.feishu_concierge.bot_*`` config
# (see ``launcher/config_store.py:bot_env_map``).
#
# Legacy ``persona`` (single-string) is still honoured — when set, it
# takes the place of ``bot_identity`` (the first section).

_DEFAULT_PERSONA_IDENTITY = (
    "你是小 W，Alice（owner）的私人小秘书。性格温和、靠谱、不啰嗦。"
    "你替 Alice 接听朋友们发到飞书的消息——你是 Alice 的助理，不是 Alice 本人。"
)

_DEFAULT_PERSONA_PROJECT_SUMMARY = (
    "你住在 Alice 的 wlwl-ass 项目里，是其中一个长连飞书机器人。"
    "Alice 自己还有另一个机器人（owner bot）给她本人用工具做事，你跟它是分开的——"
    "owner bot 那边的能力（写代码、改文件、跑命令）你都没有。"
)

_DEFAULT_PERSONA_CAPABILITY_SUMMARY = (
    "你能做的事：\n"
    "- 帮朋友查 Alice 的日历空闲，提议 2-3 个可约的时段\n"
    "- 回答 Alice 允许公开的几个小问题（比如她在哪上班、最近忙啥）\n"
    "- 朋友想紧急联系 Alice 时，把口信替她转告\n"
    "- 简短的寒暄\n"
    "你不能做的事（朋友求也不行）：\n"
    "- 跑代码、写代码、查资料、做技术辅导\n"
    "- 透露 Alice 的住址、电话、身份证、银行账号、收入这类隐私\n"
    "- 替 Alice 答应/决定她还没本人确认过的事\n"
    "- 假装代表 Alice 给承诺"
)

_DEFAULT_PERSONA_STYLE_GUIDELINES = (
    "回复风格：\n"
    "- 中文为主，1-3 句话\n"
    "- 口语化、亲切但不过分热情\n"
    "- 不要 emoji 罗列、不要 markdown 代码块、不要重复对方刚说的话\n"
    "- 不知道就直说\"我帮你问问她\"，不要瞎编"
)


@dataclass
class BotPersonaConfig:
    """Structured persona fields. All optional — empty fields are skipped
    when composing the LLM system prompt. Sensible defaults are filled
    in by :func:`config_from_mykeys` so a bare-bones config still
    produces a usable persona."""
    identity: str = ""
    project_summary: str = ""
    capability_summary: str = ""
    style_guidelines: str = ""
    extra_instructions: str = ""

    def compose(self) -> str:
        """Render the persona into a single system-prompt string. Sections
        are separated by blank lines; empty sections are omitted entirely."""
        parts = [self.identity, self.project_summary,
                 self.capability_summary, self.style_guidelines,
                 self.extra_instructions]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())




@dataclass
class RateLimit:
    window_s: int = 60
    max_msgs: int = 8


class GlobalRateLimiter:
    def __init__(self, window_s: int = 60, max_msgs: int = 60) -> None:
        self._window_s = window_s
        self._max = max_msgs
        self._times: list[float] = []
        self._lock = threading.Lock()

    def hit(self) -> bool:
        now = time.time()
        with self._lock:
            self._times = [t for t in self._times if now - t < self._window_s]
            if len(self._times) >= self._max:
                return True
            self._times.append(now)
            return False


# ── ConciergeAgent ────────────────────────────────────────────────────

@dataclass
class ConciergeConfig:
    owner_open_id_on_owner_app: str = ""
    # Legacy single-string persona. Still honoured for backward compat —
    # if set, takes the place of ``persona_config.identity``. Empty by
    # default; the structured ``persona_config`` below carries the real
    # defaults.
    persona: str = ""
    persona_config: BotPersonaConfig = field(default_factory=BotPersonaConfig)
    topics_allowed: list[str] = field(default_factory=list)
    working_hours: dict = field(default_factory=lambda: {
        "weekday": ["09:00-12:00", "14:00-18:00"],
        "weekend": [],
    })
    meeting_buffer_min: int = 15
    default_meeting_minutes: int = 60
    rate_limit_per_friend: RateLimit = field(default_factory=RateLimit)
    rate_limit_global_window_s: int = 60
    rate_limit_global_max_msgs: int = 60
    exit_phrase: str = "拜拜"
    out_of_scope_reply: str = (
        "抱歉，我只能帮我的朋友处理日程和回答她允许的几个问题。"
        "这件事你可能想直接问 Claude 或 Codex～"
    )
    rate_limited_reply: str = "稍等一下，让我跟她确认下"


class ConciergeAgent:
    """Friend-facing restricted agent.

    Dependencies are injected so tests can run with mock workers.
    Production wiring: ``kernel = llmcore.kernel.get_kernel()``;
    workers are registered via ``kernel.add_worker(...)`` in
    ``frontends/fsapp_concierge.py`` startup.
    """

    CAPABILITIES = CONCIERGE_CAPABILITIES

    def __init__(
        self, *,
        kernel: Any,
        config: ConciergeConfig,
        sessions: SessionStore,
        global_limiter: GlobalRateLimiter | None = None,
        now_fn: Callable[[], float] = time.time,
        llm_chat: Callable[[str, str], str | None] | None = None,
        llm_hooks: LLMHooks | None = None,
    ) -> None:
        self.kernel = kernel
        self.config = config
        self.sessions = sessions
        self.global_limiter = global_limiter or GlobalRateLimiter(
            window_s=config.rate_limit_global_window_s,
            max_msgs=config.rate_limit_global_max_msgs,
        )
        self._now = now_fn
        # Compose final hook bundle. Backward-compat: callers passing only
        # the legacy ``llm_chat`` keyword still get smalltalk LLM support;
        # the four newer hooks default to None (rule-based behaviour).
        hooks = llm_hooks or LLMHooks()
        if llm_chat is not None and hooks.chat is None:
            hooks = LLMHooks(
                chat=llm_chat,
                classify=hooks.classify,
                extract_schedule=hooks.extract_schedule,
                render_slots=hooks.render_slots,
                render_qa=hooks.render_qa,
            )
        self._llm = hooks
        # ``_llm_chat`` is retained for backward-compat with code that
        # introspected the old attribute.
        self._llm_chat = hooks.chat
        # Per-hook circuit breakers (see _CircuitState). Closed at startup.
        self._llm_breaker: dict[str, _CircuitState] = {
            name: _CircuitState() for name in
            ("chat", "classify", "extract_schedule", "render_slots", "render_qa")
        }

    # ── public surface ─────────────────────────────────────────────────

    def handle(self, open_id: str, text: str, *,
               friend_alias: str = "",
               progress: "Callable[[str], None] | None" = None) -> str:
        """Process one inbound message; return the reply text. Empty
        string means "do not reply" (rate-limit drop or noise).

        ``progress`` is an optional callable that the agent calls with a
        short interim message BEFORE slow operations (LLM calls, worker
        dispatches that may take >1s). The caller is responsible for
        actually delivering the interim message to the user (e.g. via
        ``app.send_text`` in fsapp_concierge). If ``progress`` is None,
        nothing is sent — agent runs silently like Phase 2.
        """
        t0 = self._now()
        sess = self.sessions.load(open_id)
        if friend_alias:
            sess.friend_alias = friend_alias
        self._progress = progress  # per-turn; reset after handle() returns

        self._audit(sess, kind="inbound", in_text=text)

        if self.global_limiter.hit():
            self._audit(sess, kind="rate_limited_global", in_text=text)
            return self.config.rate_limited_reply
        if self._per_friend_rate_limited(sess):
            self._audit(sess, kind="rate_limited_friend", in_text=text)
            return self.config.rate_limited_reply

        # exit phrase short-circuit
        if self.config.exit_phrase and self.config.exit_phrase in text:
            sess.in_flight_intent = ""
            sess.proposed_slots = []
            sess.state = "CLOSED"
            self.sessions.save(sess)
            reply = "好哒，回头再聊～"
            self._audit(sess, kind="outbound", out_text=reply,
                        latency_ms=(self._now() - t0) * 1000)
            return reply

        intent = classify_intent(text, sess)
        # LLM may *upgrade* a rule-based SMALLTALK verdict to schedule/qa/
        # escalate_now/cancel — but only that direction. Out_of_scope and
        # any other rule-firm verdict are honoured as-is. Rationale: the
        # rule layer is the security gate; the LLM is here to make the
        # bot feel smarter, not to override the gate.
        if intent.kind == INTENT_SMALLTALK and self._llm.classify is not None:
            upgraded = self._llm_classify(text, sess)
            if upgraded is not None:
                intent = upgraded
        self._audit(sess, kind="intent", intent=intent.kind, extras=intent.payload)

        try:
            reply, caps_used, new_state = self._route_intent(sess, intent, text)
        except Exception as exc:  # noqa: BLE001 — never crash the loop on a friend message
            log.exception("concierge route crashed for %s", open_id)
            self._audit(sess, kind="error", extras={"error": repr(exc)})
            reply, caps_used, new_state = ("我先帮你转告她，等回复。", [], "CLOSED")

        state_before = sess.state
        sess.state = new_state
        sess.last_msg_ts = self._now()
        self.sessions.save(sess)
        self._audit(sess, kind="outbound", out_text=reply,
                    state_before=state_before, state_after=new_state,
                    capabilities_used=caps_used,
                    latency_ms=(self._now() - t0) * 1000)
        self._progress = None  # clear per-turn callback
        return reply

    def _emit_progress(self, msg: str) -> None:
        """Send an interim 'thinking' message via the per-turn callback,
        if one was injected. Swallows all failures — never crashes the
        reply path on a progress-delivery error."""
        cb = getattr(self, "_progress", None)
        if cb is None or not msg:
            return
        try:
            cb(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("progress callback failed: %r", exc)

    # ── intent routing ─────────────────────────────────────────────────

    def _route_intent(
        self, sess: FriendSession, intent: IntentClassification, text: str,
    ) -> tuple[str, list[str], str]:
        if intent.kind == INTENT_OUT_OF_SCOPE:
            return self.config.out_of_scope_reply, [], "CLOSED"

        if intent.kind == INTENT_CANCEL:
            sess.in_flight_intent = ""
            sess.proposed_slots = []
            return "好的，那就先这样～需要的时候再说。", [], "CLOSED"

        if intent.kind == INTENT_SCHEDULE:
            return self._handle_schedule(sess, intent.payload, text)

        if intent.kind == INTENT_SCHEDULE_PICK:
            return self._handle_schedule_pick(sess, intent.payload, text)

        if intent.kind == INTENT_QA:
            return self._handle_qa(sess, text)

        if intent.kind == INTENT_ESCALATE_NOW:
            return self._handle_escalate(sess, text)

        # smalltalk fallback
        reply, caps_used = self._smalltalk_reply_with_audit(sess, text)
        return reply, caps_used, "CLOSED"

    def _handle_schedule(
        self, sess: FriendSession, hints: dict, text: str,
    ) -> tuple[str, list[str], str]:
        # User-visible "I'm working on it" before the LLM+worker chain.
        # Schedule turns can take 5-15s (extract_schedule LLM + slot worker
        # + render_slots LLM); a silent wait that long feels broken to a
        # friend chatting in Feishu.
        self._emit_progress("稍等，我帮你看下她的日历~")
        # LLM may enrich hints (duration / preferred_window / topic). Any
        # validation failure → fall back to rule hints, never crash.
        if self._llm.extract_schedule is not None:
            enriched = self._llm_extract_schedule(text, sess)
            if enriched is not None:
                hints = {**hints, **enriched}
        duration = int(hints.get("duration_minutes") or self.config.default_meeting_minutes)
        preferred = str(hints.get("preferred_window") or "any")
        topic = str(hints.get("topic_summary") or text[:60])

        now = datetime.now()
        earliest = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        latest = earliest + timedelta(days=7)
        resp = self.kernel.dispatch(
            capability="concierge.propose_slot.v1",
            payload={
                "duration_minutes": duration,
                "earliest": earliest.isoformat(),
                "latest":   latest.isoformat(),
                "preferred_window": preferred,
                "working_hours": self.config.working_hours,
                "buffer_min": self.config.meeting_buffer_min,
                "max_slots": 3,
            },
            deadline_ms=8_000,
        )
        if not resp.ok or not (resp.result or {}).get("slots"):
            # No slots → escalate immediately
            return self._handle_escalate(sess, text, summary=topic)

        slots = list(resp.result["slots"])
        sess.in_flight_intent = INTENT_SCHEDULE
        sess.proposed_slots = slots
        sess.topic_summary = topic
        caps = ["concierge.propose_slot.v1"]
        # LLM may rephrase the slot proposal. Output must still mention
        # every slot — if validation fails the deterministic template fires.
        rendered = None
        if self._llm.render_slots is not None:
            rendered = self._llm_render_slots(slots, topic, sess.friend_alias)
        if rendered is None:
            rendered = self._render_slots(slots)
        else:
            caps.append("_llm.render_slots")
        return rendered, caps, "PROPOSE_SLOTS"

    def _handle_schedule_pick(
        self, sess: FriendSession, payload: dict, text: str,
    ) -> tuple[str, list[str], str]:
        idx = int(payload.get("pick_index", 0))
        if not sess.proposed_slots or idx >= len(sess.proposed_slots):
            return "我把哪个时段记错了吗？要不你直接说个具体的时间？", [], "PROPOSE_SLOTS"
        chosen = sess.proposed_slots[idx]
        self._emit_progress("好的，正在告诉她~")
        # escalate to owner for approval — calendar.create_event happens
        # AFTER owner taps Confirm (Phase 2). For Phase 1, just escalate.
        esc_resp = self.kernel.dispatch(
            capability="concierge.escalate.v1",
            payload={
                "kind": "schedule_request",
                "friend_open_id": sess.open_id,
                "friend_alias": sess.friend_alias or "朋友",
                "summary": sess.topic_summary or "约个时间",
                "proposed_slot": chosen,
            },
            deadline_ms=5_000,
        )
        caps = ["concierge.escalate.v1"]
        if esc_resp.ok and (esc_resp.result or {}).get("escalation_id"):
            sess.escalation_id = esc_resp.result["escalation_id"]
            sess.in_flight_intent = ""
            sess.proposed_slots = []
            start = chosen.get("start", "")
            return (f"OK，我把'{sess.topic_summary or '约时间'}'发给她了"
                    f"（{start}）。她确认后我会回复你。"), caps, "AWAIT_OWNER"
        return "刚才发不出去，我等会再试一下，先这样。", caps, "PROPOSE_SLOTS"

    def _handle_qa(
        self, sess: FriendSession, text: str,
    ) -> tuple[str, list[str], str]:
        self._emit_progress("稍等，让我想想~")
        resp = self.kernel.dispatch(
            capability="concierge.kb_answer.v1",
            payload={"q": text, "topics_allowed": list(self.config.topics_allowed)},
            deadline_ms=3_000,
        )
        caps = ["concierge.kb_answer.v1"]
        if not resp.ok:
            return "嗯…我不太确定，要不我帮你转告她？", caps, "CLOSED"
        r = resp.result or {}
        if not r.get("matched"):
            # KB miss — offer to relay
            return "这个我也不太清楚哎。要不我帮你转告她？", caps, "CLOSED"
        if not r.get("in_allowlist"):
            # Matched but topic gated → polite refusal
            return ("这个我不能直接告诉你，需要她本人回复你。"
                    "我帮你转告？"), caps, "CLOSED"
        text_out = str(r.get("text") or "").strip()
        if not text_out:
            return "嗯，让我帮你问问她。", caps, "CLOSED"
        # LLM may rephrase the KB text in a friendly tone. The hook is
        # given ONLY the matched KB string; if it fabricates / mentions
        # restricted info the validator (``_llm_render_qa``) clips it back
        # to the KB text. Worst case: rule-based KB summary fires.
        if self._llm.render_qa is not None:
            rendered = self._llm_render_qa(text, text_out, str(r.get("topic") or ""))
            if rendered:
                return rendered, caps + ["_llm.render_qa"], "CLOSED"
        return text_out, caps, "CLOSED"

    def _handle_escalate(
        self, sess: FriendSession, text: str, *, summary: str = "",
    ) -> tuple[str, list[str], str]:
        self._emit_progress("稍等，我转告她~")
        resp = self.kernel.dispatch(
            capability="concierge.escalate.v1",
            payload={
                "kind": "manual_relay",
                "friend_open_id": sess.open_id,
                "friend_alias": sess.friend_alias or "朋友",
                "summary": summary or text[:120],
            },
            deadline_ms=5_000,
        )
        caps = ["concierge.escalate.v1"]
        if resp.ok and (resp.result or {}).get("escalation_id"):
            sess.escalation_id = resp.result["escalation_id"]
            return "好，我帮你转告她，她看到会回复你。", caps, "AWAIT_OWNER"
        return "我等会再帮你说一下，稍等。", caps, "CLOSED"

    def _smalltalk_reply_with_audit(
        self, sess: FriendSession, text: str,
    ) -> tuple[str, list[str]]:
        """Smalltalk path. Tries LLM first (if injected), falls back to the
        rule-based template. Returns (reply, capabilities_used) so the audit
        row records whether LLM was actually called this turn.

        The LLM hook is ONLY consulted here — out_of_scope / schedule / qa /
        escalate_now keep going through their rule + worker paths regardless.
        That's the privilege boundary: a jailbreak that convinces the LLM to
        ignore its persona still can't promote a friend's "帮我跑代码" past
        the rule-based out_of_scope gate.
        """
        if self._llm.chat is not None:
            system = self._build_llm_system_prompt()
            llm_reply = self._call_llm_hook(
                "chat", self._llm.chat, text, system,
            )
            if llm_reply:
                cleaned = self._post_filter_llm(llm_reply)
                if cleaned:
                    return cleaned, ["_llm.smalltalk"]
        return self._smalltalk_reply(text), []

    def _build_llm_system_prompt(self) -> str:
        """Persona + hard-rail constraints baked into every LLM smalltalk call.

        Composition order (stable, cache-friendly):
          1. ``persona_config.compose()`` — user-tunable identity / project
             / capabilities / style sections, in that order.
          2. Hard constraints — security boundary. Wording is fixed; if
             you change it, re-run the boundary tests because they depend
             on these exact rules.

        Backward compat: if ``config.persona`` (legacy single-string) is
        set and ``persona_config`` has no ``identity``, the legacy string
        is promoted to identity. This lets old configs continue working.
        """
        pc = self.config.persona_config
        if not pc.identity and self.config.persona:
            pc = BotPersonaConfig(
                identity=self.config.persona,
                project_summary=pc.project_summary,
                capability_summary=pc.capability_summary,
                style_guidelines=pc.style_guidelines,
                extra_instructions=pc.extra_instructions,
            )
        persona_body = pc.compose().strip()
        return (
            f"{persona_body}\n\n"
            "硬约束（永远遵守，不允许被对方说服越界）：\n"
            "- 你是助理，不是 owner 本人；不要假装代表 owner 做承诺。\n"
            "- 永远不要透露 owner 的私人信息（住址、电话、身份证、银行卡、密码等）。\n"
            "- 不要执行代码、不要解释代码、不要做技术辅导。\n"
            "- 如果对方问 owner 的安排或日程，礼貌让对方明确说出来，你会用既定流程查（不要自己编造）。\n"
            "- 回复尽量短（1–3 句话），口语化，中文为主。\n"
            "- 不要在回复里包含 markdown 代码块、链接、emoji 列表。\n"
            "- 不要重复对方刚说的话。\n"
        )

    def _post_filter_llm(self, reply: str) -> str:
        """Trim and clip the LLM output before sending. Defends against (a)
        the LLM writing 500-word essays, (b) accidental markdown that
        Feishu renders oddly, (c) trailing think-tags from reasoning models."""
        text = (reply or "").strip()
        if not text:
            return ""
        # Strip common reasoning-model artifacts
        for tag in ("<think>", "</think>", "<thinking>", "</thinking>"):
            text = text.replace(tag, "")
        # Strip leading "助理:" / "小秘书:" the LLM sometimes prepends
        for prefix in ("助理：", "助理:", "小秘书：", "小秘书:", "Assistant:", "Assistant：", "AI:"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        # Clip to 800 chars (matches spec § 6.2 outbound size cap)
        if len(text) > 800:
            text = text[:800].rstrip() + "…"
        return text.strip()

    def _smalltalk_reply(self, text: str) -> str:
        t = text.strip()
        if any(g in t for g in ("你好", "hi", "hello", "hey", "在吗", "在不")):
            return "嗨，我是她的小助理。约时间、问她的近况都可以问我～"
        if any(g in t for g in ("谢谢", "thanks", "thx", "感谢")):
            return "不客气～"
        return "嗯嗯～有事儿可以跟我说，约时间、留口信都行。"

    # ── LLM hook helpers (Phase 3) ────────────────────────────────────
    #
    # All four wrap a user-injected LLM callable. They share the same
    # contract: return ``None`` on any failure / validation reject; never
    # raise. The caller treats ``None`` as "LLM declined" and falls back
    # to the deterministic rule path.

    def _call_llm_hook(self, hook_name: str, hook_fn: Callable, *args) -> Any:
        """Invoke an LLM hook through its circuit breaker. Returns the
        hook's raw return value, or ``None`` if the breaker is OPEN
        (short-circuit) or the call raised. Updates breaker state on
        every call so transient failures don't keep paying latency."""
        breaker = self._llm_breaker.get(hook_name)
        now = self._now()
        if breaker is not None and breaker.is_open(now):
            log.debug("llm[%s] breaker OPEN (failures=%d), skipping",
                      hook_name, breaker.failures)
            return None
        try:
            result = hook_fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.exception("llm[%s] raised: %s", hook_name, exc)
            if breaker is not None:
                breaker.record_failure(self._now())
            return None
        if result is None:
            # We treat None as a soft failure — the LLM responded but
            # the output was unusable (parse fail, etc.). Count it for
            # the breaker so we don't keep paying latency on a dead
            # endpoint that returns empty / malformed responses fast.
            if breaker is not None:
                breaker.record_failure(self._now())
        else:
            if breaker is not None:
                breaker.record_success()
        return result

    def _llm_classify(
        self, text: str, sess: FriendSession,
    ) -> IntentClassification | None:
        """LLM upgrade pass for rule-classified SMALLTALK. Returns a new
        IntentClassification if (and only if) the LLM produced an
        upgrade we recognise; returns None otherwise.

        Security: the LLM cannot upgrade to OUT_OF_SCOPE — that path is
        rule-only and already ran before this hook. Anything outside
        ``_LLM_ALLOWED_UPGRADES`` is rejected; the rule verdict stands.
        """
        if self._llm.classify is None:
            return None
        raw = self._call_llm_hook("classify", self._llm.classify, text, sess.to_dict())
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("intent") or "").strip().lower()
        if kind not in _LLM_ALLOWED_UPGRADES:
            return None
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        return IntentClassification(kind=kind, payload=payload)

    def _llm_extract_schedule(
        self, text: str, sess: FriendSession,
    ) -> dict | None:
        """Ask LLM to extract clean schedule hints from free-form text.
        Validates: duration 5..480 min; preferred_window in allowlist;
        topic_summary non-empty and ≤120 chars. Drops anything else."""
        if self._llm.extract_schedule is None:
            return None
        raw = self._call_llm_hook(
            "extract_schedule", self._llm.extract_schedule, text, sess.to_dict(),
        )
        if not isinstance(raw, dict):
            return None
        out: dict[str, Any] = {}
        dur = raw.get("duration_minutes")
        try:
            if dur is not None:
                d = int(dur)
                if 5 <= d <= 480:
                    out["duration_minutes"] = d
        except (TypeError, ValueError):
            pass
        win = str(raw.get("preferred_window") or "").strip().lower()
        if win in _LLM_ALLOWED_WINDOWS:
            out["preferred_window"] = win
        topic = str(raw.get("topic_summary") or "").strip()
        if topic:
            out["topic_summary"] = topic[:120]
        return out or None

    def _llm_render_slots(
        self, slots: list[dict], topic: str, friend_alias: str,
    ) -> str | None:
        """Ask LLM to rephrase the slot proposal. Validates: every slot
        start time appears in the rendered text. If any slot is dropped
        the LLM output is rejected and the deterministic template fires.
        """
        if self._llm.render_slots is None or not slots:
            return None
        raw = self._call_llm_hook(
            "render_slots", self._llm.render_slots,
            list(slots), topic, friend_alias,
        )
        if not isinstance(raw, str):
            return None
        cleaned = self._post_filter_llm(raw)
        if not cleaned:
            return None
        # Every slot's start ISO must appear (verbatim or just the HH:MM
        # piece). Otherwise the LLM is hiding options from the friend.
        for s in slots:
            start = str(s.get("start") or "")
            if not start:
                continue
            hhmm = start[11:16] if len(start) >= 16 else start
            if hhmm and hhmm not in cleaned and start not in cleaned:
                return None
        return cleaned

    def _llm_render_qa(
        self, question: str, kb_text: str, topic: str,
    ) -> str | None:
        """Ask LLM to rephrase a KB hit in a friendlier tone. The LLM is
        given the KB summary as its ONLY information source — it must
        not introduce new facts. We can't enforce "no new facts" perfectly,
        so we clip the output length and post-filter common artifacts.
        """
        if self._llm.render_qa is None or not kb_text:
            return None
        raw = self._call_llm_hook(
            "render_qa", self._llm.render_qa, question, kb_text, topic,
        )
        if not isinstance(raw, str):
            return None
        cleaned = self._post_filter_llm(raw)
        if not cleaned:
            return None
        # Defensive clip: a QA paraphrase should not balloon past 3x KB
        # text (heuristic for "the model went off on a tangent").
        max_len = max(len(kb_text) * 3, 240)
        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len].rstrip() + "…"
        return cleaned

    # ── helpers ────────────────────────────────────────────────────────

    def _render_slots(self, slots: list[dict]) -> str:
        if not slots:
            return "她最近这周排得有点满，我帮你问问她哪天方便？"
        lines = ["好呀，我帮她看了下日历，这几个时段可以：\n"]
        for i, s in enumerate(slots, 1):
            start = s.get("start", "")
            end = s.get("end", "")
            lines.append(f"{i}. {_friendly_when(start, end)}")
        lines.append("\n你方便哪个？或者你想约的时间发给我也行。")
        return "\n".join(lines)

    def _per_friend_rate_limited(self, sess: FriendSession) -> bool:
        now = self._now()
        window = self.config.rate_limit_per_friend.window_s
        max_msgs = self.config.rate_limit_per_friend.max_msgs
        sess.msg_count_window = [t for t in sess.msg_count_window if now - t < window]
        if len(sess.msg_count_window) >= max_msgs:
            return True
        sess.msg_count_window.append(now)
        return False

    def _audit(self, sess: FriendSession, **fields: Any) -> None:
        """Best-effort audit. Failures are swallowed — auditing must never
        kill the reply path."""
        try:
            payload = {
                "friend_open_id": sess.open_id,
                **fields,
            }
            self.kernel.dispatch(
                capability="concierge.audit.v1",
                payload=payload,
                deadline_ms=2_000,
            )
        except Exception:
            log.exception("audit dispatch failed")


def _friendly_when(start_iso: str, end_iso: str) -> str:
    """Render '2026-05-21T19:00:00+08:00' → '周三 5/21 19:00–20:00'."""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else None
    except ValueError:
        return f"{start_iso} – {end_iso}"
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = weekdays[s.weekday()]
    date_part = f"{wd} {s.month}/{s.day}"
    start_t = s.strftime("%H:%M")
    if e is None:
        return f"{date_part} {start_t}"
    end_t = e.strftime("%H:%M")
    return f"{date_part} {start_t}–{end_t}"


# ── Config bridge: mykeys dict → ConciergeConfig ─────────────────────

def _coerce_rate_limit(value: Any, default: RateLimit) -> RateLimit:
    """mykeys ships RateLimit as a dict {"window_s": int, "max_msgs": int}.
    Tolerate missing keys / wrong types by falling back to defaults."""
    if not isinstance(value, dict):
        return default
    try:
        return RateLimit(
            window_s=int(value.get("window_s", default.window_s)),
            max_msgs=int(value.get("max_msgs", default.max_msgs)),
        )
    except (TypeError, ValueError):
        return default


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def config_from_mykeys(mykeys: dict) -> ConciergeConfig:
    """Build a ConciergeConfig from the flattened mykeys dict written by
    config_store.synthesize_mykeys_from_store(). All fields fall back
    to dataclass defaults when absent / malformed, so missing config
    never crashes the agent — it just runs with conservative defaults.

    Persona fields (Phase 4, 2026-05-18) are read from individual mykeys
    if set, else from the legacy single-string ``fs_concierge_persona``,
    else from the module-level _DEFAULT_PERSONA_* constants. This lets a
    user override just the bits they care about without re-writing the
    whole persona.
    """
    base = ConciergeConfig()
    wh = mykeys.get("fs_concierge_working_hours")
    if not isinstance(wh, dict):
        wh = dict(base.working_hours)
    rl_global = mykeys.get("fs_concierge_rate_limit_global")
    if not isinstance(rl_global, dict):
        rl_global = {}

    # Resolve the structured persona. Each of the 4 + 1 fields is
    # optional; missing fields fall back to the curated default.
    legacy_persona = str(mykeys.get("fs_concierge_persona", "") or "").strip()
    identity_cfg = str(mykeys.get("fs_concierge_bot_identity", "") or "").strip()
    if not identity_cfg:
        identity_cfg = legacy_persona or _DEFAULT_PERSONA_IDENTITY
    persona_config = BotPersonaConfig(
        identity=identity_cfg,
        project_summary=str(
            mykeys.get("fs_concierge_bot_project_summary", "") or ""
        ).strip() or _DEFAULT_PERSONA_PROJECT_SUMMARY,
        capability_summary=str(
            mykeys.get("fs_concierge_bot_capability_summary", "") or ""
        ).strip() or _DEFAULT_PERSONA_CAPABILITY_SUMMARY,
        style_guidelines=str(
            mykeys.get("fs_concierge_bot_style_guidelines", "") or ""
        ).strip() or _DEFAULT_PERSONA_STYLE_GUIDELINES,
        extra_instructions=str(
            mykeys.get("fs_concierge_bot_extra_instructions", "") or ""
        ).strip(),
    )

    return ConciergeConfig(
        owner_open_id_on_owner_app=str(
            mykeys.get("fs_concierge_owner_open_id", "") or ""
        ).strip(),
        persona=legacy_persona,           # kept for back-compat introspection
        persona_config=persona_config,
        topics_allowed=_coerce_str_list(mykeys.get("fs_concierge_topics_allowed")),
        working_hours=wh,
        meeting_buffer_min=_coerce_int(
            mykeys.get("fs_concierge_meeting_buffer_min"),
            base.meeting_buffer_min,
        ),
        default_meeting_minutes=_coerce_int(
            mykeys.get("fs_concierge_default_meeting_minutes"),
            base.default_meeting_minutes,
        ),
        rate_limit_per_friend=_coerce_rate_limit(
            mykeys.get("fs_concierge_rate_limit_per_friend"),
            base.rate_limit_per_friend,
        ),
        rate_limit_global_window_s=_coerce_int(
            rl_global.get("window_s"), base.rate_limit_global_window_s,
        ),
        rate_limit_global_max_msgs=_coerce_int(
            rl_global.get("max_msgs"), base.rate_limit_global_max_msgs,
        ),
        exit_phrase=str(
            mykeys.get("fs_concierge_exit_phrase", base.exit_phrase)
            or base.exit_phrase
        ),
        out_of_scope_reply=str(
            mykeys.get("fs_concierge_out_of_scope_reply", base.out_of_scope_reply)
            or base.out_of_scope_reply
        ),
    )
