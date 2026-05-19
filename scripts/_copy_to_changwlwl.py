"""One-shot: copy git-tracked + untracked-but-not-ignored files to D:\\GA\\changwlwl.

Reads the file list from D:/GA/wlwl-ass produced by:
    git ls-files; git ls-files --others --exclude-standard
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(r"D:\GA\wlwl-ass")
DST = Path(r"D:\GA\changwlwl")


def collect() -> list[str]:
    base = ["git", "-c", "core.quotepath=false"]
    tracked = subprocess.check_output(
        base + ["ls-files"], cwd=SRC, text=True, encoding="utf-8"
    ).splitlines()
    untracked = subprocess.check_output(
        base + ["ls-files", "--others", "--exclude-standard"],
        cwd=SRC, text=True, encoding="utf-8",
    ).splitlines()
    return sorted(set(tracked + untracked))


def main() -> int:
    files = collect()
    DST.mkdir(parents=True, exist_ok=True)
    n_copied = 0
    n_skipped = 0
    errors: list[str] = []
    for rel in files:
        if not rel:
            continue
        src = SRC / rel
        dst = DST / rel
        if not src.is_file():
            n_skipped += 1
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n_copied += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    print(f"copied: {n_copied}")
    print(f"skipped (non-file): {n_skipped}")
    if errors:
        print(f"errors: {len(errors)}")
        for e in errors[:20]:
            print("  " + e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
