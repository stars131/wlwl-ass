"""Persistent approval allowlist — pattern-based "user already approved this".

Reduces interrupt fatigue: once the user approves ``code_run`` for ``git
status``, the allowlist remembers it and future ``git status`` runs go
through without a prompt. Different from the existing
``session_allow_tools`` set in two ways:

  1. **Per-pattern, not per-tool**: approving ``git status`` doesn't also
     approve ``git push --force`` even though both are ``code_run``.
     Patterns match against the tool's argument string (typically the
     code/script for ``code_run``).
  2. **Persistent across sessions**: stored under
     ``memory/approval_allowlist.json`` so the user's "always allow git
     status" sticks across agent restarts.

Storage format:
  ```json
  {
    "version": 1,
    "rules": [
      {
        "id": "<uuid>",
        "tool": "code_run",
        "pattern": "^git\\s+status",
        "note": "git read-only inspection",
        "created_at": 1234567890.0,
        "last_used_at": 1234567899.0,
        "use_count": 17
      }
    ]
  }
  ```

Pattern is a Python regex (compiled lazily; never executed against
untrusted input — patterns are user-authored). Match semantics: regex
``re.search`` against the *primary string argument* (``code`` /
``script`` / ``path``) — whatever ``ToolPermissionRequest.preview``
would build.

The allowlist file is treated as user data — atomic writes (write to
``.tmp`` + rename) and gracefully degrades to "no rules" on a corrupt
file rather than aborting the agent loop. Edits via ``add_rule`` /
``remove_rule`` go through the same atomic path.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


_DEFAULT_REL_PATH = os.path.join("memory", "approval_allowlist.json")
_VERSION = 1


@dataclass
class Rule:
    id: str
    tool: str
    pattern: str = ""
    note: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0

    @classmethod
    def new(cls, tool: str, pattern: str = "", note: str = "") -> "Rule":
        now = time.time()
        return cls(
            id=str(uuid.uuid4()),
            tool=tool,
            pattern=pattern,
            note=note,
            created_at=now,
            last_used_at=0.0,
            use_count=0,
        )


@dataclass
class ApprovalAllowlist:
    """In-memory state mirrored to disk on every mutation.

    Initialize with the project root; the allowlist file path is computed
    relative to it. Pass ``path`` directly for tests / non-standard layouts.
    """
    path: str = ""
    _rules: list[Rule] = field(default_factory=list)
    _patterns_cache: dict[str, re.Pattern] = field(default_factory=dict, repr=False)

    @classmethod
    def from_project_root(cls, project_root: str) -> "ApprovalAllowlist":
        return cls.load(os.path.join(project_root, _DEFAULT_REL_PATH))

    @classmethod
    def load(cls, path: str) -> "ApprovalAllowlist":
        """Read the allowlist from disk. Missing / corrupt → empty list (the
        whole point of this is to never crash the agent loop)."""
        inst = cls(path=path)
        if not os.path.isfile(path):
            return inst
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            # Corrupt — silently start fresh. Don't overwrite the file
            # automatically; user might want to repair it manually.
            return inst
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return inst
        for r in raw.get("rules", []):
            if not isinstance(r, dict):
                continue
            try:
                inst._rules.append(Rule(
                    id=str(r.get("id") or uuid.uuid4()),
                    tool=str(r.get("tool", "")),
                    pattern=str(r.get("pattern", "")),
                    note=str(r.get("note", "")),
                    created_at=float(r.get("created_at", 0.0) or 0.0),
                    last_used_at=float(r.get("last_used_at", 0.0) or 0.0),
                    use_count=int(r.get("use_count", 0) or 0),
                ))
            except (TypeError, ValueError):
                continue
        return inst

    def _save(self) -> None:
        """Atomic write: serialize to ``.tmp``, then ``os.replace``. Replace
        is atomic on Windows (since 3.3 on Python's wrapper) and POSIX."""
        if not self.path:
            return  # in-memory only mode
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        payload: dict[str, Any] = {
            "version": _VERSION,
            "rules": [asdict(r) for r in self._rules],
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ─── Mutation API ────────────────────────────────────────────────────

    def add_rule(self, tool: str, pattern: str = "", note: str = "") -> Rule:
        """Add a rule and persist. ``pattern`` is a Python regex; an empty
        pattern means "any invocation of this tool" — useful for very
        low-risk read-only tools. Validates the regex eagerly so a
        malformed pattern fails at add-time, not at first match."""
        if pattern:
            re.compile(pattern)  # raises re.error on malformed pattern
        rule = Rule.new(tool=tool, pattern=pattern, note=note)
        self._rules.append(rule)
        self._save()
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        """Remove by id. Returns True if a rule was removed."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        removed = len(self._rules) != before
        if removed:
            # Also drop the cached compiled regex if any.
            for k in list(self._patterns_cache):
                if k.startswith(rule_id + ":"):
                    del self._patterns_cache[k]
            self._save()
        return removed

    def record_use(self, rule_id: str) -> None:
        """Bump the last_used_at + use_count for ``rule_id``. Persist."""
        for r in self._rules:
            if r.id == rule_id:
                r.last_used_at = time.time()
                r.use_count += 1
                self._save()
                return

    # ─── Read API ────────────────────────────────────────────────────────

    def list_rules(self) -> list[Rule]:
        """Return a shallow copy so callers can't mutate internal state."""
        return list(self._rules)

    def match(self, tool: str, args: dict[str, Any]) -> Rule | None:
        """Find a rule that authorizes this (tool, args) pair. ``None`` →
        no rule matches; caller falls through to interactive prompt.

        Match logic:
          * Tool name must equal ``rule.tool`` (case-sensitive).
          * If ``rule.pattern`` is empty, any invocation matches.
          * Otherwise, ``re.search(pattern, primary_arg_str)`` decides.
            ``primary_arg_str`` is built from common argument keys —
            ``code`` / ``script`` (code_run), ``path`` (file_*) — fallback
            to a stable repr of the args dict.

        Returns the *first* matching rule, not the most-specific. Order
        of rules in the list is the order they were added; the user can
        reorder by removing + re-adding."""
        primary = self._primary_arg_str(tool, args)
        for r in self._rules:
            if r.tool != tool:
                continue
            if not r.pattern:
                return r
            try:
                pat = self._compiled(r)
            except re.error:
                # Stored pattern somehow became malformed — skip it
                # rather than crash. add_rule normally rejects these.
                continue
            if pat.search(primary):
                return r
        return None

    @staticmethod
    def _primary_arg_str(tool: str, args: dict[str, Any]) -> str:
        if tool == "code_run":
            return str(args.get("code") or args.get("script") or "")
        if tool in ("file_patch", "file_write", "file_read"):
            return str(args.get("path") or "")
        if tool == "web_execute_js":
            return str(args.get("script") or "")
        # Generic fallback. Sort keys so the same dict gives the same string.
        try:
            return json.dumps(args, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return repr(args)

    def _compiled(self, rule: Rule) -> re.Pattern:
        key = f"{rule.id}:{rule.pattern}"
        cached = self._patterns_cache.get(key)
        if cached is not None:
            return cached
        compiled = re.compile(rule.pattern)
        self._patterns_cache[key] = compiled
        return compiled
