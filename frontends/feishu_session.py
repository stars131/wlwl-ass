"""Feishu-specific session context and platform action routing.

The owner Feishu bot shares the generic agent core with GUI/CLI sessions, but
incoming Feishu text has platform semantics that should not be guessed by the
generic loop. This module keeps that source context explicit and handles
high-confidence Feishu-native actions before the message falls through to the
agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TASKS_DIR = os.path.join(PROJECT_ROOT, "sche_tasks")


@dataclass(frozen=True)
class FeishuSessionContext:
    open_id: str
    chat_id: str = ""
    message_id: str = ""
    message_type: str = "text"
    receive_id: str = ""
    receive_id_type: str = "open_id"
    public_access: bool = False
    attachment_paths: tuple[str, ...] = ()
    platform: str = "feishu"

    @property
    def session_key(self) -> str:
        target = self.receive_id or self.chat_id or self.open_id
        return f"feishu:{self.open_id}:{self.receive_id_type}:{target}"

    @property
    def permission_label(self) -> str:
        return "public" if self.public_access else "authorized"


@dataclass(frozen=True)
class FeishuScheduleRequest:
    matched: bool
    schedule: str = ""
    repeat: str = "once"
    text: str = ""
    missing: str = ""
    date: str = ""


def build_context(
    *,
    open_id: str,
    chat_id: str = "",
    message_id: str = "",
    message_type: str = "text",
    public_access: bool = False,
    attachment_paths: tuple[str, ...] | list[str] = (),
) -> FeishuSessionContext:
    receive_id = chat_id or open_id
    receive_id_type = "chat_id" if chat_id else "open_id"
    return FeishuSessionContext(
        open_id=open_id,
        chat_id=chat_id or "",
        message_id=message_id or "",
        message_type=message_type or "text",
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        public_access=public_access,
        attachment_paths=tuple(str(p) for p in (attachment_paths or ()) if str(p).strip()),
    )


def render_prompt(ctx: FeishuSessionContext, persona_prompt: str = "") -> str:
    """Build the Feishu-only system prompt supplement."""
    target = ctx.receive_id or ctx.open_id
    parts = [
        "\n\n# Feishu Session Context",
        f"- platform: {ctx.platform}",
        f"- source_open_id: {ctx.open_id}",
        f"- receive_id_type: {ctx.receive_id_type}",
        f"- receive_id: {target}",
        f"- chat_id: {ctx.chat_id or '(none)'}",
        f"- message_id: {ctx.message_id or '(none)'}",
        f"- permission_mode: {ctx.permission_label}",
        "",
        "## Feishu Routing Rules",
        "- In this session, words like 我, 这里, 当前会话, 发给我, 提醒我 refer to the current Feishu sender and receive target above.",
        "- If the user asks to show or send a local file in this conversation, return [FILE:path]; the Feishu frontend will upload it through the bot's Feishu permissions and sandbox.",
        "- If the user asks to send a Feishu message to an explicit recipient, use feishu_send with the Feishu open_id/chat_id and receive_id_type. Do not route Feishu requests through WeChat, GUI, or other channels.",
        "- For reminders or scheduled Feishu messages, keep delivery bound to this Feishu receive target unless the user explicitly names another target.",
    ]
    if ctx.attachment_paths:
        parts.append("- inbound_attachments: " + ", ".join(ctx.attachment_paths))
    if persona_prompt.strip():
        parts.extend(["", "# Feishu Persona", persona_prompt.strip()])
    return "\n".join(parts)


_FILE_TOKEN_RE = re.compile(r"\[FILE:([^\]]+)\]")
_SEND_TO_ME_RE = re.compile(r"(?:发|发送|传|转发).{0,8}我|把.+?(?:发|发送|传|转发)")
_PATH_RE = re.compile(
    r"(?P<path>"
    r"[A-Za-z]:[\\/][^，。；\n]+"
    r"|(?:\.{1,2}[\\/]|temp[\\/]|assets[\\/]|docs[\\/]|gui[\\/]|launcher[\\/]|tests[\\/])[^，。；\n]+"
    r"|[\w.\-/\\]+\.[A-Za-z0-9]{1,10}"
    r")"
)


def _clean_path_token(raw: str) -> str:
    token = (raw or "").strip().strip(" \t\r\n'\"`“”‘’「」《》()（）,，。；;")
    return re.sub(r"\s*(?:发给我|发送给我|传给我|转发给我|发一下|发送一下)$", "", token).strip()


def _resolve_existing_path(raw: str, base_dir: str = PROJECT_ROOT) -> str:
    token = _clean_path_token(raw)
    if not token:
        return ""
    candidate = token if os.path.isabs(token) else os.path.join(base_dir, token)
    candidate = os.path.realpath(candidate)
    return candidate if os.path.exists(candidate) else ""


def extract_requested_files(text: str, *, base_dir: str = PROJECT_ROOT) -> list[str]:
    """Return existing local paths from a high-confidence "send file to me" ask."""
    text = text or ""
    refs = list(_FILE_TOKEN_RE.findall(text))
    if not refs and not _SEND_TO_ME_RE.search(text):
        return []
    refs.extend(m.group("path") for m in _PATH_RE.finditer(text))
    seen: set[str] = set()
    out: list[str] = []
    for ref in refs:
        path = _resolve_existing_path(ref, base_dir=base_dir)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


_SCHEDULE_INTENT_RE = re.compile(r"(定时|提醒我|提醒一下|通知我|发(?:信息|消息).{0,6}我|给我发(?:信息|消息))")
_TIME_RE = re.compile(
    r"(?P<period>每天|每日|每个工作日|工作日|每周[一二三四五六日天]?|今天|明天)?\s*"
    r"(?P<ampm>凌晨|早上|上午|中午|下午|晚上|今晚)?\s*"
    r"(?:"
    r"(?P<hour>\d{1,2})(?:(?P<sep>[:：点时])(?P<minute>\d{1,2})?)?"
    r"|(?P<hour_cn>[零〇一二两三四五六七八九十]{1,3})[点时](?P<minute_cn>[零〇一二两三四五六七八九十]{1,3})?(?P<half>半)?"
    r")\s*(?:分)?"
)


def _repeat_from_period(period: str) -> str:
    if period in {"每天", "每日"}:
        return "daily"
    if period in {"每个工作日", "工作日"}:
        return "weekday"
    if period.startswith("每周"):
        return "weekly"
    return "once"


def _date_from_period(period: str) -> str:
    if period == "今天":
        return datetime.now().strftime("%Y-%m-%d")
    if period == "明天":
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return ""


def _zh_int(text: str) -> int | None:
    if not text:
        return None
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return digits.get(text)
    return None


def _normalise_time(match: re.Match[str]) -> str:
    if match.group("hour_cn"):
        parsed_hour = _zh_int(match.group("hour_cn") or "")
        parsed_minute = _zh_int(match.group("minute_cn") or "") if match.group("minute_cn") else None
        if parsed_hour is None:
            return ""
        hour = parsed_hour
        minute = 30 if match.group("half") else int(parsed_minute or 0)
    else:
        if not match.group("sep") and not match.group("ampm") and not match.group("period"):
            return ""
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
    ampm = match.group("ampm") or ""
    if ampm in {"下午", "晚上", "今晚"} and 1 <= hour < 12:
        hour += 12
    if ampm == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _extract_schedule_text(text: str, time_match: re.Match[str]) -> str:
    after_time = text[time_match.end():]
    for sep in ("：", ":"):
        if sep in after_time:
            tail = after_time.rsplit(sep, 1)[-1].strip()
            if tail:
                return tail
    tail = after_time.strip(" ，,。；;")
    tail = re.sub(
        r"^(?:定时|提醒我|提醒一下|通知我|给我发(?:信息|消息)|发(?:信息|消息)?给我|说|内容是|消息是)+",
        "",
        tail,
    ).strip(" ，,。；;")
    return tail


def parse_schedule_request(text: str) -> FeishuScheduleRequest:
    text = (text or "").strip()
    if not _SCHEDULE_INTENT_RE.search(text):
        return FeishuScheduleRequest(False)
    time_match = _TIME_RE.search(text)
    if not time_match:
        return FeishuScheduleRequest(True, missing="time")
    schedule = _normalise_time(time_match)
    if not schedule:
        return FeishuScheduleRequest(True, missing="time")
    message = _extract_schedule_text(text, time_match)
    if not message:
        period = time_match.group("period") or ""
        return FeishuScheduleRequest(True, schedule=schedule, repeat=_repeat_from_period(period), missing="text", date=_date_from_period(period))
    period = time_match.group("period") or ""
    repeat = _repeat_from_period(period)
    return FeishuScheduleRequest(True, schedule=schedule, repeat=repeat, text=message, date=_date_from_period(period))


def register_feishu_schedule(
    ctx: FeishuSessionContext,
    req: FeishuScheduleRequest,
    *,
    tasks_dir: str = DEFAULT_TASKS_DIR,
) -> tuple[str, str]:
    if not req.schedule or not req.text:
        raise ValueError("schedule and text are required")
    os.makedirs(tasks_dir, exist_ok=True)
    seed = f"{time.time()}|{ctx.session_key}|{req.schedule}|{req.repeat}|{req.text}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    task_id = f"feishu_{req.repeat}_{req.schedule.replace(':', '')}_{digest}"
    path = os.path.join(tasks_dir, f"{task_id}.json")
    payload = {
        "schedule": req.schedule,
        "repeat": req.repeat,
        "enabled": True,
        "max_delay_hours": 1,
        "action": "feishu_send",
        "to": ctx.receive_id or ctx.open_id,
        "receive_id_type": ctx.receive_id_type,
        "text": req.text,
        "source": {
            "platform": "feishu",
            "open_id": ctx.open_id,
            "chat_id": ctx.chat_id,
            "message_id": ctx.message_id,
        },
    }
    if req.date:
        payload["date"] = req.date
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return task_id, path


def maybe_handle_platform_action(
    text: str,
    ctx: FeishuSessionContext,
    *,
    send_text: Callable[[str], None],
    send_file: Callable[[str], bool],
    tasks_dir: str = DEFAULT_TASKS_DIR,
    base_dir: str = PROJECT_ROOT,
) -> bool:
    files = extract_requested_files(text, base_dir=base_dir)
    if files:
        ok = 0
        for path in files:
            if send_file(path):
                ok += 1
        send_text(f"已按当前飞书会话处理文件发送：成功 {ok}/{len(files)} 个。")
        return True

    req = parse_schedule_request(text)
    if not req.matched:
        return False
    if req.missing:
        send_text("请补充定时消息的时间和内容，例如：每天 09:00 定时给我发信息：早安。")
        return True
    task_id, _ = register_feishu_schedule(ctx, req, tasks_dir=tasks_dir)
    target = "当前飞书会话" if ctx.receive_id_type == "chat_id" else "你的飞书"
    date_part = f" {req.date}" if req.date else ""
    send_text(f"已登记飞书定时消息：{req.repeat}{date_part} {req.schedule} 发送到{target}。任务 ID: {task_id}")
    return True
