"""Unified vision-tool entry point (#15).

The agent today has scattered Vision API calls (memory/vision_api.template.py
+ ad-hoc snippets across SOPs). This module sinks the pattern into a small
public surface:

  * ``describe_image(path_or_bytes, prompt=None, backend='auto')`` → str
  * ``ocr_image(path_or_bytes, lang='auto')`` → str
  * ``extract_structured(path_or_bytes, schema, prompt=None)`` → dict

Backends:
  * ``claude`` — uses any mykey config of kind ``native_claude``.
  * ``openai`` — any mykey config of kind ``native_oai`` whose model name
    contains a vision-capable identifier (``gpt-4o``, ``gpt-5``, ``o4``,
    or anything containing ``-v`` / ``vl`` / ``vision``).
  * ``auto`` — tries claude first, falls back to openai.

Failures are returned as ``"Error: ..."`` strings so the agent loop can
keep going. The functions never raise on network/API problems — only on
programmer errors (bad path, bad schema).
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

_DEFAULT_DESC_PROMPT = "请详细描述这张图片的内容、文字、关键元素、空间布局。"
_DEFAULT_OCR_PROMPT = (
    "提取图片中的所有可见文字。"
    "保持原始排版（行/段），不要解读，不要总结。"
    "如果没有文字，返回空字符串。"
)


# ── input handling ────────────────────────────────────────────────────


def _to_image_bytes(src: Any) -> tuple[bytes, str]:
    """Normalise ``src`` to (raw_bytes, mime_type). Accepts:

    * str/Path pointing at an image file
    * bytes / bytearray (mime guessed as image/png)
    """
    if isinstance(src, (bytes, bytearray)):
        return bytes(src), "image/png"
    if isinstance(src, (str, Path)):
        path = Path(src)
        if not path.is_file():
            raise FileNotFoundError(f"vision: image not found at {path}")
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        with open(path, "rb") as f:
            return f.read(), mime
    raise TypeError(f"vision: unsupported image input type {type(src).__name__}")


def _to_b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ── backend selection ─────────────────────────────────────────────────


def _looks_like_vision_model(model: str) -> bool:
    m = (model or "").lower()
    return any(token in m for token in (
        "gpt-4o", "gpt-5", "o4", "vl", "vision", "-v ", "-vl-", "qwen-vl",
        "claude-3", "claude-4", "claude-opus", "claude-sonnet",
    ))


def _pick_config(backend: str) -> dict[str, Any] | None:
    """Pick a working config from mykey for the given backend.

    Returns the config dict (apibase/apikey/model/kind) or None if nothing
    is configured. ``backend`` is one of ``claude`` / ``openai`` / ``auto``.

    Every saved launcher config is eligible (profile filtering was
    removed alongside ``mykey.py`` retirement). Mark the desired entry
    with ``image_capable: True`` to nudge it ahead of host-hint matches.
    """
    try:
        from launcher.api_config import load_api_configs
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        configs = load_api_configs(base)
    except Exception:
        return None

    def _match_claude(c):
        return str(c.get("kind", "")) == "native_claude"

    def _match_openai(c):
        return str(c.get("kind", "")) == "native_oai" and _looks_like_vision_model(str(c.get("model", "")))

    def _first_match(match):
        for c in configs:
            if match(c) and c.get("category") == "multimodal":
                return c
        for c in configs:
            if match(c) and c.get("image_capable"):
                return c
        for c in configs:
            if match(c):
                return c
        return None

    if backend == "claude":
        return _first_match(_match_claude)
    if backend == "openai":
        return _first_match(_match_openai)
    # auto
    for c in configs:
        if c.get("category") == "multimodal" and (_match_claude(c) or _match_openai(c)):
            return c
    for c in configs:
        if c.get("image_capable") and (_match_claude(c) or _match_openai(c)):
            return c
    for c in configs:
        if _match_claude(c):
            return c
    for c in configs:
        if _match_openai(c):
            return c
    return None


# ── HTTP plumbing ─────────────────────────────────────────────────────


def _record_usage_safe(resp_json: dict[str, Any], api_mode: str) -> None:
    """Best-effort: feed the provider's ``usage`` block into llmcore counters
    so the GUI Token Usage tab reflects vision spend too. Swallow any error
    — never break the call path over telemetry."""
    try:
        from llmcore._usage import _record_usage
        usage = resp_json.get("usage") if isinstance(resp_json, dict) else None
        model = resp_json.get("model") if isinstance(resp_json, dict) else ""
        if usage:
            _record_usage(usage, api_mode, source="vision", model=model or "")
    except Exception:
        pass


def _call_claude(cfg: dict[str, Any], b64: str, mime: str, prompt: str, *, max_tokens: int = 1024) -> str:
    import requests
    url = (cfg.get("apibase") or "").rstrip("/") + "/messages"
    headers = {
        "x-api-key": cfg.get("apikey", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": cfg.get("model"),
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
    except Exception as exc:
        return f"Error: vision claude request failed — {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"Error: vision claude HTTP {r.status_code} — {r.text[:200]}"
    try:
        data = r.json()
        _record_usage_safe(data, "messages")
        parts = data.get("content") or []
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except Exception as exc:
        return f"Error: vision claude parse — {exc}"


def _call_openai(cfg: dict[str, Any], b64: str, mime: str, prompt: str, *, max_tokens: int = 1024) -> str:
    import requests
    url = (cfg.get("apibase") or "").rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.get('apikey', '')}",
        "content-type": "application/json",
    }
    payload = {
        "model": cfg.get("model"),
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
    except Exception as exc:
        return f"Error: vision openai request failed — {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"Error: vision openai HTTP {r.status_code} — {r.text[:200]}"
    try:
        data = r.json()
        _record_usage_safe(data, "chat_completions")
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        return f"Error: vision openai parse — {exc}"


def _dispatch(src: Any, prompt: str, backend: str = "auto", *, max_tokens: int = 1024) -> str:
    raw, mime = _to_image_bytes(src)
    b64 = _to_b64(raw)
    cfg = _pick_config(backend)
    if cfg is None:
        return ("Error: vision: no usable config found. "
                "Add a native_claude or vision-capable native_oai entry to mykey.")
    if str(cfg.get("kind")) == "native_claude":
        return _call_claude(cfg, b64, mime, prompt, max_tokens=max_tokens)
    return _call_openai(cfg, b64, mime, prompt, max_tokens=max_tokens)


# ── public API ────────────────────────────────────────────────────────


def describe_image(src: Any, prompt: str | None = None, *, backend: str = "auto") -> str:
    """Get a free-form description of the image. Returns a string starting
    with ``Error:`` on failure (so the agent can branch on prefix)."""
    return _dispatch(src, prompt or _DEFAULT_DESC_PROMPT, backend)


def ocr_image(src: Any, *, lang: str = "auto", backend: str = "auto") -> str:
    """Best-effort OCR via the same vision endpoint. ``lang`` is a hint
    appended to the prompt (e.g. ``zh-CN``); the model decides what to do
    with it. Returns the extracted text."""
    prompt = _DEFAULT_OCR_PROMPT
    if lang and lang != "auto":
        prompt += f"\n语言提示：{lang}"
    return _dispatch(src, prompt, backend, max_tokens=4096)


def extract_structured(
    src: Any,
    schema: dict[str, Any] | str,
    *,
    prompt: str | None = None,
    backend: str = "auto",
) -> dict[str, Any]:
    """Ask the vision model to output a JSON object matching ``schema``.

    ``schema`` may be a Python dict (treated as a description of fields) or
    a free-text string. Returns the parsed dict, or
    ``{"error": "...", "raw": "..."}`` on failure.
    """
    if isinstance(schema, dict):
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
    else:
        schema_str = str(schema)
    full_prompt = (prompt or "请按以下 JSON schema 抽取图片中的信息：") + "\n\n" + schema_str + "\n\n只返回 JSON，不要任何其他文字、不要代码围栏。"
    raw = _dispatch(src, full_prompt, backend, max_tokens=2048)
    if raw.startswith("Error:"):
        return {"error": raw, "raw": ""}
    text = raw.strip()
    # Strip fenced code blocks if the model added them despite the prompt.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": f"json_decode: {exc}", "raw": text}
