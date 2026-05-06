"""IntentClassifier — turns a free-text utterance into a kernel-routable
``required_capabilities`` + structured args.

Strategy: prompt a chat-completion worker (via the kernel) with the user's
utterance + the registered candidate capabilities, and ask for JSON output.
Falls through to ``_chat`` if no intent fits.

For tests / no-LLM environments, ``RuleIntentClassifier`` provides a
deterministic regex-based fallback covering calendar + inspiration patterns.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("wlwl_ass.voice.intent")


@dataclass
class IntentResult:
    intent: str
    confidence: float
    args: dict
    fallback_reply: str | None = None


CHAT_INTENT = "_chat"


_CAL_CREATE_RE = re.compile(
    r"(?:增加|加|新建|创建|添加|提醒|约|安排|约见|加个|加一个|今天|明天|后天)"
    r".{0,80}(?:开会|约|拿|取|做|去|会议|拜访|出差|安排|提醒)|"
    r"(?:tomorrow|today|next week).{0,80}(?:meet|call|do|fetch|pick|attend|remind)",
    re.IGNORECASE,
)

_CAL_UPDATE_RE = re.compile(
    r"(?:把|将|改|挪|移到|改成|推迟|提前|延后|改到)",
    re.IGNORECASE,
)

_CAL_DELETE_RE = re.compile(
    r"(?:删除|取消|去掉|不要)",
    re.IGNORECASE,
)

_CAL_QUERY_RE = re.compile(
    r"(?:今天|明天|本周|这周|这周末|什么|哪些|有什么).{0,80}(?:安排|日程|要做|事情|计划|会议)",
    re.IGNORECASE,
)

_INSP_RECORD_RE = re.compile(
    r"(?:记一下|记下来|记录|帮我记|存一下|写下来|笔记|灵感).{0,200}",
    re.IGNORECASE,
)

_INSP_QUERY_RE = re.compile(
    r"(?:找一下|搜索|查一下|找个|关于).{0,80}(?:笔记|灵感|记录|想法)",
    re.IGNORECASE,
)


class RuleIntentClassifier:
    """Deterministic, no-LLM intent classifier.

    Covers the MVP intents: create / update / delete / query calendar events,
    record / query inspirations. Anything else returns ``_chat``.
    """

    def classify(self, utterance: str) -> IntentResult:
        u = utterance.strip()
        if not u:
            return IntentResult(CHAT_INTENT, 0.0, {})
        # Order matters: more specific patterns first.
        if _CAL_DELETE_RE.search(u):
            return IntentResult("calendar.delete_event.v1", 0.7,
                                {"match": {"by_title_substring": _extract_target(u)}})
        if _CAL_QUERY_RE.search(u):
            return IntentResult("calendar.query_events.v1", 0.6, _extract_query_args(u))
        if _CAL_UPDATE_RE.search(u):
            return IntentResult("calendar.update_event.v1", 0.7,
                                {"match": {"by_title_substring": _extract_target(u)},
                                 "patch": _extract_patch(u)})
        if _CAL_CREATE_RE.search(u):
            return IntentResult("calendar.create_event.v1", 0.7, _extract_create_args(u))
        if _INSP_RECORD_RE.match(u):
            return IntentResult("inspiration.record.v1", 0.75, {"text": u})
        if _INSP_QUERY_RE.search(u):
            return IntentResult("inspiration.query.v1", 0.65,
                                {"q": _extract_target(u), "limit": 10})
        return IntentResult(CHAT_INTENT, 0.0, {})


def _extract_target(u: str) -> str:
    # take chars between common verbs and the next verb/time word
    m = re.search(
        r"(?:把|删除|取消|找|关于)"
        r"([\u4e00-\u9fa5A-Za-z0-9 ]{1,20}?)"
        r"(?:改到|改成|挪到|推迟|提前|延后|删除|取消|的|了|从|在|$)",
        u,
    )
    if m and m.group(1).strip():
        return m.group(1).strip()
    m2 = re.search(r"(?:把|删除|取消|找|关于)([\u4e00-\u9fa5A-Za-z0-9 ]{1,20})", u)
    if m2:
        return m2.group(1).strip()
    return u[:20]


def _extract_patch(u: str) -> dict:
    # detect "改到 X 点" / "推迟到 …"
    m = re.search(r"(?:改到|改成|挪到|推迟到|提前到|改至)([\u4e00-\u9fa5A-Za-z0-9:点时分: ]{1,20})", u)
    if m:
        new_t = m.group(1).strip()
        iso = _parse_chinese_time(new_t)
        if iso:
            return {"start_at": iso}
    return {}


def _extract_create_args(u: str) -> dict:
    iso = _parse_chinese_time(u)
    title = re.sub(r"(明天|今天|后天|下午|上午|早上|晚上|[0-9]+点(?:[0-9]+分?)?|提醒我?)", "", u).strip()
    return {"title": title or "提醒", "start_at": iso or _today_iso()}


def _extract_query_args(u: str) -> dict:
    today = _today_iso()
    if "明天" in u:
        return {"from": _add_days(today, 1), "to": _add_days(today, 1)}
    if "本周" in u or "这周" in u:
        return {"from": today, "to": _add_days(today, 7)}
    return {"from": today, "to": today}


def _today_iso() -> str:
    import datetime as _dt
    return _dt.date.today().isoformat()


def _add_days(iso_date: str, n: int) -> str:
    import datetime as _dt
    d = _dt.date.fromisoformat(iso_date)
    return (d + _dt.timedelta(days=n)).isoformat()


def _parse_chinese_time(s: str) -> str | None:
    """Very small time parser for Chinese MVP utterances.

    Handles things like "明天下午三点", "今天晚上8点半". ISO 8601 offset
    omitted (consumer treats local time). Returns None if no match.
    """
    import datetime as _dt
    today = _dt.date.today()
    if "明天" in s:
        d = today + _dt.timedelta(days=1)
    elif "后天" in s:
        d = today + _dt.timedelta(days=2)
    elif "今天" in s:
        d = today
    else:
        d = today
    period_offset = 0
    if "下午" in s or "晚上" in s:
        period_offset = 12
    elif "中午" in s:
        period_offset = 12
    m = re.search(r"([0-9一二三四五六七八九十]+)[点时](半|[0-9]+分?)?", s)
    if not m:
        return None
    h = _cn_int(m.group(1))
    if h is None:
        return None
    if 1 <= h <= 11 and period_offset == 12:
        h += 12
    minute = 0
    rest = m.group(2) or ""
    if rest == "半":
        minute = 30
    elif rest:
        m2 = re.search(r"([0-9]+)", rest)
        if m2:
            minute = int(m2.group(1))
    return f"{d.isoformat()}T{h:02d}:{minute:02d}:00"


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    table = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s in table:
        return table[s]
    if s.startswith("十") and len(s) == 2:
        return 10 + table.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return table.get(s[0], 0) * 10
    if "十" in s and len(s) == 3:
        a, b = s.split("十", 1)
        return table.get(a, 0) * 10 + table.get(b, 0)
    return None


# ── LLM-backed classifier (uses kernel.dispatch via async route) ─────────


class LLMIntentClassifier:
    """Production classifier — asks a chat worker via the kernel to return
    JSON-shaped intent. Falls back to RuleIntentClassifier on any failure.
    """

    def __init__(self, kernel, capabilities: list[dict] | None = None) -> None:
        self._kernel = kernel
        self._caps = capabilities or _default_candidates()
        self._fallback = RuleIntentClassifier()

    def classify(self, utterance: str) -> IntentResult:
        try:
            resp = self._kernel.dispatch(
                capability="chat.intent_classify.v1",
                payload={"utterance": utterance, "candidates": self._caps},
                deadline_ms=8_000,
            )
            if resp.ok and resp.result:
                r = resp.result
                return IntentResult(
                    intent=r.get("intent", CHAT_INTENT),
                    confidence=float(r.get("confidence", 0.0)),
                    args=r.get("args") or {},
                    fallback_reply=r.get("fallback_reply"),
                )
        except Exception:
            log.exception("LLM intent classifier failed; falling back to rules")
        return self._fallback.classify(utterance)


def _default_candidates() -> list[dict]:
    """Snapshot of routable capabilities, hand-edited for the voice MVP."""
    return [
        {"id": "calendar.create_event.v1", "desc": "Add a calendar event"},
        {"id": "calendar.update_event.v1", "desc": "Modify an existing event"},
        {"id": "calendar.delete_event.v1", "desc": "Cancel/delete an event"},
        {"id": "calendar.query_events.v1", "desc": "List events in a date range"},
        {"id": "inspiration.record.v1",   "desc": "Save a note / inspiration"},
        {"id": "inspiration.query.v1",    "desc": "Search saved notes"},
    ]
