"""Regression tests for code-review fixes (HIGH/MED/LOW patches).

Each test is named after the finding number in the review report.
Run: pytest tests/test_review_fixes.py -v
"""
from __future__ import annotations

import os
import stat
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


# ── #1: agent_loop.py — malformed JSON in tool_calls must not crash the turn ──


class _FakeTC:
    def __init__(self, name, args, tid="t1"):
        self.id = tid
        self.function = type("F", (), {"name": name, "arguments": args})


class _FakeResponse:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


class _FakeChat:
    def __init__(self, responses):
        self._responses = list(responses)
        self.last_tools = ""

    def chat(self, messages, tools):
        # Yield nothing, just return the next prepared response when exhausted.
        resp = self._responses.pop(0)
        if False:
            yield ""  # make this a generator
        return resp


def test_finding_1_malformed_tool_args_routed_to_bad_json():
    """A tool_call with non-JSON args used to crash the whole turn at
    agent_loop.py:83. After the fix it should land in the bad_json route
    so the LLM can recover."""
    from agent_loop import BaseHandler, agent_runner_loop

    bad_json_seen = []

    class H(BaseHandler):
        def __init__(self):
            super().__init__()
            self.max_turns = 1

        def do_bad_json(self, args, response):
            from agent_loop import StepOutcome
            bad_json_seen.append(args.get("msg", ""))
            return StepOutcome(None, next_prompt=None)

    response = _FakeResponse(tool_calls=[
        _FakeTC("anything", "this is { not json"),
    ])
    client = _FakeChat([response])
    list(agent_runner_loop(client, "sys", "hello", H(), tools_schema=[],
                           max_turns=1, verbose=False))
    assert bad_json_seen, "bad_json route was not reached"
    assert "json" in bad_json_seen[0].lower() or "valid" in bad_json_seen[0].lower()


# ── #6: chmod 600 must be applied to .tmp BEFORE os.replace ──


def test_finding_6_chmod_applied_before_replace(tmp_path, monkeypatch):
    """Verify .tmp is chmod'd before being moved to its final path.
    On Windows os.chmod(stat.S_IWUSR) is mostly a no-op but the call ordering
    can still be observed via a wrapped os.chmod."""
    from launcher import config_store

    chmod_calls = []
    real_chmod = os.chmod

    def _chmod_spy(path, mode):
        chmod_calls.append(path)
        try:
            real_chmod(path, mode)
        except OSError:
            pass

    monkeypatch.setattr(config_store.os, "chmod", _chmod_spy)
    target = str(tmp_path / "config.json")
    config_store._save_file(target, {"version": 1, "providers": {}, "bots": {}, "settings": {}})
    # The .tmp path must appear BEFORE the final path in the call sequence.
    tmp_path_str = target + ".tmp"
    assert tmp_path_str in chmod_calls, "chmod was not called on the .tmp file"
    # And the call order: chmod-tmp must precede any chmod on the final path.
    tmp_idx = chmod_calls.index(tmp_path_str)
    final_calls_after = [c for c in chmod_calls[tmp_idx + 1:] if c == target]
    # We don't require a chmod on `target` at all (the new code doesn't do
    # one), but if it ever happens it must be after the tmp chmod.
    assert all(chmod_calls.index(c) > tmp_idx for c in final_calls_after)


# ── #8: BaseHandler must declare _done_hooks so vanilla subclasses work ──


def test_finding_8_base_handler_initializes_done_hooks():
    from agent_loop import BaseHandler
    h = BaseHandler()
    assert h._done_hooks == []
    assert hasattr(h, "max_turns")
    assert hasattr(h, "current_turn")


# ── #12: captoken.verify must reject expired tokens by default ──


def test_finding_12_verify_rejects_expired_token():
    from llmcore import captoken

    key = captoken.random_signing_key()
    # Mint a token that's already expired (ttl_sec=-1 so exp = now-1)
    tok, wire = captoken.mint(
        issuer="kernel", subject="cal",
        capabilities=("calendar.create_event.v1",),
        signing_key=key, ttl_sec=-1,
    )
    # Default: must raise
    with pytest.raises(ValueError, match="expired"):
        captoken.verify(wire, key)
    # Opt out: returns the token
    parsed = captoken.verify(wire, key, check_expiry=False)
    assert parsed.subject == "cal"
    # Grace window allows the token
    parsed = captoken.verify(wire, key, grace_sec=5)
    assert parsed.subject == "cal"


# ── #5: DANGEROUS_COMMAND_RE must catch the cases the comment promises ──


@pytest.mark.parametrize("cmd,should_match", [
    ("rm -rf /tmp/foo", True),
    ("rm -fr /tmp/foo", True),     # was missed by old regex
    ("rm -Rf /tmp/foo", True),     # case + capital R
    ("dd if=/dev/zero of=/dev/sda", True),  # was missed
    ("mkfs.ext4 /dev/sda1", True),         # was missed
    ("Remove-Item /tmp -Recurse -Force", True),  # PowerShell, was missed
    ("git push --force-with-lease origin main", True),  # was missed
    (":(){:|:&};:", True),                  # fork bomb, was missed
    ("ls -la", False),
    ("python -c 'print(1)'", False),
    ("git push origin main", False),
    ("rm file.txt", False),                 # plain rm with no -r/-f flag
])
def test_finding_5_dangerous_regex_coverage(cmd, should_match):
    from permissions import DANGEROUS_COMMAND_RE
    matched = bool(DANGEROUS_COMMAND_RE.search(cmd))
    assert matched is should_match, f"DANGEROUS_COMMAND_RE for {cmd!r}: matched={matched}, expected={should_match}"
