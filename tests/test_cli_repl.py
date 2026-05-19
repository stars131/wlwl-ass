"""Tests for the ``wlwl`` console-script entry (launcher/cli_repl.py).

These verify the cheap, side-effect-free surface: argparse plumbing, the
``--version`` short-circuit, and the agent builder's early-exit when no
LLM has been configured. We never actually spin up an LLM session here —
that's covered indirectly by the full agent test suite.
"""
from __future__ import annotations

import io
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def _import_cli():
    from launcher import cli_repl
    return cli_repl


def test_version_flag_short_circuits(capsys):
    cli = _import_cli()
    rc = cli.main(["--version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "wlwl-ass" in captured.out


def test_parser_accepts_positional_prompt():
    cli = _import_cli()
    p = cli._parser()
    ns = p.parse_args(["hello world"])
    assert ns.prompt_positional == "hello world"
    assert ns.prompt is None


def test_parser_p_flag():
    cli = _import_cli()
    p = cli._parser()
    ns = p.parse_args(["-p", "do X"])
    assert ns.prompt == "do X"


def test_parser_permission_choices():
    cli = _import_cli()
    p = cli._parser()
    for mode in ("auto", "ask", "read-only"):
        ns = p.parse_args(["--permission", mode])
        assert ns.permission == mode


def test_resolve_project_root_uses_arg_when_given(tmp_path):
    cli = _import_cli()
    assert cli._resolve_project_root(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_resolve_project_root_defaults_to_cwd(monkeypatch, tmp_path):
    cli = _import_cli()
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_project_root(None) == os.path.abspath(str(tmp_path))


def test_build_agent_returns_none_when_no_llm(monkeypatch, capsys):
    """If the user hasn't configured an LLM yet, the CLI should print a
    friendly hint and return None instead of crashing."""
    cli = _import_cli()

    # Stub agentmain.GeneraticAgent to expose an empty llmclients list.
    fake_module = types.ModuleType("agentmain")

    class _FakeAgent:
        llmclients = []
        llmclient = None
    fake_module.GeneraticAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "agentmain", fake_module)

    ns = cli._parser().parse_args([])
    assert cli._build_agent(ns) is None
    err = capsys.readouterr().err
    assert "No LLM is configured" in err


def test_build_agent_handles_constructor_no_llm(monkeypatch, capsys):
    """The real agent can fail during construction when no LLM config exists.
    The CLI should still turn that into the same friendly setup hint."""
    cli = _import_cli()

    fake_module = types.ModuleType("agentmain")

    class _FakeAgent:
        def __init__(self, runtime_context=None):
            raise Exception("[ERROR] No usable LLM config found.")

    fake_module.GeneraticAgent = _FakeAgent
    fake_module.AgentRuntimeContext = None
    monkeypatch.setitem(sys.modules, "agentmain", fake_module)

    ns = cli._parser().parse_args([])
    assert cli._build_agent(ns) is None
    err = capsys.readouterr().err
    assert "No LLM is configured" in err
