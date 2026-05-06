"""Slash-command registry for the Feishu frontend.

Lark-free by design — fsapp.py constructs a CommandContext with send/upload
callbacks before invoking dispatch(). Tests can substitute fakes for ctx and
exercise every command without lark_oapi installed.
"""
from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.sop_tools import sop_read, sop_search, sop_stats  # noqa: E402

SCREENSHOT_DIR = os.path.join(PROJECT_ROOT, "temp", "feishu_screenshots")
SOP_OUT_DIR = os.path.join(PROJECT_ROOT, "temp", "feishu_sops")


@dataclass
class CommandContext:
    """Callbacks the dispatcher hands to each command handler."""

    send_text: Callable[[str], None]
    send_file: Callable[[str], bool]
    base_dir: str = PROJECT_ROOT
    mutating_allowed: bool = True
    extras: dict = field(default_factory=dict)


COMMANDS: dict = {}
HELP_LINES: list[tuple[str, str]] = []


def cmd(name: str, help_text: str):
    def deco(fn):
        COMMANDS[name] = fn
        HELP_LINES.append((name, help_text))
        return fn

    return deco


def parse(cmd_line: str) -> tuple[str, list[str]]:
    line = (cmd_line or "").strip()
    if not line.startswith("/"):
        return "", []
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        parts = line.split()
    op = parts[0].lower() if parts else ""
    return op, parts[1:]


def dispatch(cmd_line: str, ctx: CommandContext) -> bool:
    """Try to handle a slash command. Returns True if it was a registered cmd."""
    op, args = parse(cmd_line)
    handler = COMMANDS.get(op)
    if handler is None:
        return False
    try:
        handler(args, ctx)
    except Exception as exc:
        ctx.send_text(f"[{op}] 内部错误: {type(exc).__name__}: {exc}")
    return True


def _require_mutating(ctx: CommandContext, op: str) -> bool:
    if ctx.mutating_allowed:
        return True
    ctx.send_text(
        f"❌ {op} 在公开访问 (fs_allowed_users=['*']) 下被禁用。\n"
        "请通过 GUI 的 Bots tab 或 `python -m launcher.config set bots.feishu.allowed_users '[\"ou_…\"]'` "
        "把 allowed_users 改为具体的 open_id 列表后重启。"
    )
    return False


# ─────────────── /sop ───────────────


@cmd("/sop", "/sop <query> 搜索 Sophub | /sop read <id> 拉全文 | /sop stats")
def _cmd_sop(args, ctx: CommandContext):
    if not args:
        ctx.send_text("用法: /sop <query>  或  /sop read <id>  或  /sop stats")
        return
    sub = args[0].lower()
    if sub == "read":
        if len(args) < 2:
            ctx.send_text("用法: /sop read <sop_id>")
            return
        sop_id = args[1].strip()
        text = sop_read(sop_id)
        if len(text) <= 3000:
            ctx.send_text(text)
            return
        os.makedirs(SOP_OUT_DIR, exist_ok=True)
        path = os.path.join(SOP_OUT_DIR, f"sop_{sop_id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        ctx.send_text(f"📄 SOP 较长（{len(text)} 字符），已落盘并发送：")
        ctx.send_file(path)
        return
    if sub == "stats":
        ctx.send_text(sop_stats())
        return
    query = " ".join(args).strip()
    ctx.send_text(sop_search(query, top_k=5))


# ─────────────── /screenshot ───────────────


def _take_screenshot(out_path: str, monitor: int = 0) -> tuple[str | None, str | None]:
    """Try mss → PIL.ImageGrab. Returns (path, error)."""
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            mons = sct.monitors  # [0]=virtual全屏, [1..]=单显示器
            if monitor < 0 or monitor >= len(mons):
                return None, f"显示器序号越界: {monitor}（共 {len(mons) - 1} 个；0=全屏）"
            target = mons[monitor]
            shot = sct.grab(target)
            mss.tools.to_png(shot.rgb, shot.size, output=out_path)
            return out_path, None
    except ImportError:
        pass
    except Exception as exc:
        if monitor == 0:
            pass  # fall through to PIL
        else:
            return None, f"mss 截图失败: {exc}"
    try:
        from PIL import ImageGrab

        if monitor > 0:
            return None, "PIL.ImageGrab 不支持指定显示器；请 `pip install mss`"
        img = ImageGrab.grab(all_screens=True)
        img.save(out_path, "PNG")
        return out_path, None
    except ImportError:
        pass
    except Exception as exc:
        return None, f"PIL 截图失败: {exc}"
    return None, "缺少截图依赖；请 `pip install mss`（推荐）或 `pip install pillow`"


@cmd("/screenshot", "/screenshot [N] 截屏并发回（N 默认 0=全屏）")
def _cmd_screenshot(args, ctx: CommandContext):
    monitor = 0
    if args:
        try:
            monitor = int(args[0])
        except ValueError:
            ctx.send_text("用法: /screenshot [显示器序号, 0=全屏]")
            return
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(SCREENSHOT_DIR, f"screen_{stamp}_m{monitor}.png")
    path, err = _take_screenshot(out_path, monitor=monitor)
    if err:
        ctx.send_text(f"❌ 截图失败: {err}")
        return
    if not ctx.send_file(path):
        ctx.send_text(f"⚠️ 截图已保存但发送失败: {path}")


# ─────────────── /run ───────────────


@cmd("/run", "/run <cmd> 执行 shell 命令并返回 stdout/stderr（30s 超时, 4000 字符截断）")
def _cmd_run(args, ctx: CommandContext):
    if not args:
        ctx.send_text("用法: /run <shell command>")
        return
    if not _require_mutating(ctx, "/run"):
        return
    command = " ".join(args)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        ctx.send_text(f"⏱ /run 超时 (>30s): {command}")
        return
    except Exception as exc:
        ctx.send_text(f"❌ /run 失败: {exc}")
        return
    text = proc.stdout or ""
    if proc.stderr:
        text += ("\n" if text else "") + f"[stderr]\n{proc.stderr}"
    if len(text) > 4000:
        text = text[:4000] + f"\n... (truncated, total {len(text)})"
    ctx.send_text(f"[exit={proc.returncode}]\n{text or '(no output)'}")


# ─────────────── /clip ───────────────


def _clip_get() -> tuple[str | None, str | None]:
    try:
        import tkinter

        r = tkinter.Tk()
        r.withdraw()
        try:
            data = r.clipboard_get()
        finally:
            r.destroy()
        return data, None
    except Exception as exc:
        try:
            import pyperclip

            return pyperclip.paste(), None
        except Exception as exc2:
            return None, f"tkinter:{exc} | pyperclip:{exc2}"


def _clip_set(text: str) -> tuple[bool, str | None]:
    try:
        import tkinter

        r = tkinter.Tk()
        r.withdraw()
        try:
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
        finally:
            r.destroy()
        return True, None
    except Exception as exc:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True, None
        except Exception as exc2:
            return False, f"tkinter:{exc} | pyperclip:{exc2}"


@cmd("/clip", "/clip 读剪贴板 | /clip <text> 写剪贴板")
def _cmd_clip(args, ctx: CommandContext):
    if not args:
        data, err = _clip_get()
        if err:
            ctx.send_text(f"❌ 读剪贴板失败: {err}")
            return
        ctx.send_text(f"📋 剪贴板内容 ({len(data or '')} 字符):\n{data or '(empty)'}")
        return
    if not _require_mutating(ctx, "/clip <text>"):
        return
    text = " ".join(args)
    ok, err = _clip_set(text)
    if ok:
        ctx.send_text(f"✅ 已写入剪贴板 ({len(text)} 字符)")
    else:
        ctx.send_text(f"❌ 写剪贴板失败: {err}")


# ─────────────── /open ───────────────


def _open_target(target: str) -> tuple[bool, str | None]:
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
            return True, None
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
            return True, None
        subprocess.Popen(["xdg-open", target])
        return True, None
    except Exception as exc:
        return False, str(exc)


@cmd("/open", "/open <path|url> 用默认程序打开")
def _cmd_open(args, ctx: CommandContext):
    if not args:
        ctx.send_text("用法: /open <path-or-url>")
        return
    if not _require_mutating(ctx, "/open"):
        return
    target = " ".join(args)
    ok, err = _open_target(target)
    if ok:
        ctx.send_text(f"✅ 已请求打开: {target}")
    else:
        ctx.send_text(f"❌ 打开失败: {err}")


def help_text() -> str:
    return "\n".join(f"{name:<14} {desc}" for name, desc in HELP_LINES)


def dispatch_with_shared(
    cmd_line: str,
    ctx: CommandContext,
    agent,
    *,
    on_stop=None,
    public_note: str = "",
) -> bool:
    """Unified slash-command dispatcher for the Feishu frontend.

    Tries (in order):
      1. Feishu-only commands registered in this module (``/sop``,
         ``/screenshot``, ``/run``, ``/clip``, ``/open``).
      2. ``SharedCommandHandler`` — every command in
         ``frontends.cli_commands.HELP_COMMANDS`` (``/usage`` ``/doctor``
         ``/skills`` ``/sessions`` ``/checkpoints`` ``/processes``
         ``/approval`` ``/trajectory`` ``/curator`` ``/mcp`` ``/help``
         ``/status`` ``/llm`` ``/new`` ``/restore`` ``/continue`` ``/stop``
         ``/session.X=Y``).
      3. ``/help`` is special-cased so the merged surface is shown — the
         shared HELP_TEXT plus a "飞书专属" footer with HELP_LINES.

    ``on_stop`` is called BEFORE delegating ``/stop`` to SharedCommandHandler
    so the Feishu front-end can flip its own ``user_tasks[open_id]['running']``
    flag (the agent's abort handles the rest).

    Returns ``True`` if the command was consumed (sent a reply), ``False``
    otherwise so the caller can fall through to "未知命令".
    """
    op, _ = parse(cmd_line)

    # Merge /help across both surfaces — neither side has the full picture
    # alone, so we render it here instead of letting either dispatcher win.
    if op == "/help":
        try:
            from frontends.cli_commands import HELP_TEXT as SHARED_HELP
        except ImportError:
            from cli_commands import HELP_TEXT as SHARED_HELP
        extra = "\n\n飞书专属:\n" + "\n".join(
            f"  {n:<14} {h}" for n, h in HELP_LINES
        )
        note = f"\n\n{public_note}" if public_note else ""
        ctx.send_text(SHARED_HELP + extra + note)
        return True

    # Feishu-only first (so `/run` / `/sop` / etc. take priority).
    if dispatch(cmd_line, ctx):
        return True

    # /stop has a Feishu-side side effect — flip the running flag in the
    # caller's user_tasks dict so the long-poll loop in fsapp can exit.
    if op == "/stop" and on_stop is not None:
        try:
            on_stop()
        except Exception:
            pass

    # Delegate everything else to SharedCommandHandler.
    try:
        from frontends.cli_commands import SharedCommandHandler
    except ImportError:
        from cli_commands import SharedCommandHandler
    result = SharedCommandHandler(agent).handle(cmd_line)
    if not result.handled:
        return False
    ctx.send_text(result.message or "")
    return True
