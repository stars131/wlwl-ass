"""``MixinSession`` — multi-session failover with spring-back to primary.

Wraps an ordered list of backends. Tries primary; on error, falls through
to secondary, tertiary, etc. After ``spring_back`` seconds of staying on
a non-primary, springs back to retry primary on next call. Useful when the
primary occasionally hiccups (rate-limit / 5xx) but is otherwise the
preferred destination.

Native and non-native sessions can't mix in the same MixinSession because
their ``ask`` shapes differ (native returns MockResponse, text returns a
chunk generator). The constructor enforces this with an assert.
"""
import copy
import time

from llmcore._messages import openai_tools_to_claude
from llmcore._utils import safeprint as print
from llmcore.adapters.anthropic import NativeClaudeSession


class MixinSession:
    """Multi-session fallback with spring-back to primary."""

    def __init__(self, all_sessions, cfg):
        self._retries, self._base_delay = cfg.get('max_retries', 3), cfg.get('base_delay', 1.5)
        self._spring_sec = cfg.get('spring_back', 300)
        self._sessions = [all_sessions[i].backend if isinstance(i, int) else
                          next(s.backend for s in all_sessions if type(s) is not dict and s.backend.name == i) for i in cfg.get('llm_nos', [])]
        is_native = lambda s: 'Native' in s.__class__.__name__
        groups = {is_native(s) for s in self._sessions}
        assert len(groups) == 1, f"MixinSession: sessions must be in same group (Native or non-Native), got {[type(s).__name__ for s in self._sessions]}"
        self.name = '|'.join(s.name for s in self._sessions)
        self._sessions[0] = copy.copy(self._sessions[0])
        self._orig_raw_asks = [s.raw_ask for s in self._sessions]
        self._sessions[0].raw_ask = self._raw_ask
        self.model = getattr(self._sessions[0], 'model', None)
        self._cur_idx, self._switched_at = 0, 0.0

    def __getattr__(self, name):
        return getattr(self._sessions[0], name)

    _BROADCAST_ATTRS = frozenset({'system', 'tools', 'temperature', 'max_tokens', 'reasoning_effort', 'history'})

    def __setattr__(self, name, value):
        if name in self._BROADCAST_ATTRS:
            for s in self._sessions:
                # Native Claude sessions need Claude-shaped tool schemas;
                # everyone else gets the raw OAI form. Cheap to translate
                # per-session at set-time so the rest of the agent loop
                # doesn't need to know.
                v = openai_tools_to_claude(value) if name == 'tools' and type(s) is NativeClaudeSession else value
                setattr(s, name, v)
        else:
            object.__setattr__(self, name, value)

    @property
    def primary(self):
        return self._sessions[0]

    def _pick(self):
        if self._cur_idx and time.time() - self._switched_at > self._spring_sec:
            self._cur_idx = 0
        return self._cur_idx

    def _raw_ask(self, *args, **kwargs):
        base, n = self._pick(), len(self._sessions)
        test_error = lambda x: isinstance(x, str) and x.lstrip().startswith(('!!!Error:', '[Error:'))
        for attempt in range(self._retries + 1):
            idx = (base + attempt) % n
            gen = self._orig_raw_asks[idx](*args, **kwargs)
            print(f'[MixinSession] Using session ({self._sessions[idx].name})')
            last_chunk, return_val, yielded = None, [], False
            try:
                while True:
                    chunk = next(gen)
                    last_chunk = chunk
                    if not yielded and test_error(chunk):
                        continue
                    yield chunk
                    yielded = True
            except StopIteration as e:
                return_val = e.value or []
            is_err = test_error(last_chunk)
            if not is_err:
                if attempt > 0:
                    self._cur_idx = idx
                    self._switched_at = time.time()
                return return_val
            if attempt >= self._retries:
                yield last_chunk
                return return_val
            nxt = (base + attempt + 1) % n
            if nxt == base:  # full round failed, delay before next
                rnd = (attempt + 1) // n
                delay = min(30, self._base_delay * (1.5 ** rnd))
                print(f'[MixinSession] {last_chunk[:80]}, round {rnd} exhausted, retry in {delay:.1f}s')
                time.sleep(delay)
            else:
                print(f'[MixinSession] {last_chunk[:80]}, retry {attempt+1}/{self._retries} (s{idx}→s{nxt})')
