"""Feishu / Lark outbound messaging via Open API.

Sends text/card messages to a given user identified by username or open_id.
Maintains a local open_id <-> display-name mapping cache.

Prerequisites:
  * lark-oapi already installed
  * fs_app_id / fs_app_secret set in mykey
"""
from __future__ import annotations

import json
import os
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_MAP_PATH = os.path.join(PROJECT_ROOT, "temp", "feishu_user_map.json")


def _load_user_map() -> dict[str, dict[str, str]]:
    if not os.path.exists(USER_MAP_PATH):
        return {}
    try:
        with open(USER_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_user_map(data: dict) -> None:
    os.makedirs(os.path.dirname(USER_MAP_PATH), exist_ok=True)
    with open(USER_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_client():
    import sys
    sys.path.insert(0, PROJECT_ROOT)
    from llmcore import mykeys
    import lark_oapi as lark

    app_id = str(mykeys.get("fs_app_id", "")).strip()
    app_secret = str(mykeys.get("fs_app_secret", "")).strip()
    if not app_id or not app_secret:
        raise RuntimeError("feishu credentials missing: set fs_app_id and fs_app_secret in mykey")
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.WARNING).build()


def _resolve_open_id(name: str, client: Any) -> tuple[str | None, str | None]:
    import sys
    sys.path.insert(0, PROJECT_ROOT)
    from llmcore import mykeys

    name = name.strip()

    # 1. Already an open_id?
    if name.startswith("ou_"):
        cache = _load_user_map()
        info = cache.get(name, {})
        return name, info.get("name", name)

    # 2. Local cache by name
    cache = _load_user_map()
    for oid, info in cache.items():
        if info.get("name", "").lower() == name.lower():
            return oid, info.get("name", name)

    # 3. Check mykey allowed_users / allowed_friends
    for config_key in ("allowed_users", "allowed_friends"):
        raw = mykeys.get(config_key)
        if raw:
            try:
                entries = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(entries, list):
                    for entry in entries:
                        oid = ""
                        display = ""
                        if isinstance(entry, dict):
                            oid = entry.get("open_id", "")
                            display = entry.get("name", "") or entry.get("display_name", "")
                        elif isinstance(entry, str) and entry.startswith("ou_"):
                            oid = entry
                        if oid and display.lower() == name.lower():
                            return oid, display
            except (json.JSONDecodeError, TypeError):
                pass

    return None, None


def _query_user_info(open_id: str, client: Any) -> dict | None:
    import lark_oapi as lark
    req = lark.contact.v3.user.GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    try:
        resp = client.contact.v3.user.get(req)
        if resp.success() and resp.data and resp.data.user:
            u = resp.data.user
            return {
                "name": u.name or "",
                "en_name": u.en_name or "",
                "nickname": u.nick_name or "",
                "avatar": u.avatar_url or "",
            }
    except Exception:
        pass
    return None


def feishu_send(to: str, text: str, *, files: list[str] | None = None) -> str:
    to = (to or "").strip()
    text = (text or "").strip()
    if not to or not text:
        return "[feishu_send error] to + text required"

    try:
        client = _get_client()
    except Exception as exc:
        return f"[feishu_send error] client init failed: {exc}"

    open_id, display_name = _resolve_open_id(to, client)
    if not open_id:
        return (f"[feishu_send error] user '{to}' not found. "
                f"Use open_id (ou_xxx) directly, or run feishu_refresh_users() first.")

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    content = json.dumps({"text": text}, ensure_ascii=False)
    body = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(open_id).msg_type("text").content(content).build()
        ).build()

    try:
        r = client.im.v1.message.create(body)
        if not r.success():
            return f"[feishu_send error] send failed: code={r.code}, msg={r.msg}"
    except Exception as exc:
        return f"[feishu_send error] exception: {exc}"

    sent_files: list[str] = []
    if files:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        for path in files:
            if not isinstance(path, str) or not path.strip():
                continue
            abspath = os.path.abspath(path)
            if not os.path.exists(abspath):
                return f"[feishu_send partial] text sent to {display_name}, file missing: {abspath}"
            try:
                with open(abspath, "rb") as f:
                    file_req = CreateFileRequest.builder().request_body(
                        CreateFileRequestBody.builder()
                        .file_type("stream").file(f)
                        .file_name(os.path.basename(abspath)).build()
                    ).build()
                file_resp = client.im.v1.file.create(file_req)
                if file_resp.success():
                    file_content = json.dumps({"file_key": file_resp.data.file_key}, ensure_ascii=False)
                    file_body = CreateMessageRequest.builder() \
                        .receive_id_type("open_id") \
                        .request_body(
                            CreateMessageRequestBody.builder()
                            .receive_id(open_id).msg_type("file").content(file_content).build()
                        ).build()
                    client.im.v1.message.create(file_body)
                    sent_files.append(abspath)
                else:
                    return f"[feishu_send partial] text sent, file upload failed: {abspath} code={file_resp.code}"
            except Exception as exc:
                return f"[feishu_send partial] text sent, file error {abspath}: {exc}"

    suffix = f" + {len(sent_files)} file(s)" if sent_files else ""
    return f"[feishu_send] -> {display_name} ({len(text)} chars{suffix})"


def feishu_refresh_users() -> str:
    try:
        client = _get_client()
    except Exception as exc:
        return f"[feishu_refresh error] client init failed: {exc}"

    import sys
    sys.path.insert(0, PROJECT_ROOT)
    from llmcore import mykeys

    cache = _load_user_map()
    new_count = 0

    for config_key in ("allowed_users", "allowed_friends"):
        raw = mykeys.get(config_key)
        if not raw:
            continue
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(entries, list):
                for entry in entries:
                    oid = ""
                    if isinstance(entry, dict):
                        oid = entry.get("open_id", "")
                    elif isinstance(entry, str) and entry.startswith("ou_"):
                        oid = entry
                    if oid and oid not in cache:
                        info = _query_user_info(oid, client)
                        if info:
                            cache[oid] = info
                        else:
                            cache[oid] = {"name": f"user_{oid[:8]}"}
                        new_count += 1
        except (json.JSONDecodeError, TypeError):
            pass

    _save_user_map(cache)
    return f"[feishu_refresh] {len(cache)} users cached ({new_count} new)"


def feishu_user_lookup(name: str) -> str:
    try:
        client = _get_client()
    except Exception as exc:
        return json.dumps({"error": f"client init failed: {exc}"}, ensure_ascii=False)

    oid, display = _resolve_open_id(name, client)
    if not oid:
        return json.dumps({"error": f"user not found: {name}"}, ensure_ascii=False)

    cache = _load_user_map()
    info = cache.get(oid, {})
    return json.dumps({
        "open_id": oid,
        "name": display or info.get("name", oid),
        "en_name": info.get("en_name", ""),
        "nickname": info.get("nickname", ""),
        "avatar": info.get("avatar", ""),
    }, ensure_ascii=False, indent=2)
