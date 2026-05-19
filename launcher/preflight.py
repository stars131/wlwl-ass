"""Preflight self-check — verify the project is in a runnable state.

Run with::

    python -m launcher.preflight              # all checks
    python -m launcher.preflight --json        # machine-readable
    python -m launcher.preflight --only imports,workers  # subset

Background: the 2026-05-17 incident where ``tokenjuice.py`` had a hidden
SyntaxError blocking ``fsapp.py`` import was undiscoverable until a user
sent a message in Feishu and watched the bot die. Preflight runs all the
import + smoke checks BEFORE a bot is spawned so import bombs surface in
GUI / CLI feedback instead of bot logs nobody reads.

Each check is small, idempotent, and side-effect-free (except for log
prints). Exit codes: 0 = all pass; 1 = one or more fail.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from dataclasses import dataclass, field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: float = 0.0


@dataclass
class PreflightReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    def to_dict(self) -> dict:
        return {
            "all_ok": self.all_ok,
            "results": [
                {"name": r.name, "ok": r.ok, "detail": r.detail,
                 "elapsed_ms": round(r.elapsed_ms, 1)}
                for r in self.results
            ],
        }


# ── individual checks ──────────────────────────────────────────────────

def _check_import(module_name: str) -> CheckResult:
    """Import a module; report SyntaxError / ImportError clearly."""
    import time as _time
    t0 = _time.perf_counter()
    try:
        importlib.import_module(module_name)
        return CheckResult(
            name=f"import:{module_name}", ok=True,
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except SyntaxError as exc:
        return CheckResult(
            name=f"import:{module_name}", ok=False,
            detail=f"SyntaxError at {exc.filename}:{exc.lineno} — {exc.msg}",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        # Trim the traceback to the last frame for compactness.
        tb_lines = traceback.format_exc().splitlines()
        last = tb_lines[-1] if tb_lines else repr(exc)
        return CheckResult(
            name=f"import:{module_name}", ok=False,
            detail=f"{type(exc).__name__}: {exc} ({last})",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )


def check_imports() -> list[CheckResult]:
    """Verify all bot-relevant modules import cleanly.

    Picks modules that have historically been broken by single-file bugs
    (tokenjuice, wlwl_ass, agentmain) or that the bots transitively need
    (concierge_agent, kernel, workers).
    """
    targets = [
        "tokenjuice",                       # caught the 2026-05-17 SyntaxError
        "wlwl_ass",                          # transitively imports tokenjuice
        "agentmain",                         # owner-bot agent class
        "llmcore.concierge_agent",           # concierge agent
        "llmcore.kernel",                    # kernel singleton
        "llmcore.workers.kb_worker",
        "llmcore.workers.slot_worker",
        "llmcore.workers.escalate_worker",
        "llmcore.workers.audit_worker",
        "launcher.bot_manager",              # spawns the bots
        "launcher.config_store",             # reads creds
    ]
    return [_check_import(m) for m in targets]


def check_bot_frontends() -> list[CheckResult]:
    """For each registered bot, verify ``frontends/<script>`` parses.

    We import-check via ast.parse (NOT actually executing the frontend),
    because the frontend's runtime side effects include grabbing a TCP
    lock — which we DON'T want to do from preflight.
    """
    import ast
    import time as _time
    from launcher.bot_manager import BOT_SPECS
    results: list[CheckResult] = []
    for key, spec in BOT_SPECS.items():
        t0 = _time.perf_counter()
        path = os.path.join(PROJECT_ROOT, "frontends", spec.script)
        try:
            if not os.path.exists(path):
                results.append(CheckResult(
                    name=f"frontend:{key}", ok=False,
                    detail=f"missing: {path}",
                    elapsed_ms=(_time.perf_counter() - t0) * 1000,
                ))
                continue
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            ast.parse(src, filename=path)
            results.append(CheckResult(
                name=f"frontend:{key}", ok=True,
                elapsed_ms=(_time.perf_counter() - t0) * 1000,
            ))
        except SyntaxError as exc:
            results.append(CheckResult(
                name=f"frontend:{key}", ok=False,
                detail=f"SyntaxError at line {exc.lineno}: {exc.msg}",
                elapsed_ms=(_time.perf_counter() - t0) * 1000,
            ))
        except Exception as exc:  # noqa: BLE001
            results.append(CheckResult(
                name=f"frontend:{key}", ok=False,
                detail=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(_time.perf_counter() - t0) * 1000,
            ))
    return results


def check_tokenjuice_rules() -> CheckResult:
    """Make sure every BUILTIN_RULES entry constructs cleanly. The
    Rule.__post_init__ validator would catch length mismatches and bad
    regexes at module import — we re-validate here for paranoia."""
    import time as _time
    t0 = _time.perf_counter()
    try:
        import tokenjuice
        # Touch each rule's compiled patterns by running compact_output
        # on an empty input — exercises the regex compile path.
        for rule in tokenjuice.BUILTIN_RULES:
            tokenjuice.compact_output(rule.tool, "")
        return CheckResult(
            name="tokenjuice:rules", ok=True,
            detail=f"{len(tokenjuice.BUILTIN_RULES)} rules validated",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="tokenjuice:rules", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )


def check_concierge_workers() -> CheckResult:
    """Build all 5 concierge workers in an ephemeral kernel and verify
    they instantiate. Catches "I forgot to register the factory" bugs."""
    import time as _time
    import shutil
    import tempfile
    t0 = _time.perf_counter()
    # NB: we manage the tempdir manually instead of with TemporaryDirectory()
    # because on Windows the calendar.db's sqlite handle can outlive the
    # kernel reset by a few ms — TemporaryDirectory()'s atexit cleanup
    # then races the file lock. Manual cleanup wraps in try/except.
    tmp = tempfile.mkdtemp(prefix="wlwl_preflight_")
    try:
        from llmcore.kernel import reset_kernel, get_kernel
        reset_kernel()
        k = get_kernel()
        k.add_worker({"name": "_pf_cal", "kind": "calendar",
                      "db_path": os.path.join(tmp, "cal.db")})
        k.add_worker({"name": "_pf_kb", "kind": "concierge_kb",
                      "kb_path": os.path.join(tmp, "kb.jsonl")})
        k.add_worker({"name": "_pf_slot", "kind": "concierge_slot"})
        k.add_worker({"name": "_pf_esc", "kind": "concierge_escalate",
                      "log_path": os.path.join(tmp, "esc.jsonl")})
        k.add_worker({"name": "_pf_audit", "kind": "concierge_audit",
                      "audit_path": os.path.join(tmp, "audit.jsonl")})
        reset_kernel()
        return CheckResult(
            name="concierge:workers", ok=True,
            detail="5 workers built + reset",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            from llmcore.kernel import reset_kernel
            reset_kernel()
        except Exception:
            pass
        return CheckResult(
            name="concierge:workers", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    finally:
        # Best-effort cleanup. On Windows the sqlite handle inside the
        # already-discarded calendar worker may briefly hold cal.db; ignore
        # the race — the OS tempdir reaper will sweep it later.
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def check_test_suite_collects() -> CheckResult:
    """``pytest --collect-only`` should succeed. Catches test-file syntax
    errors that would prevent the next CI run."""
    import subprocess
    import time as _time
    t0 = _time.perf_counter()
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=60,
        )
        ok = r.returncode == 0
        detail = "" if ok else (r.stderr or r.stdout or "")[-400:]
        return CheckResult(
            name="pytest:collect", ok=ok, detail=detail,
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="pytest:collect", ok=False,
            detail="timed out after 60s",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="pytest:collect", ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )


_CHECK_GROUPS = {
    "imports": check_imports,
    "frontends": check_bot_frontends,
    "tokenjuice": lambda: [check_tokenjuice_rules()],
    "workers": lambda: [check_concierge_workers()],
    "pytest": lambda: [check_test_suite_collects()],
}


def run_preflight(groups: list[str] | None = None) -> PreflightReport:
    """Run the requested check groups (default: all). Returns a report."""
    report = PreflightReport()
    selected = groups or list(_CHECK_GROUPS.keys())
    for group in selected:
        runner = _CHECK_GROUPS.get(group)
        if runner is None:
            report.results.append(CheckResult(
                name=f"group:{group}", ok=False,
                detail=f"unknown group; valid: {sorted(_CHECK_GROUPS)}",
            ))
            continue
        try:
            results = runner()
        except Exception as exc:  # noqa: BLE001
            results = [CheckResult(
                name=f"group:{group}", ok=False,
                detail=f"check group crashed: {exc!r}",
            )]
        report.results.extend(results)
    return report


def _format_report(report: PreflightReport) -> str:
    lines = []
    width = max((len(r.name) for r in report.results), default=20)
    for r in report.results:
        mark = "[ok]" if r.ok else "[FAIL]"
        detail = f" — {r.detail}" if r.detail else ""
        lines.append(f"  {mark:<6} {r.name:<{width}}  ({r.elapsed_ms:>6.1f} ms){detail}")
    summary = "ALL OK" if report.all_ok else f"{sum(1 for r in report.results if not r.ok)} FAILED"
    lines.append("")
    lines.append(f"  → {summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight self-check.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human text.")
    parser.add_argument("--only", default="",
                        help="Comma-separated subset of check groups: "
                             "imports, frontends, tokenjuice, workers, pytest")
    args = parser.parse_args(argv)
    groups = [g.strip() for g in args.only.split(",") if g.strip()] or None
    report = run_preflight(groups)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))
    return 0 if report.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
