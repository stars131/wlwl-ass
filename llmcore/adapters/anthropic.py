"""Anthropic adapter — Claude text protocol + native CC protocol.

Owns:
  * SSE parser for Anthropic's ``messages`` event stream.
  * ``ClaudeSession`` — vanilla text protocol with ``cache_control`` tags
    for prompt caching.
  * ``NativeClaudeSession`` — emulates Claude Code's HTTP shape (beta
    headers, fake-CC system prompt, Claude Code metadata) for relay
    backends that expect that exact request fingerprint.

All Anthropic-specific quirks live here: cache_control / thinking_type /
beta-flag composition / Claude Code system prompt / unsigned-thinking
filtering. Adding a new Anthropic-flavoured adapter (Bedrock, Vertex)
should mostly mean writing a new class that overrides the HTTP layer
and reuses ``_parse_claude_sse``.
"""
import json
import re
import uuid

import requests

from llmcore._messages import (
    _drop_unsigned_thinking,
    _fix_messages,
    openai_tools_to_claude,
)
from llmcore._mock import MockResponse, MockToolCall, _parse_text_tool_calls
from llmcore._usage import _accumulate_usage, _record_usage
from llmcore._utils import auto_make_url, safeprint as print
from llmcore._history import trim_messages_history
from llmcore.base import BaseSession


def _parse_claude_sse(resp_lines, model_hint=""):
    """Parse Anthropic SSE stream. Yields text chunks, returns list[content_block].

    ``model_hint`` is what the caller asked for; the server may overwrite it
    via ``message.model`` on the ``message_start`` event. We keep the most
    recently observed value and thread it through to the usage recorder so
    the cost ledger gets per-model attribution."""
    content_blocks = []
    current_block = None
    tool_json_buf = ""
    stop_reason = None
    got_message_stop = False
    warn = None
    current_model = model_hint or ""
    for line in resp_lines:
        if not line:
            continue
        line = line.decode('utf-8') if isinstance(line, bytes) else line
        if not line.startswith("data:"):
            continue
        data_str = line[5:].lstrip()
        if data_str == "[DONE]":
            break
        try:
            evt = json.loads(data_str)
        except Exception as e:
            print(f"[SSE] JSON parse error: {e}, line: {data_str[:200]}")
            continue
        evt_type = evt.get("type", "")
        if evt_type == "message_start":
            msg_obj = evt.get("message", {}) or {}
            usage = msg_obj.get("usage", {})
            srv_model = msg_obj.get("model")
            if srv_model:
                current_model = srv_model
            _record_usage(usage, "messages", model=current_model)
        elif evt_type == "content_block_start":
            block = evt.get("content_block", {})
            if block.get("type") == "text":
                current_block = {"type": "text", "text": ""}
            elif block.get("type") == "thinking":
                current_block = {"type": "thinking", "thinking": "", "signature": ""}
            elif block.get("type") == "tool_use":
                current_block = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "input": {}}
                tool_json_buf = ""
        elif evt_type == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if current_block and current_block.get("type") == "text":
                    current_block["text"] += text
                if text:
                    yield text
            elif delta.get("type") == "thinking_delta":
                if current_block and current_block.get("type") == "thinking":
                    current_block["thinking"] += delta.get("thinking", "")
            elif delta.get("type") == "signature_delta":
                if current_block and current_block.get("type") == "thinking":
                    current_block["signature"] = current_block.get("signature", "") + delta.get("signature", "")
            elif delta.get("type") == "input_json_delta":
                tool_json_buf += delta.get("partial_json", "")
        elif evt_type == "content_block_stop":
            if current_block:
                if current_block["type"] == "tool_use":
                    try:
                        current_block["input"] = json.loads(tool_json_buf) if tool_json_buf else {}
                    except Exception:
                        current_block["input"] = {"_raw": tool_json_buf}
                content_blocks.append(current_block)
                current_block = None
        elif evt_type == "message_delta":
            delta = evt.get("delta", {})
            stop_reason = delta.get("stop_reason", stop_reason)
            out_usage = evt.get("usage", {})
            out_tokens = out_usage.get("output_tokens", 0)
            if out_tokens:
                print(f"[Output] tokens={out_tokens} stop_reason={stop_reason}")
                # Output_tokens lives on the message_delta event, not the
                # message_start event, so we need a second recorder hit
                # to keep the running tally accurate. Pass 0 for input
                # so we don't double-count it.
                _accumulate_usage("messages", output_tokens=out_tokens, model=current_model)
        elif evt_type == "message_stop":
            got_message_stop = True
        elif evt_type == "error":
            err = evt.get("error", {})
            emsg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            warn = f"\n\n!!!Error: SSE {emsg}"
            break
    if not warn:
        if not got_message_stop and not stop_reason:
            warn = "\n\n[!!! 流异常中断，未收到完整响应 !!!]"
        elif stop_reason == "max_tokens":
            warn = "\n\n[!!! Response truncated: max_tokens !!!]"
    if current_block:
        if current_block["type"] == "tool_use":
            try:
                current_block["input"] = json.loads(tool_json_buf) if tool_json_buf else {}
            except Exception:
                current_block["input"] = {"_raw": tool_json_buf}
        content_blocks.append(current_block)
        current_block = None
    if warn:
        print(f"[WARN] {warn.strip()}")
        content_blocks.append({"type": "text", "text": warn})
        yield warn
    return content_blocks


class ClaudeSession(BaseSession):
    """Anthropic ``/messages`` text protocol with prompt caching.

    Emits ``cache_control`` markers on the system prompt + last 2 user
    messages so the API caches the long-running prefix between turns.
    """

    def raw_ask(self, messages):
        if self.max_tokens is None:
            self.max_tokens = 8192
        # Prompt caching is GA on Anthropic — no beta header needed. We still
        # send the per-block cache_control breakpoints so the long-running
        # system prompt + last 2 user turns get cached.
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        payload = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, "stream": True}
        if self.temperature != 1:
            payload["temperature"] = self.temperature
        self._apply_claude_thinking(payload)
        if self.system:
            # Anthropic only accepts cache_control={"type": "ephemeral"}
            # (optionally with "ttl":"5m"|"1h"). The legacy "persistent"
            # value was silently ignored, so the system prompt was never
            # cached. Use 1h TTL — system prompts are stable for hours.
            payload["system"] = [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        try:
            with requests.post(auto_make_url(self.api_base, "messages"), headers=headers, json=payload, stream=True, timeout=(self.connect_timeout, self.read_timeout)) as r:
                if r.status_code != 200:
                    raise Exception(f"HTTP {r.status_code} {r.content.decode('utf-8', errors='replace')[:500]}")
                return (yield from _parse_claude_sse(r.iter_lines(), model_hint=self.model)) or []
        except Exception as e:
            yield (err := f"!!!Error: {e}")
            return [{"type": "text", "text": err}]

    def make_messages(self, raw_list):
        msgs = _drop_unsigned_thinking([{"role": m['role'], "content": list(m['content'])} for m in raw_list])
        user_idxs = [i for i, m in enumerate(msgs) if m['role'] == 'user']
        for idx in user_idxs[-2:]:
            msgs[idx]["content"][-1] = dict(msgs[idx]["content"][-1], cache_control={"type": "ephemeral"})
        return msgs


class NativeClaudeSession(BaseSession):
    """Claude Code-shaped native tool-use HTTP client.

    Looks like a request from the official ``claude-cli`` tool: identical
    user-agent, beta header set, system prompt, and metadata. Used so
    relay services that gate on those features still serve us.
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.fake_cc_system_prompt = cfg.get("fake_cc_system_prompt", False)
        self.user_agent = cfg.get("user_agent", "claude-cli/2.1.113 (external, cli)")
        self._session_id = str(uuid.uuid4())
        self._account_uuid = str(uuid.uuid4())
        self._device_id = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        self.tools = None

    def raw_ask(self, messages):
        messages = _drop_unsigned_thinking(_fix_messages(messages))
        if self.max_tokens is None:
            self.max_tokens = 8192
        model = self.model
        beta_parts = ["claude-code-20250219", "interleaved-thinking-2025-05-14", "redact-thinking-2026-02-12", "prompt-caching-scope-2026-01-05"]
        if "[1m]" in model.lower():
            beta_parts.insert(1, "context-1m-2025-08-07")
            model = model.replace("[1m]", "").replace("[1M]", "")
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                   "anthropic-beta": ",".join(beta_parts), "anthropic-dangerous-direct-browser-access": "true",
                   "user-agent": self.user_agent, "x-app": "cli"}
        if self.api_key.startswith("sk-ant-"):
            headers["x-api-key"] = self.api_key
        else:
            headers["authorization"] = f"Bearer {self.api_key}"
        payload = {"model": model, "messages": messages, "max_tokens": self.max_tokens, "stream": self.stream}
        if self.temperature != 1:
            payload["temperature"] = self.temperature
        self._apply_claude_thinking(payload)
        payload["metadata"] = {"user_id": json.dumps({"device_id": self._device_id, "account_uuid": self._account_uuid, "session_id": self._session_id}, separators=(',', ':'))}
        if self.tools:
            claude_tools = openai_tools_to_claude(self.tools)
            tools = [dict(t) for t in claude_tools]
            tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = tools
        else:
            print("[ERROR] No tools provided for this session.")
        payload['system'] = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}}]
        if self.system:
            if self.fake_cc_system_prompt:
                messages[0]["content"].insert(0, {"type": "text", "text": self.system})
            else:
                payload["system"] = [{"type": "text", "text": self.system}]
        user_idxs = [i for i, m in enumerate(messages) if m['role'] == 'user']
        for idx in user_idxs[-2:]:
            messages[idx] = {**messages[idx], "content": list(messages[idx]["content"])}
            messages[idx]["content"][-1] = dict(messages[idx]["content"][-1], cache_control={"type": "ephemeral"})
        try:
            with requests.post(auto_make_url(self.api_base, "messages") + '?beta=true', headers=headers, json=payload, stream=self.stream, timeout=(self.connect_timeout, self.read_timeout)) as resp:
                if resp.status_code != 200:
                    raise Exception(f"HTTP {resp.status_code} {resp.content.decode('utf-8', errors='replace')[:500]}")
                if self.stream:
                    return (yield from _parse_claude_sse(resp.iter_lines(), model_hint=model)) or []
                else:
                    data = resp.json()
                    content_blocks = data.get("content", [])
                    _record_usage(data.get("usage", {}), "messages", model=data.get("model") or model)
                    for b in content_blocks:
                        if b.get("type") == "text":
                            yield b.get("text", "")
                        elif b.get("type") == "thinking":
                            yield ""
                    return content_blocks
        except Exception as e:
            yield (err := f"!!!Error: {e}")
            return [{"type": "text", "text": err}]

    def ask(self, msg):
        """Native-tool-use ``ask`` builds a ``MockResponse`` instead of yielding
        plain text — the agent loop's downstream code expects ``.tool_calls``,
        ``.thinking``, ``.content`` attributes."""
        assert type(msg) is dict
        with self.lock:
            self.history.append(msg)
            trim_messages_history(self.history, self.context_win)
            messages = [{"role": m["role"], "content": list(m["content"])} for m in self.history]
        content_blocks = None
        gen = self.raw_ask(messages)
        try:
            while True:
                yield next(gen)
        except StopIteration as e:
            content_blocks = e.value or []
        if content_blocks and not (len(content_blocks) == 1 and content_blocks[0].get("text", "").startswith("!!!Error:")):
            self.history.append({"role": "assistant", "content": content_blocks})
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        content = "\n".join(text_parts).strip()
        tool_calls = [MockToolCall(b["name"], b.get("input", {}), id=b.get("id", "")) for b in content_blocks if b.get("type") == "tool_use"]
        if not tool_calls:
            tool_calls, content = _parse_text_tool_calls(content)
        thinking_parts = [b["thinking"] for b in content_blocks if b.get("type") == "thinking"]
        thinking = "\n".join(thinking_parts).strip()
        if not thinking:
            think_pattern = r"<think(?:ing)?>(.*?)</think(?:ing)?>"
            think_match = re.search(think_pattern, content, re.DOTALL)
            if think_match:
                thinking = think_match.group(1).strip()
                content = re.sub(think_pattern, "", content, flags=re.DOTALL)
        return MockResponse(thinking, content, tool_calls, str(content_blocks))
