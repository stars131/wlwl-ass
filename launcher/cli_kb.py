"""cli_kb — manage the concierge knowledge-base file.

Usage:
  python -m launcher.cli_kb list
  python -m launcher.cli_kb add --topic employer --summary "..." [--long "..."] [--visibility friends]
  python -m launcher.cli_kb edit --topic employer --summary "新的内容"
  python -m launcher.cli_kb delete --topic employer
  python -m launcher.cli_kb show --topic employer

The KB file format is JSONL — one row per topic. Editing by hand also
works; this CLI mostly exists so the owner doesn't have to remember the
schema.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Allow `python launcher/cli_kb.py` and `python -m launcher.cli_kb` both.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from llmcore.workers.kb_worker import KBStorage, VISIBILITY_LEVELS, DEFAULT_VISIBILITY


def _default_kb_path() -> str:
    return os.path.join(PROJECT_ROOT, "temp", "concierge_kb.jsonl")


def _print_row(row: dict, *, full: bool = False) -> None:
    topic = row.get("topic", "?")
    visibility = row.get("visibility", DEFAULT_VISIBILITY)
    summary = (row.get("summary") or "").strip()
    print(f"[{topic}] ({visibility}) {summary}")
    if full and row.get("long"):
        print(f"  long: {row['long']}")
    if full and row.get("updated_at"):
        print(f"  updated_at: {row['updated_at']}")


def cmd_list(args: argparse.Namespace) -> int:
    storage = KBStorage(args.kb_path)
    rows = storage.rows()
    if not rows:
        print(f"(KB empty at {args.kb_path})")
        return 0
    for row in sorted(rows, key=lambda r: r.get("topic", "")):
        _print_row(row)
    print(f"\n{len(rows)} topic(s) in {args.kb_path}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    storage = KBStorage(args.kb_path)
    for row in storage.rows():
        if row.get("topic") == args.topic:
            _print_row(row, full=True)
            return 0
    print(f"no row with topic={args.topic!r}", file=sys.stderr)
    return 1


def cmd_add(args: argparse.Namespace) -> int:
    storage = KBStorage(args.kb_path)
    existing = {r.get("topic") for r in storage.rows()}
    if args.topic in existing and not args.force:
        print(f"topic {args.topic!r} already exists; use --force to overwrite "
              f"or `cli_kb edit`.", file=sys.stderr)
        return 1
    row = {
        "topic": args.topic,
        "summary": args.summary or "",
        "long": args.long or "",
        "visibility": args.visibility,
    }
    stored = storage.upsert(row)
    print(f"saved: ", end="")
    _print_row(stored, full=True)
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    storage = KBStorage(args.kb_path)
    current = None
    for row in storage.rows():
        if row.get("topic") == args.topic:
            current = dict(row)
            break
    if current is None:
        print(f"no row with topic={args.topic!r}; use `cli_kb add`.", file=sys.stderr)
        return 1
    if args.summary is not None:
        current["summary"] = args.summary
    if args.long is not None:
        current["long"] = args.long
    if args.visibility is not None:
        current["visibility"] = args.visibility
    stored = storage.upsert(current)
    print(f"updated: ", end="")
    _print_row(stored, full=True)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    storage = KBStorage(args.kb_path)
    ok = storage.delete(args.topic)
    if not ok:
        print(f"no row with topic={args.topic!r}", file=sys.stderr)
        return 1
    print(f"deleted: {args.topic}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli_kb",
                                description="Manage concierge KB JSONL.")
    p.add_argument("--kb-path", dest="kb_path", default=_default_kb_path(),
                   help=f"KB file (default {_default_kb_path()})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sl = sub.add_parser("list", help="List all topics.")
    sl.set_defaults(func=cmd_list)

    ss = sub.add_parser("show", help="Show one topic in full.")
    ss.add_argument("--topic", required=True)
    ss.set_defaults(func=cmd_show)

    sa = sub.add_parser("add", help="Add a new topic.")
    sa.add_argument("--topic", required=True)
    sa.add_argument("--summary", default="", help="Short answer (≤200 chars recommended).")
    sa.add_argument("--long", default="", help="Long-form context for RAG ranking.")
    sa.add_argument("--visibility", default=DEFAULT_VISIBILITY,
                    choices=VISIBILITY_LEVELS)
    sa.add_argument("--force", action="store_true",
                    help="Overwrite if topic already exists.")
    sa.set_defaults(func=cmd_add)

    se = sub.add_parser("edit", help="Update fields of an existing topic.")
    se.add_argument("--topic", required=True)
    se.add_argument("--summary", default=None)
    se.add_argument("--long", default=None)
    se.add_argument("--visibility", default=None, choices=VISIBILITY_LEVELS)
    se.set_defaults(func=cmd_edit)

    sd = sub.add_parser("delete", help="Delete a topic.")
    sd.add_argument("--topic", required=True)
    sd.set_defaults(func=cmd_delete)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
