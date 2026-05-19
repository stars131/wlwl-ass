"""Ecosystem Radar — thin CLI wrapper around launcher.radar_control.

The heavy lifting (spawn, status, kill) lives in
``launcher.radar_control`` so that both this CLI and the GUI REST
endpoints in ``launcher.api_server`` share one source of truth for how
the radar process is managed.

Usage:
    python scripts/start_radar.py            # spawn
    python scripts/start_radar.py --status   # PID + log tail
    python scripts/start_radar.py --stop     # kill
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from launcher import radar_control  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Ecosystem Radar — launcher")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stop", action="store_true", help="stop a running radar_runner")
    g.add_argument("--status", action="store_true", help="print PID + recent log tail")
    args = p.parse_args()

    if args.stop:
        r = radar_control.stop()
        print(f"[stop_radar] {r['message']} (pid={r['pid']})")
        return 0 if r["ok"] else 1

    if args.status:
        s = radar_control.status(log_lines=15)
        print(f"PID file: {s['pid']}  alive: {s['alive']}")
        if s["log_tail"]:
            print(f"\nlast 15 log lines ({s['log_path']}):")
            enc = sys.stdout.encoding or "utf-8"
            for line in s["log_tail"]:
                # On Windows (GBK console) the log may contain unicode chars
                # the codec can't render; downgrade rather than crash.
                try:
                    print("  " + line)
                except UnicodeEncodeError:
                    print("  " + line.encode(enc, errors="replace").decode(enc, errors="replace"))
        return 0

    r = radar_control.start()
    print(f"[start_radar] {r['message']} (pid={r['pid']}, alive={r['alive']})")
    if r["ok"]:
        print(f"[start_radar] log:  {radar_control.LOG_FILE}")
        print(f"[start_radar] stop: python scripts/start_radar.py --stop")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
