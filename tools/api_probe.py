"""Probe an OpenAI-compatible endpoint, score its identity, store to vault.

Given ``(base_url, key, claimed_model)``, the probe:
  1. Calls ``GET {base_url}/models`` to see what the endpoint claims to offer.
  2. Asks each fingerprint question and scores the answer.
  3. Computes ``average``, ``weighted_average``, ``actual_model_guess``.
  4. Writes everything back to the vault via ``record_probe``.

Failures (non-200, JSON error, timeout) flip the entry to ``failing`` and the
vault's archive policy decides if it gets evicted.

This is deliberately a thin stdlib-friendly probe — no openai SDK dependency,
no streaming, no tool-use. Each fingerprint question is a single chat
completion call with low ``max_tokens``.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from launcher import free_pool_vault as vault
from tools.free_pool_fingerprints import BANK, BANK_VERSION, FingerprintQuestion


# ── Per-question weights (informs ``weighted_average``) ───────────────────
# Higher weight = stronger discriminator. self_report is kept as a small
# positive signal — admitting your real identity is mildly correlated with
# not being a relabeled downgrade.
QUESTION_WEIGHTS: dict[str, float] = {
    "rust_lockfree_mpsc": 1.5,
    "monty_hall_4door": 2.0,
    "wangbi_xuan_zhi_you_xuan": 1.0,
    "sql_injection_review": 0.7,
    "self_report_model": 0.3,
    "refuse_cf_bypass": 1.0,
    "long_context_needle": 1.5,
    "chinese_modern_poem_debug": 0.5,
    "django_nplus1_review": 1.0,
}

DEFAULT_TIMEOUT_S = 30.0
LIST_MODELS_TIMEOUT_S = 10.0
MIN_PROBE_INTERVAL_S = 60  # don't reprobe the same entry more than once a minute


@dataclass
class ProbeResult:
    entry_id: str
    ok: bool
    average: float
    weighted_average: float
    self_report: str
    actual_model_guess: str
    matches_claim: str  # match | partial | mismatch | unknown
    supported_models: list[str]
    error: str | None = None


def list_models(base_url: str, key: str, *, timeout: float = LIST_MODELS_TIMEOUT_S) -> list[str]:
    """Best-effort ``GET /models`` listing. Returns ``[]`` on any failure."""
    url = base_url.rstrip("/") + "/models"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
            elif isinstance(item, str):
                out.append(item)
        return out
    except (requests.RequestException, ValueError):
        return []


def ask_once(
    *,
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, float, str | None]:
    """Send a single non-streaming chat completion. Returns ``(answer, latency_ms, error)``."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return "", (time.perf_counter() - started) * 1000.0, f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - started) * 1000.0
    if resp.status_code != 200:
        body = (resp.text or "")[:200]
        return "", latency_ms, f"http_{resp.status_code}: {body}"
    try:
        data = resp.json()
    except ValueError as exc:
        return "", latency_ms, f"bad_json: {exc}"
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "", latency_ms, f"shape: {json.dumps(data)[:200]}"
    return str(answer or ""), latency_ms, None


# ── Guess: from the self-report answer, what family does this look like? ──

_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"claude\s*(?:sonnet|opus|haiku|3|4)?", "claude"),
    (r"anthropic", "claude"),
    (r"gpt[- ]?5", "gpt-5"),
    (r"gpt[- ]?4o", "gpt-4o"),
    (r"gpt[- ]?4", "gpt-4"),
    (r"gpt[- ]?3", "gpt-3.5"),
    (r"openai", "gpt-*"),
    (r"gemini", "gemini"),
    (r"deepseek", "deepseek"),
    (r"qwen|通义", "qwen"),
    (r"glm|chatglm|智谱", "glm"),
    (r"moonshot|kimi", "kimi"),
    (r"yi[- ]?(?:34b|6b|large)", "yi"),
    (r"doubao|豆包", "doubao"),
    (r"llama", "llama"),
    (r"mistral|mixtral", "mistral"),
)


def guess_actual_family(self_report_answer: str) -> str:
    text = (self_report_answer or "").lower()
    if not text.strip():
        return "unknown"
    for pat, fam in _FAMILY_PATTERNS:
        if re.search(pat, text):
            return fam
    return "unknown"


def _claim_to_family(claimed_model: str) -> str:
    return guess_actual_family(claimed_model)


def _match_label(claimed: str, guessed: str) -> str:
    if not claimed or not guessed:
        return "unknown"
    if claimed == "unknown" or guessed == "unknown":
        return "unknown"
    if claimed == guessed:
        return "match"
    # Coarser families that overlap (e.g. gpt-4o vs gpt-4)
    if claimed.split("-")[0] == guessed.split("-")[0]:
        return "partial"
    return "mismatch"


def _select_model_to_probe(claimed: str, listed: list[str]) -> str:
    """Pick which model id to call. Prefer the claimed one *if* it's in the
    server's listed set (exact match); else the first listed; else trust
    ``claimed``. Exact match — ``startswith`` would falsely accept
    ``claimed="gpt-4"`` against a server that only serves ``"gpt-4o"``
    and then we'd send a model the server rejects."""
    if claimed and claimed in listed:
        return claimed
    if listed:
        return listed[0]
    return claimed or "gpt-4o-mini"


def probe_endpoint(
    *,
    base_url: str,
    key: str,
    claimed_model: str = "",
    source: dict[str, Any] | None = None,
    question_subset: list[str] | None = None,
    timeout_per_call: float = DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe and record. Idempotent under the ``MIN_PROBE_INTERVAL_S`` debounce."""
    base_url = (base_url or "").strip().rstrip("/")
    key = (key or "").strip()
    if not base_url or not key:
        raise ValueError("base_url and key are required")

    entry = vault.add_or_update(
        base_url=base_url,
        key=key,
        claimed_model=claimed_model,
        source=source,
    )
    entry_id = entry["id"]

    # Debounce: don't reprobe within a minute.
    last_ver = entry.get("last_verified_at") or ""
    if last_ver:
        try:
            from datetime import datetime, timezone
            last_dt = datetime.fromisoformat(last_ver)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < MIN_PROBE_INTERVAL_S:
                return ProbeResult(
                    entry_id=entry_id,
                    ok=True,
                    average=float((entry.get("fingerprint") or {}).get("average") or 0.0),
                    weighted_average=float((entry.get("fingerprint") or {}).get("weighted_average") or 0.0),
                    self_report=(entry.get("fingerprint") or {}).get("self_report", ""),
                    actual_model_guess=(entry.get("fingerprint") or {}).get("actual_model_guess", "unknown"),
                    matches_claim=(entry.get("fingerprint") or {}).get("matches_claim", "unknown"),
                    supported_models=entry.get("supported_models") or [],
                )
        except (ValueError, OSError):
            pass

    listed = list_models(base_url, key)
    target_model = _select_model_to_probe(claimed_model, listed)

    questions = [q for q in BANK if not question_subset or q.id in set(question_subset)]
    if not questions:
        questions = list(BANK)

    scores: dict[str, float] = {}
    answers: dict[str, str] = {}
    latencies: list[float] = []
    errors: dict[str, str] = {}

    for q in questions:
        answer, latency_ms, err = ask_once(
            base_url=base_url,
            key=key,
            model=target_model,
            prompt=q.prompt,
            max_tokens=q.max_tokens,
            timeout=timeout_per_call,
        )
        if err:
            errors[q.id] = err
            scores[q.id] = 0.0
            answers[q.id] = ""
            continue
        answers[q.id] = answer
        scores[q.id] = max(0.0, min(1.0, float(q.score(answer))))
        latencies.append(latency_ms)

    if errors and all(qid in errors for qid in (q.id for q in questions)):
        # Every call failed — entry doesn't really work.
        vault.record_probe(
            entry_id,
            success=False,
            error=next(iter(errors.values())),
        )
        return ProbeResult(
            entry_id=entry_id,
            ok=False,
            average=0.0,
            weighted_average=0.0,
            self_report="",
            actual_model_guess="unknown",
            matches_claim="unknown",
            supported_models=listed,
            error=next(iter(errors.values())),
        )

    average = sum(scores.values()) / max(1, len(scores))
    weighted_total = sum(scores[qid] * QUESTION_WEIGHTS.get(qid, 1.0) for qid in scores)
    weight_sum = sum(QUESTION_WEIGHTS.get(qid, 1.0) for qid in scores)
    weighted_average = weighted_total / max(1e-6, weight_sum)

    self_report = answers.get("self_report_model", "")
    actual_family = guess_actual_family(self_report)
    claim_family = _claim_to_family(claimed_model)
    match = _match_label(claim_family, actual_family)

    avg_latency = sum(latencies) / max(1, len(latencies)) if latencies else None

    fingerprint = {
        "bank_version": BANK_VERSION,
        "scores": scores,
        "answers_preview": {k: (v[:200] if v else "") for k, v in answers.items()},
        "errors": errors,
        "average": round(average, 3),
        "weighted_average": round(weighted_average, 3),
        "self_report": self_report.strip()[:500],
        "actual_model_guess": actual_family,
        "claim_family": claim_family,
        "matches_claim": match,
        "probed_with_model_id": target_model,
        "category_strong": _strong_categories(scores),
    }

    vault.record_probe(
        entry_id,
        fingerprint=fingerprint,
        supported_models=listed,
        latency_ms=avg_latency,
        success=True,
    )

    return ProbeResult(
        entry_id=entry_id,
        ok=True,
        average=round(average, 3),
        weighted_average=round(weighted_average, 3),
        self_report=fingerprint["self_report"],
        actual_model_guess=actual_family,
        matches_claim=match,
        supported_models=listed,
    )


def _strong_categories(scores: dict[str, float]) -> list[str]:
    """Categories where this entry scored ≥ 0.7 on at least one question."""
    from tools.free_pool_fingerprints import BANK_BY_ID

    strong: set[str] = set()
    for qid, score in scores.items():
        if score < 0.7:
            continue
        q: FingerprintQuestion | None = BANK_BY_ID.get(qid)
        if q is None:
            continue
        for cat in q.categories:
            strong.add(cat)
    return sorted(strong)
