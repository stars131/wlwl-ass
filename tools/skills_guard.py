"""Prompt-injection guard for skill / SOP content fetched from untrusted
sources (sophub uploads, remote fetches, etc.).

Inject content from third-party SOPs into a running agent's context only
after this guard passes. Inputs we worry about:

  1. **Direct override attempts** — the model is told to forget its
     previous instructions and obey new ones embedded in the SOP.
     ``"Ignore previous instructions"``, ``"忽略以前的指令"``, etc.

  2. **Role hijack** — re-cast the model as a different agent.
     ``"You are now ChatGPT-jailbreak"``, ``"扮演 DAN"``.

  3. **Inline system prompts** — disguise an instruction as another
     system message. ``"<|system|>"``, ``"[SYSTEM]"``, ``"<|im_start|>system"``.

  4. **Encoding tricks** — RTL override, zero-width characters, or
     base64-encoded payloads that decode to imperatives.

We classify findings into ``warn`` (heuristic) and ``fail`` (definitively
malicious) so callers can choose: warn → log + render anyway, fail →
suppress entirely. The default ``scan`` returns the structured report;
callers decide policy.

Stdlib-only, no Anthropic-style "constitutional AI" classifier. This is
a lint, not a verifier — a determined attacker can probably evade us;
the goal is to catch the obvious low-effort attacks that make up the
overwhelming majority of real-world prompt injection in the wild.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


Severity = Literal["fail", "warn"]


@dataclass
class Finding:
    """One guard rule fired. ``span`` is the matched substring (clipped to
    120 chars) so a UI can show the user *what* tripped the rule."""
    rule_id: str
    severity: Severity
    detail: str
    span: str = ""


@dataclass
class ScanResult:
    safe: bool = True
    findings: list[Finding] = field(default_factory=list)

    def add(self, f: Finding) -> None:
        self.findings.append(f)
        if f.severity == "fail":
            self.safe = False


# ─── Patterns ────────────────────────────────────────────────────────────
#
# Compile once at import time — this module's hot path is the agent loop.
# Keep the rule set small enough that every single one earns its place.
# A noisy guard whose ``warn`` users learn to ignore is worse than no guard.

# 1) Direct override — bilingual.
_OVERRIDE_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|messages?|rules?)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|messages?|rules?)", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|messages?|rules?)", re.IGNORECASE),
    re.compile(r"override\s+(all\s+)?(the\s+)?(previous|prior|earlier|system)\s+(instructions?|prompts?|messages?|rules?)", re.IGNORECASE),
    # Chinese — same intent, common phrasings. ``的[^，。]{0,4}?`` allows
    # a short interjection like ``忽略以前的所有指令`` (lit. "ignore previous
    # ALL instructions") between ``的`` and the target noun.
    re.compile(r"忽略(以前|之前|上述|上文|前面)的?[^，。]{0,6}?(指令|提示|消息|规则|系统提示)"),
    re.compile(r"忘记(以前|之前|上述|上文|前面)的?[^，。]{0,6}?(指令|提示|规则|系统提示)"),
    re.compile(r"不要(理会|遵循)(以前|之前|上述|上文)"),
]

# 2) Role hijack.
_ROLE_PATTERNS = [
    re.compile(r"\b(you|从现在起|从此|now)\s*(are|是|成为)\s+(now\s+)?[\"']?(DAN|jailbreak|developer\s+mode|无限制|越狱|root|admin|sudo)\b", re.IGNORECASE),
    re.compile(r"\b(act|pretend|behave|respond)\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?[\"']?[A-Z]", re.IGNORECASE),  # leading capital ≈ proper noun
    re.compile(r"扮演.{0,20}(模式|身份|角色|管理员|root)"),
]

# 3) Inline-system-prompt injection — the smoking-gun substrings.
_SYSTEM_TOKEN_PATTERNS = [
    re.compile(r"<\|(?:im_start|im_end)\|>\s*(?:system|developer)", re.IGNORECASE),  # ChatML
    re.compile(r"<\|system\|>", re.IGNORECASE),
    re.compile(r"\[\s*system\s*\]\s*[:：]", re.IGNORECASE),  # [SYSTEM]:
    re.compile(r"\[/?INST\]"),  # Llama-2 / Mistral instruction tags
    re.compile(r"<<\s*SYS\s*>>"),  # Llama instruct
    re.compile(r"<<<\s*system\s*>>>", re.IGNORECASE),
]

# 4) Encoding / glyph tricks.
_RTL_OVERRIDE_RE = re.compile("[\u202d\u202e\u2066\u2067\u2068]")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u200e\u200f\ufeff]")
# Base64 long enough to plausibly carry an imperative payload (~50 chars
# decoded → at least 68-char b64). Shorter strings are noise. Use
# lookaround instead of \b so trailing ``=`` padding (non-word char)
# doesn't break the right boundary.
_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{68,}={0,2}(?![A-Za-z0-9+/=])")
_SUSPICIOUS_DECODED_RE = re.compile(
    r"(ignore|disregard|forget|jailbreak|sudo|exec|eval|忽略|忘记|越狱)",
    re.IGNORECASE,
)


def scan(text: str) -> ScanResult:
    """Single-pass scan. Returns a ``ScanResult`` — never raises."""
    result = ScanResult()
    if not text:
        return result

    for rule_id, patterns, severity, detail in (
        ("inj.override", _OVERRIDE_PATTERNS, "fail",
         "Tries to override or erase the agent's existing instructions."),
        ("inj.role_hijack", _ROLE_PATTERNS, "fail",
         "Tries to re-cast the agent as a different (often unrestricted) persona."),
        ("inj.system_token", _SYSTEM_TOKEN_PATTERNS, "fail",
         "Embeds a model-specific system-prompt delimiter (ChatML / Llama / [SYSTEM])."),
    ):
        for pat in patterns:
            m = pat.search(text)
            if m:
                result.add(Finding(rule_id, severity, detail, _clip(m.group(0))))
                break  # one finding per category is enough

    if _RTL_OVERRIDE_RE.search(text):
        result.add(Finding(
            "inj.rtl_override", "warn",
            "Contains an RTL/LTR override codepoint — can hide text in renderings.",
            "(non-printable)",
        ))
    if _ZERO_WIDTH_RE.search(text):
        result.add(Finding(
            "inj.zero_width", "warn",
            "Contains zero-width or BOM characters — can smuggle hidden content.",
            "(non-printable)",
        ))

    # Base64 with a suspicious decoded payload. We only complain when both
    # tests fire — a long b64 token alone is just an asset; an imperative
    # decoded payload alone is fine because it's already plain text.
    for m in _BASE64_RE.finditer(text):
        try:
            import base64 as _b64
            decoded = _b64.b64decode(m.group(0), validate=False).decode("utf-8", errors="ignore")
        except Exception:
            decoded = ""
        if decoded and _SUSPICIOUS_DECODED_RE.search(decoded):
            result.add(Finding(
                "inj.base64_imperative", "warn",
                f"Base64 string decodes to imperative-looking text: {decoded[:80]!r}.",
                _clip(m.group(0)),
            ))
            break

    return result


def _clip(s: str, n: int = 120) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def render_findings(result: ScanResult) -> str:
    """Format a ``ScanResult`` as a human-readable banner. Returns ``""``
    when nothing fired so the caller can ``if banner: print(banner)``."""
    if not result.findings:
        return ""
    lines: list[str] = []
    fails = [f for f in result.findings if f.severity == "fail"]
    warns = [f for f in result.findings if f.severity == "warn"]
    if fails:
        lines.append(f"[skills_guard] ⚠️  {len(fails)} HIGH-RISK pattern(s) — content suppressed:")
        for f in fails:
            lines.append(f"  - {f.rule_id}: {f.detail}")
            if f.span:
                lines.append(f"    matched: {f.span!r}")
    if warns:
        lines.append(f"[skills_guard] {len(warns)} suspicious pattern(s):")
        for f in warns:
            lines.append(f"  - {f.rule_id}: {f.detail}")
    return "\n".join(lines)
