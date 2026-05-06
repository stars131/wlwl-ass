"""CLI entry point: python -m skill_search."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import (
    SearchResult,
    SkillSearchError,
    detect_environment,
    get_stats,
    raw_sop,
    read_sop,
    register_agent,
    search,
    search_sops,
)


def format_results(results: list[SearchResult], env: dict, query: str) -> str:
    lines = [
        f'Sophub search: "{query}"',
        f"Environment: {env.get('os', '?')} / {env.get('shell', '?')} / {', '.join(env.get('runtimes', []))}",
        f"Found {len(results)} result(s)\n",
    ]
    if not results:
        lines.append("No matching SOPs found. Try a shorter keyword.")
        return "\n".join(lines)
    for i, r in enumerate(results, 1):
        s = r.skill
        lines += [
            "-" * 60,
            f"#{i}  {s.name}",
            f"    id: {s.key}",
            f"    author: {s.author or '-'} | type: {s.file_type} | stars: {s.stats.get('stars_avg', 0)}",
            f"    url: {s.url}",
            f"    raw: {s.raw_url}",
        ]
        if s.one_line_summary:
            lines.append(f"    summary: {s.one_line_summary}")
        lines.append("")
    lines.append("-" * 60)
    return "\n".join(lines)


def format_results_json(results: list[SearchResult]) -> list[dict]:
    out = []
    for r in results:
        s = r.skill
        out.append(
            {
                "rank": len(out) + 1,
                "id": s.key,
                "title": s.name,
                "author": s.author,
                "file_type": s.file_type,
                "preview": s.description,
                "url": s.url,
                "raw_url": s.raw_url,
                "stats": s.stats,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="skill_search",
        description="Sophub SOP client. Searches and reads SOPs from https://fudankw.cn/sophub/.",
    )
    parser.add_argument("query", nargs="?", help="Search keyword")
    parser.add_argument("--source", choices=["official", "community"], help="Filter SOP source")
    parser.add_argument("--category", "-cat", help="Legacy alias; only official/community map to Sophub source")
    parser.add_argument("--author", help="Filter by fuzzy author name")
    parser.add_argument("--top", "-k", type=int, default=10, help="Number of search results")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--env", action="store_true", help="Show detected environment")
    parser.add_argument("--stats", action="store_true", help="Show Sophub stats")
    parser.add_argument("--api-url", help="Override Sophub API base URL")
    parser.add_argument("--read", metavar="SOP_ID", help="Read full SOP metadata/content as JSON")
    parser.add_argument("--raw", metavar="SOP_ID", help="Print raw SOP content")
    parser.add_argument("--register-agent", metavar="NAME", help="Register an agent API key and save it to keychain")
    parser.add_argument("--contact-email", help="Optional email used with --register-agent")
    args = parser.parse_args()

    if args.api_url:
        os.environ["SOPHUB_API"] = args.api_url

    env = detect_environment()

    if args.env:
        print(json.dumps(env, indent=2, ensure_ascii=False))
        return

    try:
        if args.register_agent:
            data = register_agent(args.register_agent, args.contact_email)
            safe = {k: v for k, v in data.items() if k != "api_key"}
            if data.get("api_key"):
                safe["api_key"] = "<saved to keychain>"
            print(json.dumps(safe, indent=2, ensure_ascii=False))
            return

        if args.read:
            sop = read_sop(args.read)
            print(json.dumps(sop.__dict__, indent=2, ensure_ascii=False))
            return

        if args.raw:
            print(raw_sop(args.raw))
            return

        if args.stats:
            print(json.dumps(get_stats(env), indent=2, ensure_ascii=False))
            return

        if not args.query:
            parser.print_help()
            return

        source = args.source or (args.category if args.category in {"official", "community"} else None)
        if args.author:
            data = search_sops(args.query, page_size=args.top, source=source, author_name=args.author)
            results = [SearchResult.from_dict(item) for item in data.get("items", [])]
        else:
            results = search(query=args.query, env=env, category=source, top_k=args.top)
    except SkillSearchError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(format_results_json(results), indent=2, ensure_ascii=False))
    else:
        print(format_results(results, env, args.query))


if __name__ == "__main__":
    main()
