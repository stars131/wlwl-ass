"""``mcp_client`` — single-entry MCP integration for wlwl-ass.

wlwl-ass's atomic-tool philosophy says "9 tools, don't expand". MCP offers
hundreds of community tools across ~thousands of servers — reflecting each
one as its own GA tool would blow that budget on day one. The compromise:
**one** tool, ``mcp_call``, that fronts every configured MCP server. The
LLM discovers servers and tools via the same entry point and dispatches
through it.

Design constraints (carried over from the rest of GA):

* **stdlib only** — ``subprocess`` + ``json`` + ``threading``. No SDK
  dependency; if the user has no MCP servers configured, this module is
  inert and costs zero memory + zero tokens.
* **Lazy spawn** — a server process is started on first call and cached
  for the remainder of the GA session. ``atexit`` reaps them.
* **Defensive everywhere** — a malformed server reply, a missing
  executable, or a server that hangs must surface as a string error in
  the tool result, never as an exception that aborts the agent loop.

Wire protocol: MCP over stdio is newline-delimited JSON-RPC 2.0. We do
the minimum dance:

  1. ``initialize`` → wait for response → ``notifications/initialized``.
  2. ``tools/list`` → cache the result.
  3. ``tools/call(name, arguments)`` → return the content blocks.

Servers that need richer features (resources, prompts, sampling) are
fine — we just don't surface those capabilities. ``tools/*`` is enough
to cover the 80%-case and matches the single-entry-point compromise.

Public surface:

  * :func:`mcp_call` — the function ``wlwl_ass.py``'s ``do_mcp_call`` wraps.
  * :func:`load_config` / :func:`config_path` — config discovery.
  * :class:`MCPClient` / :class:`MCPRegistry` — exposed for tests and
    the doctor diagnostic.
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any


# ─── Configuration ───────────────────────────────────────────────────────


CONFIG_FILENAME = "mcp_servers.json"
DEFAULT_INIT_TIMEOUT = 10.0   # seconds for initialize+list handshake
DEFAULT_CALL_TIMEOUT = 60.0   # seconds for a tools/call


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path() -> str:
    """Return the resolved path to ``mcp_servers.json``.

    Override with ``WLWL_MCP_CONFIG_PATH``. Defaults to project root so users
    can keep MCP credentials adjacent to ``mykey.py``.
    """
    override = os.environ.get("WLWL_MCP_CONFIG_PATH")
    if override:
        return override
    return os.path.join(_project_root(), CONFIG_FILENAME)


def load_config(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Load + lightly validate the ``mcpServers`` map.

    Returns ``{}`` if the file is missing or malformed — MCP is opt-in,
    a missing config is the common case, never an error. Each server entry
    must have at least ``command``; ``args``/``env`` are optional.
    """
    path = path or config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or not cfg.get("command"):
            continue
        out[str(name)] = {
            "command": str(cfg["command"]),
            "args": [str(a) for a in (cfg.get("args") or [])],
            "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            "cwd": cfg.get("cwd"),
        }
    return out


# ─── MCP client (one process per server) ─────────────────────────────────


@dataclass
class _Pending:
    """Bookkeeping for an in-flight JSON-RPC request."""
    event: threading.Event = field(default_factory=threading.Event)
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class MCPClient:
    """Talks JSON-RPC 2.0 over stdio with one MCP server subprocess.

    Thread-safety: a reader thread drains stdout, dispatches replies to
    the matching :class:`_Pending`. Sends are serialized through ``_lock``.
    Notifications (no ``id``) are silently absorbed — we don't surface
    server logs / progress events through the agent loop.
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.proc: subprocess.Popen[bytes] | None = None
        self.tools: list[dict[str, Any]] | None = None
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._init_error: str | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self, *, timeout: float = DEFAULT_INIT_TIMEOUT) -> None:
        """Spawn the subprocess and complete the MCP handshake.

        Idempotent — ``start`` after a successful start is a no-op. After
        a failed start, ``self._init_error`` is set; subsequent calls
        re-raise it without re-spawning (lets the agent see the same
        message every turn instead of a noisy retry storm).
        """
        if self.proc is not None and self.tools is not None:
            return
        if self._init_error is not None:
            raise MCPError(self._init_error)
        env = os.environ.copy()
        env.update(self.cfg.get("env") or {})
        cmd = [self.cfg["command"], *self.cfg.get("args", [])]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=self.cfg.get("cwd") or None,
                bufsize=0,
            )
        except (OSError, FileNotFoundError) as exc:
            self._init_error = f"failed to spawn {self.cfg['command']!r}: {exc}"
            raise MCPError(self._init_error) from exc
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "wlwl-ass", "version": "0.1.0"},
            }, timeout=timeout)
            self._notify("notifications/initialized")
            tools_resp = self._request("tools/list", {}, timeout=timeout)
        except Exception as exc:
            self._init_error = f"handshake failed: {exc}"
            self.stop()
            raise MCPError(self._init_error) from exc
        self.tools = list(tools_resp.get("tools") or [])

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -- public RPC -------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        self.start()
        return list(self.tools or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None,
                  *, timeout: float = DEFAULT_CALL_TIMEOUT) -> dict[str, Any]:
        self.start()
        return self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, timeout=timeout)

    # -- internals --------------------------------------------------------

    def _request(self, method: str, params: dict[str, Any], *,
                 timeout: float) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            pending = _Pending()
            self._pending[req_id] = pending
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._send(msg)
        if not pending.event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(f"timeout waiting for {method!r} after {timeout:.0f}s")
        if pending.error is not None:
            raise MCPError(f"{method!r} error: {pending.error.get('message') or pending.error}")
        return pending.payload or {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("server is not running")
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"failed to send to server: {exc}") from exc

    def _reader_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for raw in iter(proc.stdout.readline, b""):
            if not raw:
                break
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            req_id = msg.get("id")
            if req_id is None:
                # Notification — not surfaced.
                continue
            with self._lock:
                pending = self._pending.pop(req_id, None)
            if pending is None:
                continue
            if "error" in msg:
                pending.error = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            else:
                pending.payload = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            pending.event.set()
        # Wake any still-waiting callers so they don't hang on a dead server.
        with self._lock:
            stale = list(self._pending.values())
            self._pending.clear()
        for p in stale:
            if p.error is None and p.payload is None:
                p.error = {"message": "server closed stream before responding"}
            p.event.set()


class MCPError(RuntimeError):
    """Surfaced as a ``[mcp_call error] ...`` string in tool output."""


# ─── Registry — one MCPClient per server, shared across the session ─────


class MCPRegistry:
    def __init__(self, config: dict[str, dict[str, Any]] | None = None):
        self.config = config if config is not None else load_config()
        self._clients: dict[str, MCPClient] = {}
        self._lock = threading.Lock()

    def server_names(self) -> list[str]:
        return sorted(self.config.keys())

    def get(self, name: str) -> MCPClient:
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                cfg = self.config.get(name)
                if cfg is None:
                    raise MCPError(f"unknown MCP server {name!r}; configured: {self.server_names() or 'none'}")
                client = MCPClient(name, cfg)
                self._clients[name] = client
        return client

    def shutdown_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:
            try:
                c.stop()
            except Exception:
                pass


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: MCPRegistry | None = None


def _registry() -> MCPRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = MCPRegistry()
            atexit.register(_REGISTRY.shutdown_all)
    return _REGISTRY


def reset_registry_for_tests(config: dict[str, dict[str, Any]] | None = None) -> MCPRegistry:
    """Hard-reset the module singleton — for tests only.

    Tests pass a fake config (or a config with mocked subprocess commands)
    and need a clean slate per test rather than the file-on-disk default.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is not None:
            try:
                _REGISTRY.shutdown_all()
            except Exception:
                pass
        _REGISTRY = MCPRegistry(config=config or {})
    return _REGISTRY


# ─── Public entry point ──────────────────────────────────────────────────


def mcp_call(server: str | None = None, tool: str | None = None,
             arguments: dict[str, Any] | None = None,
             *, timeout: float | None = None) -> str:
    """Three-way overloaded entry point — see module docstring.

    Returns a human-readable string suitable for direct LLM consumption.
    Errors are stringified, never raised.
    """
    try:
        reg = _registry()
    except Exception as exc:
        return f"[mcp_call error] registry init failed: {exc}"

    if not server:
        names = reg.server_names()
        if not names:
            return ("[mcp_call] no MCP servers configured. "
                    f"Create {config_path()} (see assets/mcp_servers.template.json).")
        lines = [f"{len(names)} MCP server(s) configured:"]
        for n in names:
            cfg = reg.config[n]
            lines.append(f"  - {n}  ({cfg['command']} {' '.join(cfg.get('args') or [])})".rstrip())
        lines.append("")
        lines.append("Call mcp_call(server=NAME) to list its tools.")
        return "\n".join(lines)

    try:
        client = reg.get(server)
    except MCPError as exc:
        return f"[mcp_call error] {exc}"

    if not tool:
        try:
            tools = client.list_tools()
        except MCPError as exc:
            return f"[mcp_call error] list tools on {server!r}: {exc}"
        if not tools:
            return f"[mcp_call] server {server!r} exposes no tools."
        lines = [f"{len(tools)} tool(s) on {server!r}:"]
        for t in tools:
            desc = (t.get("description") or "").splitlines()
            head = desc[0][:140] if desc else ""
            lines.append(f"  - {t.get('name')}: {head}")
        lines.append("")
        lines.append(f"Call mcp_call(server={server!r}, tool=NAME, arguments={{...}}) to invoke.")
        return "\n".join(lines)

    try:
        result = client.call_tool(tool, arguments or {},
                                  timeout=timeout or DEFAULT_CALL_TIMEOUT)
    except MCPError as exc:
        return f"[mcp_call error] {server}/{tool}: {exc}"
    return _format_call_result(server, tool, result)


def _format_call_result(server: str, tool: str, result: dict[str, Any]) -> str:
    """Render an MCP ``tools/call`` result as plain text.

    MCP returns ``{content: [{type: "text", text: "..."}, ...], isError?}``.
    We concatenate text blocks and prefix non-text blocks with their type
    so the LLM at least knows something was elided.
    """
    is_error = bool(result.get("isError"))
    blocks = result.get("content") or []
    out: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            out.append(str(b.get("text") or ""))
        elif btype in ("image", "resource"):
            out.append(f"[{btype} block: {b.get('mimeType') or '?'}, {len(json.dumps(b))} bytes elided]")
        else:
            out.append(f"[{btype or 'unknown'} block]")
    body = "\n".join(s for s in out if s)
    if is_error:
        return f"[mcp_call error] {server}/{tool}: {body or '(no detail)'}"
    return body or f"[mcp_call] {server}/{tool} returned no content"
