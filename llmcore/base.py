"""``BaseSession`` — common state for every provider adapter.

Holds connection params (api_key, api_base, proxies, timeouts), the
running history buffer, and provider-agnostic generation knobs (model,
temperature, max_tokens, reasoning_effort, thinking_type). Concrete
adapters override ``raw_ask`` (provider-specific HTTP) and ``make_messages``
(provider-specific message shape).

``_apply_claude_thinking`` lives here despite the "claude" in the name —
the same payload shape (``thinking`` / ``output_config.effort``) is used
by both ``ClaudeSession`` (text protocol) and ``NativeClaudeSession``
(native CC tools). Renaming would cascade through every consumer; punted
to a follow-up cleanup PR.
"""
import json
import threading

from llmcore._history import trim_messages_history
from llmcore._utils import safeprint as print


class BaseSession:
    def __init__(self, cfg):
        self.api_key = cfg['apikey']
        self.api_base = cfg['apibase'].rstrip('/')
        self.model = cfg.get('model', '')
        self.context_win = cfg.get('context_win', 28000)
        self.history = []
        self.lock = threading.Lock()
        self.system = ""
        self.name = cfg.get('name', self.model)
        proxy = cfg.get('proxy')
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.max_retries = max(0, int(cfg.get('max_retries', 1)))
        self.stream = cfg.get('stream', True)
        default_ct, default_rt = (5, 30) if self.stream else (10, 240)
        self.connect_timeout = max(1, int(cfg.get('connect_timeout', cfg.get('timeout', default_ct))))
        self.read_timeout = max(5, int(cfg.get('read_timeout', default_rt)))

        def _enum(key, valid):
            v = cfg.get(key)
            v = None if v is None else str(v).strip().lower()
            return v if not v or v in valid else print(f"[WARN] Invalid {key} {v!r}, ignored.")

        self.reasoning_effort = _enum('reasoning_effort', {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'})
        self.thinking_type = _enum('thinking_type', {'adaptive', 'enabled', 'disabled'})
        self.thinking_budget_tokens = cfg.get('thinking_budget_tokens')
        mode = str(cfg.get('api_mode', 'chat_completions')).strip().lower().replace('-', '_')
        self.api_mode = 'responses' if mode in ('responses', 'response') else 'chat_completions'
        self.disable_response_storage = bool(cfg.get('disable_response_storage', False))
        self.temperature = cfg.get('temperature', 1)
        self.max_tokens = cfg.get('max_tokens')

    def _apply_claude_thinking(self, payload):
        """Inject ``thinking`` and ``output_config.effort`` if configured.

        Used by Anthropic adapters (both text and native-CC paths). Despite
        the name, the only thing Claude-specific here is the shape of the
        payload keys — the underlying knobs are universal."""
        if self.thinking_type:
            thinking = {"type": self.thinking_type}
            if self.thinking_type == 'enabled':
                if self.thinking_budget_tokens is None:
                    print("[WARN] thinking_type='enabled' requires thinking_budget_tokens, ignored.")
                else:
                    thinking["budget_tokens"] = self.thinking_budget_tokens
                    payload["thinking"] = thinking
            else:
                payload["thinking"] = thinking
        if self.reasoning_effort:
            effort = {'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'max'}.get(self.reasoning_effort)
            if effort:
                payload["output_config"] = {"effort": effort}
            else:
                print(f"[WARN] reasoning_effort {self.reasoning_effort!r} is unsupported for Claude output_config.effort, ignored.")

    def ask(self, prompt, stream=False):
        """Default ``ask`` for text-protocol sessions (ClaudeSession,
        LLMSession). Native sessions override this with a ``MockResponse``-
        building variant — see ``adapters/anthropic.NativeClaudeSession.ask``."""
        def _ask_gen():
            with self.lock:
                self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
                trim_messages_history(self.history, self.context_win)
                messages = self.make_messages(self.history)
            content_blocks = None
            content = ''
            gen = self.raw_ask(messages)
            try:
                while True:
                    chunk = next(gen)
                    content += chunk
                    yield chunk
            except StopIteration as e:
                content_blocks = e.value or []
            if len(content_blocks) > 1:
                print(f"[DEBUG BaseSession.ask] content_blocks: {content_blocks}")
            for block in (content_blocks or []):
                if block.get('type', '') == 'tool_use':
                    tu = {'name': block.get('name', ''), 'arguments': block.get('input', {})}
                    yield f'<tool_use>{json.dumps(tu, ensure_ascii=False)}</tool_use>'
            if not content.startswith("!!!Error:"):
                self.history.append({"role": "assistant", "content": [{"type": "text", "text": content}]})

        return _ask_gen() if stream else ''.join(list(_ask_gen()))
