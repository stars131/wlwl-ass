"""Image generation tool (#16): thin wrapper around OpenAI-compatible
``/images/generations`` endpoints.

Usage:

    >>> from tools.image_generation import generate_image
    >>> path = generate_image("a watercolor of a fox in fog, soft light",
    ...                       output_path="temp/foxes/1.png")

By default, picks the first ``native_oai`` config in mykey whose entry has
``image_capable: True`` (explicit opt-in) — falls back to looking for known
host hints (``api.openai.com`` etc).

We support both the OpenAI shape (``data[0].b64_json`` or ``data[0].url``)
and the response-as-binary shape (some self-hosted SD wrappers). Failures
return ``"Error: ..."`` strings.
"""
from __future__ import annotations

import base64
import os
from typing import Any

_IMAGE_HOST_HINTS = ("api.openai.com", "stability", "fal.ai", "replicate", "openai")


def _pick_image_config() -> dict[str, Any] | None:
    """Pick an image-capable config from the launcher API list.

    Profile filtering was removed alongside ``mykey.py`` retirement —
    every saved config is now considered. Mark the desired entry with
    ``image_capable: True`` to make it the explicit pick.
    """
    try:
        from launcher.api_config import load_api_configs
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        configs = load_api_configs(base)
    except Exception:
        return None
    for c in configs:
        if c.get("image_capable"):
            return c
    for c in configs:
        if str(c.get("kind")) != "native_oai":
            continue
        base_url = str(c.get("apibase") or "").lower()
        if any(h in base_url for h in _IMAGE_HOST_HINTS):
            return c
    return None


def generate_image(
    prompt: str,
    output_path: str,
    *,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str = "standard",
    n: int = 1,
    extra: dict[str, Any] | None = None,
) -> str:
    """Generate an image and write it to ``output_path``. Returns the path
    on success, ``"Error: ..."`` otherwise.

    If ``n > 1``, the function writes ``output_path`` and additional sibling
    files numbered ``output_path-2.ext``, ``-3.ext``, … and returns the
    primary path. The full list is in the returned string only if you ask
    for it (TODO: structured return)."""
    if not prompt or not str(prompt).strip():
        return "Error: prompt is empty"
    cfg = _pick_image_config()
    if cfg is None:
        return ("Error: no image-capable native_oai config in mykey. "
                "Set ``image_capable: True`` on the desired entry.")
    import requests

    url = (cfg.get("apibase") or "").rstrip("/") + "/images/generations"
    headers = {
        "Authorization": f"Bearer {cfg.get('apikey', '')}",
        "content-type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model or cfg.get("image_model") or cfg.get("model") or "dall-e-3",
        "prompt": prompt,
        "size": size,
        "n": n,
    }
    if quality:
        payload["quality"] = quality
    if extra:
        payload.update(extra)
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except Exception as exc:
        return f"Error: image request failed — {type(exc).__name__}: {exc}"
    if r.status_code >= 400:
        return f"Error: image HTTP {r.status_code} — {r.text[:200]}"

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        data = r.json()
    except Exception:
        # Some self-hosted SD wrappers return raw bytes — treat as one image.
        with open(output_path, "wb") as f:
            f.write(r.content)
        return output_path
    items = data.get("data") or []
    if not items:
        return f"Error: image: no data returned. Body: {str(data)[:200]}"

    # Write each item; numbered siblings if n > 1.
    written: list[str] = []
    for i, item in enumerate(items):
        target = output_path if i == 0 else _numbered_sibling(output_path, i + 1)
        ok, err = _write_image_item(item, target)
        if not ok:
            return err
        written.append(target)
    return written[0]


def _numbered_sibling(path: str, n: int) -> str:
    """``foo.png`` + 2 → ``foo-2.png``"""
    root, ext = os.path.splitext(path)
    return f"{root}-{n}{ext}"


def _write_image_item(item: dict[str, Any], target: str) -> tuple[bool, str]:
    """OpenAI returns either ``b64_json`` or ``url``; handle both."""
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            with open(target, "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception as exc:
            return False, f"Error: image: b64 decode/write failed — {exc}"
        return True, target
    url = item.get("url")
    if isinstance(url, str) and url:
        import requests
        try:
            r = requests.get(url, timeout=120)
        except Exception as exc:
            return False, f"Error: image: fetch failed — {exc}"
        if r.status_code >= 400:
            return False, f"Error: image: fetch HTTP {r.status_code}"
        with open(target, "wb") as f:
            f.write(r.content)
        return True, target
    return False, "Error: image: response item has neither b64_json nor url"
