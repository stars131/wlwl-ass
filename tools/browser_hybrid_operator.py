"""Hybrid browser operator strategy.

Default browser automation in wlwl-ass should remain DOM/JS based because it
is precise and auditable. Visual operation is reserved for pages that cannot
be represented well as DOM: canvas apps, remote desktops, complex login
flows, and heavily obfuscated pages.
"""
from __future__ import annotations

from typing import Any, Callable


MIN_STRUCTURED_CHARS = 80


def decide_browser_strategy(scan_result: Any, *, min_chars: int = MIN_STRUCTURED_CHARS) -> dict[str, Any]:
    if isinstance(scan_result, dict) and scan_result.get("status") == "error":
        return {
            "strategy": "visual",
            "reason": str(scan_result.get("msg") or scan_result.get("error") or "scan_error"),
        }
    text = _extract_text(scan_result)
    if len(text.strip()) < min_chars:
        return {
            "strategy": "visual",
            "reason": f"structured text too short ({len(text.strip())} chars)",
        }
    visual_markers = ("<canvas", "remote desktop", "webgl", "captcha", "verification code")
    lowered = text.lower()
    if any(marker in lowered for marker in visual_markers):
        return {"strategy": "visual", "reason": "visual-only marker detected"}
    return {"strategy": "dom", "reason": "structured DOM/text is usable", "chars": len(text.strip())}


def run_browser_task(
    instruction: str,
    *,
    scan_func: Callable[..., Any],
    visual_run_func: Callable[..., dict[str, Any]],
    force_visual: bool = False,
    dry_run: bool = True,
    max_loop: int = 5,
    loop_wait: float = 1.0,
    backend: str = "auto",
) -> dict[str, Any]:
    instruction = instruction.strip()
    if not instruction:
        return {"status": "error", "error": "missing_instruction"}
    scan_result: Any = None
    decision = {"strategy": "visual", "reason": "forced visual"}
    if not force_visual:
        # ``scan_func`` is ``wlwl_ass.web_scan`` in production; tests pass a
        # ``lambda **kwargs`` so unexpected kwargs are absorbed. Surface real
        # errors instead of degrading silently — a signature drift here used
        # to be hidden by a TypeError fallback that called scan_func with
        # zero args, which discarded the ``text_only=True`` intent.
        try:
            scan_result = scan_func(text_only=True)
        except Exception as exc:
            scan_result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        decision = decide_browser_strategy(scan_result)
    if decision["strategy"] == "dom":
        return {
            "status": "success",
            "strategy": "dom",
            "decision": decision,
            "dom": scan_result,
            "next": "Use web_execute_js for precise browser actions; switch force_visual=true if DOM action fails.",
        }
    visual_instruction = "Operate the current browser window visually. " + instruction
    result = visual_run_func(
        visual_instruction,
        max_loop=max_loop,
        loop_wait=loop_wait,
        dry_run=dry_run,
        backend=backend,
    )
    return {
        "status": result.get("status", "unknown"),
        "strategy": "visual",
        "decision": decision,
        "visual": result,
    }


def _extract_text(scan_result: Any) -> str:
    if isinstance(scan_result, str):
        return scan_result
    if isinstance(scan_result, dict):
        parts: list[str] = []
        for key in ("text", "html", "content", "body", "markdown"):
            value = scan_result.get(key)
            if isinstance(value, str):
                parts.append(value)
        if not parts:
            parts.extend(str(v) for v in scan_result.values() if isinstance(v, str))
        return "\n".join(parts)
    return str(scan_result or "")
