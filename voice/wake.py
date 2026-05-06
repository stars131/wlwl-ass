"""Wake / exit phrase matcher operating on partial STT transcripts.

Strategy: normalize (NFC + lowercase + strip punctuation), then do substring
match. For Chinese, also try a pinyin-fallback so 三体 ↔ 散体 (homophone STT
errors) still hits.

Pinyin support is optional: if the ``pypinyin`` package is installed we use
it; otherwise we silently skip the pinyin pass. Wake matching still works on
exact transcript hits.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

try:
    from pypinyin import lazy_pinyin
    _HAS_PINYIN = True
except ImportError:
    _HAS_PINYIN = False


_PUNCT = re.compile(r"[\s\u3000\.,!?;:'\"()，。！？；：、""''《》【】「」]+")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    return _PUNCT.sub("", s).strip()


def to_pinyin(s: str) -> str:
    if not _HAS_PINYIN:
        return ""
    parts = lazy_pinyin(s)
    return "".join(parts).lower()


@dataclass
class MatchResult:
    phrase: str           # the configured phrase that hit
    matched_text: str     # the part of the transcript that hit
    method: str           # "exact" | "pinyin"


class PhraseMatcher:
    def __init__(self, phrases: list[str], *, min_confidence: float = 0.6) -> None:
        self.phrases = list(phrases)
        self.min_confidence = min_confidence
        self._exact = [(p, normalize(p)) for p in self.phrases]
        self._pinyin = [(p, to_pinyin(p)) for p in self.phrases] if _HAS_PINYIN else []

    def feed(self, text: str, conf: float = 1.0) -> MatchResult | None:
        if conf < self.min_confidence:
            return None
        norm = normalize(text)
        if not norm:
            return None
        for orig, n in self._exact:
            if n and n in norm:
                return MatchResult(phrase=orig, matched_text=text, method="exact")
        if self._pinyin:
            py = to_pinyin(text)
            for orig, target in self._pinyin:
                if target and target in py:
                    return MatchResult(phrase=orig, matched_text=text, method="pinyin")
        return None
