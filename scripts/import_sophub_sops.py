"""One-shot script to fetch + verify + persist 7 Sophub SOPs into memory/.

Re-runs skills_guard.scan() on full content (defense in depth).
Appends a 1-line provenance footer.
Run from project root: python scripts/import_sophub_sops.py
"""
from __future__ import annotations

import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory", "skill_search"))

from skill_search import read_sop  # type: ignore
from tools.skill_frontmatter import parse_frontmatter  # type: ignore
from tools.skills_guard import scan, render_findings  # type: ignore


CANDIDATES = [
    ("69f325a2cb3bf06150bb3baf", "deepsearch_sop.md", "DeepSearch (Grok+Tavily+FireCrawl)"),
    ("69f207e974962f84e0625e0d", "deepresearch_sop.md", "DeepResearch DAG SOP"),
    ("69f2112374962f84e0625e0f", "code_review_sop.md", "Code Review Principles"),
    ("69f20d4e74962f84e0625e0e", "github_project_sop.md", "GitHub Project Learning"),
    ("69f22686ba77d8b04fb0b9be", "pandoc_sop.md", "Pandoc Document Conversion"),
    ("69f509090399f28c1add9e8e", "js_hook_sop.md", "JS Runtime Hook Playbook"),
    ("69f32598cb3bf06150bb3bab", "cloudflare_turnstile_sop.md", "Cloudflare Turnstile Bypass"),
]

MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")
TODAY = time.strftime("%Y-%m-%d")


def main() -> int:
    failures = []
    written = []
    for sop_id, filename, label in CANDIDATES:
        try:
            sop = read_sop(sop_id)
        except Exception as exc:
            failures.append((label, f"fetch failed: {exc}"))
            continue
        body = sop.content or sop.preview or ""
        if not body.strip():
            failures.append((label, "empty body"))
            continue
        # Strip any frontmatter the upstream embedded.
        meta, body_clean = parse_frontmatter(body)
        # Defense-in-depth: re-scan on full content.
        guard = scan(body_clean)
        if not guard.safe:
            failures.append((label, f"skills_guard fail-level: {render_findings(guard)[:200]}"))
            continue
        author = getattr(sop, "author", "?") or "?"
        title = getattr(sop, "title", label) or label
        footer = (
            f"\n\n---\n"
            f"_Sourced from Sophub `{sop_id}` (author: {author}). "
            f"Fetched {TODAY}. See `ATTRIBUTION.md` for borrowing record._\n"
        )
        target = os.path.join(MEMORY_DIR, filename)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(body_clean.rstrip() + footer)
        written.append((filename, sop_id, author, len(body_clean), title))
        print(f"[OK] {filename:<32} {len(body_clean):>6} chars  author={author}  title={title!r}")

    print()
    print(f"Wrote {len(written)} files to {MEMORY_DIR}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for label, reason in failures:
            print(f"  - {label}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
