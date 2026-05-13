"""YAML-driven journey runner for the wlwl-ass GUI.

Usage::

    # Capture baselines (first time, after intentional UI changes)
    python -m tests.journeys.runner --capture-baseline

    # Regression run (compares current screenshots to baselines)
    python -m tests.journeys.runner

    # Run a single journey
    python -m tests.journeys.runner --journey 02_view_activity

The runner is Playwright-driven (sync API). The GUI must already be
running at ``--base-url`` (default ``http://127.0.0.1:1420``). The
runner does NOT start the GUI on its own — that's a deployment
concern the SOP handles (see ``memory/autonomous_operation_sop.md``
Phase 2 section).

Step schema (YAML)::

    name: 02_view_activity
    description: Navigate to Activity tab, verify cost bar visible
    steps:
      - action: goto
        path: /
      - action: click
        selector: 'button:has-text("Activity")'
      - action: wait_for
        selector: 'text=今日'
      - action: screenshot
        name: activity_loaded
        expected_change: ""        # empty = should match baseline exactly
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
JOURNEY_DIR = ROOT / "tests" / "journeys"
BASELINE_DIR = JOURNEY_DIR / "baselines"
RUNS_DIR = JOURNEY_DIR / "runs"


@dataclass
class StepResult:
    name: str
    action: str
    ok: bool
    elapsed_ms: float = 0.0
    error: str = ""
    screenshot_path: str = ""
    baseline_path: str = ""
    vision_verdict: str = ""  # "PASS" / "FAIL" / "UNCLEAR" / "BASELINE_CAPTURED" / "SKIPPED"
    vision_reason: str = ""


@dataclass
class JourneyResult:
    journey: str
    description: str
    started_at: str
    finished_at: str = ""
    ok: bool = True
    steps: list[StepResult] = field(default_factory=list)


def _load_journeys(only: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(JOURNEY_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            continue
        name = data.get("name") or path.stem
        if only and only != name and only != path.stem:
            continue
        data["name"] = name
        out.append(data)
    return out


def _run_step(page, step: dict[str, Any], run_dir: Path, journey_name: str,
              *, capture_baseline: bool, vision_enabled: bool) -> StepResult:
    action = str(step.get("action") or "").strip()
    step_name = str(step.get("name") or action or "?")
    res = StepResult(name=step_name, action=action, ok=False)
    started = time.perf_counter()
    try:
        if action == "goto":
            page.goto(step["path"], wait_until=step.get("wait_until", "load"))
        elif action == "click":
            page.click(step["selector"], timeout=step.get("timeout_ms", 10_000))
        elif action == "fill":
            page.fill(step["selector"], str(step.get("text", "")),
                      timeout=step.get("timeout_ms", 10_000))
        elif action == "press":
            target = step.get("selector")
            if target:
                page.press(target, step["key"])
            else:
                page.keyboard.press(step["key"])
        elif action == "wait_for":
            page.wait_for_selector(step["selector"], timeout=step.get("timeout_ms", 15_000))
        elif action == "sleep":
            time.sleep(float(step.get("seconds", 1)))
        elif action == "screenshot":
            shot_dir = run_dir / journey_name
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / f"{step_name}.png"
            page.screenshot(path=str(shot_path), full_page=bool(step.get("full_page", False)))
            res.screenshot_path = str(shot_path)
            baseline_path = BASELINE_DIR / journey_name / f"{step_name}.png"
            res.baseline_path = str(baseline_path)
            if capture_baseline:
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                # Copy current to baseline.
                baseline_path.write_bytes(shot_path.read_bytes())
                res.vision_verdict = "BASELINE_CAPTURED"
                res.vision_reason = "first-run baseline saved"
            elif not baseline_path.exists():
                res.vision_verdict = "FAIL"
                res.vision_reason = f"no baseline at {baseline_path}"
            elif not vision_enabled:
                res.vision_verdict = "SKIPPED"
                res.vision_reason = "vision check disabled via --no-vision"
            else:
                from tests.journeys.vision_assert import assert_visual_match
                verdict = assert_visual_match(
                    current_png=str(shot_path),
                    baseline_png=str(baseline_path),
                    expected_change=str(step.get("expected_change") or ""),
                )
                res.vision_verdict = verdict.get("verdict", "UNCLEAR")
                res.vision_reason = verdict.get("reason", "")
        else:
            raise ValueError(f"unknown action {action!r}")
        res.ok = True
        if res.vision_verdict == "FAIL":
            res.ok = False
    except Exception as exc:
        res.ok = False
        res.error = f"{type(exc).__name__}: {exc}"
    res.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return res


def run(base_url: str, only: str | None, *, capture_baseline: bool,
        headless: bool, vision_enabled: bool) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    journeys = _load_journeys(only)
    if not journeys:
        return {"ok": False, "error": "no journeys matched", "journeys": []}

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results: list[JourneyResult] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 800})
            for journey in journeys:
                jr = JourneyResult(
                    journey=journey["name"],
                    description=journey.get("description", ""),
                    started_at=dt.datetime.now().isoformat(timespec="seconds"),
                )
                page = context.new_page()
                try:
                    for step in (journey.get("steps") or []):
                        sr = _run_step(
                            page, step, run_dir, journey["name"],
                            capture_baseline=capture_baseline,
                            vision_enabled=vision_enabled,
                        )
                        jr.steps.append(sr)
                        if not sr.ok and step.get("required", True):
                            jr.ok = False
                            break
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                jr.finished_at = dt.datetime.now().isoformat(timespec="seconds")
                results.append(jr)
        finally:
            browser.close()

    report = {
        "run_id": run_id,
        "base_url": base_url,
        "capture_baseline": capture_baseline,
        "vision_enabled": vision_enabled,
        "started_at": results[0].started_at if results else "",
        "finished_at": results[-1].finished_at if results else "",
        "ok": all(j.ok for j in results),
        "journeys": [asdict(j) for j in results],
    }
    report_path = run_dir / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    report["report_path"] = str(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wlwl-ass GUI journey runner")
    parser.add_argument("--base-url", default=os.environ.get("WLWL_GUI_URL", "http://127.0.0.1:1420"))
    parser.add_argument("--journey", default=None, help="run a single journey by name")
    parser.add_argument("--capture-baseline", action="store_true",
                        help="save current screenshots as the new baseline")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--no-vision", action="store_true",
                        help="skip vision-LLM screenshot comparison (still saves shots)")
    args = parser.parse_args(argv)

    try:
        report = run(
            base_url=args.base_url,
            only=args.journey,
            capture_baseline=args.capture_baseline,
            headless=not args.headed,
            vision_enabled=not args.no_vision,
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"runner failed: {exc}", file=sys.stderr)
        return 2

    # Console summary
    print("\n=== Journey Run Summary ===")
    print(f"run_id={report.get('run_id')} ok={report.get('ok')}")
    for j in report.get("journeys", []):
        marker = "✓" if j.get("ok") else "✗"
        print(f" {marker} {j['journey']}: {j.get('description','')}")
        for s in j.get("steps", []):
            sub = "✓" if s.get("ok") else "✗"
            extra = f" [{s.get('vision_verdict')}]" if s.get("vision_verdict") else ""
            if s.get("error"):
                extra += f" err={s['error']}"
            print(f"     {sub} {s['action']}/{s['name']}{extra}")
    print(f"\nreport: {report.get('report_path')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
