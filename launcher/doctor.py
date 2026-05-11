"""Diagnostics — "is this install actually going to work?"

Prescriptive over descriptive: every failed check carries the exact command
that fixes it. Designed to answer the most common new-user question:
"I configured X but it doesn't work, what do I run?"

Output:
  * :func:`run_diagnostics` returns a structured dict the GUI / API can render.
  * ``python -m launcher.doctor`` prints a colored CLI report.

Design rules:
  * Stdlib only — running doctor must never fail because doctor's own deps
    are missing.
  * Each check is an independent function returning a :class:`Check`. Order
    doesn't matter; one failure never short-circuits the rest.
  * Network checks are opt-in (``include_network=True``) because they're slow
    and unreliable on first run before a config exists.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import shutil
import socket
import sys
import urllib.request
import urllib.error
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Literal

# Re-use the bot SDK manifest so doctor and the bots tab can never disagree
# about what counts as "configured" or "installed".
from launcher.bot_manager import BOT_SPECS, _load_mykeys

# Re-use the onboarding placeholder detector so doctor's "real key" criterion
# matches the wizard's "needs setup" criterion exactly.
from launcher.onboarding import _is_placeholder, _CONFIG_KEY_HINT_RE

Severity = Literal["ok", "warn", "fail", "info"]


@dataclass
class Check:
    """One diagnostic result.

    Fix is intentionally a single line of shell — copy-pasteable. ``detail``
    can be longer free-text. Severity ``warn`` = something is off but the
    feature is opt-in; ``fail`` = a feature the user is trying to use will
    not work."""
    id: str
    title: str
    severity: Severity
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    project_root: str = ""

    def add(self, c: Check) -> None:
        self.checks.append(c)


def _project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


# ─── Individual checks ────────────────────────────────────────────────────


# Known import-name → pip-install-name mismatches. Without this map, doctor
# would suggest `pip install Crypto` (which is a different, abandoned package)
# instead of `pip install pycryptodome`. Source: each bot's documented install
# in README.md plus the bot_manager.BOT_SPECS sdk_modules tuples.
_PIP_PACKAGE_FOR_IMPORT = {
    "Crypto": "pycryptodome",
    "telegram": "python-telegram-bot",
    "botpy": "qq-botpy",
    "lark_oapi": "lark-oapi",
    "wecom_aibot_sdk": "wecom_aibot_sdk",
    "dingtalk_stream": "dingtalk-stream",
}


def _pip_name(module: str) -> str:
    """Map a Python import name to its PyPI distribution name when they differ."""
    return _PIP_PACKAGE_FOR_IMPORT.get(module, module)


def check_python_version() -> Check:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        return Check(
            "python.version",
            f"Python {major}.{minor}",
            "fail",
            "wlwl-ass requires Python ≥ 3.10 (f-string parsing, walrus, structural pattern matching).",
            "Install Python 3.11+: https://www.python.org/downloads/",
        )
    return Check("python.version", f"Python {major}.{minor}.{sys.version_info[2]}", "ok")


def check_core_deps() -> list[Check]:
    out: list[Check] = []
    # ``requests`` is the one hard core-runtime dep beyond stdlib.
    if importlib.util.find_spec("requests") is None:
        out.append(Check(
            "deps.requests", "core: requests", "fail",
            "The agent loop's HTTP client uses requests.",
            "pip install requests",
        ))
    else:
        out.append(Check("deps.requests", "core: requests", "ok"))
    return out


_GUI_DEPS = [
    ("PySide6", "Qt legacy launcher (`launch.pyw --qt-legacy`)"),
]


def check_gui_deps() -> list[Check]:
    out: list[Check] = []
    for mod, what in _GUI_DEPS:
        if importlib.util.find_spec(mod) is None:
            out.append(Check(
                f"deps.gui.{mod}",
                f"GUI: {mod}",
                "warn",
                f"Missing — needed for {what}.",
                f"pip install {mod}",
            ))
        else:
            out.append(Check(f"deps.gui.{mod}", f"GUI: {mod}", "ok"))
    return out


def check_tauri_toolchain() -> list[Check]:
    out: list[Check] = []
    npm = shutil.which("npm")
    node = shutil.which("node")
    cargo = shutil.which("cargo")
    if not npm or not node:
        out.append(Check(
            "tauri.node",
            "Tauri GUI: npm + node",
            "warn",
            "Tauri dev mode (default `python launch.pyw`) needs Node + npm. Falling back to Qt or webview.",
            "Install Node 20+: https://nodejs.org",
        ))
    else:
        out.append(Check("tauri.node", f"Tauri GUI: npm ({npm})", "ok"))
    if not cargo:
        out.append(Check(
            "tauri.rust",
            "Tauri GUI: cargo (Rust)",
            "warn",
            "Tauri dev mode needs the Rust toolchain to compile the shell binary.",
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        ))
    else:
        out.append(Check("tauri.rust", f"Tauri GUI: cargo ({cargo})", "ok"))
    return out


def check_llm_config() -> list[Check]:
    """Classify how the user has (or hasn't) configured an LLM.

    Walks the merged credential view from :func:`llmcore.reload_mykeys`
    and reports one of:

      * ``ok`` — at least one real (non-placeholder) session config
      * ``fail`` (placeholder-only) — every config dict has a template
        apikey like ``sk-YOUR-KEY``
      * ``fail`` (nothing) — no config sources produced anything
    """
    out: list[Check] = []
    root = _project_root()
    try:
        import llmcore
        mk = llmcore.reload_mykeys(project_root=root)[0]
    except Exception:
        mk = {}
    real_keys: list[str] = []
    placeholders: list[str] = []
    for name, value in mk.items():
        if not _CONFIG_KEY_HINT_RE.search(name):
            continue
        if not isinstance(value, dict):
            continue
        if "mixin" in name.lower():
            continue
        apikey = value.get("apikey", "")
        if isinstance(apikey, str) and apikey:
            (placeholders if _is_placeholder(apikey) else real_keys).append(name)
    if real_keys:
        out.append(Check(
            "llm.config", f"{len(real_keys)} configured LLM session(s)",
            "ok",
            f"Active sessions: {', '.join(real_keys[:5])}",
        ))
    elif placeholders:
        out.append(Check(
            "llm.config",
            "Only placeholder keys",
            "fail",
            f"Found {len(placeholders)} session(s) but every apikey looks like a template placeholder.",
            "Open the GUI's API Config tab to add a real key, "
            "or edit ~/.wlwl-ass/config.json directly.",
        ))
    else:
        out.append(Check(
            "llm.config", "No usable LLM config", "fail",
            "No API key found in shell env / .env / ~/.wlwl-ass/config.json / temp/launcher_api_configs.json.",
            "Launch the Tauri GUI (start_from_zero.cmd or `python launch.pyw`) "
            "and add an entry under the API Config tab.",
        ))
    return out


def check_bots() -> list[Check]:
    """Per-bot status. Surfaces *exactly* the pip command needed to fix each
    SDK-missing bot — this is the Hermes-style prescriptive bit."""
    out: list[Check] = []
    root = _project_root()
    mk = _load_mykeys(root)
    for key, spec in BOT_SPECS.items():
        configured = all(str(mk.get(f, "") or "").strip() for f in spec.mykey_fields) \
            if spec.mykey_fields else True
        missing_modules = [
            m for m in spec.sdk_modules if importlib.util.find_spec(m) is None
        ]
        if not spec.auto_start:
            packages = " ".join(_pip_name(m) for m in missing_modules)
            out.append(Check(
                f"bot.{key}", f"bot {spec.display_name}: disabled by default", "info",
                "Optional. It will not auto-start on GUI launch; start it manually from the Bots tab when needed.",
                f"pip install {packages}" if packages else None,
            ))
        elif not configured and not missing_modules:
            out.append(Check(
                f"bot.{key}", f"bot {spec.display_name}: not configured", "info",
                f"Optional. Set {', '.join(spec.mykey_fields)} via the GUI's Bots tab "
                f"or `python -m launcher.config set bots.{key}.<field> ...` to enable.",
            ))
        elif not configured:
            out.append(Check(
                f"bot.{key}", f"bot {spec.display_name}: not configured", "info",
                f"Both unconfigured AND SDK missing — leaving alone (opt-in feature).",
            ))
        elif missing_modules:
            # Configured but SDK missing → exactly the failure mode the user
            # hit with Feishu. Loud + actionable.
            packages = " ".join(_pip_name(m) for m in missing_modules)
            out.append(Check(
                f"bot.{key}", f"bot {spec.display_name}: SDK missing", "fail",
                f"Configured but Python module(s) {missing_modules} not importable.",
                f"pip install {packages}",
            ))
        else:
            out.append(Check(
                f"bot.{key}", f"bot {spec.display_name}: ready", "ok",
            ))
    return out


def check_paths() -> list[Check]:
    out: list[Check] = []
    root = _project_root()
    for sub, label, severity_if_missing in [
        ("memory", "memory/ (skill SOPs + L1-L4)", "fail"),
        ("temp", "temp/ (runtime scratch)", "warn"),
        ("temp/activity", "temp/activity/ (activity log)", "info"),
        ("frontends", "frontends/ (bot scripts)", "fail"),
    ]:
        p = os.path.join(root, sub)
        if os.path.isdir(p):
            out.append(Check(f"path.{sub.replace('/', '.')}", label, "ok"))
        else:
            out.append(Check(
                f"path.{sub.replace('/', '.')}", f"{label} — missing",
                severity_if_missing,  # type: ignore[arg-type]
                f"Expected directory not found: {p}",
                f"mkdir -p {sub}" if severity_if_missing != "fail"
                else "Reinstall the project — these dirs ship in the repo.",
            ))
    # Writability spot-check on temp/.
    tmp = os.path.join(root, "temp")
    if os.path.isdir(tmp):
        probe = os.path.join(tmp, ".doctor_writeprobe")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.unlink(probe)
            out.append(Check("path.temp.writable", "temp/ is writable", "ok"))
        except OSError as exc:
            out.append(Check(
                "path.temp.writable", "temp/ is NOT writable", "fail",
                f"Write probe failed: {exc}",
                "Check filesystem permissions / disk space.",
            ))
    return out


def check_mcp() -> list[Check]:
    """Surface MCP config presence + parseability without spawning servers.

    Doctor is allowed to be slow on network checks but should never wait
    on user-controlled subprocess startup, so we stop at "config parses
    and references a launchable command". Real handshake errors surface
    naturally the first time the agent calls mcp_call.
    """
    out: list[Check] = []
    try:
        from tools.mcp_client import config_path, load_config
    except ImportError as exc:
        return [Check("mcp.import", "tools.mcp_client unavailable", "warn",
                      str(exc),
                      "MCP integration is optional; ignore if you don't use it.")]
    path = config_path()
    if not os.path.isfile(path):
        return [Check("mcp.config", f"No MCP config at {path}", "info",
                      "MCP integration is opt-in. Servers are configured in "
                      f"{path} (gitignored).",
                      "cp assets/mcp_servers.template.json " + os.path.basename(path))]
    cfg = load_config(path)
    if not cfg:
        return [Check("mcp.config", f"MCP config at {path} parses to no servers", "warn",
                      "File exists but contains no valid `mcpServers` entries "
                      "(missing `command`, malformed JSON, etc.).",
                      f"Compare against assets/mcp_servers.template.json")]
    out.append(Check("mcp.config", f"{len(cfg)} MCP server(s) configured", "ok",
                     ", ".join(sorted(cfg.keys()))))
    # Verify each server's command is on PATH (or absolute and exists).
    for name, server in sorted(cfg.items()):
        cmd = server["command"]
        if os.path.isabs(cmd):
            present = os.path.isfile(cmd)
        else:
            present = shutil.which(cmd) is not None
        if present:
            out.append(Check(f"mcp.server.{name}", f"{name}: {cmd} on PATH", "ok"))
        else:
            out.append(Check(
                f"mcp.server.{name}", f"{name}: {cmd!r} not on PATH", "warn",
                f"Server {name!r} declares command {cmd!r} but it's not "
                "currently launchable. mcp_call will fail until installed.",
                f"Install / locate {cmd!r}, or remove the entry from {path}",
            ))
    return out


def check_network(timeout: float = 3.0) -> list[Check]:
    """Probe each configured apibase. ``include_network=True`` only — slow."""
    out: list[Check] = []
    root = _project_root()
    mk = _load_mykeys(root)
    bases: dict[str, str] = {}
    for name, value in mk.items():
        if not _CONFIG_KEY_HINT_RE.search(name):
            continue
        if not isinstance(value, dict):
            continue
        base = str(value.get("apibase", "") or "").rstrip("/")
        if base and base not in bases.values():
            bases[name] = base
    if not bases:
        return [Check("net.apibase", "No apibase to probe", "info")]
    for name, base in bases.items():
        ok, detail = _probe_url(base, timeout=timeout)
        if ok:
            out.append(Check(f"net.{name}", f"reachable: {base}", "ok"))
        else:
            out.append(Check(
                f"net.{name}", f"unreachable: {base}", "warn", detail,
                "Check apibase URL / proxy / network. (Some endpoints reject "
                "GET / and only accept POST — a 405 response is fine.)",
            ))
    return out


def _probe_url(url: str, *, timeout: float) -> tuple[bool, str]:
    """Returns (reachable, detail). 405/401/403 still count as reachable
    (the host answered; it just doesn't like our empty GET)."""
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "ga-doctor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 4xx with body means the server is alive and answering.
        return True, f"HTTP {exc.code} (server reachable)"
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ─── Composition + entry points ──────────────────────────────────────────


CheckFn = Callable[[], Check | Iterable[Check]]


def _as_checks(value: Check | Iterable[Check]) -> list[Check]:
    if isinstance(value, Check):
        return [value]
    return [c for c in value if isinstance(c, Check)]


def _run_check_group(name: str, fn: CheckFn) -> list[Check]:
    try:
        return _as_checks(fn())
    except Exception as exc:
        return [Check(
            f"doctor.{name}",
            f"Doctor check failed: {name}",
            "fail",
            f"{type(exc).__name__}: {exc}",
            "Open the Settings diagnostics log or run `python -m launcher.doctor --json` for details.",
        )]


def run_diagnostics(*, include_network: bool = False) -> dict[str, Any]:
    report = Report(project_root=_project_root())
    groups: list[tuple[str, CheckFn]] = [
        ("python_version", check_python_version),
        ("core_deps", check_core_deps),
        ("gui_deps", check_gui_deps),
        ("tauri_toolchain", check_tauri_toolchain),
        ("llm_config", check_llm_config),
        ("bots", check_bots),
        ("paths", check_paths),
        ("mcp", check_mcp),
    ]
    if include_network:
        groups.append(("network", check_network))

    results: list[list[Check]] = [[] for _ in groups]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(groups) or 1)) as pool:
        future_to_idx = {
            pool.submit(_run_check_group, name, fn): idx
            for idx, (name, fn) in enumerate(groups)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()

    for group_results in results:
        for c in group_results:
            report.add(c)

    summary = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for c in report.checks:
        summary[c.severity] = summary.get(c.severity, 0) + 1
    report.summary = summary
    return {
        "project_root": report.project_root,
        "summary": summary,
        "checks": [asdict(c) for c in report.checks],
    }


# ─── CLI ─────────────────────────────────────────────────────────────────


_CLI_COLORS = {
    "ok": "\033[32m✓\033[0m",
    "warn": "\033[33m●\033[0m",
    "fail": "\033[31m✗\033[0m",
    "info": "\033[2m·\033[0m",
}


def _format_cli(result: dict[str, Any], *, color: bool = True) -> str:
    lines = [f"wlwl-ass doctor — {result['project_root']}"]
    for c in result["checks"]:
        sev = c["severity"]
        marker = _CLI_COLORS.get(sev, "?") if color else f"[{sev:4s}]"
        lines.append(f"  {marker} {c['title']}")
        if c.get("detail"):
            lines.append(f"      {c['detail']}")
        if c.get("fix"):
            lines.append(f"      \033[2mfix:\033[0m {c['fix']}" if color
                         else f"      fix: {c['fix']}")
    s = result["summary"]
    lines.append("")
    lines.append(
        f"summary: {s.get('ok',0)} ok · {s.get('warn',0)} warn "
        f"· {s.get('fail',0)} fail · {s.get('info',0)} info"
    )
    return "\n".join(lines)


def _force_utf8_stdout() -> None:
    """Reconfigure sys.stdout to UTF-8 on Windows cmd / PowerShell.

    BOT_SPECS' display_name is Chinese ("飞书" / "微信" / "钉钉" / "企业微信"),
    so without this the user sees ``bot ����: SDK missing`` in the very check
    that's supposed to tell them what went wrong. Best-effort — if reconfigure
    is unavailable (very old Python, redirected stdout that doesn't support
    it) we silently fall through and accept whatever encoding ships."""
    try:
        enc = (sys.stdout.encoding or "").lower()
        if enc and enc not in ("utf-8", "utf8", "cp65001"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive only
        pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    import argparse
    parser = argparse.ArgumentParser(prog="doctor", description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of text.")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--network", action="store_true",
                        help="Probe each apibase URL (slow, requires network).")
    args = parser.parse_args(argv)
    result = run_diagnostics(include_network=args.network)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_format_cli(result, color=not args.no_color))
    # Exit non-zero only on hard failures so CI can gate on this.
    return 0 if result["summary"].get("fail", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
