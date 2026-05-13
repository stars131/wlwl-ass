from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_parse_uitars_box_coordinates():
    from tools.gui_operator import parse_action, parsed_action_to_dict

    parsed = parse_action(
        "click(start_box='[100, 200, 300, 400]')",
        screen_width=1920,
        screen_height=1080,
    )

    assert parsed.action_type == "click"
    assert parsed.start_coords == (384, 324)
    assert parsed_action_to_dict(parsed)["start_coords"] == [384, 324]


def test_parse_point_alias_and_multiple_actions():
    from tools.gui_operator import parse_actions

    parsed = parse_actions(
        "Thought: focus then type\nAction: click(point='<point>500 500</point>')\n\n"
        "type(content='hello\\n')",
        screen_width=1000,
        screen_height=800,
    )

    assert [p.action_type for p in parsed] == ["click", "type"]
    assert parsed[0].start_coords == (500, 400)
    assert parsed[1].action_inputs["content"] == "hello\\n"


def test_parse_repeated_action_markers_and_code_fence():
    from tools.gui_operator import parse_prediction

    actions = parse_prediction(
        "Thought: two steps\n"
        "Action: ```\nclick(start_box='[100,100,100,100]')\n```\n"
        "Action: type(content='ok')"
    )

    assert actions == [
        "click(start_box='[100,100,100,100]')",
        "type(content='ok')",
    ]


def test_parse_bbox_alias():
    from tools.gui_operator import parse_action

    parsed = parse_action(
        "click(bbox='<bbox>250 250 750 750</bbox>')",
        screen_width=200,
        screen_height=100,
    )

    assert parsed.start_coords == (100, 50)


def test_execute_dry_run_has_no_side_effects(monkeypatch):
    from tools.gui_operator import execute_desktop_action

    monkeypatch.setenv("WLWL_ACTIVITY_LOG_OFF", "1")
    result = execute_desktop_action(
        "drag(start_box='[100,100,100,100]', end_box='[900,900,900,900]')",
        screen_width=1000,
        screen_height=1000,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["parsed"]["start_coords"] == [100, 100]
    assert result["parsed"]["end_coords"] == [900, 900]


def test_execute_defaults_to_dry_run(monkeypatch):
    from tools.gui_operator import execute_desktop_action

    monkeypatch.setenv("WLWL_ACTIVITY_LOG_OFF", "1")
    result = execute_desktop_action(
        "click(start_box='[500,500,500,500]')",
        screen_width=1000,
        screen_height=1000,
    )

    assert result["status"] == "dry_run"


def test_run_visual_task_dry_run_loop(monkeypatch, tmp_path):
    from tools import gui_operator

    monkeypatch.setenv("WLWL_ACTIVITY_LOG_OFF", "1")
    monkeypatch.setattr(gui_operator, "_project_root", lambda: tmp_path)

    def fake_observe(**kwargs):
        return {
            "status": "success",
            "target": "desktop",
            "path": str(tmp_path / "screen.png"),
            "width": 1000,
            "height": 800,
            "logical_width": 1000,
            "logical_height": 800,
            "scale_factor": 1,
        }

    monkeypatch.setattr(gui_operator, "observe_desktop", fake_observe)
    monkeypatch.setattr(
        gui_operator,
        "_predict_action_with_vision",
        lambda image_path, *, prompt, backend: "click(start_box='[500,500,500,500]')",
    )

    result = gui_operator.run_visual_task("click the center", max_loop=3)

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert len(result["steps"]) == 1
    assert result["steps"][0]["parsed"]["start_coords"] == [500, 400]


def test_tool_schema_json_contains_gui_operator():
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "assets", "tools_schema.json"), encoding="utf-8") as f:
        tools = json.load(f)
    by_name = {item["function"]["name"]: item["function"] for item in tools}
    names = set(by_name)
    assert "gui_operator" in names
    action_schema = by_name["gui_operator"]["parameters"]["properties"]["action"]
    assert "run" in action_schema["enum"]
    assert "browser_operator" in names


def test_browser_operator_uses_dom_when_structured():
    from tools.browser_hybrid_operator import run_browser_task

    visual_calls = []

    result = run_browser_task(
        "click submit",
        scan_func=lambda **kwargs: {"text": "Submit " * 30},
        visual_run_func=lambda *args, **kwargs: visual_calls.append((args, kwargs)) or {"status": "dry_run"},
    )

    assert result["strategy"] == "dom"
    assert result["status"] == "success"
    assert visual_calls == []


def test_browser_operator_falls_back_to_visual_for_sparse_dom():
    from tools.browser_hybrid_operator import run_browser_task

    result = run_browser_task(
        "click canvas button",
        scan_func=lambda **kwargs: {"text": ""},
        visual_run_func=lambda instruction, **kwargs: {"status": "dry_run", "instruction": instruction},
    )

    assert result["strategy"] == "visual"
    assert result["status"] == "dry_run"
    assert "structured text too short" in result["decision"]["reason"]


def test_gui_operator_runtime_tracks_dry_run(monkeypatch, tmp_path):
    from launcher.gui_operator_runtime import GuiOperatorRegistry
    from tools import gui_operator

    monkeypatch.setenv("WLWL_ACTIVITY_LOG_OFF", "1")
    monkeypatch.setattr(gui_operator, "_project_root", lambda: tmp_path)

    def fake_observe(**kwargs):
        return {
            "status": "success",
            "target": "desktop",
            "path": str(tmp_path / "screen.png"),
            "width": 1000,
            "height": 800,
            "logical_width": 1000,
            "logical_height": 800,
            "scale_factor": 1,
        }

    monkeypatch.setattr(gui_operator, "observe_desktop", fake_observe)
    monkeypatch.setattr(
        gui_operator,
        "_predict_action_with_vision",
        lambda image_path, *, prompt, backend: "click(start_box='[500,500,500,500]')",
    )

    registry = GuiOperatorRegistry()
    run = registry.start(instruction="click center", max_loop=2, include_base64=False)
    deadline = time.time() + 5
    while run.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.02)

    assert run.status == "dry_run"
    assert run.steps
    assert run.run_dir.endswith(run.run_id)
    assert str(tmp_path) in run.run_dir
    assert registry.get(run.run_id) is run


def test_trajectory_exports_html_with_screenshot(tmp_path):
    from launcher import trajectory

    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"not really png")
    day = tmp_path / "2026-05-12.jsonl"
    events = [
        {"ts": "2026-05-12T00:00:00.000Z", "phase": "turn_end", "turn": 1, "summary": "start"},
        {
            "ts": "2026-05-12T00:00:01.000Z",
            "phase": "gui_step",
            "target": "desktop",
            "action": "click",
            "status": "dry_run",
            "screenshot_path": str(screenshot),
            "width": 100,
            "height": 80,
            "scale_factor": 1,
        },
    ]
    day.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    out = tmp_path / "replay" / "trajectory.html"

    result = trajectory.export_html(activity_dir=str(tmp_path), output_path=str(out))

    assert result["count"] == 1
    html = out.read_text(encoding="utf-8")
    assert "GUI Trajectory Replay" in html
    assert "shot.png" in html


def test_trajectory_keeps_gui_steps(tmp_path):
    from launcher import trajectory

    day = tmp_path / "2026-05-12.jsonl"
    events = [
        {"ts": "2026-05-12T00:00:00.000Z", "phase": "turn_end", "turn": 1, "summary": "start"},
        {
            "ts": "2026-05-12T00:00:01.000Z",
            "phase": "gui_step",
            "target": "desktop",
            "action": "observe",
            "status": "success",
            "screenshot_path": "temp/gui_runs/a.png",
            "width": 100,
            "height": 80,
            "scale_factor": 1,
        },
    ]
    day.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    runs = trajectory.iter_runs(activity_dir=str(tmp_path), include_args=True)

    assert len(runs) == 1
    assert any(ev.get("phase") == "gui_step" for ev in runs[0].events)
    gui_ev = [ev for ev in runs[0].events if ev.get("phase") == "gui_step"][0]
    assert gui_ev["screenshot_path"] == "temp/gui_runs/a.png"
