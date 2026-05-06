"""History compression / trimming. Keeps the agent's working memory small
enough to stay under each session's ``context_win`` while preserving the
last few turns verbatim.

Two strategies stack:
  * ``compress_history_tags`` — every Nth turn, in-place truncate large
    payload-tagged regions (``<thinking>`` / ``<tool_use>`` / ``<tool_result>``)
    inside *older* messages. Recent N messages are left untouched.
  * ``trim_messages_history`` — when the running char count blows past
    3× the context window, drop oldest messages (after compression),
    re-anchoring on the next ``user`` message. Trim breaks the prompt cache,
    so we compress more aggressively in the same pass.
"""
import json
import re

from llmcore._utils import safeprint as print


def compress_history_tags(messages, keep_recent=10, max_len=800, force=False):
    """Compress ``<thinking>``/``<tool_use>``/``<tool_result>`` tags in older
    messages to save tokens. Function attribute ``_cd`` acts as a counter so
    we only run every 5th call (cheap; keeps the pass amortized)."""
    compress_history_tags._cd = getattr(compress_history_tags, '_cd', 0) + 1
    if force:
        compress_history_tags._cd = 0
    if compress_history_tags._cd % 5 != 0:
        return messages
    _before = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    _pats = {tag: re.compile(rf'(<{tag}>)([\s\S]*?)(</{tag}>)') for tag in ('thinking', 'think', 'tool_use', 'tool_result')}
    _hist_pat = re.compile(r'<(history|key_info)>[\s\S]*?</\1>')

    def _trunc_str(s):
        return s[:max_len // 2] + '\n...[Truncated]...\n' + s[-max_len // 2:] if isinstance(s, str) and len(s) > max_len else s

    def _trunc(text):
        text = _hist_pat.sub(lambda m: f'<{m.group(1)}>[...]</{m.group(1)}>', text)
        for pat in _pats.values():
            text = pat.sub(lambda m: m.group(1) + _trunc_str(m.group(2)) + m.group(3), text)
        return text

    for i, msg in enumerate(messages):
        if i >= len(messages) - keep_recent:
            break
        key = 'content' if 'content' in msg else ('prompt' if 'prompt' in msg else None)
        if key is None:
            continue
        c = msg[key]
        if isinstance(c, str):
            msg[key] = _trunc(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get('type')
                if t == 'text' and isinstance(b.get('text'), str):
                    b['text'] = _trunc(b['text'])
                elif t == 'tool_result':
                    tc = b.get('content')
                    if isinstance(tc, str):
                        b['content'] = _trunc_str(tc)
                    elif isinstance(tc, list):
                        for sub in tc:
                            if isinstance(sub, dict) and sub.get('type') == 'text':
                                sub['text'] = _trunc_str(sub.get('text'))
                elif t == 'tool_use' and isinstance(b.get('input'), dict):
                    for k, v in b['input'].items():
                        b['input'][k] = _trunc_str(v)
    print(f"[Cut] {_before} -> {sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)}")
    return messages


def _sanitize_leading_user_msg(msg):
    """Rewrite ``tool_result`` blocks in a leading user message to plain text.

    After we drop older turns, a ``user`` message that starts with a
    ``tool_result`` would dangle (no preceding ``tool_use``). Anthropic +
    OpenAI APIs both reject that. Flatten such blocks to plain text so the
    history stays valid.
    """
    msg = dict(msg)  # shallow copy outer dict
    content = msg.get('content')
    if not isinstance(content, list):
        return msg
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'tool_result':
            c = block.get('content', '')
            if isinstance(c, list):  # content itself can be a list of {type:text,text:...}
                texts.extend(b.get('text', '') for b in c if isinstance(b, dict))
            else:
                texts.append(str(c))
        elif block.get('type') == 'text':
            texts.append(block.get('text', ''))
    msg['content'] = [{"type": "text", "text": '\n'.join(t for t in texts if t)}]
    return msg


def trim_messages_history(history, context_win):
    """Compress + drop-old-messages until ``cost ≤ 0.6 × 3 × context_win``.

    Char count is a coarse proxy for tokens (≈ 1:1 for Latin, 1:0.5 for CJK)
    that's good enough — the 3× safety margin absorbs the imprecision."""
    compress_history_tags(history)
    cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
    print(f'[Debug] Current context: {cost} chars, {len(history)} messages.')
    if cost > context_win * 3:
        compress_history_tags(history, keep_recent=4, force=True)  # trim breaks cache, so compress more btw
        target = context_win * 3 * 0.6
        while len(history) > 5 and cost > target:
            history.pop(0)
            while history and history[0].get('role') != 'user':
                history.pop(0)
            if history and history[0].get('role') == 'user':
                history[0] = _sanitize_leading_user_msg(history[0])
            cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
        print(f'[Debug] Trimmed context, current: {cost} chars, {len(history)} messages.')
