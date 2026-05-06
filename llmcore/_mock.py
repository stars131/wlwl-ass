"""Mock response objects — synthesized to look like ``openai.ChatCompletion``
results so the agent loop can treat native and text-protocol responses
through one interface.

``ToolClient`` (text protocol) builds these from regex-parsed tool_use tags.
``NativeClaudeSession`` builds them from Anthropic native content blocks.
Either way, the agent loop sees ``response.tool_calls``, ``response.content``,
``response.thinking``, ``response.stop_reason`` — uniform shape.
"""
import json
import re

from llmcore._utils import tryparse


class MockFunction:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class MockToolCall:
    def __init__(self, name, args, id=''):
        arg_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else (args or '{}')
        self.function = MockFunction(name, arg_str)
        self.id = id


class MockResponse:
    def __init__(self, thinking, content, tool_calls, raw, stop_reason='end_turn'):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls
        self.raw = raw
        self.stop_reason = 'tool_use' if tool_calls else stop_reason

    def __repr__(self):
        return f"<MockResponse thinking={bool(self.thinking)}, content='{self.content}', tools={bool(self.tool_calls)}>"


def _parse_text_tool_calls(content):
    """Fallback: extract tool calls from plain text when the model didn't
    use native ``tool_use`` blocks but still emitted ``<tool_use>`` /
    ``<tool_call>`` tags or a JSON array. Returns ``(tool_calls, remaining_text)``.
    """
    tcs = []
    # try JSON array: [{"type":"tool_use", "name":..., "input":...}]
    _jp = next((p for p in ['[{"type":"tool_use"', '[{"type": "tool_use"'] if p in content), None)
    if _jp and content.endswith('}]'):
        try:
            idx = content.index(_jp)
            raw = json.loads(content[idx:])
            tcs = [MockToolCall(b["name"], b.get("input", {}), id=b.get("id", "")) for b in raw if b.get("type") == "tool_use"]
            return tcs, content[:idx].strip()
        except Exception:
            pass
    # try XML tags: <tool_call>{"name":..., "arguments":...}</tool_call>
    _xp = r"<(?:tool_use|tool_call)>((?:(?!<(?:tool_use|tool_call)>).){15,}?)</(?:tool_use|tool_call)>"
    for s in re.findall(_xp, content, re.DOTALL):
        try:
            d = tryparse(s.strip())
            name = d.get('name')
            args = d.get('arguments') or d.get('args') or d.get('input') or {}
            if name:
                tcs.append(MockToolCall(name, args))
        except Exception:
            pass
    if tcs:
        content = re.sub(_xp, "", content, flags=re.DOTALL).strip()
    return tcs, content
