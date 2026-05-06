"""OpenAI adapter — chat-completions + responses APIs, plus the relay-to-
Claude bridge.

Owns:
  * ``_parse_openai_sse`` / ``_parse_openai_json`` — both API modes.
  * ``_openai_stream`` — single retry-aware HTTP entry point used by every
    OAI-compatible adapter (GLM, Kimi, MiniMax, DeepSeek, GPT-N, …).
  * ``LLMSession`` — text protocol over OAI; tools optional.
  * ``NativeOAISession`` — native tool-use over OAI; reuses
    ``NativeClaudeSession.__init__`` (Claude Code-style metadata) so relays
    that look at session_id / device_id still see consistent IDs.
  * ``_stamp_oai_cache_markers`` — bridge: when OAI relay is fronting a
    Claude model, stamp ``cache_control`` markers on the request so prompt
    caching still works.

This is the file to edit when adding model-specific quirks (temperature
constraints, max_tokens key naming, reasoning_effort routing).
"""
import json
import time
import uuid

import requests

from llmcore._messages import _fix_messages, _msgs_claude2oai, _try_parse_tool_args
from llmcore._usage import _record_usage
from llmcore._utils import auto_make_url, safeprint as print
from llmcore.base import BaseSession
from llmcore.adapters.anthropic import NativeClaudeSession

# Stable per-process key so OAI Responses API can opportunistically hit the
# server-side prompt cache. Generated lazily once at import time.
_RESP_CACHE_KEY = str(uuid.uuid4())


def _parse_openai_sse(resp_lines, api_mode="chat_completions"):
    """Parse OpenAI SSE stream (chat_completions or responses API).
    Yields text chunks, returns list[content_block].
    content_block: {type:'text', text:str} | {type:'tool_use', id:str, name:str, input:dict}
    """
    content_text = ""
    if api_mode == "responses":
        seen_delta = False
        fc_buf = {}
        current_fc_idx = None
        for line in resp_lines:
            if not line:
                continue
            line = line.decode('utf-8', errors='replace') if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue
            data_str = line[5:].lstrip()
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except Exception:
                continue
            etype = evt.get("type", "")
            if etype == "response.output_text.delta":
                delta = evt.get("delta", "")
                if delta:
                    seen_delta = True
                    content_text += delta
                    yield delta
            elif etype == "response.output_text.done" and not seen_delta:
                text = evt.get("text", "")
                if text:
                    content_text += text
                    yield text
            elif etype == "response.output_item.added":
                item = evt.get("item", {})
                if item.get("type") == "function_call":
                    idx = evt.get("output_index", 0)
                    fc_buf[idx] = {"id": item.get("call_id", item.get("id", "")), "name": item.get("name", ""), "args": ""}
                    current_fc_idx = idx
            elif etype == "response.function_call_arguments.delta":
                idx = evt.get("output_index", current_fc_idx or 0)
                if idx in fc_buf:
                    fc_buf[idx]["args"] += evt.get("delta", "")
            elif etype == "response.function_call_arguments.done":
                idx = evt.get("output_index", current_fc_idx or 0)
                if idx in fc_buf:
                    fc_buf[idx]["args"] = evt.get("arguments", fc_buf[idx]["args"])
            elif etype == "error":
                err = evt.get("error", {})
                emsg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if emsg:
                    content_text += f"!!!Error: {emsg}"
                    yield f"!!!Error: {emsg}"
                break
            elif etype == "response.completed":
                usage = evt.get("response", {}).get("usage", {})
                _record_usage(usage, api_mode)
                break
        blocks = []
        if content_text:
            blocks.append({"type": "text", "text": content_text})
        for idx in sorted(fc_buf):
            fc = fc_buf[idx]
            inps = _try_parse_tool_args(fc["args"])
            for i, inp in enumerate(inps):
                bid = fc["id"] or ''
                if len(inps) > 1:
                    bid = f"{bid}_{i}" if bid else f"split_{i}"
                blocks.append({"type": "tool_use", "id": bid, "name": fc["name"], "input": inp})
        return blocks
    else:
        tc_buf = {}  # index -> {id, name, args}
        reasoning_text = ""
        for line in resp_lines:
            if not line:
                continue
            line = line.decode('utf-8', errors='replace') if isinstance(line, bytes) else line
            if not line.startswith("data:"):
                continue
            data_str = line[5:].lstrip()
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except Exception:
                continue
            ch = (evt.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning_text += delta["reasoning_content"]
            if delta.get("content"):
                text = delta["content"]
                content_text += text
                yield text
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                has_name = bool(tc.get("function", {}).get("name"))
                if idx not in tc_buf:
                    if has_name or not tc_buf:
                        tc_buf[idx] = {"id": tc.get("id") or '', "name": "", "args": ""}
                    else:
                        idx = max(tc_buf)
                if has_name:
                    tc_buf[idx]["name"] = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tc_buf[idx]["args"] += tc["function"]["arguments"]
                if tc.get("id") and not tc_buf[idx]["id"]:
                    tc_buf[idx]["id"] = tc["id"]
            usage = evt.get("usage")
            if usage:
                _record_usage(usage, api_mode)
        blocks = []
        if reasoning_text:
            blocks.append({"type": "thinking", "thinking": reasoning_text})
        if content_text:
            blocks.append({"type": "text", "text": content_text})
        for idx in sorted(tc_buf):
            tc = tc_buf[idx]
            inps = _try_parse_tool_args(tc["args"])
            for i, inp in enumerate(inps):
                bid = tc["id"] or ''
                if len(inps) > 1:
                    bid = f"{bid}_{i}" if bid else f"split_{i}"
                blocks.append({"type": "tool_use", "id": bid, "name": tc["name"], "input": inp})
        return blocks


def _parse_openai_json(data, api_mode="chat_completions"):
    blocks = []
    if api_mode == "responses":
        _record_usage(data.get("usage") or {}, api_mode)
        for item in (data.get("output") or []):
            if item.get("type") == "message":
                for p in (item.get("content") or []):
                    if p.get("type") in ("output_text", "text") and p.get("text"):
                        blocks.append({"type": "text", "text": p["text"]})
                        yield p["text"]
            elif item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments", "")) if item.get("arguments") else {}
                except Exception:
                    args = {"_raw": item.get("arguments", "")}
                blocks.append({"type": "tool_use", "id": item.get("call_id", item.get("id", "")),
                               "name": item.get("name", ""), "input": args})
    else:
        _record_usage(data.get("usage") or {}, api_mode)
        msg = (data.get("choices") or [{}])[0].get("message", {})
        reasoning = msg.get("reasoning_content", "")
        if reasoning:
            blocks.append({"type": "thinking", "thinking": reasoning})
        content = msg.get("content", "")
        if content:
            blocks.append({"type": "text", "text": content})
            yield content
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "")) if fn.get("arguments") else {}
            except Exception:
                args = {"_raw": fn.get("arguments", "")}
            blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": args})
    return blocks


def _stamp_oai_cache_markers(messages, model):
    """Add ``cache_control`` to last 2 user messages when an OAI-compatible
    relay is fronting an Anthropic model. Bridge logic — the relay forwards
    the marker to the underlying Claude API which honors it."""
    ml = model.lower()
    if not any(k in ml for k in ('claude', 'anthropic')):
        return
    user_idxs = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    for idx in user_idxs[-2:]:
        c = messages[idx].get('content')
        if isinstance(c, str):
            messages[idx] = {**messages[idx], 'content': [{'type': 'text', 'text': c, 'cache_control': {'type': 'ephemeral'}}]}
        elif isinstance(c, list) and c:
            c = list(c)
            c[-1] = dict(c[-1], cache_control={'type': 'ephemeral'})
            messages[idx] = {**messages[idx], 'content': c}


def _openai_stream(api_base, api_key, messages, model, api_mode='chat_completions', *,
                   system=None, temperature=0.5, max_tokens=None, tools=None, reasoning_effort=None,
                   max_retries=0, connect_timeout=10, read_timeout=300, proxies=None, stream=True):
    """Shared OpenAI-compatible streaming request with retry. Yields text
    chunks, returns ``list[content_block]``.

    Per-vendor knobs:
      * Kimi / Moonshot / MiniMax force ``temperature=1`` (or ≤1) — their
        APIs reject temperatures the OAI default lets through.
      * gpt-5 / o1 / o2 / o3 / o4 use ``max_completion_tokens`` instead of
        ``max_tokens``.
    """
    ml = model.lower()
    if 'kimi' in ml or 'moonshot' in ml:
        temperature = 1
    elif 'minimax' in ml:
        temperature = max(0.01, min(temperature, 1.0))  # MiniMax requires temp in (0, 1]
    force_temperature = any(k in ml for k in ('kimi', 'moonshot', 'minimax'))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_mode == "responses":
        url = auto_make_url(api_base, "responses")
        payload = {"model": model, "input": _to_responses_input(messages), "stream": stream,
                   "prompt_cache_key": _RESP_CACHE_KEY, "instructions": system or "You are an Omnipotent Executor."}
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if max_tokens:
            payload["max_output_tokens"] = max_tokens
    else:
        url = auto_make_url(api_base, "chat/completions")
        if system:
            messages = [{"role": "system", "content": system}] + messages
        _stamp_oai_cache_markers(messages, model)
        payload = {"model": model, "messages": messages, "stream": stream}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if force_temperature or temperature != 1:
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_completion_tokens" if ml.startswith(("gpt-5", "o1", "o2", "o3", "o4")) else "max_tokens"] = max_tokens
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload["tools"] = _prepare_oai_tools(tools, api_mode)
    RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}

    def _delay(resp, attempt):
        try:
            ra = float((resp.headers or {}).get("retry-after"))
        except Exception:
            ra = None
        return max(0.5, ra if ra is not None else min(30.0, 1.5 * (2 ** attempt)))

    for attempt in range(max_retries + 1):
        streamed = False
        try:
            with requests.post(url, headers=headers, json=payload, stream=stream,
                               timeout=(connect_timeout, read_timeout), proxies=proxies) as r:
                if r.status_code >= 400:
                    if r.status_code in RETRYABLE and attempt < max_retries:
                        d = _delay(r, attempt)
                        print(f"[LLM Retry] HTTP {r.status_code}, retry in {d:.1f}s ({attempt+1}/{max_retries+1})")
                        time.sleep(d)
                        continue
                    body = ""
                    try:
                        body = r.text.strip()[:500]
                    except Exception:
                        pass
                    err = f"!!!Error: HTTP {r.status_code}" + (f": {body}" if body else "")
                    yield err
                    return [{"type": "text", "text": err}]
                gen = _parse_openai_sse(r.iter_lines(), api_mode) if stream else _parse_openai_json(r.json(), api_mode)
                try:
                    while True:
                        streamed = True
                        yield next(gen)
                except StopIteration as e:
                    return e.value or []
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < max_retries and not streamed:
                d = _delay(None, attempt)
                print(f"[LLM Retry] {type(e).__name__}, retry in {d:.1f}s ({attempt+1}/{max_retries+1})")
                time.sleep(d)
                continue
            err = f"!!!Error: {type(e).__name__}"
            yield err
            return [{"type": "text", "text": err}]
        except Exception as e:
            err = f"!!!Error: {type(e).__name__}: {e}"
            yield err
            return [{"type": "text", "text": err}]


def _prepare_oai_tools(tools, api_mode="chat_completions"):
    """Tool-schema shape adapter. Responses API flattens ``function`` into
    the top-level dict; chat-completions keeps the standard ``{type, function}``
    nesting."""
    if api_mode == "responses":
        resp_tools = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                rt = {"type": "function"}
                rt.update(t["function"])
                resp_tools.append(rt)
            else:
                resp_tools.append(t)
        return resp_tools
    return tools


def _to_responses_input(messages):
    """Translate Anthropic-shaped history → OAI Responses-API ``input`` array.

    Two key shape differences vs chat-completions:
      * roles ``system`` → ``developer`` (Responses API rename)
      * tool calls / results live as standalone items, not wrapped in messages
    """
    result, pending = [], []
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        if role == "tool":
            cid = msg.get("tool_call_id") or (pending.pop(0) if pending else f"call_{uuid.uuid4().hex[:8]}")
            result.append({"type": "function_call_output", "call_id": cid, "output": msg.get("content", "")})
            continue
        if role not in ["user", "assistant", "system", "developer"]:
            role = "user"
        if role == "system":
            role = "developer"  # Responses API uses 'developer' instead of 'system'
        content = msg.get("content", "")
        text_type = "output_text" if role == "assistant" else "input_text"
        parts = []
        if isinstance(content, str):
            if content:
                parts.append({"type": text_type, "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text", "")
                    if text:
                        parts.append({"type": text_type, "text": text})
                elif ptype == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url and role != "assistant":
                        parts.append({"type": "input_image", "image_url": url})
        if len(parts) == 0:
            parts = [{"type": text_type, "text": str(content) if not isinstance(content, list) else '[empty]'}]
        result.append({"role": role, "content": parts})
        pending = []
        for tc in (msg.get("tool_calls") or []):
            f = tc.get("function", {})
            cid = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            pending.append(cid)
            result.append({"type": "function_call", "call_id": cid, "name": f.get("name", ""), "arguments": f.get("arguments", "")})
    return result


class LLMSession(BaseSession):
    """Text-protocol OAI session. Used by the non-native ``ToolClient``."""

    def raw_ask(self, messages, model=None, api_mode=None, system=None, temperature=None, max_tokens=None,
                tools=None, reasoning_effort=None, stream=None):
        return (yield from _openai_stream(
            self.api_base,
            self.api_key,
            messages,
            model or self.model,
            self.api_mode if api_mode is None else api_mode,
            system=self.system if system is None else system,
            temperature=self.temperature if temperature is None else temperature,
            reasoning_effort=self.reasoning_effort if reasoning_effort is None else reasoning_effort,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            tools=tools,
            max_retries=self.max_retries,
            stream=self.stream if stream is None else stream,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            proxies=self.proxies,
        ))

    def make_messages(self, raw_list):
        return _msgs_claude2oai(raw_list)


class NativeOAISession(NativeClaudeSession):
    """Native tool-use over OAI. Inherits Claude Code-style metadata
    (session_id / device_id / user_agent) from ``NativeClaudeSession`` so
    relays expecting the CC fingerprint still get one — only the wire
    protocol is OAI."""

    def raw_ask(self, messages, model=None, api_mode=None, system=None, temperature=None, max_tokens=None,
                tools=None, reasoning_effort=None, stream=None):
        messages = _fix_messages(messages)
        return (yield from _openai_stream(
            self.api_base,
            self.api_key,
            _msgs_claude2oai(messages),
            model or self.model,
            self.api_mode if api_mode is None else api_mode,
            system=self.system if system is None else system,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            tools=self.tools if tools is None else tools,
            reasoning_effort=self.reasoning_effort if reasoning_effort is None else reasoning_effort,
            max_retries=self.max_retries,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            proxies=self.proxies,
            stream=self.stream if stream is None else stream,
        ))
