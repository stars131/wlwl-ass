from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest import mock
from datetime import datetime, timedelta


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_prompt_includes_feishu_source_context():
    from frontends.feishu_session import build_context, render_prompt

    ctx = build_context(
        open_id="ou_user",
        chat_id="oc_group",
        message_id="om_msg",
        public_access=False,
        attachment_paths=("temp/a.png",),
    )

    prompt = render_prompt(ctx, "persona text")

    assert "Feishu Session Context" in prompt
    assert "source_open_id: ou_user" in prompt
    assert "receive_id_type: chat_id" in prompt
    assert "receive_id: oc_group" in prompt
    assert "persona text" in prompt
    assert "feishu_send" in prompt


def test_file_request_routes_to_current_feishu_sender(tmp_path):
    from frontends.feishu_session import build_context, maybe_handle_platform_action

    f = tmp_path / "report.txt"
    f.write_text("ok", encoding="utf-8")
    sent_files: list[str] = []
    sent_texts: list[str] = []
    ctx = build_context(open_id="ou_user")

    handled = maybe_handle_platform_action(
        f"把 {f} 发给我",
        ctx,
        send_text=sent_texts.append,
        send_file=lambda p: sent_files.append(p) or True,
        base_dir=str(tmp_path),
        tasks_dir=str(tmp_path / "tasks"),
    )

    assert handled is True
    assert sent_files == [str(f.resolve())]
    assert "成功 1/1" in sent_texts[-1]


def test_schedule_request_registers_feishu_chat_task(tmp_path):
    from frontends.feishu_session import build_context, maybe_handle_platform_action

    tasks_dir = tmp_path / "tasks"
    sent_texts: list[str] = []
    ctx = build_context(open_id="ou_user", chat_id="oc_group", message_id="om_msg")

    handled = maybe_handle_platform_action(
        "每天 09:00 定时给我发信息：早安",
        ctx,
        send_text=sent_texts.append,
        send_file=lambda _p: False,
        tasks_dir=str(tasks_dir),
    )

    assert handled is True
    task_files = list(tasks_dir.glob("feishu_daily_0900_*.json"))
    assert len(task_files) == 1
    data = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert data["action"] == "feishu_send"
    assert data["to"] == "oc_group"
    assert data["receive_id_type"] == "chat_id"
    assert data["text"] == "早安"
    assert data["source"]["open_id"] == "ou_user"
    assert "daily 09:00" in sent_texts[-1]


def test_tomorrow_schedule_registers_due_date(tmp_path):
    from frontends.feishu_session import build_context, maybe_handle_platform_action

    tasks_dir = tmp_path / "tasks"
    ctx = build_context(open_id="ou_user")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    handled = maybe_handle_platform_action(
        "明天早上九点提醒我提交周报",
        ctx,
        send_text=lambda _text: None,
        send_file=lambda _p: False,
        tasks_dir=str(tasks_dir),
    )

    assert handled is True
    task_files = list(tasks_dir.glob("feishu_once_0900_*.json"))
    assert len(task_files) == 1
    data = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert data["date"] == tomorrow
    assert data["schedule"] == "09:00"
    assert data["text"] == "提交周报"


def test_schedule_parser_does_not_treat_item_count_as_time():
    from frontends.feishu_session import parse_schedule_request

    req = parse_schedule_request("提醒我买 3 个苹果")

    assert req.matched is True
    assert req.missing == "time"


def test_feishu_send_tool_passes_receive_id_type(monkeypatch):
    from wlwl_ass import WlwlAssHandler

    calls = []

    def fake_send(to, text, *, files=None, receive_id_type="open_id"):
        calls.append((to, text, files, receive_id_type))
        return "sent"

    monkeypatch.setattr("tools.feishu.feishu_send", fake_send)
    handler = WlwlAssHandler(parent=object())
    gen = handler.do_feishu_send(
        {"to": "oc_group", "text": "hello", "receive_id_type": "chat_id", "files": ["a.txt"]},
        response=None,
    )

    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        outcome = stop.value

    assert calls == [("oc_group", "hello", ["a.txt"], "chat_id")]
    assert outcome.data == "sent"
    assert any("[feishu_send]" in c for c in chunks)


def test_scheduler_direct_feishu_action_writes_done_report(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_SCHEDULER_NO_LOCK", "1")
    import reflect.scheduler as scheduler

    scheduler = importlib.reload(scheduler)
    task_dir = tmp_path / "tasks"
    done_dir = task_dir / "done"
    task_dir.mkdir()
    done_dir.mkdir()
    monkeypatch.setattr(scheduler, "TASKS", str(task_dir))
    monkeypatch.setattr(scheduler, "DONE", str(done_dir))
    monkeypatch.setattr(scheduler, "_l4_t", 10**12)

    (task_dir / "feishu_due.json").write_text(
        json.dumps(
            {
                "schedule": "00:00",
                "repeat": "once",
                "enabled": True,
                "action": "feishu_send",
                "to": "oc_group",
                "receive_id_type": "chat_id",
                "text": "hello",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with mock.patch("tools.feishu.feishu_send", return_value="sent") as send:
        result = scheduler.check()

    assert result is None
    send.assert_called_once_with("oc_group", "hello", files=None, receive_id_type="chat_id")
    reports = list(done_dir.glob("*_feishu_due.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "[动作] feishu_send" in report
    assert "[结果] sent" in report


def test_scheduler_skips_future_dated_feishu_action(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_SCHEDULER_NO_LOCK", "1")
    import reflect.scheduler as scheduler

    scheduler = importlib.reload(scheduler)
    task_dir = tmp_path / "tasks"
    done_dir = task_dir / "done"
    task_dir.mkdir()
    done_dir.mkdir()
    monkeypatch.setattr(scheduler, "TASKS", str(task_dir))
    monkeypatch.setattr(scheduler, "DONE", str(done_dir))
    monkeypatch.setattr(scheduler, "_l4_t", 10**12)

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    (task_dir / "feishu_future.json").write_text(
        json.dumps(
            {
                "date": tomorrow,
                "schedule": "00:00",
                "repeat": "once",
                "enabled": True,
                "action": "feishu_send",
                "to": "ou_user",
                "text": "hello",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with mock.patch("tools.feishu.feishu_send") as send:
        result = scheduler.check()

    assert result is None
    send.assert_not_called()
    assert list(done_dir.glob("*.md")) == []
