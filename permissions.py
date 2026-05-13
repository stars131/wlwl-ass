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
    # wechat_send drives the WeChat desktop client via wxauto: irreversible
    # outbound IM. Always ASK, never silently allow.
    "wechat_send": ToolMetadata("Send WeChat message via wxauto", "high", PermissionDecision.ASK),
    # gui_operator can move the mouse, type text, and click real UI controls.
    # Observation is harmless, but the tool is classified by its most powerful
    # action because permission prompts are per-tool today.
    "gui_operator": ToolMetadata("Control desktop GUI", "high", PermissionDecision.ASK),
    "browser_operator": ToolMetadata("Hybrid browser operator", "high", PermissionDecision.ASK),
    # Free-pool family. forum_harvest reads the user's logged-in linux.do
    # session; api_probe burns tokens on unknown endpoints; free_pool_ask
    # sends user prompts to untrusted relays. All three are reversible but
    # the user should see them firing.
    "forum_harvest": ToolMetadata("Harvest free APIs from linux.do", "medium", PermissionDecision.ASK),
    "api_probe": ToolMetadata("Probe a free-pool API endpoint", "medium", PermissionDecision.ASK),
    "free_pool_ask": ToolMetadata("Ask an LLM through the free pool", "medium", PermissionDecision.ASK),
    # pm_* tools sit on the self-evolution side. They only READ logs and
    # write proposal markdown; they never modify code or SOPs directly. Low
    # blast radius but still ASK so the user sees PM cycles run.
    "pm_friction_scan": ToolMetadata("Mine friction signals for PM Track A", "low", PermissionDecision.ASK),
    "pm_proposal_decide": ToolMetadata("Record a PM proposal decision", "low", PermissionDecision.ASK),
}


WRITE_TOOLS = {"file_patch", "file_write"}
EXEC_TOOLS = {"code_run", "web_execute_js"}
READ_ONLY_ALLOW = {"file_read", "ask_user", "update_working_checkpoint", "no_tool"}
# Best-effort UX nudge — NOT a security boundary. The real defense is that
# code_run is registered as `high` + ASK in TOOL_METADATA above; this regex
# just upgrades the permission prompt's wording when an *obviously* dangerous
# pattern slips through in non-interactive AUTO mode. It is trivially bypassed
# (split flags, env-var indirection, $(...) subshells, base64-decoded scripts);
# do NOT rely on it to block a determined adversarial LLM.
DANGEROUS_COMMAND_RE = re.compile(
    r"(?:"
    # POSIX destructive
    r"\brm\s+-[a-zA-Z]*[rRfF][a-zA-Z]*\b"           # rm -rf, rm -fr, rm -Rf, rm -r -f (loose)
    r"|\bdd\s+(?:if|of)="                            # dd if=/dev/... of=/dev/...
    r"|\bmkfs(?:\.[a-z0-9]+)?\b"                     # mkfs, mkfs.ext4
    r"|\bchmod\s+-R\s+0?00\b"                        # chmod -R 000
    r"|\bchown\s+-R\s+\S+\s+/\b"                     # chown -R user /
    # Windows destructive
    r"|\bdel\s+/[sqfSQF]"
    r"|\brmdir\s+/[sqSQ]"
    r"|\bformat\b|\bshutdown\b|\breboot\b"
    # PowerShell destructive
    r"|Remove-Item\b[^|]*-Recurse\b[^|]*-Force\b"
    r"|Remove-Item\b[^|]*-Force\b[^|]*-Recurse\b"
    # Git destructive
    r"|\bgit\s+reset\s+--hard\b"
    r"|\bgit\s+push\s+(?:[^|]*\s)?(?:-f\b|--force\b|--force-with-lease\b)"
    r"|\bgit\s+clean\s+-[a-z]*[fF][a-z]*\b"
    # Fork bomb (POSIX)
    r"|:\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"
    r")",
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
