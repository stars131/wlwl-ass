"""``wlwl readme`` — Readme.skill-style self-Readme generator (local-data version).

Mirrors the philosophy of https://github.com/study8677/Readme.skill: aggregate
the developer's signal into a shareable, anonymized markdown profile. But
instead of reading ~/.claude/ + GitHub API + system git log (which the upstream
skill does), this command works strictly off the project's own data:

    temp/cost_ledger.jsonl              tokens spent + per-model
    temp/activity/YYYY-MM-DD.jsonl      agent action volume / kinds
    temp/autonomous_reports/history.txt autonomous run topics
    git log -n 200 (the project repo)   commit cadence + top files
    MEMORY.md (~/.claude/projects/...)  long-term user profile (optional)

Output:
    temp/self_readme.md         (default; --out to override)
    or stdout                   (--stdout)

By default the generator anonymizes project names and external repo URLs,
matching the upstream skill's "shareable" mode. Pass --no-anonymize to
keep concrete names (for personal use).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

# Force stdout/stderr into errors='replace' on Windows GBK consoles —
# emoji headers in the generated markdown would otherwise crash --stdout.
try:
    from agent_loop import ensure_safe_std_streams
    ensure_safe_std_streams()
except Exception:
    pass


def _project_root() -> str:
    return os.environ.get("WLWL_PROJECT_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


def _read_ledger():
    path = os.path.join(_project_root(), "temp", "cost_ledger.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _scan_activity():
    """Aggregate temp/activity/*.jsonl into counts by action_kind and day."""
    root = os.path.join(_project_root(), "temp", "activity")
    by_kind: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    if not os.path.isdir(root):
        return {"by_kind": [], "by_day": [], "rows": 0, "days_active": 0}
    rows = 0
    for name in sorted(os.listdir(root)):
        if not name.endswith(".jsonl"):
            continue
        day = name[:10]
        full = os.path.join(root, name)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rows += 1
                    by_day[day] += 1
                    kind = str(rec.get("kind") or rec.get("type") or rec.get("event") or "")
                    if kind:
                        by_kind[kind] += 1
        except OSError:
            continue
    return {
        "by_kind": by_kind.most_common(15),
        "by_day": sorted(by_day.items()),
        "rows": rows,
        "days_active": len(by_day),
    }


def _git_log_summary(limit: int = 500) -> dict:
    """Run git log in PROJECT_ROOT. Returns commits + top files. Best-effort:
    swallow errors when not in a git repo or git is unavailable."""
    root = _project_root()
    out = {"commits": 0, "first": None, "last": None, "top_files": [], "by_author": []}
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        if rev.returncode != 0 or rev.stdout.strip() != "true":
            return out
        log = subprocess.run(
            ["git", "log", f"-n{limit}", "--pretty=%H|%an|%ad", "--date=iso-strict"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        commits = []
        for line in log.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append((parts[0], parts[1], parts[2]))
        out["commits"] = len(commits)
        if commits:
            out["last"] = commits[0][2][:10]
            out["first"] = commits[-1][2][:10]
        by_author = Counter(c[1] for c in commits)
        out["by_author"] = by_author.most_common(5)
        names = subprocess.run(
            ["git", "log", f"-n{limit}", "--name-only", "--pretty=format:"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        files = Counter()
        for line in names.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            files[line] += 1
        out["top_files"] = files.most_common(15)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _autonomous_topics():
    """Read temp/autonomous_reports/history.txt: count categories + recent topics."""
    path = os.path.join(_project_root(), "temp", "autonomous_reports", "history.txt")
    by_cat: Counter[str] = Counter()
    recent: list[tuple[str, str]] = []
    if not os.path.exists(path):
        return {"by_category": [], "recent": [], "total": 0}
    rgx = re.compile(r"^R(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = rgx.match(line)
                if not m:
                    continue
                cat = m.group(3).strip()
                topic = m.group(4).strip()
                by_cat[cat] += 1
                if len(recent) < 12:
                    recent.append((m.group(1), topic))
    except OSError:
        pass
    return {
        "by_category": by_cat.most_common(),
        "recent": recent,
        "total": sum(by_cat.values()),
    }


def _aggregate_ledger(rows: list) -> dict:
    if not rows:
        return {"calls": 0, "models": [], "first": None, "last": None,
                "input": 0, "output": 0, "cache_read": 0, "cost_usd": 0.0,
                "cache_leverage": None}
    tok = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    cost = 0.0
    by_model: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_read": 0, "calls": 0, "cost_usd": 0.0}
    )
    timestamps: list[str] = []
    for row in rows:
        for k in tok:
            tok[k] += int(row.get(k) or 0)
        cost += float(row.get("cost_usd") or 0.0)
        m = str(row.get("model") or "(unknown)")
        bm = by_model[m]
        bm["input"] += int(row.get("input") or 0)
        bm["output"] += int(row.get("output") or 0)
        bm["cache_read"] += int(row.get("cache_read") or 0)
        bm["calls"] += 1
        bm["cost_usd"] += float(row.get("cost_usd") or 0.0)
        ts = str(row.get("ts") or "")
        if ts:
            timestamps.append(ts)
    timestamps.sort()
    cache_leverage = None
    if tok["input"] > 0:
        cache_leverage = round(tok["cache_read"] / max(1, tok["input"] - tok["cache_read"]), 1) \
            if (tok["input"] - tok["cache_read"]) > 0 else None
    models = sorted(
        by_model.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
    )
    return {
        "calls": len(rows),
        "first": timestamps[0] if timestamps else None,
        "last": timestamps[-1] if timestamps else None,
        "input": tok["input"],
        "output": tok["output"],
        "cache_read": tok["cache_read"],
        "cost_usd": cost,
        "cache_leverage": cache_leverage,
        "models": models[:8],
    }


def _fmt_tok(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _anonymize(s: str, alias_map: dict[str, str], prefix: str = "Project") -> str:
    if s in alias_map:
        return alias_map[s]
    alias = f"{prefix} {chr(ord('A') + len(alias_map) % 26)}{len(alias_map) // 26 if len(alias_map) >= 26 else ''}".strip()
    alias_map[s] = alias
    return alias


def build_readme(*, anonymize: bool = True) -> str:
    rows = _read_ledger()
    led = _aggregate_ledger(rows)
    act = _scan_activity()
    git = _git_log_summary()
    auto = _autonomous_topics()
    today = _dt.datetime.now().strftime("%Y-%m-%d")

    days_active = 0
    if led["first"] and led["last"]:
        try:
            first = _dt.datetime.fromisoformat(led["first"].replace("Z", "+00:00"))
            last = _dt.datetime.fromisoformat(led["last"].replace("Z", "+00:00"))
            days_active = max(1, (last - first).days + 1)
        except Exception:
            pass
    days_active = max(days_active, act.get("days_active", 0))

    alias_map: dict[str, str] = {}
    project_name = os.path.basename(_project_root().rstrip(r"\/"))
    project_label = _anonymize(project_name, alias_map) if anonymize else project_name

    file_label = lambda p: (
        _anonymize(os.path.basename(p), alias_map, prefix="File") if anonymize else p
    )

    # ── markdown ──
    lines: list[str] = []
    lines.append(f"# AI-Native Developer Profile · {today}")
    lines.append("")
    lines.append(
        f"_自动从 `temp/cost_ledger.jsonl` · `temp/activity/` · git log · "
        f"`temp/autonomous_reports/history.txt` 聚合生成（{'anonymized' if anonymize else 'raw'} 模式）_"
    )
    lines.append("")

    lines.append("## 🚀 Highlights")
    lines.append("")
    bullets = []
    bullets.append(f"- **{days_active}** active days across this project")
    bullets.append(f"- **{led['calls']:,}** LLM calls · **{_fmt_tok(led['input'])}** input tokens · "
                   f"**{_fmt_tok(led['output'])}** output")
    if led["cache_leverage"]:
        bullets.append(f"- **{led['cache_leverage']}×** cache read leverage "
                       f"({_fmt_tok(led['cache_read'])} cached / "
                       f"{_fmt_tok(led['input'] - led['cache_read'])} fresh input)")
    bullets.append(f"- Spent **${led['cost_usd']:.2f}** on inference across "
                   f"{len(led['models'])} model(s)")
    if git["commits"]:
        bullets.append(f"- **{git['commits']}** git commits ({git['first']} → {git['last']})")
    if auto["total"]:
        bullets.append(f"- **{auto['total']}** autonomous reports filed")
    lines.extend(bullets)
    lines.append("")

    if led["models"]:
        lines.append("## 🤖 Models")
        lines.append("")
        lines.append("| model | calls | input | output | cache_read | cost |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for model, v in led["models"]:
            label = _anonymize(model, alias_map, prefix="Model") if anonymize else model
            lines.append(
                f"| {label} | {v['calls']:,} | {_fmt_tok(v['input'])} | "
                f"{_fmt_tok(v['output'])} | {_fmt_tok(v['cache_read'])} | "
                f"${v['cost_usd']:.4f} |"
            )
        lines.append("")

    if act["by_kind"]:
        lines.append("## 🛠 Agent activity")
        lines.append("")
        for kind, n in act["by_kind"][:8]:
            lines.append(f"- `{kind}`  ×{n:,}")
        lines.append(f"\n_Total agent activity rows: {act['rows']:,} across "
                     f"{act['days_active']} day(s)_")
        lines.append("")

    if auto["by_category"]:
        lines.append("## 🧭 Autonomous focus areas")
        lines.append("")
        for cat, n in auto["by_category"]:
            lines.append(f"- **{cat}**: {n} report(s)")
        if auto["recent"]:
            lines.append("\n最近若干个主题：")
            for rid, topic in auto["recent"][:8]:
                lines.append(f"  - R{rid}: {topic}")
        lines.append("")

    if git["top_files"]:
        lines.append("## 📂 Most-touched files (recent)")
        lines.append("")
        for path, n in git["top_files"][:8]:
            lines.append(f"- `{file_label(path)}` ×{n}")
        lines.append("")

    if git["by_author"]:
        lines.append("## 👥 Authors")
        lines.append("")
        for author, n in git["by_author"]:
            label = _anonymize(author, alias_map, prefix="Author") if anonymize else author
            lines.append(f"- {label}: {n} commit(s)")
        lines.append("")

    lines.append("---")
    lines.append(f"_工程：{project_label} · 生成器：`wlwl readme`（受 "
                 "[Readme.skill](https://github.com/study8677/Readme.skill) 启发，"
                 "本项目使用本地数据复刻）_")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wlwl readme")
    p.add_argument("--out", default=None,
                   help="output path (default temp/self_readme.md)")
    p.add_argument("--stdout", action="store_true",
                   help="print to stdout instead of writing a file")
    p.add_argument("--no-anonymize", action="store_true",
                   help="keep original project / model / author names")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    md = build_readme(anonymize=not args.no_anonymize)
    if args.stdout:
        sys.stdout.write(md)
        return 0
    out_path = args.out or os.path.join(_project_root(), "temp", "self_readme.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print(f"[wlwl readme] wrote {out_path} ({len(md)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
