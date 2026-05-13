"""Free-pool vault: persistent storage for harvested + probed API endpoints.

This is the single source of truth for "what free APIs do we currently have".
The harvester writes candidate entries here through ``add_or_update``; the
probe writes scores back through ``record_probe``; the router reads via
``verified_entries``.

Files
-----
* ``temp/free_api_vault.json`` — live entries, JSON object on disk
* ``temp/free_api_vault_archive.jsonl`` — append-only history of evicted /
  archived entries; one JSON object per line. Used for "which posters tend
  to share stable APIs" analysis later.
* ``temp/free_pool_leaderboard.md`` — human-readable digest, rebuilt on
  each ``record_probe`` call so the user can eyeball the current pool.

Concurrency
-----------
A module-level ``threading.RLock`` guards in-process callers. Cross-process
writes (if any) rely on atomic-rename via a ``.tmp`` file; the launcher API
process is the only writer in practice.

Eviction rules
--------------
* ``status == "failing"`` and ``fail_count >= MAX_FAIL_IN_ROW`` → archive
* ``expires_at`` past current time without being reverified → status set to
  ``expired``; the next ``sweep_expired`` call archives it.
* Explicit user/agent eviction via ``archive(id, reason)``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import contextlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


VAULT_VERSION = 1
DEFAULT_TTL_S = 2 * 60 * 60  # 2 hours: entries past expires_at get archived; revalidation cadence is bounded by this
MAX_FAIL_IN_ROW = 3
DEFAULT_LATENCY_BUDGET_MS = 20_000
# Vault + archive contain plaintext API keys. Restrict to owner read/write
# on POSIX. Windows ignores mode bits but inherits NTFS ACLs from temp/,
# which is already user-scoped under the project root.
_KEYFILE_MODE = 0o600
# Archive jsonl grows unbounded; rotate the head into ``.1`` once we cross
# the threshold so the live file stays scannable.
_ARCHIVE_ROTATE_BYTES = 5 * 1024 * 1024  # 5 MiB
_ARCHIVE_ROTATE_KEEP = 3                  # archive.jsonl.1 .. .3


_LOCK = threading.RLock()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def vault_path() -> Path:
    return _project_root() / "temp" / "free_api_vault.json"


def archive_path() -> Path:
    return _project_root() / "temp" / "free_api_vault_archive.jsonl"


def leaderboard_path() -> Path:
    return _project_root() / "temp" / "free_pool_leaderboard.md"


def candidates_path() -> Path:
    return _project_root() / "temp" / "free_api_candidates.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_id(base_url: str, key: str) -> str:
    digest = hashlib.sha1(f"{base_url}|{key}".encode("utf-8")).hexdigest()
    return f"fa_{digest[:10]}"


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***" + key[-2:]
    return key[:5] + "..." + key[-4:]


def _restrict_file_mode(path: Path) -> None:
    """chmod the file to owner-only. POSIX-only effect; Windows is a no-op
    (the file already inherits user-scoped ACL from temp/). Best-effort —
    a chmod failure must not block the write."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, _KEYFILE_MODE)
    except OSError:
        pass


def _empty_vault() -> dict[str, Any]:
    return {"version": VAULT_VERSION, "updated_at": _now_iso(), "entries": []}


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    # Restrict mode BEFORE the rename so the live file never appears with a
    # permissive mode in between (cf. mktemp race patterns).
    _restrict_file_mode(tmp)
    os.replace(tmp, path)


def _rotate_archive_if_needed(path: Path) -> None:
    """Roll archive.jsonl → .1 → .2 → ... once it crosses the size threshold.
    Best-effort; rotation failure must not block the append."""
    try:
        if not path.is_file() or path.stat().st_size < _ARCHIVE_ROTATE_BYTES:
            return
        # Drop the oldest, shift the rest up.
        oldest = path.with_suffix(path.suffix + f".{_ARCHIVE_ROTATE_KEEP}")
        if oldest.exists():
            with contextlib.suppress(OSError):
                oldest.unlink()
        for i in range(_ARCHIVE_ROTATE_KEEP - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i + 1}")
            if src.exists():
                with contextlib.suppress(OSError):
                    os.replace(src, dst)
        rotated = path.with_suffix(path.suffix + ".1")
        with contextlib.suppress(OSError):
            os.replace(path, rotated)
        _restrict_file_mode(rotated)
    except OSError:
        pass


def _append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path == archive_path():
        _rotate_archive_if_needed(path)
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    if is_new:
        _restrict_file_mode(path)


@contextmanager
def _locked() -> Iterator[None]:
    with _LOCK:
        yield


def load_vault() -> dict[str, Any]:
    """Return the on-disk vault (or an empty skeleton if missing/corrupt)."""
    path = vault_path()
    if not path.is_file():
        return _empty_vault()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_vault()
    if not isinstance(data, dict) or "entries" not in data:
        return _empty_vault()
    data.setdefault("version", VAULT_VERSION)
    data.setdefault("entries", [])
    return data


def _save_vault(data: dict[str, Any]) -> None:
    data["updated_at"] = _now_iso()
    _atomic_write_json(vault_path(), data)


def _find_entry(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def add_or_update(
    *,
    base_url: str,
    key: str,
    claimed_model: str | None = None,
    source: dict[str, Any] | None = None,
    supported_models: list[str] | None = None,
) -> dict[str, Any]:
    """Insert or refresh a harvested candidate. Returns the stored entry.

    Re-discovering the same (base_url, key) updates ``source.last_seen_at``
    but never overwrites scoring/probe fields that ``record_probe`` owns.
    """
    base_url = (base_url or "").strip().rstrip("/")
    key = (key or "").strip()
    if not base_url or not key:
        raise ValueError("base_url and key are required")
    entry_id = _entry_id(base_url, key)
    with _locked():
        data = load_vault()
        existing = _find_entry(data["entries"], entry_id)
        if existing is None:
            entry = {
                "id": entry_id,
                "base_url": base_url,
                "key": key,
                "key_masked": _mask_key(key),
                "claimed_model": claimed_model or "",
                "discovered_at": _now_iso(),
                "last_verified_at": "",
                "expires_at": "",
                "status": "candidate",
                "fail_count": 0,
                "fingerprint": {},
                "stats": {"successful_calls": 0, "failed_calls": 0},
                "supported_models": supported_models or [],
                "source": source or {},
            }
            data["entries"].append(entry)
            _save_vault(data)
            return entry
        # Touch only soft fields when re-discovered.
        existing.setdefault("source", {})
        existing["source"]["last_seen_at"] = _now_iso()
        if claimed_model and not existing.get("claimed_model"):
            existing["claimed_model"] = claimed_model
        if supported_models and not existing.get("supported_models"):
            existing["supported_models"] = supported_models
        _save_vault(data)
        return existing


def record_probe(
    entry_id: str,
    *,
    fingerprint: dict[str, Any] | None = None,
    supported_models: list[str] | None = None,
    latency_ms: float | None = None,
    success: bool = True,
    ttl_s: int = DEFAULT_TTL_S,
    error: str | None = None,
) -> dict[str, Any]:
    """Update an entry with probe results, then rebuild the leaderboard.

    On success: status → verified, fail_count reset, expires_at refreshed.
    On failure: fail_count++, status → failing; archived once it crosses
    ``MAX_FAIL_IN_ROW`` (the caller does not need to track this).
    """
    with _locked():
        data = load_vault()
        entry = _find_entry(data["entries"], entry_id)
        if entry is None:
            raise KeyError(f"unknown entry: {entry_id}")
        stats = entry.setdefault("stats", {"successful_calls": 0, "failed_calls": 0})
        if success:
            entry["status"] = "verified"
            entry["fail_count"] = 0
            entry["last_verified_at"] = _now_iso()
            entry["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
            ).isoformat(timespec="seconds")
            stats["successful_calls"] = int(stats.get("successful_calls", 0)) + 1
            if fingerprint is not None:
                entry["fingerprint"] = fingerprint
            if supported_models is not None:
                entry["supported_models"] = supported_models
            if latency_ms is not None:
                _push_latency(stats, latency_ms)
            entry.pop("last_error", None)
        else:
            entry["status"] = "failing"
            entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
            stats["failed_calls"] = int(stats.get("failed_calls", 0)) + 1
            if error:
                entry["last_error"] = error
            if entry["fail_count"] >= MAX_FAIL_IN_ROW:
                _archive_locked(data, entry_id, reason="max_fail_in_row")
                _save_vault(data)
                _write_leaderboard_locked(data)
                return entry
        _save_vault(data)
        _write_leaderboard_locked(data)
        return entry


def _push_latency(stats: dict[str, Any], latency_ms: float) -> None:
    """Track a rolling p50/p95 estimate over the last 32 samples."""
    samples = stats.setdefault("latency_samples_ms", [])
    samples.append(round(float(latency_ms), 1))
    if len(samples) > 32:
        del samples[: len(samples) - 32]
    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    stats["latency_p50_ms"] = sorted_samples[max(0, n // 2 - 1)]
    stats["latency_p95_ms"] = sorted_samples[max(0, int(n * 0.95) - 1)]


def archive(entry_id: str, *, reason: str) -> bool:
    """Move an entry from vault to archive immediately."""
    with _locked():
        data = load_vault()
        moved = _archive_locked(data, entry_id, reason=reason)
        if moved:
            _save_vault(data)
            _write_leaderboard_locked(data)
        return moved


def _archive_locked(data: dict[str, Any], entry_id: str, *, reason: str) -> bool:
    entries = data["entries"]
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            entry["archived_at"] = _now_iso()
            entry["archive_reason"] = reason
            _append_jsonl(archive_path(), entry)
            del entries[i]
            return True
    return False


def sweep_expired() -> list[str]:
    """Mark entries past their TTL as expired and archive them. Returns the
    archived entry ids."""
    archived: list[str] = []
    with _locked():
        data = load_vault()
        now = datetime.now(timezone.utc)
        for entry in list(data["entries"]):
            exp = entry.get("expires_at")
            if not exp:
                continue
            try:
                exp_dt = datetime.fromisoformat(exp)
            except ValueError:
                continue
            if exp_dt < now:
                _archive_locked(data, entry["id"], reason="expired")
                archived.append(entry["id"])
        if archived:
            _save_vault(data)
            _write_leaderboard_locked(data)
    return archived


def verified_entries(
    *,
    min_score: float = 0.0,
    category_required: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return live ``verified`` entries, best-scored first."""
    with _locked():
        data = load_vault()
    rows = [e for e in data["entries"] if e.get("status") == "verified"]
    if category_required:
        rows = [
            e for e in rows
            if any(
                category_required in (q or "")
                for q in (e.get("fingerprint", {}).get("category_strong", []) or [])
            )
        ]
    rows = [
        e for e in rows
        if _entry_score(e) >= min_score
    ]
    rows.sort(key=_entry_score, reverse=True)
    if limit:
        rows = rows[:limit]
    return rows


def list_all() -> list[dict[str, Any]]:
    with _locked():
        return list(load_vault()["entries"])


def get(entry_id: str) -> dict[str, Any] | None:
    with _locked():
        return _find_entry(load_vault()["entries"], entry_id)


def _entry_score(entry: dict[str, Any]) -> float:
    fp = entry.get("fingerprint") or {}
    return float(fp.get("weighted_average") or fp.get("average") or 0.0)


def _write_leaderboard_locked(data: dict[str, Any]) -> None:
    rows = sorted(data["entries"], key=_entry_score, reverse=True)
    lines: list[str] = [
        "# Free Pool Leaderboard",
        "",
        f"_最后更新：{data.get('updated_at', '')}_",
        "",
        "| Rank | id | claimed → guess | score | latency p50 | status | source |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, entry in enumerate(rows, start=1):
        fp = entry.get("fingerprint") or {}
        guess = fp.get("actual_model_guess") or "?"
        score = _entry_score(entry)
        latency = (entry.get("stats") or {}).get("latency_p50_ms") or "-"
        src = entry.get("source") or {}
        src_str = src.get("forum", "") or "-"
        if src.get("topic_id"):
            src_str = f"{src_str}#{src['topic_id']}"
        lines.append(
            f"| {i} | `{entry.get('id', '')}` "
            f"| `{entry.get('claimed_model', '?')}` → `{guess}` "
            f"| {score:.2f} | {latency} | {entry.get('status', '?')} | {src_str} |"
        )
    if not rows:
        lines.append("| _empty_ | | | | | | |")
    lines.extend([
        "",
        f"_共 {len(rows)} 条；archive 见 `temp/free_api_vault_archive.jsonl`_",
        "",
    ])
    leaderboard_path().parent.mkdir(parents=True, exist_ok=True)
    leaderboard_path().write_text("\n".join(lines), encoding="utf-8")


def append_candidate(candidate: dict[str, Any]) -> None:
    """Harvester sink: append a raw candidate to the jsonl tail."""
    _append_jsonl(candidates_path(), {**candidate, "appended_at": _now_iso()})


def iter_candidates() -> Iterator[dict[str, Any]]:
    path = candidates_path()
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
