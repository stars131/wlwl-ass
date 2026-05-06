"""Interactive CLI onboarding wizard.

Drives :func:`launcher.onboarding.save_minimal` from a terminal so command-line
users have the same 30-second first-run experience as the GUI wizard.

Entry points:
  * ``python -m launcher.cli_init``      — explicit invocation
  * ``python -m launcher.cli_init --check`` — exit 0/1 by setup status (CI gate)
  * Auto-triggered by ``agentmain.py`` when ``onboarding.status()`` says
    ``needs_setup`` AND stdin is a TTY AND ``--no-wizard`` not passed.

Design rules:
  * Stdlib only (no rich/click) — first-run can never fail on missing deps.
  * No network calls in the wizard itself; offer a separate "test connection"
    step that the user can skip.
  * Idempotent: running twice with the same answers produces the same .env.
  * Survive Ctrl+C cleanly without leaving a half-written .env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from launcher import onboarding


# ─── Provider catalog ────────────────────────────────────────────────────
#
# Curated list of common base URLs to suggest. The user can always type a
# custom URL — these are just shortcuts so first-time users don't have to
# remember the exact OpenAI-compat endpoint of their relay of choice.

_PROVIDER_PRESETS: dict[str, list[dict[str, str]]] = {
    "openai": [
        {"label": "Official OpenAI", "base": "https://api.openai.com/v1", "model": "gpt-5.4"},
        {"label": "Azure OpenAI (custom)", "base": "https://YOUR-RESOURCE.openai.azure.com", "model": "gpt-4o"},
        {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"},
        {"label": "Moonshot / Kimi", "base": "https://api.moonshot.cn/v1", "model": "kimi-k2"},
        {"label": "智谱 GLM", "base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5"},
        {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "model": "anthropic/claude-opus-4-7"},
        {"label": "OAI-Free relay", "base": "https://hub.oaifree.com/v1", "model": "gpt-5.4"},
    ],
    "anthropic": [
        {"label": "Official Anthropic", "base": "https://api.anthropic.com", "model": "claude-opus-4-7"},
        {"label": "Anthropic via relay (sk-* / cr_*)", "base": "https://YOUR-RELAY/v1", "model": "claude-opus-4-7"},
    ],
}


# ─── Small terminal helpers (color + prompts) ────────────────────────────
#
# Color is on by default but auto-disabled when stdout isn't a TTY (CI, pipes).

def _isatty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


_COLOR_ON = _isatty() and os.environ.get("NO_COLOR", "") == ""


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR_ON else s


def _bold(s: str) -> str: return _c(s, "1")
def _dim(s: str) -> str:  return _c(s, "2")
def _green(s: str) -> str: return _c(s, "32")
def _yellow(s: str) -> str: return _c(s, "33")
def _red(s: str) -> str: return _c(s, "31")
def _cyan(s: str) -> str: return _c(s, "36")


def _ask(prompt: str, *, default: Optional[str] = None, allow_empty: bool = False) -> str:
    """Prompt with optional default. Returns user input or default; raises
    KeyboardInterrupt to caller for clean abort."""
    suffix = f" [{_dim(default)}]" if default else ""
    while True:
        ans = input(f"{prompt}{suffix} > ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        if allow_empty:
            return ""
        print(_yellow("(input required)"))


def _ask_choice(prompt: str, choices: list[str], *, default_idx: int = 0) -> int:
    """Numbered menu. Returns the selected index. Empty input → default_idx."""
    print(prompt)
    for i, c in enumerate(choices):
        marker = "→" if i == default_idx else " "
        print(f"  {marker} [{i + 1}] {c}")
    while True:
        ans = input(f"choice [1-{len(choices)}, default {default_idx + 1}] > ").strip()
        if not ans:
            return default_idx
        try:
            n = int(ans)
            if 1 <= n <= len(choices):
                return n - 1
        except ValueError:
            pass
        print(_yellow(f"please enter a number in 1..{len(choices)}"))


def _ask_yes_no(prompt: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{prompt} {suffix} > ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print(_yellow("answer 'y' or 'n'"))


# ─── Connection test ─────────────────────────────────────────────────────


def _test_openai_compat(base_url: str, apikey: str, model: str, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Hit ``GET <base>/models`` (the universal OAI-compat probe). Many
    relays only accept POST, in which case we fall back to a tiny chat
    completion. Returns (ok, detail).

    Network test is intentionally cheap — we want to confirm "credentials
    parse and the host responds", not measure quality.
    """
    base = base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {apikey}",
        "User-Agent": "ga-cli-init/1.0",
    }
    # Step 1: GET /models (cheap, doesn't burn quota).
    try:
        req = urllib.request.Request(base + "/models", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, f"GET /models → HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 401/403 = key invalid; 404/405 = endpoint shape differs but server reachable.
        if exc.code == 401:
            return False, "HTTP 401 — apikey rejected"
        if exc.code == 403:
            return False, "HTTP 403 — apikey forbidden (check plan / region)"
        # 404/405/etc → fall through to the chat probe.
    except (urllib.error.URLError, OSError) as exc:
        return False, f"network: {type(exc).__name__}: {exc}"

    # Step 2: minimal POST /chat/completions.
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, f"POST /chat/completions → HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} — apikey rejected"
        return False, f"HTTP {exc.code} — server reachable but rejects probe"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"network: {type(exc).__name__}: {exc}"


# ─── Wizard flow ─────────────────────────────────────────────────────────


def _print_banner() -> None:
    print()
    print(_bold("┌─ wlwl-ass first-run setup ────────────────────────────"))
    print(_bold("│"))
    print(_bold("│ ") + "We'll save your API config to " + _cyan(".env") + " — no Python edits.")
    print(_bold("│ ") + _dim("Existing settings are preserved; only the keys you provide are touched."))
    print(_bold("└──────────────────────────────────────────────────────────"))
    print()


def run_wizard(*, test_connection: bool = True) -> dict[str, Any]:
    """Execute the interactive wizard. Returns the final
    :func:`onboarding.status` snapshot."""
    _print_banner()

    # 1. Provider
    providers = list(_PROVIDER_PRESETS.keys())
    p_idx = _ask_choice("Which API to start with?", providers, default_idx=0)
    provider = providers[p_idx]

    # 2. Base URL preset (or custom)
    presets = _PROVIDER_PRESETS[provider]
    preset_labels = [f"{p['label']}  {_dim(p['base'])}" for p in presets] + ["(custom URL)"]
    b_idx = _ask_choice(f"\nWhich {provider} endpoint?", preset_labels, default_idx=0)
    if b_idx < len(presets):
        base_url = presets[b_idx]["base"]
        default_model = presets[b_idx]["model"]
    else:
        base_url = _ask("Custom base URL (e.g. https://my-relay.example.com/v1)")
        default_model = "gpt-5.4" if provider == "openai" else "claude-opus-4-7"

    # 3. API key — never echoed back; getpass would hide it but breaks on some
    #    Windows terminals during pipe input, so we just prompt openly. (User
    #    sees their own key as they paste; not a leak vector.)
    apikey = _ask(f"\n{provider.upper()} API key (paste here)")
    if len(apikey) < 10:
        print(_yellow("⚠  key looks unusually short — continuing anyway."))

    # 4. Model name
    model = _ask("\nModel name", default=default_model)

    # 5. Optional connection test
    if test_connection and provider == "openai":
        if _ask_yes_no("\nTest the connection now? (3-second probe, no quota burn)", default=True):
            print(_dim("  probing…"))
            ok, detail = _test_openai_compat(base_url, apikey, model)
            if ok:
                print(_green(f"  ✓ {detail}"))
            else:
                print(_red(f"  ✗ {detail}"))
                if not _ask_yes_no("Save anyway?", default=False):
                    print(_yellow("Aborted — nothing written."))
                    return onboarding.status()

    # 6. Persist
    print()
    result = onboarding.save_minimal(provider, apikey=apikey, base_url=base_url, model=model)

    print(_bold("┌─ Done ────────────────────────────────────────────────────"))
    env_path = os.path.join(onboarding._project_root(), ".env")
    print(_bold("│ ") + f"Saved to {_cyan(env_path)}")
    print(_bold("│ ") + f"Provider: {_green(provider)}   Model: {_green(model)}")
    print(_bold("│ ") + f"Status:   needs_setup={result['needs_setup']}")
    print(_bold("│"))
    print(_bold("│ ") + "Next:")
    print(_bold("│ ") + f"  • {_cyan('python agentmain.py')}        — start a CLI session")
    print(_bold("│ ") + f"  • {_cyan('python -m launcher.doctor')}   — check the rest of your install")
    print(_bold("│ ") + f"  • {_cyan('python -m launcher.metrics')}  — see your task / tool stats")
    print(_bold("└──────────────────────────────────────────────────────────"))
    return result


# ─── CLI ─────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ga init",
        description="Interactive first-run setup. Writes to .env.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Exit 0 if already configured, 1 otherwise. No prompts.")
    parser.add_argument("--no-test", action="store_true",
                        help="Skip the connection probe step.")
    parser.add_argument("--force", action="store_true",
                        help="Run wizard even if a usable config already exists.")
    args = parser.parse_args(argv)

    st = onboarding.status()
    if args.check:
        if st.get("needs_setup"):
            print(_yellow("needs_setup: ") + (st.get("reason") or ""))
            return 1
        print(_green("ok: ") + ("env-recognized" if st.get("env_recognized") else "configured"))
        return 0

    if not args.force and not st.get("needs_setup"):
        print(_green("Already configured.") + " "
              + _dim(f"(env_recognized={st.get('env_recognized')}, has_mykey={st.get('has_mykey')})"))
        print(_dim("Re-run with --force to reconfigure."))
        return 0

    try:
        run_wizard(test_connection=not args.no_test)
    except KeyboardInterrupt:
        print()
        print(_yellow("Aborted by user — no changes written."))
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
