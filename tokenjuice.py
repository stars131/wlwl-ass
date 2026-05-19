"""
TokenJuice — 终端输出压缩引擎
受 OpenHuman (tinyhumansai/openhuman) 启发的 Python 移植版。
在工具输出进入 LLM 上下文之前，用声明式规则做结构化压缩。

用法:
    from tokenjuice import compact_output
    result = compact_output("git status", raw_stdout)
    print(result.compressed)  # 压缩后文本
    print(f"节省 {result.saved_pct}% token")
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CompactResult:
    compressed: str
    original_len: int
    compressed_len: int
    rules_applied: list[str] = field(default_factory=list)

    @property
    def saved_pct(self) -> int:
        if self.original_len == 0:
            return 0
        return round((1 - self.compressed_len / self.original_len) * 100)


@dataclass
class Rule:
    """单条压缩规则"""
    name: str
    tool: str                   # 匹配的工具名 (git/pip/npm/cargo/...)
    patterns: list[str]         # 要删除/替换的正则列表
    replacements: list[str]     # 与 patterns 一一对应的替换文本
    max_lines: int = 0          # 超过此行数截断, 0=不限
    keep_first: int = 5         # 截断时保留首 N 行
    keep_last: int = 5          # 截断时保留尾 N 行

    def __post_init__(self) -> None:
        # Catches the exact bug class that bricked tokenjuice's import
        # for an unknown period: a Rule with mismatched patterns/replacements.
        # zip() silently drops extras, so the failure mode was "rule didn't
        # do what we thought" — make it loud at construction time instead.
        if len(self.patterns) != len(self.replacements):
            raise ValueError(
                f"Rule {self.name!r}: patterns ({len(self.patterns)}) and "
                f"replacements ({len(self.replacements)}) must be the same length"
            )
        # Sanity-check the regexes upfront so a typo doesn't crash deep
        # inside compact_output() weeks later.
        for i, p in enumerate(self.patterns):
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(
                    f"Rule {self.name!r} pattern[{i}] is not a valid regex: {exc}"
                ) from exc


# ── 内置规则 ──────────────────────────────────────────────

BUILTIN_RULES: list[Rule] = [
    # git status
    Rule(
        name="git_status_compact",
        tool="git",
        patterns=[r"^On branch .+\n?", r"^Changes not staged for commit:\n?\s+.*\n?",
                 r"^Untracked files:\n?\s+.*\n?", r'^\s+\(use "git .+?\n?',
                 r"^nothing added to commit .+\n?", r"^no changes added to commit .+\n?"],
        replacements=[""] * 6,
    ),
    # git log --oneline
    Rule(
        name="git_log_compact",
        tool="git",
        patterns=[],
        replacements=[],
        max_lines=10,
        keep_first=10,
        keep_last=0,
    ),
    # pip install
    Rule(
        name="pip_install_compact",
        tool="pip",
        patterns=[r"^\s*Downloading .+", r"^\s*Collecting .+",
                  r"^\s*Using cached .+", r"^\s*Installing collected packages:.*",
                  r"^\s*Successfully installed .+"],
        replacements=[""] * 4 + ["[pip] installed successfully"],
    ),
    # npm install
    Rule(
        name="npm_install_compact",
        tool="npm",
        patterns=[r"npm WARN .+", r"^\s*added \d+ packages.*"],
        replacements=["", ""],
    ),
    # cargo build
    Rule(
        name="cargo_build_compact",
        tool="cargo",
        patterns=[r"^\s*Compiling .+ v[\d.]+", r"^\s*Downloading .+",
                  r"^\s*Updating .+", r"^\s*Finished .+"],
        replacements=[""] * 3 + ["[cargo] build finished"],
    ),
    # docker pull
    Rule(
        name="docker_pull_compact",
        tool="docker",
        patterns=[r"^\s*[a-f0-9]{12}:\s+(Pull complete|Already exists|Waiting|Downloading)"],
        replacements=[""],
    ),
    # pytest
    Rule(
        name="pytest_compact",
        tool="pytest",
        patterns=[r"^=+$", r"^PASSED\s+", r"^\s*\.{3,}$"],
        replacements=[""] * 3,
        max_lines=30,
        keep_first=10,
        keep_last=15,
    ),
]


def _match_tool(tool_name: str, rule_tool: str) -> bool:
    """判断工具名是否匹配规则 (前缀匹配)"""
    return tool_name.lower().startswith(rule_tool.lower())


def compact_output(tool_name: str, stdout: str, stderr: str = "",
                   max_output_lines: int = 200,
                   extra_rules: Optional[list[Rule]] = None) -> CompactResult:
    """
    压缩工具输出。

    Args:
        tool_name: 工具名 (git/pip/npm/...)
        stdout: 标准输出
        stderr: 标准错误
        max_output_lines: 全局最大行数
        extra_rules: 用户自定义规则 (优先于内置规则)

    Returns:
        CompactResult
    """
    text = stdout
    if stderr.strip():
        text += f"\n[STDERR]\n{stderr}"

    original_len = len(text)
    applied = []

    # 合并规则: extra > builtin
    all_rules = (extra_rules or []) + BUILTIN_RULES

    for rule in all_rules:
        if not _match_tool(tool_name, rule.tool):
            continue
        # 应用正则替换
        for pat, repl in zip(rule.patterns, rule.replacements):
            text = re.sub(pat, repl, text, flags=re.MULTILINE)
        # 截断
        if rule.max_lines > 0:
            lines = text.split("\n")
            if len(lines) > rule.max_lines:
                head = lines[:rule.keep_first]
                tail = lines[-rule.keep_last:] if rule.keep_last > 0 else []
                omitted = len(lines) - rule.keep_first - rule.keep_last
                text = "\n".join(head) + f"\n... [{omitted} lines omitted] ...\n" + "\n".join(tail)
        applied.append(rule.name)

    # 全局截断兜底
    lines = text.split("\n")
    if len(lines) > max_output_lines:
        head = lines[:max_output_lines // 2]
        tail = lines[-(max_output_lines // 2):]
        omitted = len(lines) - len(head) - len(tail)
        text = "\n".join(head) + f"\n... [{omitted} lines omitted] ...\n" + "\n".join(tail)
        applied.append("global_truncation")

    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return CompactResult(
        compressed=text,
        original_len=original_len,
        compressed_len=len(text),
        rules_applied=applied,
    )


# ── 便捷函数 ──

def compact(text: str, tool_name: str = "", max_lines: int = 200) -> str:
    """最简单的接口：给文本和工具名，返回压缩后文本"""
    return compact_output(tool_name, text, max_output_lines=max_lines).compressed