"""Message-format conversion + protocol normalization.

Cross-provider plumbing: the agent stores history in Anthropic-shaped
content blocks (the lingua franca because it's the most expressive — text /
image / tool_use / tool_result / thinking with signature). Converters here
adapt that to OpenAI's chat-completions format, repair malformed sequences
before send, and normalize tool schema between the two.
"""
import json
import re


def _try_parse_tool_args(raw):
    """Parse tool args string; split concatenated JSON objects like ``{..}{..}``
    if needed. Returns a list of parsed dicts (length ≥ 1).

    Some providers stream multiple tool_call args concatenated without proper
    delimiters — split on ``}{`` boundaries to recover."""
    if not raw:
        return [{}]
    try:
        return [json.loads(raw)]
    except Exception:
        pass
    parts = re.split(r'(?<=\})(?=\{)', raw)
    if len(parts) > 1:
        parsed = []
        for p in parts:
            try:
                parsed.append(json.loads(p))
            except Exception:
                return [{"_raw": raw}]
        return parsed
    return [{"_raw": raw}]


def _keep_claude_block(b):
    """Drop ``thinking`` blocks that have no signature — Anthropic API
    rejects unsigned thinking on subsequent turns."""
    return not isinstance(b, dict) or b.get("type") != "thinking" or b.get("signature")


def _drop_unsigned_thinking(messages):
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            m["content"] = [b for b in c if _keep_claude_block(b)]
    return messages


def _fix_messages(messages):
    """Make ``messages`` valid for Claude API: alternating roles, paired
    tool_use/tool_result.

    Two repair passes:
      * Adjacent same-role messages get merged with a newline separator.
      * If an assistant turn emitted tool_use IDs that the next user turn
        didn't return tool_result for, fabricate ``(error)`` tool_results so
        the API doesn't reject the request. The model will see them and
        usually ask the user to retry.
    """
    if not messages:
        return messages
    _wrap = lambda c: c if isinstance(c, list) else [{"type": "text", "text": str(c)}]
    fixed = []
    for m in messages:
        if fixed and m['role'] == fixed[-1]['role']:
            fixed[-1] = {**fixed[-1], 'content': _wrap(fixed[-1]['content']) + [{"type": "text", "text": "\n"}] + _wrap(m['content'])}
            continue
        if fixed and fixed[-1]['role'] == 'assistant' and m['role'] == 'user':
            uses = [b.get('id') for b in fixed[-1].get('content', []) if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('id')]
            has = {b.get('tool_use_id') for b in _wrap(m['content']) if isinstance(b, dict) and b.get('type') == 'tool_result'}
            miss = [uid for uid in uses if uid not in has]
            if miss:
                m = {**m, 'content': [{"type": "tool_result", "tool_use_id": uid, "content": "(error)"} for uid in miss] + _wrap(m['content'])}
        fixed.append(m)
    while fixed and fixed[0]['role'] != 'user':
        fixed.pop(0)
    return fixed


def _msgs_claude2oai(messages):
    """Translate Anthropic content-block messages to OpenAI chat-completions
    format. Splits a single Anthropic ``user`` message containing both text
    and ``tool_result`` blocks into multiple OAI messages — OAI requires
    tool results in their own ``role:'tool'`` messages.
    """
    result = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        if role == "assistant":
            text_parts, tool_calls, reasoning = [], [], ""
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking" and b.get("thinking"):
                    reasoning = b["thinking"]
                elif b.get("type") == "text" and b.get("text"):
                    text_parts.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or '', "type": "function",
                        "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}
                    })
            m = {"role": "assistant"}
            if reasoning:
                m["reasoning_content"] = reasoning
            if text_parts:
                m["content"] = text_parts
            else:
                m["content"] = ""
            if tool_calls:
                m["tool_calls"] = tool_calls
            result.append(m)
        elif role == "user":
            text_parts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    if text_parts:
                        result.append({"role": "user", "content": text_parts})
                        text_parts = []
                    tr = b.get("content", "")
                    if isinstance(tr, list):
                        tr = "\n".join(x.get("text", "") for x in tr if isinstance(x, dict) and x.get("type") == "text")
                    result.append({"role": "tool", "tool_call_id": b.get("tool_use_id") or '', "content": tr if isinstance(tr, str) else str(tr)})
                elif b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        text_parts.append({"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
                elif b.get("type") == "image_url":
                    text_parts.append(b)
                elif b.get("type") == "text" and b.get("text"):
                    text_parts.append({"type": "text", "text": b.get("text", "")})
            if text_parts:
                result.append({"role": "user", "content": text_parts})
        else:
            result.append(msg)
    return result


def openai_tools_to_claude(tools):
    """``[{type:'function', function:{name,description,parameters}}]`` →
    ``[{name, description, input_schema}]``. Idempotent — already-Claude
    schemas pass through unchanged."""
    result = []
    for t in tools:
        if 'input_schema' in t:
            result.append(t)
            continue  # already Claude format
        fn = t.get('function', t)
        result.append({
            'name': fn['name'],
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters', {'type': 'object', 'properties': {}}),
        })
    return result
