"""Live self-test for the concierge LLM hooks.

Exercises each of the 5 LLM hooks against the configured production LLM
session and reports pass/fail. Designed to catch:

  * Prompt → JSON parse mismatches (e.g. the model returns prose instead
    of structured output)
  * Slot rendering that hides options (validator reject)
  * QA paraphrasing that times out / hallucinates
  * Misconfigured API endpoint (returns nothing / errors)

Usage::

    python -m launcher.cli_concierge_llm_test
    python -m launcher.cli_concierge_llm_test --json
    python -m launcher.cli_concierge_llm_test --hook classify

Exit codes: 0 = all hooks pass; 1 = at least one failed. Safe to wire
into CI for a smoke test of "the configured LLM still talks to us."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@dataclass
class HookResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: float = 0.0
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "ok": self.ok, "detail": self.detail,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "raw_output": self.raw_output[:400],
        }


def _build_hooks_for_test():
    """Construct the production LLM hooks bundle. Returns (hooks, label)
    or (None, reason)."""
    from llmcore import reload_mykeys
    try:
        reload_mykeys()
    except Exception:
        pass
    from frontends.fsapp_concierge import _build_llm_hooks
    hooks = _build_llm_hooks()
    if hooks is None:
        return None, "No LLM session built — check llmcore.mykeys (need api/config entries)"
    return hooks, "LLM session built"


def _check_chat(hooks) -> HookResult:
    t0 = time.perf_counter()
    try:
        from llmcore.concierge_agent import ConciergeConfig, ConciergeAgent
        from llmcore.concierge_agent import _CircuitState  # noqa: F401 — module import side-effect check
        sys_p = "你是 Alice 的助理小 W。1 句话回复。"
        out = hooks.chat("你好", sys_p)
        ok = isinstance(out, str) and bool(out.strip())
        return HookResult(
            name="chat", ok=ok,
            detail="empty response" if not ok else "",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            raw_output=str(out or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return HookResult(
            name="chat", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def _check_classify(hooks) -> HookResult:
    t0 = time.perf_counter()
    try:
        out = hooks.classify("下周想跟你吃个饭", {})
        ok = isinstance(out, dict) and out.get("intent") == "schedule"
        return HookResult(
            name="classify", ok=ok,
            detail=("expected intent=schedule, got " + repr(out)) if not ok else "",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            raw_output=str(out or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return HookResult(
            name="classify", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def _check_extract_schedule(hooks) -> HookResult:
    t0 = time.perf_counter()
    try:
        out = hooks.extract_schedule("明天晚上聊半小时", {})
        if not isinstance(out, dict):
            return HookResult(
                name="extract_schedule", ok=False,
                detail=f"expected dict, got {type(out).__name__}: {out!r}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        # Soft check: at least one of the expected fields should be present
        # AND in a sensible range. We don't require all three since some
        # models will only emit what they're confident about.
        dur_ok = isinstance(out.get("duration_minutes"), int) and \
                 5 <= out["duration_minutes"] <= 480
        win_ok = out.get("preferred_window") in ("morning", "afternoon", "evening", "any")
        topic_ok = isinstance(out.get("topic_summary"), str) and out["topic_summary"]
        ok = dur_ok or win_ok or topic_ok
        return HookResult(
            name="extract_schedule", ok=ok,
            detail=("none of duration/window/topic valid" if not ok else
                    f"got duration={out.get('duration_minutes')!r} "
                    f"window={out.get('preferred_window')!r} "
                    f"topic={out.get('topic_summary')!r}"),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            raw_output=str(out),
        )
    except Exception as exc:  # noqa: BLE001
        return HookResult(
            name="extract_schedule", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def _check_render_slots(hooks) -> HookResult:
    t0 = time.perf_counter()
    try:
        slots = [
            {"start": "2026-05-21T19:00:00", "end": "2026-05-21T20:00:00"},
            {"start": "2026-05-21T20:00:00", "end": "2026-05-21T21:00:00"},
        ]
        out = hooks.render_slots(slots, "晚饭", "张三")
        if not isinstance(out, str):
            return HookResult(
                name="render_slots", ok=False,
                detail=f"expected str, got {type(out).__name__}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        # Validator-style check: every slot's HH:MM must appear
        missing = [s["start"][11:16] for s in slots if s["start"][11:16] not in out]
        ok = not missing
        return HookResult(
            name="render_slots", ok=ok,
            detail=(f"missing slot times: {missing}" if missing else ""),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            raw_output=str(out or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return HookResult(
            name="render_slots", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


def _check_render_qa(hooks) -> HookResult:
    t0 = time.perf_counter()
    try:
        out = hooks.render_qa(
            "她现在在哪上班",
            "她在 ABC 公司做 ML，2025 年开始的。",
            "employer",
        )
        if not isinstance(out, str):
            return HookResult(
                name="render_qa", ok=False,
                detail=f"expected str, got {type(out).__name__}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        ok = bool(out.strip()) and "ABC" in out
        return HookResult(
            name="render_qa", ok=ok,
            detail=("ABC not in output — model may be ignoring source" if not ok else ""),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            raw_output=str(out or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return HookResult(
            name="render_qa", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )


_HOOK_CHECKS = {
    "chat": _check_chat,
    "classify": _check_classify,
    "extract_schedule": _check_extract_schedule,
    "render_slots": _check_render_slots,
    "render_qa": _check_render_qa,
}


def run_all(only: list[str] | None = None) -> tuple[bool, list[HookResult]]:
    hooks, label = _build_hooks_for_test()
    if hooks is None:
        return False, [HookResult(name="build", ok=False, detail=label)]
    results: list[HookResult] = [HookResult(name="build", ok=True, detail=label)]
    selected = only or list(_HOOK_CHECKS.keys())
    for name in selected:
        check = _HOOK_CHECKS.get(name)
        if check is None:
            results.append(HookResult(
                name=name, ok=False,
                detail=f"unknown hook; valid: {sorted(_HOOK_CHECKS)}",
            ))
            continue
        results.append(check(hooks))
    all_ok = all(r.ok for r in results)
    return all_ok, results


def _format(results: list[HookResult]) -> str:
    width = max(len(r.name) for r in results)
    lines = []
    for r in results:
        mark = "[ok]" if r.ok else "[FAIL]"
        detail = f" — {r.detail}" if r.detail else ""
        lines.append(f"  {mark:<6} {r.name:<{width}}  ({r.elapsed_ms:>7.1f} ms){detail}")
        if r.raw_output and not r.ok:
            preview = r.raw_output[:200].replace("\n", " ")
            lines.append(f"           raw: {preview!r}")
    n_fail = sum(1 for r in results if not r.ok)
    lines.append("")
    lines.append("  → ALL OK" if not n_fail else f"  → {n_fail} HOOK(S) FAILED")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Concierge LLM live self-test.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")
    parser.add_argument("--hook", action="append", default=[],
                        help="Run only this hook (repeatable). Choices: "
                             + ", ".join(_HOOK_CHECKS))
    args = parser.parse_args(argv)
    only = args.hook or None
    ok, results = run_all(only)
    if args.json:
        print(json.dumps({
            "all_ok": ok,
            "results": [r.to_dict() for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        print(_format(results))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
