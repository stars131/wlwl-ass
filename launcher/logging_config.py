"""Centralized logging configuration for wlwl-ass.

Goals:
  * One ``setup_logging()`` callable used by all entry points.
  * Rotating file handler (default ``temp/logs/wlwl-ass.log``, 10 MB × 5).
  * Optional structured JSON output (``WLWL_LOG_FORMAT=json``).
  * Per-module level overrides via ``WLWL_LOG_LEVEL_<MODULE>`` env vars
    (e.g. ``WLWL_LOG_LEVEL_LLMCORE_KERNEL=DEBUG``).
  * Correlation IDs auto-injected via :class:`launcher.correlation.CorrelationFilter`.
  * UTF-8 forced on Windows console (stdlib defaults to cp936 there).
  * Idempotent — safe to call from multiple entry points.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Any

from launcher.correlation import CorrelationFilter

_CONFIGURED = False
DEFAULT_LOG_DIR = "temp/logs"
DEFAULT_LOG_FILE = "wlwl-ass.log"
TEXT_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s "
    "[call=%(call_id)s session=%(session_id)s worker=%(worker_name)s] | %(message)s"
)
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class JsonFormatter(logging.Formatter):
    """One-line JSON per record. Stable schema; consumers can grep/parse."""

    _STD_ATTRS = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, DATE_FORMAT) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "call_id": getattr(record, "call_id", "-"),
            "session_id": getattr(record, "session_id", "-"),
            "worker_name": getattr(record, "worker_name", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
        }
        # Custom kwargs passed via `extra={...}`
        for k, v in record.__dict__.items():
            if k in self._STD_ATTRS or k in payload:
                continue
            if k.startswith("_"):
                continue
            payload[k] = _safe_json(v)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _safe_json(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)


def setup_logging(
    *, level: str | int | None = None, fmt: str | None = None,
    log_file: str | None = None, log_dir: str | None = None,
    max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5,
    force: bool = False,
) -> None:
    """Configure root logger with file + console handlers + correlation filter.

    Idempotent: re-calls return early unless ``force=True``.

    Resolution order for each parameter:
      explicit arg > ``WLWL_LOG_*`` env var > sensible default
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level = _resolve_level(level)
    fmt = (fmt or os.environ.get("WLWL_LOG_FORMAT") or "text").lower()
    log_dir = log_dir or os.environ.get("WLWL_LOG_DIR") or _default_log_dir()
    log_file = log_file or os.environ.get("WLWL_LOG_FILE") or DEFAULT_LOG_FILE

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    # Build handlers
    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(TEXT_FORMAT, datefmt=DATE_FORMAT)

    correlation = CorrelationFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(correlation)

    console_stream = _utf8_console_stream()
    console = logging.StreamHandler(console_stream)
    console.setFormatter(formatter)
    console.addFilter(correlation)

    root = logging.getLogger()
    # clear any prior handlers (idempotency under force=True)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console)

    # Per-module overrides: WLWL_LOG_LEVEL_<DOTS_TO_UNDERSCORES>
    for env_key, env_val in os.environ.items():
        if not env_key.startswith("WLWL_LOG_LEVEL_") or env_key == "WLWL_LOG_LEVEL":
            continue
        module = env_key[len("WLWL_LOG_LEVEL_"):].lower().replace("_", ".")
        try:
            logging.getLogger(module).setLevel(_resolve_level(env_val))
        except (ValueError, TypeError):
            continue

    # Tame chatty third parties; we don't need their DEBUG.
    for noisy in ("websockets", "urllib3", "asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    _CONFIGURED = True
    logging.getLogger("wlwl_ass.logging").info(
        "logging configured: level=%s format=%s file=%s",
        logging.getLevelName(level), fmt, log_path,
    )


def _resolve_level(level: str | int | None) -> int:
    if level is None:
        level = os.environ.get("WLWL_LOG_LEVEL", "INFO")
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    return logging.INFO


def _default_log_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), DEFAULT_LOG_DIR)


def _utf8_console_stream():
    """Make sure the console handler can print non-ASCII without crashing on Windows."""
    stream = sys.stderr
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return stream


# ── Diagnostic helpers ──────────────────────────────────────────────────


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Structured event log helper. Adds ``event=`` field; works for both
    text and JSON formatters."""
    extras = {"event": event, **fields}
    logger.info(f"event={event} " + " ".join(f"{k}={v}" for k, v in fields.items()),
                extra=extras)
