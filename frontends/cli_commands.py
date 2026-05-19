import json
import os
import re
from dataclasses import dataclass

from wlwl_ass import smart_format
try:
    from continue_cmd import handle_frontend_command, reset_conversation
except ImportError:
    from .continue_cmd import handle_frontend_command, reset_conversation


HELP_COMMANDS = (
    ("/help", "显示帮助"),
    ("/status", "查看状态"),
    ("/stop", "停止当前任务"),
    ("/new", "开启新对话并清空当前上下文"),
    ("/restore", "恢复上次对话历史"),
    ("/continue", "列出可恢复会话"),
    ("/continue [n]", "恢复第 n 个会话"),
    ("/llm", "查看当前模型列表"),
    ("/llm [n]", "切换到第 n 个模型"),
    ("/session.<key>=<value>", "设置当前 LLM session 参数"),
    ("/usage", "Token 用量(总计 + by-source)"),
    ("/doctor", "运行健康检查并给出修复命令"),
    ("/skills", "列出已沉淀的 SOP / Skill"),
    ("/sessions <q>", "在 L4 会话归档中检索"),
    ("/checkpoints", "列出长任务 checkpoint"),
    ("/processes", "列出 agent 注册的后台进程"),
    ("/approval", "查看命令白名单(approval allowlist)"),
    ("/trajectory", "列出已完成的 agent runs"),
    ("/curator", "查看待审 curator proposals"),
    ("/mcp", "列出已配置 MCP servers"),
    ("/config", "API 配置管理(list/presets/use/preset/import/backups)"),
)


TELEGRAM_MENU_COMMANDS = (
    ("help", "显示帮助"),
    ("status", "查看状态"),
    ("stop", "停止当前任务"),
    ("new", "开启新对话并清空当前上下文"),
    ("restore", "恢复上次对话历史"),
    ("continue", "列出可恢复会话；/continue n 恢复第 n 个"),
    ("llm", "查看模型列表；/llm n 切换到指定模型"),
    ("usage", "Token 用量(by-source)"),
    ("doctor", "健康检查"),
    ("skills", "已沉淀的 Skills"),
    ("sessions", "L4 会话归档检索;/sessions <query>"),
    ("checkpoints", "长任务 checkpoint 列表"),
    ("processes", "后台进程列表"),
    ("approval", "命令白名单"),
    ("trajectory", "已完成 runs"),
    ("curator", "待审 proposals"),
    ("mcp", "MCP servers"),
)


def build_help_text(commands=HELP_COMMANDS):
    return "📖 命令列表:\n" + "\n".join(f"{cmd} - {desc}" for cmd, desc in commands)


HELP_TEXT = build_help_text()


@dataclass
class CommandResult:
    handled: bool
    message: str | None = None
    query: str | None = None
    should_exit: bool = False


class SharedCommandHandler:
    def __init__(self, agent):
        self.agent = agent

    def handle(self, raw_query):
        text = (raw_query or "").strip()
        if not text.startswith("/"):
            return CommandResult(False, query=raw_query)
        parts = text.split()
        op = (parts[0] if parts else "").lower()
        if op == "/help":
            return CommandResult(True, HELP_TEXT)
        if op == "/stop":
            self.agent.abort()
            return CommandResult(True, "⏹️ 正在停止...")
        if op == "/status":
            return CommandResult(True, self._status())
        if op == "/llm":
            return CommandResult(True, self._llm(parts))
        if op == "/usage":
            return CommandResult(True, self._usage())
        if op == "/doctor":
            return CommandResult(True, self._doctor())
        if op == "/skills":
            return CommandResult(True, self._skills())
        if op == "/sessions":
            return CommandResult(True, self._sessions(parts[1:]))
        if op == "/checkpoints":
            return CommandResult(True, self._checkpoints())
        if op == "/processes":
            return CommandResult(True, self._processes())
        if op == "/approval":
            return CommandResult(True, self._approval())
        if op == "/trajectory":
            return CommandResult(True, self._trajectory())
        if op == "/curator":
            return CommandResult(True, self._curator())
        if op == "/mcp":
            return CommandResult(True, self._mcp(parts[1:]))
        if op == "/config":
            return CommandResult(True, self._config(parts[1:]))
        if op == "/new":
            return CommandResult(True, reset_conversation(self.agent))
        if op == "/restore":
            return CommandResult(True, self._restore_latest())
        if op == "/continue":
            return CommandResult(True, handle_frontend_command(self.agent, text))
        if op == "/resume":
            return CommandResult(False, query=r'用re.findall(r"<history>\\n\[(?:USER\|Agent)\].*?</history>", content, re.DOTALL) 扫temp/model_responses/下时间最近的10个文件(除本PID)，取每文件最后一个匹配(注意JSON里换行是字面\\n)作为该会话内容，按mtime倒序，每个用一句话总结聊了什么让我选择；选定后再简单读该文件末尾作为聊天基础')
        if m := re.match(r"/session\.(\w+)=(.*)", text):
            return CommandResult(True, self._set_session(m.group(1), m.group(2)))
        return CommandResult(True, HELP_TEXT)

    def _status(self):
        llm = self.agent.get_llm_name() if getattr(self.agent, "llmclient", None) else "未配置"
        running = "🔴 运行中" if getattr(self.agent, "is_running", False) else "🟢 空闲"
        lines = [f"状态: {running}", f"LLM: [{self.agent.llm_no}] {llm}"]
        if root := getattr(self.agent, "project_root", ""):
            lines.append(f"Project: {root}")
        if mode := getattr(self.agent, "permission_mode", ""):
            lines.append(f"Permission: {mode}")
        if ctx := getattr(self.agent, "project_context", None):
            files = getattr(ctx, "files", []) or []
            lines.append(f"Context files: {len(files)}")
            lines.extend(f"  - {os.path.basename(str(p))}" for p in files[:5])
        lines.append(f"History: {len(getattr(self.agent, 'history', []))}")
        return "\n".join(lines)

    def _llm(self, parts):
        if not getattr(self.agent, "llmclient", None):
            return "❌ 当前没有可用的 LLM 配置"
        if len(parts) > 1:
            try:
                self.agent.next_llm(int(parts[1]))
                return f"✅ 已切换到 [{self.agent.llm_no}] {self.agent.get_llm_name()}"
            except Exception:
                return f"用法: /llm <0-{len(self.agent.list_llms()) - 1}>"
        lines = [f"{'→' if cur else ' '} [{i}] {name}" for i, name, cur in self.agent.list_llms()]
        return "LLMs:\n" + "\n".join(lines)

    def _set_session(self, key, value):
        vfile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp", value)
        if os.path.isfile(vfile):
            value = open(vfile, encoding="utf-8").read().strip()
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        setattr(self.agent.llmclient.backend, key, value)
        return smart_format(f"✅ session.{key} = {repr(value)}", max_str_len=500)

    def _restore_latest(self):
        try:
            from chatapp_common import format_restore
            restored_info, err = format_restore()
            if err:
                return err
            restored, fname, count = restored_info
            self.agent.abort()
            self.agent.history.extend(restored)
            return f"✅ 已恢复 {count} 轮对话\n来源: {fname}\n(仅恢复上下文，请输入新问题继续)"
        except Exception as e:
            return f"❌ 恢复失败: {e}"

    # ── new tool commands (Q: CLI 新工具命令) ──────────────────────────────
    # Each surface keeps the failure mode "return ❌-prefixed string", so a
    # missing optional dep (or unconfigured feature) is visible to the chat
    # user but never crashes the agent loop.

    @staticmethod
    def _project_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _usage(self) -> str:
        try:
            import llmcore
            snap = llmcore.get_token_usage()
        except Exception as e:
            return f"❌ /usage 失败: {e}"
        t = snap.get("totals") or {}
        lines = [
            f"📊 Token 用量 (since {snap.get('since', '?')})",
            f"  in={t.get('input', 0)} out={t.get('output', 0)} "
            f"cache_creation={t.get('cache_creation', 0)} "
            f"cache_read={t.get('cache_read', 0)} calls={t.get('calls', 0)}",
        ]
        rate = snap.get("cache_hit_rate")
        lines.append(f"  cache hit rate: {rate * 100:.1f}%" if rate is not None
                     else "  cache hit rate: —")
        by_src = snap.get("by_source") or {}
        if by_src:
            lines.append("  by source:")
            for src, v in sorted(by_src.items(), key=lambda kv: -kv[1].get("calls", 0)):
                lines.append(
                    f"    - {src}: in={v.get('input', 0)} out={v.get('output', 0)} "
                    f"calls={v.get('calls', 0)}"
                )
        return "\n".join(lines)

    def _doctor(self) -> str:
        try:
            from launcher import doctor
            result = doctor.run_diagnostics(include_network=False)
        except Exception as e:
            return f"❌ /doctor 失败: {e}"
        s = result.get("summary") or {}
        lines = [
            f"🩺 doctor — {s.get('ok', 0)} ok · {s.get('warn', 0)} warn · "
            f"{s.get('fail', 0)} fail · {s.get('info', 0)} info",
        ]
        # Surface the actionable rows: failures first, then warnings.
        actionable = [c for c in (result.get("checks") or [])
                      if c.get("severity") in ("fail", "warn")]
        for c in actionable[:10]:
            mark = "✗" if c.get("severity") == "fail" else "●"
            lines.append(f"  {mark} {c.get('title', '')}")
            if c.get("fix"):
                lines.append(f"      fix: {c['fix']}")
        if len(actionable) > 10:
            lines.append(f"  …and {len(actionable) - 10} more — run "
                         "`python -m launcher.doctor` for the full report.")
        if not actionable:
            lines.append("  (everything green — no fix needed)")
        return "\n".join(lines)

    def _skills(self) -> str:
        try:
            from launcher import skills
            items = skills.list_skills()
        except Exception as e:
            return f"❌ /skills 失败: {e}"
        if not items:
            return "(no skills found in memory/)"
        lines = [f"🧠 Skills — {len(items)} total (top 15 by usage):"]
        for it in items[:15]:
            outcomes = it.get("outcomes") or {}
            total = outcomes.get("total", 0) if outcomes else 0
            stem = it.get("name", "?")
            title = (it.get("title") or "")[:60]
            lines.append(f"  - {stem}  ({total} uses) {title}")
        return "\n".join(lines)

    def _sessions(self, args) -> str:
        if not args:
            return "用法: /sessions <query>  (FTS5 搜索 L4 会话归档)"
        query = " ".join(args)
        try:
            from tools import session_search
            return session_search.session_search(query, top_k=5)
        except Exception as e:
            return f"❌ /sessions 失败: {e}"

    def _checkpoints(self) -> str:
        try:
            from launcher.checkpoint_manager import CheckpointManager
            cm = CheckpointManager(self._project_root())
            tasks = cm.list_tasks()
        except Exception as e:
            return f"❌ /checkpoints 失败: {e}"
        if not tasks:
            return "(no checkpoints — agent hasn't saved any long-task state yet)"
        lines = [f"💾 Checkpoints — {len(tasks)} task(s):"]
        for t in tasks[:20]:
            note = (t.get("latest_note") or "").strip()
            note_part = f" — {note}" if note else ""
            lines.append(
                f"  - {t.get('task_id')}: {t.get('count')} cps, "
                f"latest {t.get('latest_saved_at')}{note_part}"
            )
        return "\n".join(lines)

    def _processes(self) -> str:
        try:
            from launcher.process_registry import get_registry
            entries = get_registry(self._project_root()).list()
        except Exception as e:
            return f"❌ /processes 失败: {e}"
        if not entries:
            return "(no agent-registered background processes)"
        lines = [f"⚙️ Processes — {len(entries)} entry(s):"]
        for e in entries[:20]:
            alive = "✓" if e.get("alive") else "✗"
            cmd = (e.get("cmd") or "")[:50]
            lines.append(
                f"  {alive} [{e.get('pid')}] {e.get('label', '?')} "
                f"({e.get('kind', 'proc')}) {cmd}"
            )
        return "\n".join(lines)

    def _config(self, args) -> str:
        """In-REPL `/config <sub> [args...]` — delegates to
        :mod:`launcher.cli_config`, capturing its stdout/stderr so the
        chat surface gets one string back.

        Why capture instead of letting it print directly? The REPL hands
        the agent a "done" payload, then redraws the prompt; if subcommand
        output went to raw stdout, it would race with the prompt redraw
        and look interleaved. Capturing keeps the slash command tidy."""
        import io
        import contextlib
        try:
            from launcher import cli_config
        except Exception as exc:
            return f"❌ /config 加载失败: {exc}"

        # Sub-action defaults to `list` so plain `/config` is useful.
        if not args:
            args = ["list"]
        sub_cmd = (args[0] or "").lower()
        VALID = {"list", "presets", "add", "use", "remove", "import",
                 "export", "probe", "backups"}
        if sub_cmd not in VALID:
            return ("用法: /config <list|presets|use NAME|preset>\n"
                    "  /config list                  installed configs\n"
                    "  /config presets [QUERY]       browse preset library\n"
                    "  /config use NAME              make NAME default\n"
                    "  /config remove NAME           delete config\n"
                    "  /config import URL            wlwl-config:// import\n"
                    "  /config backups               list rotated snapshots\n"
                    "  /config probe NAME            measure apibase latency\n"
                    "(`add` / `export` 需要交互式输入 apikey，请用终端 `wlwl config ...` 而不是 /config。)")

        parser = __import__("argparse").ArgumentParser(prog="/config")
        sub = parser.add_subparsers(dest="root")
        cli_config.build_subparser(sub)

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                # argparse needs the leading "config" word to enter the
                # config subparser tree.
                ns = parser.parse_args(["config"] + list(args))
                rc = ns.func(ns)
        except SystemExit as exc:
            return f"❌ /config 参数错误: rc={exc.code}\n{buf.getvalue()}"
        except Exception as exc:
            return f"❌ /config 执行失败: {exc}\n{buf.getvalue()}"
        out = buf.getvalue().strip()
        if rc and rc != 0:
            return f"❌ /config 返回 rc={rc}\n{out}"
        return out or "(no output)"

    def _approval(self) -> str:
        try:
            from launcher.approval import ApprovalAllowlist
            rules = ApprovalAllowlist.from_project_root(self._project_root()).list_rules()
        except Exception as e:
            return f"❌ /approval 失败: {e}"
        if not rules:
            return "(approval allowlist empty — every prompt still asks)"
        lines = [f"🛡️ Approval allowlist — {len(rules)} rule(s):"]
        for r in rules[:20]:
            pat = r.pattern or "*"
            note = f" — {r.note}" if r.note else ""
            lines.append(f"  - {r.tool}: {pat}  ({r.use_count} uses){note}")
        return "\n".join(lines)

    def _trajectory(self) -> str:
        try:
            from launcher import trajectory
            runs = trajectory.iter_runs()
        except Exception as e:
            return f"❌ /trajectory 失败: {e}"
        if not runs:
            return "(no completed runs in activity log yet)"
        lines = [f"📈 Trajectory — {len(runs)} run(s) (most recent 10):"]
        for r in runs[-10:][::-1]:
            outcomes = r.outcomes or {}
            ok = outcomes.get("ok", 0)
            other = sum(v for k, v in outcomes.items() if k != "ok")
            lines.append(
                f"  - {r.run_id}: {r.n_turns}t  ok={ok} other={other}  "
                f"skills={','.join(r.skills_used[:3]) or '—'}"
            )
        return "\n".join(lines)

    def _curator(self) -> str:
        path = os.path.join(self._project_root(), "memory", "curator_proposals.jsonl")
        if not os.path.isfile(path):
            return "(no curator proposals — agent hasn't surfaced any reflections yet)"
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except Exception as e:
            return f"❌ /curator 失败: {e}"
        if not rows:
            return "(curator proposals file is empty)"
        lines = [f"🌱 Curator proposals — {len(rows)} pending review (most recent 5):"]
        for r in rows[-5:][::-1]:
            insight = (r.get("insight") or "")[:120]
            target = r.get("target", "?")
            lines.append(f"  - [{target}] {insight}")
        return "\n".join(lines)

    def _mcp(self, args) -> str:
        try:
            from tools import mcp_client
            # No-arg form (or "list"): mcp_call() lists configured servers.
            # `/mcp <server>` lists tools on that server.
            if not args or args[0] == "list":
                return mcp_client.mcp_call()
            return mcp_client.mcp_call(server=args[0])
        except Exception as e:
            return f"❌ /mcp 失败: {e}"
