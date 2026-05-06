"""Tool-using clients — wrap a session ``backend`` and present ``chat()``.

Two flavours:
  * ``ToolClient`` — text protocol. Adds a system prompt that teaches the
    model the ``<tool_use>{json}</tool_use>`` XML protocol, then regex-parses
    the response. Used when the model doesn't speak native tool-use.
  * ``NativeToolClient`` — for ``NativeClaudeSession`` / ``NativeOAISession``.
    Funnels messages directly through the session's ``ask`` (which already
    builds a ``MockResponse``); this class adds the THINKING_PROMPT_* system
    suffix and a ``_pending_tool_ids`` tracker for tool_result reconciliation.

Both share the goal of making provider/protocol diversity invisible to the
agent loop — it just sees ``response.tool_calls`` / ``response.content``.
"""
import json
import os
import re

from llmcore._mock import MockResponse, MockToolCall
from llmcore._utils import safeprint as print, tryparse, _write_llm_log_if_enabled


THINKING_PROMPT_ZH = """
### 行动规范（持续有效）
每次回复（含工具调用轮）都先在回复文字中包含一个<summary></summary> 中输出极简单行（<30字）物理快照：上次结果新信息+本次意图。此内容进入长期工作记忆。
\n**若用户需求未完成，必须进行工具调用！**
""".strip()
THINKING_PROMPT_EN = """
### Action Protocol (always in effect)
The reply body should first include a minimal one-line (<30 words) physical snapshot in <summary></summary>: new info from last result + current intent. This goes into long-term working memory.
\n**If the user's request is not yet complete, tool calls are required!**
""".strip()


class ToolClient:
    """Text-protocol wrapper. Builds a single concatenated prompt that
    includes a tool-use protocol primer + the tool schemas + the conversation
    history; pulls tool calls out of the response with regex.

    ``auto_save_tokens`` skips re-emitting the protocol primer when the same
    tool set is in use across consecutive turns — protocol stays cached on
    the model's side; we just mention "tools still active".
    """

    def __init__(self, backend, auto_save_tokens=True):
        self.backend = backend
        self.auto_save_tokens = auto_save_tokens
        self.last_tools = ''
        self.name = self.backend.name
        self.total_cd_tokens = 0

    def chat(self, messages, tools=None):
        full_prompt = self._build_protocol_prompt(messages, tools)
        print("Full prompt length:", len(full_prompt), 'chars')
        prompt_log = full_prompt
        gen = self.backend.ask(full_prompt, stream=True)
        _write_llm_log_if_enabled(self.backend, 'Prompt', prompt_log)
        raw_text = ''
        summarytag = '[NextWillSummary]'
        for chunk in gen:
            raw_text += chunk
            if chunk != summarytag:
                yield chunk
        if raw_text.endswith(summarytag):
            self.last_tools = ''
            raw_text = raw_text[:-len(summarytag)]
        _write_llm_log_if_enabled(self.backend, 'Response', raw_text)
        return self._parse_mixed_response(raw_text)

    def _estimate_content_len(self, content):
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += len(part.get("text", ""))
                elif part.get("type") == "image_url":
                    total += 1000
            return total
        return len(str(content))

    def _prepare_tool_instruction(self, tools):
        tool_instruction = ""
        if not tools:
            return tool_instruction
        tools_json = json.dumps(tools, ensure_ascii=False, separators=(',', ':'))
        _en = os.environ.get('WLWL_LANG') == 'en'
        if _en:
            tool_instruction = f"""
### Interaction Protocol (must follow strictly, always in effect)
Follow these steps to think and act:
1. **Think**: Analyze the current situation and strategy inside `<thinking>` tags.
2. **Summarize**: Output a minimal one-line (<30 words) physical snapshot in `<summary>`: new info from last tool result + current tool call intent. This goes into long-term working memory. Must contain real information, no filler.
3. **Act**: If you need to call tools, output one or more **<tool_use> blocks** after your reply, then stop.
"""
        else:
            tool_instruction = f"""
### 交互协议 (必须严格遵守，持续有效)
请按照以下步骤思考并行动：
1. **思考**: 在 `<thinking>` 标签中先进行思考，分析现状和策略。
2. **总结**: 在 `<summary>` 中输出*极为简短*的高度概括的单行（<30字）物理快照，包括上次工具调用结果产生的新信息+本次工具调用意图。此内容将进入长期工作记忆，记录关键信息，严禁输出无实际信息增量的描述。
3. **行动**: 如需调用工具，请在回复正文之后输出一个（或多个）**<tool_use>块**，然后结束。
"""
        tool_instruction += f'\nFormat: ```<tool_use>{{"name": "tool_name", "arguments": {{...}}}}</tool_use>```\n\n### Tools (mounted, always in effect):\n{tools_json}\n'
        if self.auto_save_tokens and self.last_tools == tools_json:
            tool_instruction = "\n### Tools: still active, **ready to call**. Protocol unchanged.\n" if _en else "\n### 工具库状态：持续有效（code_run/file_read等），**可正常调用**。调用协议沿用。\n"
        else:
            self.total_cd_tokens = 0
        self.last_tools = tools_json
        return tool_instruction

    def _build_protocol_prompt(self, messages, tools):
        system_content = next((m['content'] for m in messages if m['role'].lower() == 'system'), "")
        history_msgs = [m for m in messages if m['role'].lower() != 'system']
        tool_instruction = self._prepare_tool_instruction(tools)
        system = ""
        user = ""
        if system_content:
            system += f"{system_content}\n"
        system += f"{tool_instruction}"
        for m in history_msgs:
            role = "USER" if m['role'] == 'user' else "ASSISTANT"
            user += f"=== {role} ===\n"
            for tr in m.get('tool_results', []):
                user += f'<tool_result>{tr["content"]}</tool_result>\n'
            user += str(m['content']) + "\n"
            self.total_cd_tokens += self._estimate_content_len(user)
        if self.total_cd_tokens > 9000:
            self.last_tools = ''
        user += "=== ASSISTANT ===\n"
        return system + user

    def _parse_mixed_response(self, text):
        remaining_text = text
        thinking = ''
        think_pattern = r"<think(?:ing)?>(.*?)</think(?:ing)?>"
        think_match = re.search(think_pattern, text, re.DOTALL)

        if think_match:
            thinking = think_match.group(1).strip()
            remaining_text = re.sub(think_pattern, "", remaining_text, flags=re.DOTALL)

        tool_calls = []
        json_strs = []
        errors = []
        tool_pattern = r"<(?:tool_use|tool_call)>((?:(?!<(?:tool_use|tool_call)>).){15,}?)</(?:tool_use|tool_call)>"
        tool_all = re.findall(tool_pattern, remaining_text, re.DOTALL)

        if tool_all:
            tool_all = [s.strip() for s in tool_all]
            json_strs.extend([s for s in tool_all if s.startswith('{') and s.endswith('}')])
            remaining_text = re.sub(tool_pattern, "", remaining_text, flags=re.DOTALL)
        elif '<tool_use>' in remaining_text:
            weaktoolstr = remaining_text.split('<tool_use>')[-1].strip().strip('><')
            json_str = weaktoolstr if weaktoolstr.endswith('}') else ''
            if json_str == '' and '```' in weaktoolstr and weaktoolstr.split('```')[0].strip().endswith('}'):
                json_str = weaktoolstr.split('```')[0].strip()
            if json_str:
                json_strs.append(json_str)
            remaining_text = remaining_text.replace('<tool_use>' + weaktoolstr, "")
        elif '"name":' in remaining_text and '"arguments":' in remaining_text:
            json_match = re.search(r'\{.*"name":.*\}', remaining_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(0).strip()
                json_strs.append(json_str)
                remaining_text = remaining_text.replace(json_str, "").strip()

        for json_str in json_strs:
            try:
                data = tryparse(json_str)
                func_name = data.get('name') or data.get('function') or data.get('tool')
                args = data.get('arguments') or data.get('args') or data.get('params') or data.get('parameters')
                if args is None:
                    args = data
                if func_name:
                    tool_calls.append(MockToolCall(func_name, args))
            except json.JSONDecodeError:
                errors.append({'err': f"[Warn] Failed to parse tool_use JSON: {json_str}", 'bad_json': f'Failed to parse tool_use JSON: {json_str[:200]}'})
                self.last_tools = ''  # llm forgot tool schema, re-send
            except Exception as e:
                errors.append({'err': f'[Warn] Exception during tool_use parsing: {str(e)} {str(data)}'})
        if len(tool_calls) == 0:
            for e in errors:
                print(e['err'])
                if 'bad_json' in e:
                    tool_calls.append(MockToolCall('bad_json', {'msg': e['bad_json']}))
        content = remaining_text.strip()
        return MockResponse(thinking, content, tool_calls, text)


class NativeToolClient:
    """Native-tool wrapper for ``NativeClaudeSession`` / ``NativeOAISession``.

    The session's ``ask`` already builds a ``MockResponse``; this class:
      * Stamps THINKING_PROMPT_* onto the system prompt.
      * Tracks ``_pending_tool_ids`` so any tool_use IDs the model emits but
        the agent loop didn't fulfill get auto-padded with empty
        ``tool_result`` blocks on the next turn (Anthropic API requires
        every tool_use to be paired).
    """

    @staticmethod
    def _thinking_prompt():
        return THINKING_PROMPT_EN if os.environ.get('WLWL_LANG') == 'en' else THINKING_PROMPT_ZH

    def __init__(self, backend):
        self.backend = backend
        self.backend.system = self._thinking_prompt()
        self.name = self.backend.name
        self._pending_tool_ids = []

    def set_system(self, extra_system):
        combined = f"{extra_system}\n\n{self._thinking_prompt()}" if extra_system else self._thinking_prompt()
        if combined != self.backend.system:
            print(f"[Debug] Updated system prompt, length {len(combined)} chars.")
        self.backend.system = combined

    def chat(self, messages, tools=None):
        if tools:
            self.backend.tools = tools
        combined_content = []
        resp = None
        tool_results = []
        for msg in messages:
            c = msg.get('content', '')
            if msg['role'] == 'system':
                self.set_system(c)
                continue
            if isinstance(c, str):
                combined_content.append({"type": "text", "text": c})
            elif isinstance(c, list):
                combined_content.extend(c)
            if msg['role'] == 'user' and msg.get('tool_results'):
                tool_results.extend(msg['tool_results'])
        tr_id_set = set()
        tool_result_blocks = []
        for tr in tool_results:
            tool_use_id, content = tr.get("tool_use_id", ""), tr.get("content", "")
            tr_id_set.add(tool_use_id)
            if tool_use_id:
                tool_result_blocks.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": tr.get("content", "")})
            else:
                combined_content = [{"type": "text", "text": f'<tool_result>{content}</tool_result>'}] + combined_content
        for tid in self._pending_tool_ids:
            if tid not in tr_id_set:
                tool_result_blocks.append({"type": "tool_result", "tool_use_id": tid, "content": ""})
        self._pending_tool_ids = []
        merged = {"role": "user", "content": tool_result_blocks + combined_content}
        _write_llm_log_if_enabled(self.backend, 'Prompt', json.dumps(merged, ensure_ascii=False, indent=2))
        gen = self.backend.ask(merged)
        try:
            while True:
                chunk = next(gen)
                yield chunk
        except StopIteration as e:
            resp = e.value
        if resp and not getattr(resp, 'thinking', '') and isinstance(getattr(resp, 'content', None), str):
            think_pattern = r"<think(?:ing)?>(.*?)</think(?:ing)?>"
            think_match = re.search(think_pattern, resp.content, re.DOTALL)
            if think_match:
                resp.thinking = think_match.group(1).strip()
                resp.content = re.sub(think_pattern, "", resp.content, flags=re.DOTALL).strip()
        if resp:
            _write_llm_log_if_enabled(self.backend, 'Response', resp.raw)
        if resp and hasattr(resp, 'tool_calls') and resp.tool_calls:
            self._pending_tool_ids = [tc.id for tc in resp.tool_calls]
        return resp
