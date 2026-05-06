"""Voice tools (#14): transcription + TTS thin wrappers.

Per the doc this item was flagged "建议跳过" — heavy deps, narrow utility
unless we go mobile/car. We still provide a *thin* wrapper so the agent can
opt into voice without hand-rolling each call:

  * ``transcribe(audio_path, *, model='whisper-1', backend='auto') -> str``
    Uses an OpenAI-compatible /audio/transcriptions endpoint. Works with
    any compatible provider configured in mykey (OpenAI, ModelScope-with-
    whisper, local groq/openai-clone, etc).
  * ``tts(text, output_path, *, voice='alloy', model='tts-1') -> str``
    Calls /audio/speech and writes the binary blob (mp3/wav/opus depending
    on response). Returns the actual output path.
  * ``tts_edge(text, output_path, voice='zh-CN-XiaoxiaoNeural') -> str``
    Optional path that uses ``edge-tts`` if installed (free, no API key).
    Falls back to a clear error if the package is missing.

We pick credentials by scanning ``mykey``-loaded api_configs for the first
``native_oai`` config whose apibase looks audio-capable (matches OpenAI or
explicitly opt-in with ``audio_capable: True``).
"""
from __future__ import annotations

import os
from typing import Any

_AUDIO_HOST_HINTS = ("api.openai.com", "groq.com", "deepinfra.com", "azure.com", "openai")


def _pick_audio_config() -> dict[str, Any] | None:
    """Pick an audio-capable config from the launcher API list.

    Profile filtering was removed alongside ``mykey.py`` retirement —
    every saved config is now considered. Mark the desired entry with
    ``audio_capable: True`` to make it the explicit pick.
    """
    try:
        from launcher.api_config import load_api_configs
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        configs = load_api_configs(base)
    except Exception:
        return None
    # Prefer an explicit opt-in flag, then the host hint.
    for c in configs:
        if c.get("audio_capable"):
            return c
    for c in configs:
        if str(c.get("kind")) != "native_oai":
            continue
        base_url = str(c.get("apibase") or "").lower()
        if any(h in base_url for h in _AUDIO_HOST_HINTS):
            return c
    return None


def transcribe(
    audio_path: str,
    *,
    model: str = "whisper-1",
    backend: str = "auto",
    language: str | None = None,
    prompt: str | None = None,
) -> str:
    """Send the file at ``audio_path`` to /audio/transcriptions. Returns the
    transcription text or ``"Error: ..."``.

    ``backend`` is reserved — only ``auto`` (use mykey config) is implemented
    today; the parameter exists so future Modelscope / Whisper-local paths
    can be plugged in without changing callers.
    """
    if not os.path.isfile(audio_path):
        return f"Error: audio file not found at {audio_path}"
    cfg = _pick_audio_config()
    if cfg is None:
        return ("Error: no audio-capable native_oai config in mykey. "
                "Set ``audio_capable: True`` on the desired entry.")
    import requests

    url = (cfg.get("apibase") or "").rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {cfg.get('apikey', '')}"}
    files = {"file": (os.path.basename(audio_path), open(audio_path, "rb"))}
    data: dict[str, Any] = {"model": model}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    try:
        try:
            r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
        finally:
            files["file"][1].close()
    except Exception as exc:
        return f"Error: transcribe request failed — {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"Error: transcribe HTTP {r.status_code} — {r.text[:200]}"
    try:
        data = r.json()
        return str(data.get("text") or "")
    except Exception as exc:
        return f"Error: transcribe parse — {exc}"


def tts(
    text: str,
    output_path: str,
    *,
    voice: str = "alloy",
    model: str = "tts-1",
    response_format: str = "mp3",
) -> str:
    """Call /audio/speech with the given text. Writes the binary response to
    ``output_path``. Returns the output path on success or
    ``"Error: ..."`` on failure (note: still a string, not an exception)."""
    if not text or not str(text).strip():
        return "Error: tts: text is empty"
    cfg = _pick_audio_config()
    if cfg is None:
        return ("Error: no audio-capable native_oai config in mykey.")
    import requests

    url = (cfg.get("apibase") or "").rstrip("/") + "/audio/speech"
    headers = {
        "Authorization": f"Bearer {cfg.get('apikey', '')}",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": response_format,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
    except Exception as exc:
        return f"Error: tts request failed — {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"Error: tts HTTP {r.status_code} — {r.text[:200]}"
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(r.content)
    return output_path


def tts_edge(
    text: str,
    output_path: str,
    *,
    voice: str = "zh-CN-XiaoxiaoNeural",
) -> str:
    """Free TTS via the optional ``edge-tts`` package (no API key needed).

    Returns the output path on success, ``"Error: ..."`` otherwise. Soft
    dependency — if ``edge-tts`` isn't installed we just say so."""
    try:
        import edge_tts  # type: ignore
    except ImportError:
        return ("Error: edge-tts not installed. Install with `pip install edge-tts` "
                "if you want free local TTS.")
    import asyncio

    async def _run() -> None:
        comm = edge_tts.Communicate(text, voice)
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        await comm.save(output_path)

    try:
        asyncio.run(_run())
    except Exception as exc:
        return f"Error: edge-tts failed — {type(exc).__name__}: {exc}"
    return output_path
