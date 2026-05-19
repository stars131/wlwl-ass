"""Regenerate temp/autonomous_reports_index.md from the on-disk report files
and history.txt. Idempotent: re-runs are safe.

Run from project root:
    python scripts/regen_autonomous_reports_index.py            # write
    python scripts/regen_autonomous_reports_index.py --check    # dry-run

Inputs (read-only):
    temp/autonomous_reports/history.txt
    temp/autonomous_reports/R<n>_*.md

Outputs:
    temp/autonomous_reports_index.md                # overwritten if changed
    temp/autonomous_reports_index.md.bak            # previous version, only when content differs

The old hand-curated R3-style entries are not preserved verbatim — this file
is generated, not authored.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_ROOT = os.path.join(PROJECT_ROOT, "temp")
REPORTS_DIR = os.path.join(TEMP_ROOT, "autonomous_reports")
HISTORY_PATH = os.path.join(REPORTS_DIR, "history.txt")
INDEX_PATH = os.path.join(TEMP_ROOT, "autonomous_reports_index.md")
INDEX_BAK_PATH = INDEX_PATH + ".bak"

R_FILE_RE = re.compile(r"^R(\d+)_(.+)\.md$")
HIST_LINE_RE = re.compile(
    r"^R(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*)$"
)


def scan_reports():
    """Map R-number -> list of (filename, size). Multiple files per R = duplicates."""
    files = defaultdict(list)
    try:
        for name in os.listdir(REPORTS_DIR):
            m = R_FILE_RE.match(name)
            if not m:
                continue
            n = int(m.group(1))
            full = os.path.join(REPORTS_DIR, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            files[n].append((name, size))
    except OSError as e:
        print(f"[regen] cannot list reports dir: {e}", file=sys.stderr)
    return files


def scan_history():
    """List of (n, date, category, topic, summary), order preserved (top first)."""
    rows = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                m = HIST_LINE_RE.match(line)
                if not m:
                    continue
                rows.append(
                    (
                        int(m.group(1)),
                        m.group(2).strip(),
                        m.group(3).strip(),
                        m.group(4).strip(),
                        m.group(5).strip(),
                    )
                )
    except OSError as e:
        print(f"[regen] cannot read history.txt: {e}", file=sys.stderr)
    return rows


def build_index_text(files_by_n, history_rows):
    hist_by_n = {row[0]: row for row in history_rows}
    all_ns = sorted(set(files_by_n) | set(hist_by_n), reverse=True)

    duplicates = sorted(n for n, lst in files_by_n.items() if len(lst) > 1)
    file_only = sorted(n for n in files_by_n if n not in hist_by_n)
    history_only = sorted(n for n in hist_by_n if n not in files_by_n)

    by_category = defaultdict(list)
    for n in all_ns:
        cat = hist_by_n[n][2] if n in hist_by_n else "(no history)"
        by_category[cat].append(n)

    lines = []
    lines.append("# autonomous_reports 索引（generated）")
    lines.append("")
    lines.append(
        f"生成日期：{time.strftime('%Y-%m-%d')}  "
    )
    lines.append(
        "来源：只读 `temp/autonomous_reports/history.txt` 与 `temp/autonomous_reports/R*.md` 元数据。  "
    )
    lines.append(
        "生成脚本：`scripts/regen_autonomous_reports_index.py`（idempotent，可重复运行）"
    )
    lines.append("")
    lines.append("## 1. 物理计数")
    lines.append("")
    lines.append(f"- 报告文件数：{sum(len(lst) for lst in files_by_n.values())}")
    lines.append(f"- 不同 R 编号：{len(files_by_n)}")
    lines.append(f"- history 行数：{len(history_rows)}")
    top_n = max(files_by_n) if files_by_n else None
    lines.append(f"- 最新 R 编号（文件）：R{top_n}" if top_n is not None else "- 最新 R 编号：N/A")
    lines.append("")

    if duplicates or file_only or history_only:
        lines.append("## 2. 编号异常 / 差异")
        lines.append("")
        if duplicates:
            lines.append("### 重复（同一 R 号对应多文件）")
            for n in duplicates:
                items = ", ".join(f"`{name}` ({sz}B)" for name, sz in files_by_n[n])
                lines.append(f"- R{n}: {items}")
            lines.append("")
        if file_only:
            lines.append("### 仅文件（history 缺该行）")
            for n in file_only:
                names = ", ".join(name for name, _ in files_by_n[n])
                lines.append(f"- R{n}: {names}")
            lines.append("")
        if history_only:
            lines.append("### 仅 history（无对应报告文件）")
            for n in history_only:
                row = hist_by_n[n]
                lines.append(f"- R{n} | {row[1]} | {row[2]} | {row[3]}")
            lines.append("")

    lines.append("## 3. 全表（按 R 号倒序）")
    lines.append("")
    lines.append("| R | 日期 | 类型 | 主题 | 文件 | 大小 |")
    lines.append("|---|---|---|---|---|---:|")
    for n in all_ns:
        row = hist_by_n.get(n)
        date = row[1] if row else "—"
        cat = row[2] if row else "(no history)"
        topic = row[3] if row else "(no history)"
        if n in files_by_n:
            name, size = files_by_n[n][0]
            file_cell = f"`autonomous_reports/{name}`"
            size_cell = str(size)
        else:
            file_cell = "—"
            size_cell = "—"
        lines.append(f"| R{n} | {date} | {cat} | {topic} | {file_cell} | {size_cell} |")
    lines.append("")

    lines.append("## 4. 按类型分组")
    lines.append("")
    for cat in sorted(by_category):
        ns = by_category[cat]
        lines.append(f"### {cat}（{len(ns)} 条）")
        lines.append("")
        for n in ns:
            row = hist_by_n.get(n)
            topic = row[3] if row else "(no history)"
            if n in files_by_n:
                name = files_by_n[n][0][0]
                lines.append(f"- R{n} — {topic} — `autonomous_reports/{name}`")
            else:
                lines.append(f"- R{n} — {topic} — (no file)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="dry-run; print to stdout, do not write")
    args = ap.parse_args()

    files_by_n = scan_reports()
    history_rows = scan_history()
    new_text = build_index_text(files_by_n, history_rows)

    if args.check:
        sys.stdout.write(new_text)
        return 0

    old_text = ""
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8", errors="replace") as f:
            old_text = f.read()

    if old_text == new_text:
        print(f"[regen] no change: {INDEX_PATH}")
        return 0

    if old_text:
        with open(INDEX_BAK_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(old_text)
        print(f"[regen] backed up old index -> {INDEX_BAK_PATH}")

    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
    print(f"[regen] wrote {INDEX_PATH} ({len(new_text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
