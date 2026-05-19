"""wlwl-ass interactive terminal CLI — entry point for the ``wlwl`` command.

Mirrors the UX of ``claude`` / ``codex``: drop into an interactive REPL by
default, or run one-shot with a positional / ``-p`` argument. The actual
agent is :class:`agentmain.GeneraticAgent`; slash commands are handled
inside the agent's run loop via
:class:`frontends.cli_commands.SharedCommandHandler`, so this module is a
thin terminal frontend.

Usage:
    wlwl                          # interactive REPL in current dir
    wlwl "list python files"      # one-shot (positional)
    wlwl -p "list python files"   # one-shot (flag form)
    wlwl --llm 1                  # use the 2nd configured LLM
    wlwl --permission ask         # prompt before each tool call
    wlwl --cwd /path/to/project   # treat that dir as project root
    wlwl --version
"""
from __future__ import annotations

import argparse
import importlib
import os
import queue
import sys
import threading


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _setup_readline() -> None:
    try:
        import readline  # noqa: F401  Unix: autoloads history + arrow keys
    except ImportError:
        try:
            import pyreadline3  # noqa: F401  Windows: optional pip dep
        except ImportError:
            pass


def _resolve_project_root(cwd_arg):
    return os.path.abspath(cwd_arg) if cwd_arg else os.getcwd()


def _print_no_llm_hint(project_root):
    config_path = os.path.join(project_root, "temp", "launcher_api_configs.json")
    print(
        "[wlwl] No LLM is configured. Add an API key first via the GUI "
        "(start_from_zero.cmd / `python launch.pyw`) or by editing "
        f"{config_path}.",
        file=sys.stderr,
    )


def _looks_like_no_llm_config(exc):
    text = str(exc).lower()
    return (
        "no usable llm config" in text
        or "no llm" in text
        or isinstance(exc, (IndexError, ZeroDivisionError))
    )


def _print_banner(agent):
    llm = agent.get_llm_name() if agent.llmclient else "(no LLM configured)"
    print()
    print("  wlwl-ass CLI")
    print(f"  LLM       : {llm}")
    print(f"  Project   : {agent.project_root}")
    print(f"  Permission: {agent.permission_mode}")
    print()
    print("  /help to list commands · /quit or Ctrl+D to exit · Ctrl+C to abort a running task")
    print()


def _drain_queue(agent, dq, prompt_label=""):
    """Pump streaming chunks from the agent's display queue to stdout.

    Returns the final response text. The agent puts deltas (``{'next': ...}``)
    when ``agent.inc_out`` is True and a final ``{'done': full_text}`` once the
    turn finishes."""
    prev_len = 0
    while True:
        try:
            item = dq.get(timeout=3600)
        except queue.Empty:
            print(f"\n[{prompt_label or 'wlwl'}] timed out waiting for response.",
                  file=sys.stderr)
            return ""
        if "done" in item:
            done_text = item.get("done") or ""
            if len(done_text) > prev_len:
                sys.stdout.write(done_text[prev_len:])
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()
            return done_text
        if "next" in item:
            chunk = item.get("next") or ""
            if not chunk:
                continue
            if getattr(agent, "inc_out", False):
                sys.stdout.write(chunk)
                sys.stdout.flush()
                prev_len += len(chunk)
            else:
                if len(chunk) > prev_len:
                    sys.stdout.write(chunk[prev_len:])
                    sys.stdout.flush()
                    prev_len = len(chunk)


def _build_agent(args):
    project_root = _resolve_project_root(args.cwd)
    try:
        agentmain = importlib.import_module("agentmain")
        GeneraticAgent = agentmain.GeneraticAgent
        AgentRuntimeContext = getattr(agentmain, "AgentRuntimeContext", None)
    except Exception as exc:
        print(f"[wlwl] Failed to import agent runtime: {exc}", file=sys.stderr)
        return None

    context = None
    if AgentRuntimeContext is not None:
        context = AgentRuntimeContext(
            project_root=project_root,
            llm_no=int(args.llm),
            permission_mode=args.permission,
            use_project_context=not args.no_project_context,
        )
    try:
        try:
            agent = GeneraticAgent(runtime_context=context) if context is not None else GeneraticAgent()
        except TypeError as exc:
            if context is None or "runtime_context" not in str(exc):
                raise
            agent = GeneraticAgent()
    except Exception as exc:
        if _looks_like_no_llm_config(exc):
            _print_no_llm_hint(project_root)
        else:
            print(f"[wlwl] Failed to initialize agent: {exc}", file=sys.stderr)
        return None

    if not getattr(agent, "llmclients", None):
        _print_no_llm_hint(project_root)
        return None
    try:
        agent.next_llm(int(args.llm))
    except Exception as exc:
        print(f"[wlwl] Failed to select LLM #{args.llm}: {exc}", file=sys.stderr)
        return None
    agent.configure_cli(
        permission_mode=args.permission,
        project_root=project_root,
        use_project_context=not args.no_project_context,
        interactive=(args.permission == "ask"),
        cwd_project=True,
    )
    agent.inc_out = True
    return agent


def _one_shot(agent, prompt):
    dq = agent.put_task(prompt, source="cli")
    _drain_queue(agent, dq)
    return 0


def _repl(agent):
    _setup_readline()
    _print_banner(agent)
    while True:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            if agent.is_running:
                agent.abort()
                print("\n[wlwl] aborted current task — type a new request or /quit to exit")
                continue
            print()
            break
        line = (line or "").strip()
        if not line:
            continue
        if line.lower() in ("/quit", "/exit", ":q", ":quit"):
            break
        dq = agent.put_task(line, source="cli")
        try:
            _drain_queue(agent, dq)
        except KeyboardInterrupt:
            agent.abort()
            print("\n[wlwl] aborted — type a new request or /quit to exit")
    return 0


def _parser():
    p = argparse.ArgumentParser(
        prog="wlwl",
        description="wlwl-ass interactive terminal CLI (like `claude` or `codex`).",
        epilog=(
            "Subcommands (run with --help for details):\n"
            "  wlwl config ...   manage API configs (list/presets/add/use/remove/import/export/probe/backups)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Note: `config` is routed manually in main() BEFORE argparse runs so
    # that `wlwl "do X"` keeps working as one-shot mode. See
    # _split_subcommand for the dispatch.
    p.add_argument("prompt_positional", nargs="?",
                   help="one-shot mode: run this prompt and exit")
    p.add_argument("-p", "--prompt",
                   help="one-shot mode (alternative to positional form)")
    p.add_argument("--llm", type=int, default=0,
                   help="LLM index from the configured list (default 0)")
    p.add_argument("--permission", default="auto",
                   choices=["auto", "ask", "read-only"],
                   help="tool-call permission policy (default: auto)")
    p.add_argument("--cwd",
                   help="project root for this session (default: current dir)")
    p.add_argument("--no-project-context", action="store_true",
                   help="don't load README / CLAUDE.md / etc. into the system prompt")
    p.add_argument("--version", action="store_true",
                   help="print version and exit")
    return p


_SUBCOMMANDS = ("config", "tokens", "readme")


def _split_subcommand(argv):
    """Argparse subparsers consume the first positional even when it isn't
    a registered subcommand, which would break ``wlwl "do X"``. Peel the
    subcommand off manually: only route to a subparser when ``argv[0]``
    is explicitly one of the registered names; everything else falls
    through to the normal positional parser."""
    if not argv:
        return None, argv
    first = argv[0]
    if first in _SUBCOMMANDS:
        return first, argv[1:]
    return None, argv


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    sub_name, rest = _split_subcommand(argv)
    if sub_name == "config":
        # Run the config CLI through its own argparse — keeps subcommand
        # help / usage independent of the main parser.
        from launcher import cli_config
        return cli_config.main(rest)
    if sub_name == "tokens":
        from launcher import cli_tokens
        return cli_tokens.main(rest)
    if sub_name == "readme":
        from launcher import cli_readme
        return cli_readme.main(rest)

    args = _parser().parse_args(rest)

    if args.version:
        try:
            from importlib.metadata import version, PackageNotFoundError
            try:
                print(f"wlwl-ass {version('wlwl-ass')}")
            except PackageNotFoundError:
                print("wlwl-ass (version unknown — package not installed)")
        except Exception:
            print("wlwl-ass (version unknown)")
        return 0

    one_shot = args.prompt or args.prompt_positional

    agent = _build_agent(args)
    if agent is None:
        return 2

    runner = threading.Thread(target=agent.run, name="wlwl-agent-run", daemon=True)
    runner.start()

    try:
        if one_shot:
            return _one_shot(agent, one_shot)
        return _repl(agent)
    finally:
        try:
            agent.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
