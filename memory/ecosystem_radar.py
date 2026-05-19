"""Ecosystem radar — orchestration entry point.

Three modes, all callable from the CLI (``python -m memory.ecosystem_radar
--mode watch``), the agent tool (``do_ecosystem_radar``), or the scheduler:

  * ``watch`` (every 2h): collect → dedupe → score → push critical / buffer
    the rest. Returns a small summary dict.
  * ``poc`` (weekday 09:13): pick the highest-scoring un-evaluated repo
    from the buffer, return a prompt that drives the autonomous agent
    through the PoC funnel (see ecosystem_radar_sop.md §PoC). Does NOT run
    the PoC itself — PoC needs ``code_run`` which only the agent has.
  * ``digest`` (daily 21:07): LLM-summarise yesterday's buffer into a single
    Feishu card, archive the buffer file, prune the 30-day seen set.

Why ``poc`` returns a prompt instead of running: the scheduler infra is a
"prompt generator" (reflect/scheduler.py:124-129). When a sche_tasks/*.json
task fires, the scheduler feeds its prompt into the agent loop. So
``orchestrate(mode='poc')`` is meant to return the next prompt string;
the actual git clone / venv / smoke happen in the next agent turn.

Persistence:
  * ``temp/ecosystem_radar_buffer.jsonl`` — all scored items, one per line.
    The digest reads from here and archives it.
  * ``temp/ecosystem_radar_archive/<YYYY-MM>/buffer_<YYYY-MM-DD>.jsonl`` —
    archived daily buffers, for backfill / forensics.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)  # so 'tools.*' and 'launcher.*' resolve when invoked via -m

from tools.ecosystem_sources import RawItem, collect_all, DEFAULT_WATCHLIST
from tools.ecosystem_scorer import (
    ScoredItem,
    dedupe,
    score,
    tier_at_or_above,
    TIER_ORDER,
)

BUFFER_PATH = os.path.join(PROJECT_ROOT, "temp", "ecosystem_radar_buffer.jsonl")
ARCHIVE_ROOT = os.path.join(PROJECT_ROOT, "temp", "ecosystem_radar_archive")
RADAR_NOTES_PATH = os.path.join(PROJECT_ROOT, "memory", "external_tools_radar.md")
LOG_PATH = os.path.join(PROJECT_ROOT, "temp", "ecosystem_radar.log")


def _log(line: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except Exception:
        pass


# ── env helpers ───────────────────────────────────────────────────────


def _resolve_watchlist() -> list[str]:
    env = (os.environ.get("WLWL_RADAR_WATCHLIST") or "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip() and "/" in s][:20]
    return list(DEFAULT_WATCHLIST)


def _notify_recipient() -> str:
    """Resolve the Feishu recipient: env > mykeys > 'owner' literal."""
    v = (os.environ.get("WLWL_RADAR_NOTIFY_TO") or "").strip()
    if v:
        return v
    try:
        from llmcore import mykeys
        v = str(mykeys.get("radar_notify_to") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return "owner"


def _quiet_hours_active() -> bool:
    """Honour ``WLWL_RADAR_QUIET_HOURS=HH-HH`` (e.g. ``22-8``)."""
    spec = (os.environ.get("WLWL_RADAR_QUIET_HOURS") or "22-8").strip().lower()
    if spec in ("off", "none", "0"):
        return False
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", spec)
    if not m:
        return False
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    now = datetime.now().hour
    if start <= end:
        return start <= now < end
    # wraps midnight (e.g. 22-8)
    return now >= start or now < end


# ── feishu ────────────────────────────────────────────────────────────


def _feishu_send(text: str, *, files: list[str] | None = None) -> str:
    """Thin wrapper around tools.feishu.feishu_send. Records the outcome to
    the radar log so failures show up in one place."""
    try:
        from tools.feishu import feishu_send
    except Exception as exc:
        _log(f"feishu import failed: {exc}")
        return f"[feishu import failed: {exc}]"
    recipient = _notify_recipient()
    try:
        out = feishu_send(recipient, text, files=files)
        _log(f"feishu_send → {recipient}: {out}")
        return out
    except Exception as exc:
        _log(f"feishu_send crashed: {exc}")
        return f"[feishu_send crashed: {exc}]"


# ── buffer i/o ────────────────────────────────────────────────────────


def _append_buffer(scored_items: list[ScoredItem]) -> int:
    if not scored_items:
        return 0
    os.makedirs(os.path.dirname(BUFFER_PATH), exist_ok=True)
    with open(BUFFER_PATH, "a", encoding="utf-8") as f:
        for s in scored_items:
            d = s.to_dict()
            d["_recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return len(scored_items)


def _read_buffer(*, since: datetime | None = None) -> list[dict[str, Any]]:
    if not os.path.isfile(BUFFER_PATH):
        return []
    out: list[dict[str, Any]] = []
    with open(BUFFER_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if since is not None:
                ts = row.get("_recorded_at") or ""
                try:
                    t = datetime.fromisoformat(ts)
                except Exception:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t < since:
                    continue
            out.append(row)
    return out


def _archive_buffer(*, for_date: datetime) -> str | None:
    """Move the current buffer file into the archive folder, named by date.
    Returns the archive path, or None if there was nothing to archive."""
    if not os.path.isfile(BUFFER_PATH):
        return None
    month_dir = os.path.join(ARCHIVE_ROOT, for_date.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)
    target = os.path.join(month_dir, f"buffer_{for_date.strftime('%Y-%m-%d')}.jsonl")
    try:
        shutil.move(BUFFER_PATH, target)
    except Exception as exc:
        _log(f"archive move failed: {exc}")
        return None
    return target


# ── notes (memory/external_tools_radar.md) ────────────────────────────


_NOTES_HEADER = """# External tools radar — 沉淀池

来源：`memory.ecosystem_radar.orchestrate(mode='poc')` 和 autonomous 雷达任务。\
每条 entry 顶在前（最新在前）。同一 repo 二次评估在原 entry 末尾追加 \
`### 复评 YYYY-MM-DD` 而不是新建。

字段：
- `repo`：owner/repo
- `evaluated_at`：YYYY-MM-DD
- `capability_gap`：这个工具补什么能力差距
- `verdict`：adopt | revisit | pass
- `deploy_advice`：装哪些 / 占多少 / 怎么回滚（pass 也写"为何 pass"）
- `notes`：跑 PoC 时踩的坑、关键命令、smoke 输出片段

---

"""


def ensure_notes_file() -> None:
    if os.path.isfile(RADAR_NOTES_PATH):
        return
    os.makedirs(os.path.dirname(RADAR_NOTES_PATH), exist_ok=True)
    with open(RADAR_NOTES_PATH, "w", encoding="utf-8") as f:
        f.write(_NOTES_HEADER)


def repo_already_evaluated(repo: str) -> bool:
    """Cheap grep for whether external_tools_radar.md already has an entry
    for this repo. Used to skip duplicate PoCs."""
    if not os.path.isfile(RADAR_NOTES_PATH):
        return False
    try:
        with open(RADAR_NOTES_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    pattern = re.compile(rf"^[-*\s]*\*?\*?repo\*?\*?\s*[:：]\s*`?{re.escape(repo)}`?\s*$", re.MULTILINE)
    return bool(pattern.search(text))


# ── orchestrate: watch ────────────────────────────────────────────────


def _format_critical_card(s: ScoredItem) -> str:
    parts = [
        f"🚨 [行业雷达·critical] {s.item.title[:120]}",
        f"来源: {s.item.source}",
    ]
    if s.item.repo:
        parts.append(f"仓库: {s.item.repo}")
    parts.append(f"链接: {s.item.url}")
    if s.rationale:
        parts.append(f"理由: {s.rationale}")
    if s.capability_gap:
        parts.append(f"能补什么: {s.capability_gap}")
    return "\n".join(parts)


def _run_watch(*, dry_run: bool, force_notify: bool) -> dict[str, Any]:
    watchlist = _resolve_watchlist()
    items, errors = collect_all(watchlist=watchlist, timeout_per_source=25.0)
    _log(f"watch collected={len(items)} errors={errors}")
    fresh, already = dedupe(items)
    _log(f"watch fresh={len(fresh)} already_seen={len(already)}")
    if not fresh:
        return {
            "mode": "watch",
            "collected": len(items),
            "fresh": 0,
            "critical": 0,
            "notable": 0,
            "buffered": 0,
            "errors": errors,
        }
    scored = score(fresh, watchlist=watchlist)
    _append_buffer(scored)

    critical = [s for s in scored if s.tier == "critical"]
    notable = [s for s in scored if s.tier == "notable"]

    pushed = 0
    quiet = _quiet_hours_active() and not force_notify
    for s in critical:
        if dry_run:
            print(f"# DRY-RUN would push critical: {s.item.id}")
            continue
        if quiet:
            _log(f"quiet hours active, deferring critical {s.item.id} to digest")
            continue
        _feishu_send(_format_critical_card(s))
        pushed += 1

    return {
        "mode": "watch",
        "collected": len(items),
        "fresh": len(fresh),
        "scored": len(scored),
        "critical": len(critical),
        "critical_pushed": pushed,
        "notable": len(notable),
        "buffered": len(scored),
        "errors": errors,
        "quiet_hours_active": quiet,
    }


# ── orchestrate: poc ──────────────────────────────────────────────────


_POC_PROMPT_TEMPLATE = """[生态雷达 PoC] {repo}

候选来源：{source} (tier={tier}, score={score:.1f})
标题：{title}
URL：{url}
能力差距假设：{capability_gap}

按 memory/ecosystem_radar_sop.md §PoC 段落执行：
  1. 用 update_working_checkpoint 记录本次 PoC 目标 + 报告路径 {report_path}
  2. git clone {url} 到 temp/radar_poc/{repo_safe}/
  3. 探测 build system 顺序：pyproject.toml → setup.py → package.json → Cargo.toml → go.mod → README 的 install 段。
     一律在 temp/radar_poc/{repo_safe}/ 内 `python -m venv .venv-radar` 或对应隔离方式。
  4. 跑 README 给的最小 demo / smoke。绑端口必须 ≥ 49152；占低位端口直接判 pass。
  5. 评估 verdict ∈ {{adopt, revisit, pass}}，按 ecosystem_radar_sop 模板把 entry append 到
     memory/external_tools_radar.md 头部（最新在前）。
  6. 调 feishu_send（通过 code_run）推一句结论给用户 ({notify_to})。
  7. rm -rf temp/radar_poc/{repo_safe}/（保留报告里需要的关键截图/日志副本）。
  8. 报告写到 {report_path}。

注意：不要装服务、不要占低位端口、不要改全局 site-packages。这些都属于审批门后。
"""


def _run_poc(*, force_repo: str | None) -> dict[str, Any]:
    """Pick the top buffered candidate (or ``force_repo``) and emit the
    next-turn prompt. The actual PoC execution happens in the agent loop
    after the scheduler delivers this prompt."""
    ensure_notes_file()

    # Find candidate
    candidate: dict[str, Any] | None = None
    if force_repo:
        # Search buffer for any item with this repo first, fall back to a
        # synthetic record so the user can force-evaluate even when there's
        # no recent signal.
        for row in reversed(_read_buffer()):
            if (row.get("item") or {}).get("repo") == force_repo:
                candidate = row
                break
        if candidate is None:
            candidate = {
                "item": {
                    "id": f"forced:{force_repo}",
                    "source": "manual",
                    "title": force_repo,
                    "url": f"https://github.com/{force_repo}",
                    "repo": force_repo,
                    "summary": "",
                    "signal_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                "tier": "notable",
                "score": 7.0,
                "rationale": "用户手动指定",
                "capability_gap": "",
            }
    else:
        # Look at last 24h of buffer, filter to tier ≥ trending with a real repo,
        # sort by score desc, take first un-evaluated.
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        buf = _read_buffer(since=since)
        eligible = []
        for row in buf:
            tier = row.get("tier") or "skip"
            if TIER_ORDER.get(tier, 0) < TIER_ORDER["trending"]:
                continue
            repo = (row.get("item") or {}).get("repo")
            if not repo:
                continue
            if repo_already_evaluated(repo):
                continue
            eligible.append(row)
        eligible.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        if eligible:
            candidate = eligible[0]

    if candidate is None:
        return {"mode": "poc", "selected": None, "reason": "no eligible candidate in buffer"}

    item = candidate.get("item") or {}
    repo = item.get("repo") or ""
    repo_safe = repo.replace("/", "_") if repo else "unknown"
    today = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = os.path.join(PROJECT_ROOT, "autonomous_reports", f"radar_poc_{repo_safe}_{today}.md")
    prompt = _POC_PROMPT_TEMPLATE.format(
        repo=repo or "(unknown)",
        repo_safe=repo_safe,
        source=item.get("source", ""),
        tier=candidate.get("tier", ""),
        score=float(candidate.get("score") or 0),
        title=item.get("title", ""),
        url=item.get("url", ""),
        capability_gap=candidate.get("capability_gap", "") or "（自行判断）",
        report_path=report_path,
        notify_to=_notify_recipient(),
    )
    return {
        "mode": "poc",
        "selected": {"repo": repo, "score": candidate.get("score"), "tier": candidate.get("tier")},
        "prompt": prompt,
        "report_path": report_path,
    }


# ── orchestrate: digest ───────────────────────────────────────────────


_DIGEST_PROMPT_TEMPLATE = """以下是过去 24 小时通过行业雷达抓到的全部信号（含 tier）。\
请按主题聚类，合并同源/同事件，给我前 5 条最具行动价值的，每条一行：

格式：`[<tier>] <一句话本质摘要> — <URL>`

排除噪音/重复/明显广告。如果不足 5 条值得说的，给几条就几条，不要凑数。\
直接输出列表，不要前后解释。

信号：
{rows_json}
"""


def _run_digest(*, dry_run: bool) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    buf = _read_buffer(since=since)
    if not buf:
        return {"mode": "digest", "skipped": "empty buffer"}

    lite = []
    for row in buf:
        it = row.get("item") or {}
        lite.append({
            "tier": row.get("tier"),
            "score": row.get("score"),
            "source": it.get("source"),
            "title": (it.get("title") or "")[:140],
            "url": it.get("url"),
            "repo": it.get("repo"),
        })
    prompt = _DIGEST_PROMPT_TEMPLATE.format(rows_json=json.dumps(lite, ensure_ascii=False, indent=2)[:6000])

    # Use the same scorer routing — free pool first, then any paid session.
    from tools.ecosystem_scorer import _call_llm_for_score
    summary = _call_llm_for_score(prompt, timeout=60.0) or ""
    summary = summary.strip()
    if not summary:
        # Fallback: stitch a flat list of top scored.
        top = sorted(buf, key=lambda r: float(r.get("score") or 0), reverse=True)[:5]
        lines = []
        for r in top:
            it = r.get("item") or {}
            lines.append(f"[{r.get('tier')}] {it.get('title','')[:80]} — {it.get('url','')}")
        summary = "\n".join(lines) if lines else "（无可推送的信号）"

    today = datetime.now()
    card = f"📰 [行业雷达·日报 {today.strftime('%Y-%m-%d')}]\n\n{summary}"

    pushed = False
    if not dry_run:
        _feishu_send(card)
        pushed = True

    # Archive yesterday's buffer (we just summarized it; rotate it out).
    yesterday = today - timedelta(days=1)
    archived = _archive_buffer(for_date=yesterday) if not dry_run else None

    return {
        "mode": "digest",
        "items_summarized": len(buf),
        "summary_chars": len(summary),
        "pushed": pushed,
        "archived_to": archived,
    }


# ── public entry point ────────────────────────────────────────────────


def orchestrate(
    mode: str,
    *,
    dry_run: bool = False,
    force_notify: bool = False,
    force_repo: str | None = None,
) -> dict[str, Any]:
    mode = (mode or "watch").strip().lower()
    if mode == "watch":
        return _run_watch(dry_run=dry_run, force_notify=force_notify)
    if mode == "poc":
        return _run_poc(force_repo=force_repo)
    if mode == "digest":
        return _run_digest(dry_run=dry_run)
    return {"error": f"unknown mode: {mode!r} (expected: watch | poc | digest)"}


# ── CLI ───────────────────────────────────────────────────────────────


def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Ecosystem radar orchestrator")
    p.add_argument("--mode", required=True, choices=["watch", "poc", "digest"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-notify", action="store_true", help="bypass quiet hours")
    p.add_argument("--force-repo", default="", help="(poc mode) force a specific owner/repo")
    args = p.parse_args()

    out = orchestrate(
        args.mode,
        dry_run=args.dry_run,
        force_notify=args.force_notify,
        force_repo=args.force_repo or None,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
