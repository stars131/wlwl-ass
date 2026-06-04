from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def test_agent_loop_preserves_next_prompt_order():
    from agent_loop import BaseHandler, StepOutcome, agent_runner_loop, exhaust

    class _Function:
        def __init__(self, name: str, arguments: str):
            self.name = name
            self.arguments = arguments

    class _ToolCall:
        def __init__(self, ident: str, name: str):
            self.id = ident
            self.function = _Function(name, "{}")

    class _Response:
        content = ""
        tool_calls = [_ToolCall("a", "first"), _ToolCall("b", "second")]

    class _Client:
        last_tools = ""

        def chat(self, **_kwargs):
            if False:
                yield None
            return _Response()

    class _Handler(BaseHandler):
        def __init__(self):
            super().__init__()
            self.seen_next_prompt = ""

        def do_first(self, _args, _response):
            return StepOutcome({"ok": 1}, next_prompt="first prompt")

        def do_second(self, _args, _response):
            return StepOutcome({"ok": 2}, next_prompt="second prompt")

        def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
            self.seen_next_prompt = next_prompt
            return next_prompt

    handler = _Handler()
    exhaust(agent_runner_loop(_Client(), "sys", "user", handler, [], max_turns=1, verbose=False))

    assert handler.seen_next_prompt == "first prompt\nsecond prompt"


def test_file_patch_duplicate_error_includes_line_numbers(tmp_path):
    from wlwl_ass import file_patch

    path = tmp_path / "sample.txt"
    path.write_text("alpha\nneedle\nbeta\nneedle\ngamma\n", encoding="utf-8")

    result = file_patch(str(path), "needle", "changed")

    assert result["status"] == "error"
    assert "找到 2 处匹配" in result["msg"]
    assert "行2" in result["msg"]
    assert "行4" in result["msg"]


def test_file_patch_missing_error_includes_nearest_line_hint(tmp_path):
    from wlwl_ass import file_patch

    path = tmp_path / "sample.txt"
    path.write_text("alpha\nneedle current value\nbeta\n", encoding="utf-8")

    result = file_patch(str(path), "needle old value", "changed")

    assert result["status"] == "error"
    assert "未找到匹配" in result["msg"]
    assert "最接近片段" in result["msg"]
    assert "行2" in result["msg"]
