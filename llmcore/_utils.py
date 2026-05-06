"""Internal utilities: safe print, URL building, JSON parsing, response logging.

No dependencies on other llmcore submodules — kept tight so anyone (sessions,
adapters, mixin) can import from here without circular-import worries. The
``print = safeprint`` pattern is repeated at the top of every submodule so
that broken pipes / closed terminals (Windows long-running session edge case)
never crash the agent loop.
"""
import json
import os
import re
from datetime import datetime

from project_context import get_model_responses_dir

# Replace the builtin ``print`` inside this module so callers that ``from
# llmcore._utils import safeprint as print`` get the OSError-tolerant variant
# transparently. Don't touch the global builtin — that's invasive and other
# packages rely on the real one.
_oldprint = print


def safeprint(*argv):
    try:
        _oldprint(*argv)
    except OSError:
        # Closed stdout / broken pipe — common when the agent is launched as
        # a subprocess and the parent goes away. Swallow rather than crash.
        pass


# Make this module's own ``print`` calls OSError-safe too.
print = safeprint


def auto_make_url(base, path):
    """Append ``path`` to ``base`` deciding when to inject ``/v1/``.

    ``base`` ending in ``$`` is treated as "verbatim, no path appended" — the
    config wants a single fixed endpoint. Otherwise we look for a ``/vN``
    segment to decide whether to inject ``/v1/`` automatically.
    """
    b, p = base.rstrip('/'), path.strip('/')
    if b.endswith('$'):
        return b[:-1].rstrip('/')
    if b.endswith(p):
        return b
    return f"{b}/{p}" if re.search(r'/v\d+(/|$)', b) else f"{b}/v1/{p}"


def tryparse(json_str):
    """Best-effort JSON repair. Tries: as-is, strip code fences, drop trailing
    char, truncate to last ``}``. Last attempt raises if everything fails so
    the caller can decide whether to fall back to a plain text reply."""
    try:
        return json.loads(json_str)
    except Exception:
        pass
    json_str = json_str.strip().strip('`').replace('json\n', '', 1).strip()
    try:
        return json.loads(json_str)
    except Exception:
        pass
    try:
        return json.loads(json_str[:-1])
    except Exception:
        pass
    if '}' in json_str:
        json_str = json_str[:json_str.rfind('}') + 1]
    return json.loads(json_str)


def _write_llm_log(label, content):
    """Append a (timestamped) record of one prompt or response to disk.

    Used by ToolClient / NativeToolClient for offline debugging. Per-PID so
    multiple concurrent agents don't interleave each other's transcripts.
    """
    log_dir = get_model_responses_dir(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f'model_responses_{os.getpid()}.txt')
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a', encoding='utf-8', errors='replace') as f:
        f.write(f"=== {label} === {ts}\n{content}\n\n")


def _write_llm_log_if_enabled(backend, label, content):
    """Per-backend opt-out — Anthropic API forbids long-term storage of
    response data when ``disable_response_storage`` is set. Don't write
    anything in that case."""
    if getattr(backend, 'disable_response_storage', False):
        return
    _write_llm_log(label, content)
