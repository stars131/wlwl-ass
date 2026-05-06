import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionMode(str, Enum):
    ASK = "ask"
    AUTO = "auto"
    READ_ONLY = "read-only"
    DANGEROUS = "dangerous"


@dataclass
class ToolMetadata:
    display_name: str
    risk: str = "low"
    permission: PermissionDecision = PermissionDecision.ALLOW


@dataclass
class ToolPermissionRequest:
    tool_name: str
    arguments: dict
    cwd: str
    project_root: str | None = None
    metadata: ToolMetadata | None = None

    @property
    def display_name(self):
        return (self.metadata.display_name if self.metadata else self.tool_name)

    @property
    def preview(self):
        if self.tool_name == "code_run":
            code = self.arguments.get("code") or self.arguments.get("script") or ""
            typ = self.arguments.get("type", "python")
            return f"{typ}: {str(code).strip()[:300]}"
        if self.tool_name in {"file_patch", "file_write", "file_read"}:
            return str(self.arguments.get("path", ""))
        if self.tool_name == "web_execute_js":
            return str(self.arguments.get("script", ""))[:300]
        if self.tool_name == "mcp_call":
            srv = self.arguments.get("server") or "?"
            tool = self.arguments.get("tool") or "(discovery)"
            args_blob = str(self.arguments.get("arguments") or {})[:200]
            return f"{srv}/{tool} args={args_blob}"
        return str({k: v for k, v in self.arguments.items() if not str(k).startswith("_")})[:300]


@dataclass
class ToolPermissionResponse:
    decision: PermissionDecision
    message: str | None = None


TOOL_METADATA = {
    "file_read": ToolMetadata("Read file", "low", PermissionDecision.ALLOW),
    "update_working_checkpoint": ToolMetadata("Update working memory", "low", PermissionDecision.ALLOW),
    "ask_user": ToolMetadata("Ask user", "low", PermissionDecision.ALLOW),
    "no_tool": ToolMetadata("Final response", "low", PermissionDecision.ALLOW),
    "web_scan": ToolMetadata("Scan browser", "medium", PermissionDecision.ASK),
    "web_execute_js": ToolMetadata("Execute browser JavaScript", "high", PermissionDecision.ASK),
    "code_run": ToolMetadata("Run command", "high", PermissionDecision.ASK),
    "file_patch": ToolMetadata("Patch file", "high", PermissionDecision.ASK),
    "file_write": ToolMetadata("Write file", "high", PermissionDecision.ASK),
    "start_long_term_update": ToolMetadata("Start memory update", "medium", PermissionDecision.ASK),
    "sop_search": ToolMetadata("Search Sophub SOPs", "low", PermissionDecision.ALLOW),
    "sop_read": ToolMetadata("Read Sophub SOP", "low", PermissionDecision.ALLOW),
    # mcp_call wraps an arbitrary external server — same risk class as
    # code_run because the called tool can do anything the server is
    # configured to allow. Default ASK.
    "mcp_call": ToolMetadata("Invoke MCP server tool", "high", PermissionDecision.ASK),
}


WRITE_TOOLS = {"file_patch", "file_write"}
EXEC_TOOLS = {"code_run", "web_execute_js"}
READ_ONLY_ALLOW = {"file_read", "ask_user", "update_working_checkpoint", "no_tool"}
DANGEROUS_COMMAND_RE = re.compile(
    r"\b(rm\s+-rf|del\s+/[sq]|rmdir\s+/[sq]|format\b|shutdown\b|reboot\b|git\s+reset\s+--hard|git\s+push\s+--force)\b",
    re.IGNORECASE,
)


@dataclass
class PermissionPolicy:
    mode: str = PermissionMode.AUTO.value
    interactive: bool = False
    session_allow_tools: set[str] = field(default_factory=set)
    # Optional persistent allowlist. ``None`` → no cross-session memory
    # (the original behavior). Pass an ``ApprovalAllowlist`` instance to
    # enable pattern-based "user already approved this in a past session".
    allowlist: object | None = None  # avoid import cycle; type is ApprovalAllowlist

    def decide(self, request: ToolPermissionRequest) -> ToolPermissionResponse:
        mode = PermissionMode(self.mode)
        if mode == PermissionMode.DANGEROUS:
            return ToolPermissionResponse(PermissionDecision.ALLOW)
        if request.tool_name in self.session_allow_tools:
            return ToolPermissionResponse(PermissionDecision.ALLOW)
        # Persistent allowlist — checked BEFORE the dangerous-command +
        # path-outside-project checks because the user explicitly approved
        # this exact pattern earlier (often a long-running git workflow).
        # If they wanted to revoke, they'd remove the rule.
        if self.allowlist is not None:
            try:
                rule = self.allowlist.match(request.tool_name, request.arguments)
            except Exception:
                rule = None
            if rule is not None:
                try:
                    self.allowlist.record_use(rule.id)
                except Exception:
                    pass  # never let bookkeeping break a tool call
                return ToolPermissionResponse(PermissionDecision.ALLOW)
        if mode == PermissionMode.READ_ONLY:
            if request.tool_name in READ_ONLY_ALLOW and self._path_inside_project(request):
                return ToolPermissionResponse(PermissionDecision.ALLOW)
            return ToolPermissionResponse(PermissionDecision.DENY, f"Permission denied in read-only mode: {request.display_name}")
        if self._is_dangerous_command(request):
            return self._ask_or_deny(request, "Potentially destructive command requires confirmation")
        if not self._path_inside_project(request):
            return self._ask_or_deny(request, "Path is outside the project root")
        default = (request.metadata.permission if request.metadata else PermissionDecision.ASK)
        if mode == PermissionMode.AUTO and default == PermissionDecision.ASK:
            return ToolPermissionResponse(PermissionDecision.ALLOW)
        if default == PermissionDecision.ASK:
            return self._ask_or_deny(request)
        return ToolPermissionResponse(default)

    def allow_tool_for_session(self, tool_name):
        self.session_allow_tools.add(tool_name)

    def _ask_or_deny(self, request, message=None):
        if self.interactive:
            return ToolPermissionResponse(PermissionDecision.ASK, message)
        return ToolPermissionResponse(PermissionDecision.DENY, message or f"Permission required: {request.display_name}")

    def _is_dangerous_command(self, request):
        if request.tool_name != "code_run":
            return False
        code = str(request.arguments.get("code") or request.arguments.get("script") or "")
        return bool(DANGEROUS_COMMAND_RE.search(code))

    def _path_inside_project(self, request):
        if not request.project_root or request.tool_name not in WRITE_TOOLS | {"file_read"}:
            return True
        path = request.arguments.get("path") or ""
        if not path:
            return True
        base = Path(request.cwd or request.project_root).resolve()
        target = Path(path)
        if not target.is_absolute():
            target = base / target
        try:
            target.resolve().relative_to(Path(request.project_root).resolve())
            return True
        except ValueError:
            return False


class InteractivePermissionPrompter:
    def __init__(self, policy: PermissionPolicy):
        self.policy = policy

    def ask(self, request: ToolPermissionRequest, reason=None):
        print(f"\nPermission required: {request.display_name}")
        if reason:
            print(f"Reason: {reason}")
        print(f"Preview: {request.preview}")
        while True:
            ans = input("Allow? [y]es / [n]o / [a]lways for this session > ").strip().lower()
            if ans in {"y", "yes"}:
                return ToolPermissionResponse(PermissionDecision.ALLOW)
            if ans in {"a", "always"}:
                self.policy.allow_tool_for_session(request.tool_name)
                return ToolPermissionResponse(PermissionDecision.ALLOW)
            if ans in {"n", "no", ""}:
                return ToolPermissionResponse(PermissionDecision.DENY, f"User denied {request.display_name}")


def tool_metadata(tool_name):
    return TOOL_METADATA.get(tool_name, ToolMetadata(tool_name, "medium", PermissionDecision.ASK))
