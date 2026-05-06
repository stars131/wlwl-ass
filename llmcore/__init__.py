"""``llmcore`` package — provider-agnostic LLM session management.

Public API (preserved exactly from the pre-refactor monolithic ``llmcore.py``):

  * Sessions: ``ClaudeSession``, ``LLMSession``, ``NativeClaudeSession``,
    ``NativeOAISession``, ``MixinSession``
  * Clients: ``ToolClient``, ``NativeToolClient``
  * Mock response shape: ``MockResponse``, ``MockToolCall``, ``MockFunction``
  * Mykey loading: ``mykeys`` (lazy, PEP 562), ``reload_mykeys``, ``_load_mykeys``
  * Helpers: ``compress_history_tags``, ``trim_messages_history``,
    ``auto_make_url``, ``_openai_stream``

Layout:
  * Provider-specific code lives in ``llmcore.adapters.{anthropic,openai}``.
    Adding Bedrock / Vertex / Azure means a new file there.
  * Shared internals (history compression, message conversion, token usage,
    Mock response shape) live in ``_history`` / ``_messages`` / ``_usage`` /
    ``_mock`` / ``_utils``.
  * ``BaseSession`` is in ``base``.
  * ``MixinSession`` lives in ``mixin``; ``ToolClient`` / ``NativeToolClient``
    in ``clients``.

This file is the only one consumers should ``import llmcore.X`` from. Direct
imports from ``llmcore.adapters.openai`` etc. are private and may change.
"""
import urllib3

import requests  # noqa: F401 — re-exported so ``patch('llmcore.requests.post')``
                 # in tests still resolves; mock then mutates the real
                 # requests module which adapters/openai.py imports from.

# Submodule re-exports — keep at the top so ``from llmcore import X`` resolves
# regardless of where X lives now. Order matters only for import sequencing
# (mykeys/usage have module-level state; submodules with ``print = safeprint``
# need _utils first; adapters depend on base/_messages; mixin depends on
# adapters.anthropic).
from llmcore._utils import (
    auto_make_url,
    safeprint,
    tryparse,
    _write_llm_log,
    _write_llm_log_if_enabled,
)
from llmcore._keys import (
    _candidate_mykey_paths,
    _candidate_mykey_signature,
    _load_mykeys,
    reload_mykeys,
)
from llmcore._history import (
    compress_history_tags,
    _sanitize_leading_user_msg,
    trim_messages_history,
)
from llmcore._usage import (
    _record_usage,
    _accumulate_usage,
    get_token_usage,
    reset_token_usage,
    _USAGE_RECENT_CAP,
)
from llmcore._messages import (
    _msgs_claude2oai,
    openai_tools_to_claude,
    _fix_messages,
    _drop_unsigned_thinking,
    _keep_claude_block,
    _try_parse_tool_args,
)
from llmcore._mock import (
    MockFunction,
    MockToolCall,
    MockResponse,
    _parse_text_tool_calls,
)
from llmcore.base import BaseSession
from llmcore.adapters.anthropic import (
    _parse_claude_sse,
    ClaudeSession,
    NativeClaudeSession,
)
from llmcore.adapters.openai import (
    _RESP_CACHE_KEY,
    _parse_openai_sse,
    _parse_openai_json,
    _stamp_oai_cache_markers,
    _to_responses_input,
    _prepare_oai_tools,
    _openai_stream,
    LLMSession,
    NativeOAISession,
)
from llmcore.clients import (
    ToolClient,
    NativeToolClient,
    THINKING_PROMPT_EN,
    THINKING_PROMPT_ZH,
)
from llmcore.mixin import MixinSession

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def __getattr__(name):
    """PEP 562 lazy attribute. ``llmcore.mykeys`` triggers a (fast) reload-on-
    demand the first time it's read, then returns the cached dict afterwards.

    Stays here in the package ``__init__`` so ``from llmcore import mykeys``
    works the same as before — the binding lives in ``_keys`` module's
    globals; ``reload_mykeys()[0]`` returns it."""
    if name == 'mykeys':
        return reload_mykeys()[0]
    raise AttributeError(f"module 'llmcore' has no attribute {name}")
