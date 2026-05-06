"""``python -m launcher.workers`` — CLI for kernel worker hot-ops.

Identical surface to the (future) HTTP and GUI drivers (ADR-0009 §9.4).

  list                          # workers + state
  add <kind> <name> [--config k=v ...]
  remove <name> [--drain-ms N]
  reload <name> [--config k=v ...]
  silence <name> [--reason X] [--ttl SEC]
  unsilence <name>
  inspect <name>
  factories                     # list registered factories
  forum-tail [--topic TOPIC] [-n N]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from llmcore.kernel import RegistrationError, get_kernel


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m launcher.workers")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list workers")
    sub.add_parser("factories", help="list registered factories")

    p_add = sub.add_parser("add", help="add a worker")
    p_add.add_argument("kind")
    p_add.add_argument("name")
    p_add.add_argument("--config", action="append", default=[], metavar="K=V")

    p_rm = sub.add_parser("remove", help="remove a worker")
    p_rm.add_argument("name")
    p_rm.add_argument("--drain-ms", type=int, default=30_000)

    p_reload = sub.add_parser("reload", help="reload a worker")
    p_reload.add_argument("name")
    p_reload.add_argument("--config", action="append", default=[])

    p_sil = sub.add_parser("silence", help="silence a worker")
    p_sil.add_argument("name")
    p_sil.add_argument("--reason", default="")
    p_sil.add_argument("--ttl", type=int, default=None)

    p_un = sub.add_parser("unsilence", help="unsilence a worker")
    p_un.add_argument("name")

    p_ins = sub.add_parser("inspect", help="inspect a worker")
    p_ins.add_argument("name")

    p_tail = sub.add_parser("forum-tail", help="tail a forum topic")
    p_tail.add_argument("--topic", default="audit")
    p_tail.add_argument("-n", type=int, default=20)

    p_metrics = sub.add_parser("metrics", help="show per-worker metrics")
    p_metrics.add_argument("--worker", default=None, help="filter to one worker")
    p_metrics.add_argument("--json", action="store_true", help="JSON output")
    p_metrics.add_argument("--from-spool", action="store_true",
                           help="read latest snapshot from temp/metrics.jsonl instead of live registry")

    args = p.parse_args(argv)
    k = get_kernel()
    cmd = args.cmd

    if cmd == "list":
        rows = k.list_workers()
        if not rows:
            print("(no workers)")
            return 0
        print(f"{'NAME':<24} {'KIND':<14} {'STATE':<10} {'IN-FLIGHT':<10} CAPS")
        for w in rows:
            state = "silenced" if w.silenced else w.health_state
            caps = ", ".join(w.capabilities)
            print(f"{w.name:<24} {w.kind:<14} {state:<10} {w.in_flight:<10} {caps}")
        return 0

    if cmd == "factories":
        for f in k.list_factories():
            print(f"{f.factory_id:<32} api={f.api_version} transport={f.transport}")
            if f.capabilities_offered:
                print(f"    caps: {', '.join(f.capabilities_offered)}")
        return 0

    if cmd == "add":
        cfg = {"name": args.name, "kind": args.kind, **_parse_kv(args.config)}
        try:
            meta = k.add_worker(cfg, source="cli")
        except RegistrationError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"added: {meta.name} kind={meta.kind} caps={list(meta.capabilities)}")
        return 0

    if cmd == "remove":
        k.remove_worker(args.name, drain_timeout_ms=args.drain_ms, reason="cli_remove")
        print(f"removed: {args.name}")
        return 0

    if cmd == "reload":
        cfg = _parse_kv(args.config) if args.config else None
        try:
            meta = k.reload_worker(args.name, new_config=cfg)
        except (KeyError, RegistrationError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"reloaded: {meta.name}")
        return 0

    if cmd == "silence":
        try:
            k.silence(args.name, args.reason, ttl_sec=args.ttl)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"silenced: {args.name}")
        return 0

    if cmd == "unsilence":
        try:
            k.unsilence(args.name)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"unsilenced: {args.name}")
        return 0

    if cmd == "inspect":
        try:
            ins = k.inspect_worker(args.name)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        out = {
            "name": ins.view.name, "kind": ins.view.kind,
            "capabilities": list(ins.view.capabilities),
            "silenced": ins.view.silenced,
            "silence_reason": ins.view.silence_reason,
            "in_flight": ins.view.in_flight,
            "health_state": ins.view.health_state,
            "factory_id": ins.view.factory_id,
            "config": ins.config_redacted,
            "token_expires_at": ins.token_expires_at,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if cmd == "forum-tail":
        try:
            msgs = k.forum.tail(args.topic, reader="kernel", n=args.n)
        except KeyError as e:
            print(f"error: unknown topic {args.topic}", file=sys.stderr)
            return 2
        for m in msgs:
            print(f"#{m.seq:<5} {m.type:<24} by={m.author:<12} ts={m.timestamp:.2f} {json.dumps(m.payload, ensure_ascii=False)}")
        return 0

    if cmd == "metrics":
        if args.from_spool:
            from llmcore.metrics import read_spool
            import os as _os
            spool = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                   "temp", "metrics.jsonl")
            recs = list(read_spool(spool, since_seconds=24 * 3600))
            if not recs:
                print("(no spool snapshots)")
                return 0
            latest = recs[-1]
            workers = latest["workers"]
        else:
            workers = k.metrics.snapshot()
        if args.worker:
            workers = [w for w in workers if w["worker"] == args.worker]
        if args.json:
            print(json.dumps(workers, indent=2, ensure_ascii=False))
            return 0
        if not workers:
            print("(no metrics)")
            return 0
        for w in workers:
            print(f"worker={w['worker']} in_flight={int(w['in_flight'])} circuit={w['circuit_state']}")
            for cap, stats in w["by_capability"].items():
                line = (f"  cap={cap} ok={int(stats['ok'])} err={int(stats['err_total'])} "
                         f"success={stats['success_rate']:.2%} "
                         f"p50={int(stats['latency']['p50_ms'])}ms "
                         f"p90={int(stats['latency']['p90_ms'])}ms "
                         f"p99={int(stats['latency']['p99_ms'])}ms")
                if stats['cost_usd_total']:
                    line += f" cost=${stats['cost_usd_total']:.4f}"
                if stats['err_by_code']:
                    line += f"  errors={stats['err_by_code']}"
                print(line)
        return 0

    return 1


def _parse_kv(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        v_stripped = v.strip()
        if v_stripped.lower() in ("true", "false"):
            out[k.strip()] = v_stripped.lower() == "true"
        elif v_stripped.lstrip("-").isdigit():
            out[k.strip()] = int(v_stripped)
        else:
            out[k.strip()] = v_stripped
    return out


if __name__ == "__main__":
    sys.exit(main())
