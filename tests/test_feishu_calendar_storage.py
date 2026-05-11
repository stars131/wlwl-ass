"""Unit tests for FeishuCalendarStorage with a mocked lark client.

We don't hit the actual Feishu API. The tests verify:
  - event ↔ lark dict round-trip preserves title/start/end/location/notes/tags
  - create / patch / delete / get / list call the right resource methods
  - failed responses raise RuntimeError with code+msg
  - tags are encoded into description and decoded back
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── helpers ─────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, *, ok: bool = True, data=None, code: int = 0,
                 msg: str = "ok"):
        self._ok = ok
        self.data = data
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return "log_test_42"


class _Box:
    """Bare object whose attribute set we control from kwargs."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _make_lark_event(*, eid="evt_1", summary="T", description="",
                     start_ts="1747983600", end_ts="1747987200",
                     location_name=None, create_time="1747900000000"):
    from lark_oapi.api.calendar.v4 import TimeInfo, EventLocation, CalendarEvent
    e = CalendarEvent()
    e.event_id = eid
    e.summary = summary
    e.description = description
    e.start_time = TimeInfo.builder().timestamp(start_ts).timezone("Asia/Shanghai").build()
    e.end_time = TimeInfo.builder().timestamp(end_ts).timezone("Asia/Shanghai").build()
    if location_name:
        loc = EventLocation()
        loc.name = location_name
        e.location = loc
    e.create_time = create_time
    return e


def _fake_client_with_create(event):
    cal_event = _Box(
        create=lambda req: _Resp(data=_Box(event=event)),
        patch=lambda req: _Resp(data=_Box(event=event)),
        delete=lambda req: _Resp(data=None),
        get=lambda req: _Resp(data=_Box(event=event)),
        list=lambda req: _Resp(data=_Box(items=[event], has_more=False, page_token=None)),
    )
    cal = _Box(calendar_event=cal_event, calendar=_Box(primary=lambda req: _Resp()))
    return _Box(calendar=_Box(v4=cal))


# ── tests ───────────────────────────────────────────────────────────


def test_event_to_lark_round_trip_preserves_fields():
    from llmcore.workers.feishu_calendar_storage import _event_to_lark, _lark_to_event
    src = {
        "title": "Standup",
        "start_at": "2026-05-10T15:00:00",
        "end_at": "2026-05-10T16:00:00",
        "location": "Room A",
        "notes": "agenda body",
        "tags": ["work", "weekly"],
    }
    lk = _event_to_lark(src)
    assert lk.summary == "Standup"
    assert lk.description.startswith("[tags:work,weekly]\n")
    assert lk.location.name == "Room A"
    # set the extras a real response would carry
    lk.event_id = "evt_xyz"
    lk.create_time = "1700000000000"
    back = _lark_to_event(lk)
    assert back["title"] == "Standup"
    assert back["start_at"] == "2026-05-10T15:00:00"
    assert back["end_at"] == "2026-05-10T16:00:00"
    assert back["location"] == "Room A"
    assert back["notes"] == "agenda body"
    assert back["tags"] == ["work", "weekly"]
    assert back["id"] == "evt_xyz"


def test_create_calls_calendar_event_create_and_returns_event_dict():
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    captured = {}

    def _create(req):
        captured["req"] = req
        # echo what was sent, plus an event_id
        sent = req.request_body
        sent.event_id = "evt_new"
        sent.create_time = "1700000000000"
        return _Resp(data=_Box(event=sent))

    cal_event = _Box(create=_create)
    cal_v4 = _Box(calendar_event=cal_event)
    client = _Box(calendar=_Box(v4=cal_v4))

    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    ev = s.create({"title": "T", "start_at": "2026-05-10T09:00:00",
                   "end_at": "2026-05-10T10:00:00", "tags": ["a"]})
    assert ev["id"] == "evt_new"
    assert ev["title"] == "T"
    assert ev["tags"] == ["a"]
    assert captured["req"].calendar_id == "cal_1"


def test_failed_response_raises_runtimeerror():
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    cal_event = _Box(create=lambda req: _Resp(ok=False, code=99991672,
                                              msg="Access denied"))
    client = _Box(calendar=_Box(v4=_Box(calendar_event=cal_event)))
    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    with pytest.raises(RuntimeError) as ei:
        s.create({"title": "T", "start_at": "2026-05-10T09:00:00",
                  "end_at": "2026-05-10T10:00:00"})
    msg = str(ei.value)
    assert "create_event" in msg
    assert "99991672" in msg
    assert "Access denied" in msg


def test_query_filters_by_q_substring_and_tags():
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    e1 = _make_lark_event(eid="e1", summary="Standup",
                          description="[tags:work,weekly]\nbody1")
    e2 = _make_lark_event(eid="e2", summary="Lunch",
                          description="[tags:social]\nbody2")
    cal_event = _Box(list=lambda req: _Resp(data=_Box(items=[e1, e2],
                                                      has_more=False,
                                                      page_token=None)))
    client = _Box(calendar=_Box(v4=_Box(calendar_event=cal_event)))
    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    only_standup = s.query(q="stand")
    assert [e["id"] for e in only_standup] == ["e1"]
    only_social = s.query(tags=["social"])
    assert [e["id"] for e in only_social] == ["e2"]


def test_query_supplies_default_window_when_no_args():
    """REGRESSION: Feishu list-events requires start_time + end_time. When
    callers (matching SQLiteStorage's contract) pass nothing, we must inject
    a wide default window so the API doesn't 400."""
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    captured = {}

    def _list(req):
        captured["req"] = req
        return _Resp(data=_Box(items=[], has_more=False, page_token=None))

    client = _Box(calendar=_Box(v4=_Box(calendar_event=_Box(list=_list))))
    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    s.query()  # no from_iso, no to_iso
    req = captured["req"]
    # Both must be set on the lark request, and must be unix-second strings
    assert req.start_time and req.start_time.isdigit()
    assert req.end_time and req.end_time.isdigit()
    # And the window should bracket "now" (start < now < end)
    import time as _t
    now = int(_t.time())
    assert int(req.start_time) < now < int(req.end_time)


def test_resolve_timezone_returns_iana_name(monkeypatch):
    """REGRESSION: tzname() on Windows-Chinese returns '中国标准时间' which
    Feishu rejects. _resolve_timezone must always return an IANA-shaped name."""
    from llmcore.workers import feishu_calendar_storage as fcs
    # Force the platform tzname() path by clearing TZ + breaking tzlocal
    monkeypatch.delenv("TZ", raising=False)
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "tzlocal", None)
    tz = fcs._resolve_timezone()
    assert "/" in tz, f"timezone {tz!r} is not IANA-shaped"


def test_delete_soft_patches_with_deleted_prefix():
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    e = _make_lark_event(eid="evt_1", summary="MeetX")
    captured = {}

    def _patch(req):
        captured["req"] = req
        return _Resp(data=_Box(event=req.request_body))

    def _get(req):
        return _Resp(data=_Box(event=e))

    cal_event = _Box(get=_get, patch=_patch)
    client = _Box(calendar=_Box(v4=_Box(calendar_event=cal_event)))
    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    r = s.delete({"by_id": "evt_1"}, soft=True)
    assert r == {"id": "evt_1", "deleted": True}
    assert captured["req"].request_body.summary.startswith("[deleted] ")


def test_delete_hard_calls_delete_endpoint():
    from llmcore.workers.feishu_calendar_storage import FeishuCalendarStorage
    e = _make_lark_event(eid="evt_1", summary="MeetX")
    called = {"delete": 0}

    def _delete(req):
        called["delete"] += 1
        assert req.event_id == "evt_1"
        return _Resp(data=None)

    def _get(req):
        return _Resp(data=_Box(event=e))

    cal_event = _Box(get=_get, delete=_delete)
    client = _Box(calendar=_Box(v4=_Box(calendar_event=cal_event)))
    s = FeishuCalendarStorage(app_id="x", app_secret="y",
                              calendar_id="cal_1", client=client)
    r = s.delete({"by_id": "evt_1"}, soft=False)
    assert r == {"id": "evt_1", "deleted": True}
    assert called["delete"] == 1


def test_factory_picks_sqlite_by_default_and_feishu_when_requested(tmp_path):
    """Factory dispatch: storage="sqlite" (default) vs storage="feishu"."""
    from llmcore.workers.calendar_worker import CalendarFactory, SQLiteCalendarStorage

    class _Handle:
        pass

    f = CalendarFactory()
    db = tmp_path / "cal.db"
    w = f.build({"name": "cal", "db_path": str(db)}, _Handle())
    assert isinstance(w._storage, SQLiteCalendarStorage)

    # When storage="feishu" but no creds in config, from_config_store raises
    # RuntimeError — factory should propagate cleanly (kernel catches it).
    with pytest.raises(Exception):
        f.build({"name": "cal2", "storage": "feishu"}, _Handle())
