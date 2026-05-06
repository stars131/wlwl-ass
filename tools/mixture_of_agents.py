"""Mixture-of-Agents tool (#18).

For one prompt, fan out to N LLM configs in parallel, then synthesise the
answers via an aggregator model. This is *real* mixture-of-agents — distinct
from ``llmcore.MixinSession``, which is failover (try config 1, fall back to
config 2). Both are useful; they don't replace each other.

Pattern:

  >>> from tools.mixture_of_agents import run_moa
  >>> result = run_moa(
  ...     prompt="Implement a thread-safe LRU cache in Python.",
  ...     member_names=["claude-opus-4-7", "gpt-5", "gemini-2-flash"],
  ...     aggregator_name="claude-opus-4-7",
  ... )
  >>> print(result["final"])

The members run concurrently via threads; aggregator runs once with all
member outputs concatenated. Returns:

    {
        "final": str,            # aggregated answer
        "member_outputs": [{"name": str, "text": str, "elapsed_ms": float, "error": str | None}, ...],
        "elapsed_ms": float,
    }

Failure modes handled:
  * a member raises → captured into ``error`` field, ``text`` is empty
  * aggregator fails → falls back to concatenating successful members
  * member_names that don't exist in api_configs → reported as error rows
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_DEFAULT_AGGREGATOR_PROMPT = (
    "You are an aggregator that received independent answers to the SAME user "
    "question from several capable models. Read every answer, then write the "
    "single best response — combining strong points, correcting mistakes, and "
    "removing redundancy. Do not list per-model attribution; produce one "
    "polished final answer.\n\n"
    "User question:\n---\n{prompt}\n---\n\n"
    "Model answers:\n{members}\n\n"
    "Final answer:"
)


def _build_session(name: str):
    """Build a one-shot LLM session bound to the named api_config entry.

    Important: MoA needs a text-protocol session (one that accepts
    ``ask(prompt: str, stream=False) -> str``). The ``Native*`` session
    classes are for native tool-calling and have a different signature
    (they take a ``{role, content}`` dict and return a MockResponse). So
    even though the user's mykey config might be ``native_oai`` /
    ``native_claude``, we deliberately route through the text-protocol
    classes here — same endpoint, simpler interface."""
    from launcher.api_config import load_api_configs

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    configs = load_api_configs(base)
    cfg = next((c for c in configs if str(c.get("name")) == name), None)
    if cfg is None:
        raise ValueError(f"api_config {name!r} not found")
    kind = str(cfg.get("kind") or "native_oai")
    if kind == "mixin":
        # Resolve to the first member — mixin within MoA gets confusing.
        members = cfg.get("llm_nos") or []
        if not members:
            raise ValueError(f"mixin {name!r} has no members")
        return _build_session(str(members[0]))
    # Text-protocol session — works for both Anthropic and OpenAI-shaped
    # endpoints depending on kind.
    if "claude" in kind or "anthropic" in str(cfg.get("apibase", "")).lower():
        from llmcore.adapters.anthropic import ClaudeSession
        return ClaudeSession(cfg)
    from llmcore.adapters.openai import LLMSession
    return LLMSession(cfg)


def _ask_one(name: str, prompt: str, results: list[dict[str, Any]], idx: int) -> None:
    t0 = time.monotonic()
    out = {"name": name, "text": "", "elapsed_ms": 0.0, "error": None}
    try:
        sess = _build_session(name)
        text = sess.ask(prompt, stream=False)
        if isinstance(text, str):
            out["text"] = text
        else:
            # Streaming generator — collect.
            out["text"] = "".join(list(text))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    results[idx] = out


def run_moa(
    prompt: str,
    member_names: list[str],
    *,
    aggregator_name: str | None = None,
    aggregator_prompt_template: str | None = None,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Run mixture-of-agents and return aggregated + per-member outputs.

    ``aggregator_name`` defaults to ``member_names[0]`` if not provided. If
    aggregation fails, the function falls back to a deterministic concat of
    the successful members so the caller still gets *something*.
    """
    if not prompt or not str(prompt).strip():
        raise ValueError("prompt is required")
    if not member_names:
        raise ValueError("member_names must contain at least one config name")
    aggregator = aggregator_name or member_names[0]

    started = time.monotonic()
    results: list[dict[str, Any]] = [{} for _ in member_names]
    threads: list[threading.Thread] = []
    for i, name in enumerate(member_names):
        t = threading.Thread(target=_ask_one, args=(name, prompt, results, i), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout_s
    for t in threads:
        remaining = max(0.1, deadline - time.monotonic())
        t.join(timeout=remaining)
    # Any thread still alive: mark its slot as timed-out (won't kill the
    # underlying HTTP — but at least we report it).
    for i, t in enumerate(threads):
        if t.is_alive() and not results[i]:
            results[i] = {
                "name": member_names[i],
                "text": "",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "error": "timeout",
            }

    successful = [r for r in results if r.get("text") and not r.get("error")]
    template = aggregator_prompt_template or _DEFAULT_AGGREGATOR_PROMPT
    final = ""
    if len(successful) == 1:
        # Single-member MoA is degenerate — aggregator just paraphrases the
        # one answer and burns tokens. Skip and return the member directly.
        final = successful[0]["text"]
    elif successful:
        members_block = "\n\n".join(
            f"### Model {i+1} ({r['name']}):\n{r['text']}" for i, r in enumerate(successful)
        )
        full = template.format(prompt=prompt, members=members_block)
        try:
            agg_session = _build_session(aggregator)
            final = agg_session.ask(full, stream=False)
            if not isinstance(final, str):
                final = "".join(list(final))
            if not final.strip():
                # Aggregator returned nothing usable — fall back to the
                # longest successful member rather than handing back "".
                best = max(successful, key=lambda r: len(r["text"]))
                final = (
                    "[aggregator returned empty; falling back to single-member answer "
                    f"({best['name']})]\n\n{best['text']}"
                )
        except Exception as exc:
            best = max(successful, key=lambda r: len(r["text"]))
            final = (
                f"[aggregator failed: {type(exc).__name__}: {exc}]\n\n"
                f"Falling back to single-member answer ({best['name']}):\n\n"
                f"{best['text']}"
            )
    else:
        final = "[mixture_of_agents] all members failed; nothing to aggregate."

    return {
        "final": final,
        "member_outputs": results,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }
