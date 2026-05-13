"""Sensitivity gate + selection logic over the free-pool vault.

The router is intentionally a pure-library module: callers pass in a paid
fallback callable when they have one (the agent's tool handler does this),
so the router doesn't need to import the agent. This makes it unit-testable
without spinning up a real LLM session.

Public surface:

* ``check_sensitivity(sensitivity)`` — raise on invalid input
* ``select(...)`` — return verified vault entries, best-first, after
  sensitivity gating
* ``ask(...)`` — try N free entries; on exhaust fall through to a paid
  callable if provided

All ``private`` requests are refused; the vault never sees them. ``internal``
skips free pool entirely and goes straight to paid (if available).
``public`` is the only path that touches the vault.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from launcher import free_pool_vault as vault
from tools import api_probe


VALID_SENSITIVITIES = {"public", "internal", "private"}


class SensitivityError(ValueError):
    """Raised when a caller asks for a path the gate refuses to open."""


def check_sensitivity(sensitivity: str) -> str:
    """Normalise + validate. Returns the lowercased label."""
    s = (sensitivity or "").strip().lower()
    if s not in VALID_SENSITIVITIES:
        raise SensitivityError(
            f"sensitivity must be one of {sorted(VALID_SENSITIVITIES)}, got {sensitivity!r}"
        )
    return s


def select(
    *,
    sensitivity: str = "public",
    min_score: float = 0.0,
    category_required: str | None = None,
    limit: int = 5,
    autonomous: bool = False,
) -> list[dict[str, Any]]:
    """Return verified vault entries this caller is allowed to use, best first.

    ``autonomous=True`` enforces the SOP rule "autonomous self-selected tasks
    are always public" — even if the caller tries to pass a higher
    sensitivity. The decision is loud (raise), not silent (downgrade),
    because a silent downgrade would mask a misconfigured task.
    """
    s = check_sensitivity(sensitivity)
    if autonomous and s != "public":
        raise SensitivityError(
            "autonomous-selected tasks must use sensitivity='public'; "
            "user-pinned tasks may use 'internal' but only when explicitly opted in"
        )
    if s != "public":
        return []  # internal/private never touch the free pool
    # Make sure expired entries are out of the way before we hand a list out.
    vault.sweep_expired()
    return vault.verified_entries(
        min_score=min_score,
        category_required=category_required,
        limit=limit,
    )


def ask(
    prompt: str,
    *,
    sensitivity: str = "public",
    max_attempts: int = 3,
    min_score: float = 0.0,
    category_required: str | None = None,
    max_tokens: int = 1024,
    timeout_per_call: float = api_probe.DEFAULT_TIMEOUT_S,
    paid_fallback_fn: Callable[[str], str] | None = None,
    autonomous: bool = False,
) -> dict[str, Any]:
    """Run one inference, preferring the free pool.

    Returns ``{"answer", "used", "entry_id", "tried", "error"}``:

    * ``used`` ∈ ``{"free-pool", "paid", "none"}``
    * ``tried`` — list of (entry_id, error_or_ok) pairs in attempt order
    * ``error`` — empty when ``used`` ≠ ``"none"``

    A success on a free entry increments its stats; an error increments
    fail_count and may push it to ``failing`` status (vault handles eviction
    after ``MAX_FAIL_IN_ROW`` consecutive failures).
    """
    s = check_sensitivity(sensitivity)
    if autonomous and s != "public":
        raise SensitivityError(
            "autonomous tasks may only ask at sensitivity='public'"
        )

    tried: list[dict[str, Any]] = []

    # ── private: refuse outright; vault must not see the prompt ──────
    if s == "private":
        return {
            "answer": "",
            "used": "none",
            "entry_id": "",
            "tried": tried,
            "error": "sensitivity=private blocks both free pool and remote paid",
        }

    # ── internal: skip free pool, go straight to paid if available ──
    if s == "internal":
        if paid_fallback_fn is None:
            return {
                "answer": "",
                "used": "none",
                "entry_id": "",
                "tried": tried,
                "error": "sensitivity=internal requires a paid fallback; none provided",
            }
        try:
            answer = paid_fallback_fn(prompt)
        except Exception as exc:
            return {
                "answer": "",
                "used": "none",
                "entry_id": "",
                "tried": tried,
                "error": f"paid_fallback_failed: {type(exc).__name__}: {exc}",
            }
        return {
            "answer": answer,
            "used": "paid",
            "entry_id": "",
            "tried": tried,
            "error": "",
        }

    # ── public: free pool first ──────────────────────────────────────
    candidates = select(
        sensitivity="public",
        min_score=min_score,
        category_required=category_required,
        limit=max_attempts,
        autonomous=autonomous,
    )

    for entry in candidates:
        entry_id = entry["id"]
        target_model = _pick_model(entry)
        answer, latency_ms, err = api_probe.ask_once(
            base_url=entry["base_url"],
            key=entry["key"],
            model=target_model,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=timeout_per_call,
        )
        if err is None and answer:
            vault.record_probe(
                entry_id,
                fingerprint=entry.get("fingerprint") or {},
                supported_models=entry.get("supported_models") or [],
                latency_ms=latency_ms,
                success=True,
            )
            tried.append({"entry_id": entry_id, "ok": True, "latency_ms": round(latency_ms, 1)})
            return {
                "answer": answer,
                "used": "free-pool",
                "entry_id": entry_id,
                "tried": tried,
                "error": "",
            }
        tried.append({"entry_id": entry_id, "ok": False, "error": err or "empty_answer"})
        vault.record_probe(entry_id, success=False, error=err or "empty_answer")

    # ── all free entries failed (or none available) ──────────────────
    if paid_fallback_fn is None:
        return {
            "answer": "",
            "used": "none",
            "entry_id": "",
            "tried": tried,
            "error": "free_pool_exhausted_no_fallback",
        }
    try:
        answer = paid_fallback_fn(prompt)
    except Exception as exc:
        return {
            "answer": "",
            "used": "none",
            "entry_id": "",
            "tried": tried,
            "error": f"paid_fallback_failed: {type(exc).__name__}: {exc}",
        }
    return {
        "answer": answer,
        "used": "paid",
        "entry_id": "",
        "tried": tried,
        "error": "",
    }


def _pick_model(entry: dict[str, Any]) -> str:
    """Choose the model id to send for an entry. Prefer the claimed model if
    the endpoint actually lists it; otherwise fall back to the first
    supported model, then to the claimed name verbatim.

    We require an *exact* match against ``supported_models`` rather than
    ``startswith`` — fuzzy prefix matching used to make
    ``claimed="gpt-4"`` accept a server that only listed ``"gpt-4o"``,
    but then we'd send ``"gpt-4"`` which the server doesn't actually serve
    and rejects with a 404."""
    claimed = entry.get("claimed_model") or ""
    supported = entry.get("supported_models") or []
    if claimed and (not supported or claimed in supported):
        return claimed
    if supported:
        return supported[0]
    return claimed or "gpt-4o-mini"
