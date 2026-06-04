import glob, json, os, queue as Q, re, socket, sys, threading, time
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
from agentmain import GeneraticAgent
from frontends.chatapp_common import FILE_HINT, format_restore
from frontends.continue_cmd import handle_frontend_command as handle_continue_frontend, reset_conversation
from frontends.feishu_session import build_context, maybe_handle_platform_action, render_prompt
import llmcore
from llmcore import mykeys
from launcher.llm_binding import (
    parse_binding, resolve_to_config_names, synthesize_mixin_entry,
)
from launcher.api_config import list_api_configs
from launcher.profiles import load_profiles

import traceback
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

_TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_AUDIO_EXTS = {".opus", ".mp3", ".wav", ".m4a", ".aac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
_MSG_TYPE_MAP = {"image": "[image]", "audio": "[audio]", "file": "[file]", "media": "[media]", "sticker": "[sticker]"}

TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
MEDIA_DIR = os.path.join(TEMP_DIR, "feishu_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

FEISHU_LOCK_PORT = 19532
_INSTANCE_LOCK = None


def _acquire_single_instance():
    """Hold a localhost lock port for the process lifetime.

    Multiple Feishu long-connection clients can split event delivery across
    old and new processes. When that happens the GUI appears to be running,
    but the active bot may not receive the message the user just sent.
    """
    global _INSTANCE_LOCK
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", FEISHU_LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _INSTANCE_LOCK = sock
    return True

# Lark IM API 上传上限（参见 open.feishu.cn server-docs/im-v1）。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024

# 出站文件沙箱：只允许 PROJECT_ROOT 下的路径，且未命中拒绝列表。
SANDBOX_ROOT = os.path.realpath(PROJECT_ROOT)
SANDBOX_DENY = (
    re.compile(r'(?:^|[\\/])\.git(?:[\\/]|$)'),
    re.compile(r'(?:^|[\\/])\.wlwl-ass(?:[\\/]|$)'),
    re.compile(r'(?:^|[\\/])memory(?:[\\/]|$)'),
    re.compile(r'(?:^|[\\/])\.env$'),
    re.compile(r'launcher_api_configs\.json$'),
    re.compile(r'mykey[^\\/]*\.py$'),
)


def _user_media_dir(open_id):
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', open_id or '')[:64] or 'unknown'
    d = os.path.join(MEDIA_DIR, safe)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_save(open_id, base_filename, data):
    """落盘到 per-open_id 子目录，文件名 timestamp + 净化后的 basename，避免冲突。"""
    name, ext = os.path.splitext(os.path.basename(base_filename or 'file'))
    name = re.sub(r'[^\w.\-]', '_', name)[:80] or 'file'
    ext = re.sub(r'[^\w.]', '', ext)[:16]
    user_dir = _user_media_dir(open_id)
    ts = time.strftime('%Y%m%d_%H%M%S')
    target = os.path.join(user_dir, f"{ts}_{name}{ext}")
    n = 1
    while os.path.exists(target):
        target = os.path.join(user_dir, f"{ts}_{n}_{name}{ext}")
        n += 1
        if n > 99:
            break
    with open(target, "wb") as f:
        f.write(data)
    return target


def _path_in_sandbox(file_path):
    """(ok, reason). ok=True 才允许发出去。"""
    try:
        real = os.path.realpath(file_path)
    except Exception:
        return False, "无法解析路径"
    if not (real == SANDBOX_ROOT or real.startswith(SANDBOX_ROOT + os.sep)):
        return False, "项目目录外"
    needle = real.lower() if os.name == 'nt' else real
    for pat in SANDBOX_DENY:
        if pat.search(needle):
            return False, "命中拒绝列表"
    return True, ""


def _check_size(file_path, max_bytes):
    try:
        size = os.path.getsize(file_path)
    except Exception:
        return False, "无法获取文件大小"
    if size > max_bytes:
        return False, f"文件 {size/1024/1024:.1f}MB 超 Lark 上限 {max_bytes//1024//1024}MB"
    return True, ""


# 飞书 OpenAPI 返回这些 code 是配置/权限层硬错误，重试无意义。
# 99991672: 应用缺少 im:resource[:upload] scope。
_HARD_UPLOAD_CODES = {99991672}


class _UploadHardError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


_hard_error_notified = set()


def _upload_with_retry(uploader, file_path, attempts=2):
    """uploader 返回 truthy 即成功；失败重试，指数退避。硬错误立即抛出，不重试。"""
    for i in range(attempts):
        try:
            r = uploader(file_path)
            if r:
                return r
        except _UploadHardError:
            raise
        except Exception as e:
            print(f"[fs upload] attempt {i+1} error: {e!r}")
        time.sleep(0.5 * (i + 1))
    return None


def _clean(text):
    for pat in _TAG_PATS:
        text = re.sub(pat, "", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or "..."


def _extract_files(text):
    return re.findall(r"\[FILE:([^\]]+)\]", text or "")


def _strip_files(text):
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def _display_text(text):
    return _strip_files(_clean(text)) or "..."


def _to_allowed_set(value):
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _extract_share_card_content(content_json, msg_type):
    parts = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join([p for p in parts if p]).strip() or f"[{msg_type}]"


def _extract_interactive_content(content):
    parts = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts
    title = content.get("title")
    if isinstance(title, dict):
        title_text = title.get("content", "") or title.get("text", "")
        if title_text:
            parts.append(f"title: {title_text}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")
    elements = content.get("elements", [])
    if isinstance(elements, list):
        for row in elements:
            if isinstance(row, dict):
                parts.extend(_extract_element_content(row))
            elif isinstance(row, list):
                for el in row:
                    parts.extend(_extract_element_content(el))
    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))
    header = content.get("header", {})
    if isinstance(header, dict):
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    return [p for p in parts if p]


def _extract_element_content(element):
    parts = []
    if not isinstance(element, dict):
        return parts
    tag = element.get("tag", "")
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str) and text:
            parts.append(text)
        for field in element.get("fields", []) or []:
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    content = field_text.get("content", "") or field_text.get("text", "")
                    if content:
                        parts.append(content)
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            content = text.get("content", "") or text.get("text", "")
            if content:
                parts.append(content)
        url = element.get("url", "") or (element.get("multi_url", {}) or {}).get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = element.get("alt", {})
        if isinstance(alt, dict):
            parts.append(alt.get("content", "[image]") or "[image]")
        else:
            parts.append("[image]")
    for child in element.get("elements", []) or []:
        parts.extend(_extract_element_content(child))
    for col in element.get("columns", []) or []:
        for child in (col.get("elements", []) if isinstance(col, dict) else []):
            parts.extend(_extract_element_content(child))
    return parts


def _extract_post_content(content_json):
    def _parse_block(block):
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if block.get("title"):
            texts.append(block.get("title"))
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and el.get("image_key"):
                    images.append(el["image_key"])
        text = " ".join([t for t in texts if t]).strip()
        return text or None, images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs
    return "", []


APP_ID = str(mykeys.get("fs_app_id", "") or "").strip()
APP_SECRET = str(mykeys.get("fs_app_secret", "") or "").strip()
ALLOWED_USERS = _to_allowed_set(mykeys.get("fs_allowed_users", []))
# Security: only allow public access when the user *explicitly* writes "*" into
# allowed_users. An empty/missing list now means "deny everyone" — historically
# it meant "allow everyone", which combined with the agent's do_code_run made
# any unconfigured Feishu app a remote-code-exec surface.
PUBLIC_ACCESS = "*" in ALLOWED_USERS
if not ALLOWED_USERS:
    print(
        "⚠️  bots.feishu.allowed_users 未配置 —— 默认拒绝所有用户。\n"
        "   单人使用：python -m launcher.config set bots.feishu.allowed_users '[\"ou_yourid\"]'\n"
        "   想公开（高风险，agent 可跑代码）：python -m launcher.config set bots.feishu.allowed_users '[\"*\"]'"
    )
AGENT_TIMEOUT_SEC = 900
SYSTEM_PROMPT = str(mykeys.get("fs_system_prompt", "") or "").strip()
USER_PROMPTS = mykeys.get("fs_user_prompts") or {}
if not isinstance(USER_PROMPTS, dict): USER_PROMPTS = {}
IDLE_TIMEOUT_S = 3600.0
CLEANUP_INTERVAL_S = 300.0


@dataclass
class _AgentSlot:
    agent: object
    thread: threading.Thread
    last_used_ts: float


_agent_slots: dict[str, _AgentSlot] = {}
_agent_lock = threading.Lock()
client, user_tasks = None, {}


# ── Per-bot LLM binding (set via WLWL_BOT_LLM_BINDING env var by
#    launcher.bot_manager.start). Empty = keep current default behaviour.
#    Resolved at module load time so the cost is paid once, not per inbound
#    message.
_BINDING_KIND, _BINDING_NAME = parse_binding(os.environ.get("WLWL_BOT_LLM_BINDING", ""))
_TARGET_LLM_NAME: str | None = None  # what agent.select_llm_by_name() should match


def _apply_binding_once() -> None:
    """Resolve the binding into either a config ``name`` or an injected mixin
    entry in ``llmcore.mykeys``. Idempotent — safe to call multiple times.

    Single-config case: just record the name. ``GeneraticAgent``'s
    ``load_llm_sessions`` already builds a client for every config; we just
    pick which one is active.

    Profile case: synthesize a virtual ``mixin_config_bot_feishu`` entry
    and mutate ``llmcore.mykeys`` so ``load_llm_sessions`` wraps the named
    children into a ``MixinSession``. ``MixinSession.name`` then equals
    ``'|'.join(child.name for child in members)`` (see mixin.py:32), which
    is what ``select_llm_by_name`` will compare against.
    """
    global _TARGET_LLM_NAME
    if not _BINDING_KIND:
        return
    try:
        profiles = load_profiles(PROJECT_ROOT)
        configs = list_api_configs(PROJECT_ROOT)
    except Exception as exc:
        print(f"[fsapp] binding resolve failed: {exc!r} — using default LLM")
        return
    names = resolve_to_config_names(_BINDING_KIND, _BINDING_NAME, profiles, configs)
    if not names:
        return
    if _BINDING_KIND == "config":
        _TARGET_LLM_NAME = names[0]
        print(f"[fsapp] binding config:{_BINDING_NAME!r} → pinned to {_TARGET_LLM_NAME!r}")
        return
    # profile → mixin
    mykeys_key, mixin_cfg = synthesize_mixin_entry("feishu", names)
    # Mutate the module-level mykeys dict. llmcore.mykeys / the `mykeys`
    # imported above / _keys.py's globals all reference the same dict object
    # because of the PEP-562 lazy attribute in llmcore/__init__.py — see
    # llmcore/__init__.py:104 __getattr__.
    llmcore.mykeys[mykeys_key] = mixin_cfg
    _TARGET_LLM_NAME = "|".join(names)
    print(f"[fsapp] binding profile:{_BINDING_NAME!r} → mixin chain {names} (mixin.name={_TARGET_LLM_NAME!r})")


_apply_binding_once()


def _resolve_extra_prompt(open_id, session_ctx=None):
    p = (USER_PROMPTS.get(open_id) or SYSTEM_PROMPT or "").strip()
    if session_ctx is not None:
        return render_prompt(session_ctx, p)
    return f"\n\n# Feishu Persona\n{p}" if p else ""


def _apply_prompt(agent, prompt_text):
    # next_llm 切换 active client，所以一次写入全部 backend 才能稳定生效。
    for c in getattr(agent, "llmclients", []) or []:
        b = getattr(c, "backend", None)
        if b is not None:
            b.extra_sys_prompt = prompt_text


def _get_agent(open_id, session_ctx=None):
    with _agent_lock:
        slot = _agent_slots.get(open_id)
        prompt_text = _resolve_extra_prompt(open_id, session_ctx)
        if slot and slot.thread.is_alive():
            slot.last_used_ts = time.time()
            if session_ctx is not None:
                _apply_prompt(slot.agent, prompt_text)
            return slot.agent
        a = GeneraticAgent()
        if _TARGET_LLM_NAME:
            ok = a.select_llm_by_name(_TARGET_LLM_NAME)
            if not ok:
                print(f"[fsapp] WARN: target LLM {_TARGET_LLM_NAME!r} not in agent clients, using default llm_no={a.llm_no}")
        _apply_prompt(a, prompt_text)
        t = threading.Thread(target=a.run, name=f"fs-agent-{open_id[:8]}", daemon=True)
        t.start()
        _agent_slots[open_id] = _AgentSlot(a, t, time.time())
        return a


def _reap_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        now = time.time()
        with _agent_lock:
            stale = [k for k, s in _agent_slots.items()
                     if now - s.last_used_ts > IDLE_TIMEOUT_S]
            stale_slots = [(k, _agent_slots.pop(k)) for k in stale if k in _agent_slots]
        for k, slot in stale_slots:
            try:
                shutdown = getattr(slot.agent, 'shutdown', None)
                if callable(shutdown): shutdown()
                else: slot.agent.abort()
            except Exception as e:
                print(f"[fs-reaper] shutdown {k} error: {e!r}")
            try:
                slot.thread.join(timeout=5)
            except Exception:
                pass
            if slot.thread.is_alive():
                print(f"[fs-reaper] agent {k} did not exit after shutdown")


threading.Thread(target=_reap_loop, name="fs-reaper", daemon=True).start()


def create_client():
    return lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).log_level(lark.LogLevel.INFO).build()


def _card_raw(elements):
    return json.dumps({
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": elements},
    }, ensure_ascii=False)


def _card(text):
    return _card_raw([{"tag": "markdown", "content": text}])


def _send_raw(receive_id, payload, msg_type, rtype):
    body = CreateMessageRequest.builder().receive_id_type(rtype).request_body(
        CreateMessageRequestBody.builder().receive_id(receive_id).msg_type(msg_type).content(payload).build()
    ).build()
    r = client.im.v1.message.create(body)
    if r.success():
        return r.data.message_id if r.data else None
    print(f"发送失败: {r.code}, {r.msg}")
    return None


def _patch_card(message_id, card_json):
    body = PatchMessageRequest.builder().message_id(message_id).request_body(
        PatchMessageRequestBody.builder().content(card_json).build()
    ).build()
    r = client.im.v1.message.patch(body)
    if not r.success():
        print(f"[ERROR] patch_card 失败: {r.code}, {r.msg}")
    return r.success()


def send_message(receive_id, content, msg_type="text", use_card=False, receive_id_type="open_id"):
    if use_card:
        return _send_raw(receive_id, _card(content), "interactive", receive_id_type)
    if msg_type == "text":
        return _send_raw(receive_id, json.dumps({"text": content}, ensure_ascii=False), "text", receive_id_type)
    return _send_raw(receive_id, content, msg_type, receive_id_type)


def update_message(message_id, content):
    return _patch_card(message_id, _card(content))


def _upload_image_sync(file_path):
    try:
        with open(file_path, "rb") as f:
            request = CreateImageRequest.builder().request_body(
                CreateImageRequestBody.builder().image_type("message").image(f).build()
            ).build()
            response = client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            print(f"[ERROR] upload image failed: {response.code}, {response.msg}")
            if response.code in _HARD_UPLOAD_CODES:
                raise _UploadHardError(response.code, response.msg)
    except _UploadHardError:
        raise
    except Exception as e:
        print(f"[ERROR] upload image failed {file_path}: {e}")
    return None


def _upload_file_sync(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    file_type = _FILE_TYPE_MAP.get(ext, "stream")
    file_name = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            request = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(f).build()
            ).build()
            response = client.im.v1.file.create(request)
            if response.success():
                return response.data.file_key
            print(f"[ERROR] upload file failed: {response.code}, {response.msg}")
            if response.code in _HARD_UPLOAD_CODES:
                raise _UploadHardError(response.code, response.msg)
    except _UploadHardError:
        raise
    except Exception as e:
        print(f"[ERROR] upload file failed {file_path}: {e}")
    return None


def _download_image_sync(message_id, image_key):
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type("image").build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download image failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download image failed {image_key}: {e}")
    return None, None


def _download_file_sync(message_id, file_key, resource_type="file"):
    if resource_type == "audio":
        resource_type = "file"
    try:
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build()
        response = client.im.v1.message_resource.get(request)
        if response.success():
            data = response.file.read() if hasattr(response.file, "read") else response.file
            return data, response.file_name
        print(f"[ERROR] download {resource_type} failed: {response.code}, {response.msg}")
    except Exception as e:
        print(f"[ERROR] download {resource_type} failed {file_key}: {e}")
    return None, None


def _download_and_save_media(msg_type, content_json, message_id, open_id):
    data, filename = None, None
    if msg_type == "image":
        image_key = content_json.get("image_key")
        if image_key and message_id:
            data, filename = _download_image_sync(message_id, image_key)
            if not filename:
                filename = f"{image_key[:16]}.jpg"
    elif msg_type in ("audio", "file", "media"):
        file_key = content_json.get("file_key")
        if file_key and message_id:
            data, filename = _download_file_sync(message_id, file_key, msg_type)
            if not filename:
                filename = file_key[:16]
            if msg_type == "audio" and filename and not filename.endswith(".opus"):
                filename = f"{filename}.opus"
    if data and filename:
        file_path = _safe_save(open_id, filename, data)
        return file_path, os.path.basename(file_path)
    return None, None


def _describe_media(msg_type, file_path, filename):
    if msg_type == "image":
        return f"[image: {filename}]\n[Image: source: {file_path}]"
    if msg_type == "audio":
        return f"[audio: {filename}]\n[File: source: {file_path}]"
    if msg_type in ("file", "media"):
        return f"[{msg_type}: {filename}]\n[File: source: {file_path}]"
    return f"[{msg_type}]\n[File: source: {file_path}]"


def _send_local_file(receive_id, file_path, receive_id_type="open_id"):
    base = os.path.basename(file_path)
    if not os.path.isfile(file_path):
        send_message(receive_id, f"⚠️ 文件不存在: {base}", receive_id_type=receive_id_type)
        return False
    ok, reason = _path_in_sandbox(file_path)
    if not ok:
        print(f"[fs sandbox] 拒绝 [FILE:{file_path}] — {reason}")
        send_message(receive_id, f"⚠️ 拒绝发送（{reason}）: {base}", receive_id_type=receive_id_type)
        return False
    ext = os.path.splitext(file_path)[1].lower()
    is_image = ext in _IMAGE_EXTS
    max_bytes = MAX_IMAGE_BYTES if is_image else MAX_FILE_BYTES
    ok, reason = _check_size(file_path, max_bytes)
    if not ok:
        send_message(receive_id, f"⚠️ {reason}: {base}", receive_id_type=receive_id_type)
        return False
    if is_image:
        try:
            image_key = _upload_with_retry(_upload_image_sync, file_path)
        except _UploadHardError as e:
            _notify_upload_hard_error(receive_id, e, receive_id_type)
            return False
        if image_key:
            send_message(receive_id, json.dumps({"image_key": image_key}, ensure_ascii=False), msg_type="image", receive_id_type=receive_id_type)
            return True
    else:
        try:
            file_key = _upload_with_retry(_upload_file_sync, file_path)
        except _UploadHardError as e:
            _notify_upload_hard_error(receive_id, e, receive_id_type)
            return False
        if file_key:
            msg_type = "media" if ext in _AUDIO_EXTS or ext in _VIDEO_EXTS else "file"
            send_message(receive_id, json.dumps({"file_key": file_key}, ensure_ascii=False), msg_type=msg_type, receive_id_type=receive_id_type)
            return True
    send_message(receive_id, f"⚠️ 上传失败（已重试 2 次）: {base}", receive_id_type=receive_id_type)
    return False


def _notify_upload_hard_error(receive_id, err, receive_id_type):
    """硬错误（权限/配额）每个 receive_id+code 只通知一次，避免刷屏。"""
    key = (receive_id, err.code)
    if key in _hard_error_notified:
        return
    _hard_error_notified.add(key)
    if err.code == 99991672:
        text = "⚠️ 文件上传失败：飞书应用缺少 im:resource:upload 权限。请管理员到开放平台「权限管理」开通后发布新版本。"
    else:
        text = f"⚠️ 文件上传失败（飞书 code {err.code}）：{err.msg}"
    send_message(receive_id, text, receive_id_type=receive_id_type)


def _send_generated_files(receive_id, raw_text, receive_id_type="open_id"):
    for file_path in _extract_files(raw_text):
        _send_local_file(receive_id, file_path, receive_id_type)


def _build_user_message(message, open_id):
    msg_type = message.message_type
    message_id = message.message_id
    content_json = _parse_json(message.content)
    parts, image_paths = [], []
    if msg_type == "text":
        text = str(content_json.get("text", "") or "").strip()
        if text:
            parts.append(text)
    elif msg_type == "post":
        text, image_keys = _extract_post_content(content_json)
        if text:
            parts.append(text)
        for image_key in image_keys:
            file_path, filename = _download_and_save_media("image", {"image_key": image_key}, message_id, open_id)
            if file_path and filename:
                parts.append(_describe_media("image", file_path, filename))
                image_paths.append(file_path)
            else:
                parts.append("[image: download failed]")
    elif msg_type in ("image", "audio", "file", "media"):
        file_path, filename = _download_and_save_media(msg_type, content_json, message_id, open_id)
        if file_path and filename:
            parts.append(_describe_media(msg_type, file_path, filename))
            if msg_type == "image":
                image_paths.append(file_path)
        else:
            parts.append(f"[{msg_type}: download failed]")
    elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
        parts.append(_extract_share_card_content(content_json, msg_type))
    else:
        parts.append(_MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))
    return "\n".join([p for p in parts if p]).strip(), image_paths


def _fmt_tool_call(tc):
    name = tc.get('tool_name', '?')
    args = {k: v for k, v in (tc.get('args') or {}).items() if not k.startswith('_')}
    return f"- `{name}`({json.dumps(args, ensure_ascii=False)[:200]})"


_THINKING_LIMIT = 500
_STRIP_THINKING_RE = re.compile(r"### 💭 Thinking\n.*?(?=### |\Z)", re.DOTALL)

def _strip_thinking(detail):
    """移除详情中的 Thinking 段落（用于旧 step 去重）。"""
    return _STRIP_THINKING_RE.sub("", detail).strip()


def _build_step_detail(resp, tool_calls):
    """从 LLM response + tool_calls 组装单步展开详情（纯函数）。"""
    parts = []
    thinking = (getattr(resp, 'thinking', '') or '').strip() if resp else ''
    if thinking:
        if len(thinking) > _THINKING_LIMIT:
            thinking = thinking[:_THINKING_LIMIT] + f"\n…(已截断,共 {len(thinking)} 字符)"
        parts.append(f"### 💭 Thinking\n{thinking}")
    if tool_calls:
        parts.append("### 🛠 Tool Calls\n" + "\n".join(_fmt_tool_call(tc) for tc in tool_calls))
    content = _display_text((getattr(resp, 'content', '') or '')).strip() if resp else ''
    if content and content != '...':
        parts.append(f"### 📝 Output\n{content}")
    return "\n\n".join(parts)


class _TaskCard:
    """飞书任务卡片：单卡片持续 patch；每步一个独立折叠面板（header 显示 summary，展开看详情）。"""
    _DETAIL_LIMIT = 8000

    def __init__(self, receive_id, rid_type):
        self.rid, self.rtype = receive_id, rid_type
        self.steps = []          # 始终只存当前一步（旧 turn 不留）
        self.turn = 0
        self.status = "🤔 思考中..."
        self.final = None
        self.msg_id = None

    def _step_panel(self, idx, summary, detail):
        detail = detail or "_(无输出)_"
        if len(detail) > self._DETAIL_LIMIT:
            detail = detail[:self._DETAIL_LIMIT] + f"\n\n…(已截断,共 {len(detail)} 字符)"
        return {
            "tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": f"Turn {idx} · {summary}"}},
            "elements": [{"tag": "markdown", "content": detail}],
        }

    def _build(self):
        els = [{"tag": "markdown", "content": f"**{self.status}**"}]
        if self.steps:
            s, d = self.steps[-1]
            els.append(self._step_panel(self.turn, s, d))
        if self.final:
            els += [{"tag": "hr"}, {"tag": "markdown", "content": self.final}]
        return _card_raw(els)

    def _push(self):
        card = self._build()
        if self.msg_id:
            _patch_card(self.msg_id, card)
        else:
            self.msg_id = _send_raw(self.rid, card, "interactive", self.rtype)

    # ── 公开接口 ──

    def start(self):
        self._push()

    def step(self, summary, detail=""):
        self.turn += 1
        self.steps = [(summary, detail)]  # 旧 turn 彻底丢弃，不保留
        self.status = f"⏳ 工作中 · Turn {self.turn}"
        self._push()

    def done(self, text):
        self.status = "✅ 已完成"
        self.final = text or "_(无文本输出)_"
        self._push()

    def fail(self, msg):
        self.status = f"❌ {msg}"
        self._push()


def _make_task_hook(card, done_event, on_final):
    """飞书任务 hook：每轮 patch 卡片状态；结束触发 on_final(raw) 处理附件。"""
    def hook(ctx):
        try:
            if ctx.get('exit_reason'):
                resp = ctx.get('response')
                raw = resp.content if hasattr(resp, 'content') else str(resp)
                card.done(_display_text(raw))
                on_final(raw)
                done_event.set()
            elif ctx.get('summary'):
                detail = _build_step_detail(ctx.get('response'), ctx.get('tool_calls') or [])
                card.step(ctx['summary'], detail)
        except Exception as e:
            print(f"[fs hook] error: {e}")
    return hook


def handle_message(data):
    event, message, sender = data.event, data.event.message, data.event.sender
    open_id = sender.sender_id.open_id
    chat_id = message.chat_id
    if not PUBLIC_ACCESS and open_id not in ALLOWED_USERS:
        print(f"未授权用户: {open_id}")
        return
    user_input, image_paths = _build_user_message(message, open_id)
    if not user_input:
        if chat_id:
            send_message(chat_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}", receive_id_type="chat_id")
        else:
            send_message(open_id, f"⚠️ 暂不支持处理此类飞书消息：{message.message_type}")
        return
    # 默认对消息正文截短 + 脱敏，避免把私聊内容直接落到服务器 stdout（任何 launcher
    # log 都能读到）。设 WLWL_FSAPP_LOG_VERBOSE=1 恢复完整 200 字预览以便 debug。
    if os.environ.get("WLWL_FSAPP_LOG_VERBOSE", "").strip():
        preview = user_input[:200]
    else:
        preview = (user_input[:40] + "…") if len(user_input) > 40 else user_input
    print(f"收到消息 [{open_id}] ({message.message_type}, {len(image_paths)} images): {preview}")
    session_ctx = build_context(
        open_id=open_id,
        chat_id=chat_id,
        message_id=getattr(message, "message_id", ""),
        message_type=getattr(message, "message_type", "text"),
        public_access=PUBLIC_ACCESS,
        attachment_paths=tuple(image_paths),
    )
    if message.message_type == "text" and user_input.startswith("/"):
        return handle_command(open_id, user_input, chat_id)

    receive_id = session_ctx.receive_id
    rid_type = session_ctx.receive_id_type
    if message.message_type == "text":
        handled = maybe_handle_platform_action(
            user_input,
            session_ctx,
            send_text=lambda text: send_message(receive_id, text, receive_id_type=rid_type),
            send_file=lambda path: _send_local_file(receive_id, path, receive_id_type=rid_type),
            base_dir=PROJECT_ROOT,
        )
        if handled:
            return

    agent = _get_agent(open_id, session_ctx)

    def run_agent():
        user_tasks[open_id] = {"running": True}
        done_event = threading.Event()
        hook_key = f"fs_{open_id}"
        card = _TaskCard(receive_id, rid_type)
        card.start()
        on_final = lambda raw: _send_generated_files(receive_id, raw, receive_id_type=rid_type)
        if not hasattr(agent, '_turn_end_hooks'): agent._turn_end_hooks = {}
        agent._turn_end_hooks[hook_key] = _make_task_hook(card, done_event, on_final)
        try:
            agent.put_task(f"{FILE_HINT}\n\n{user_input}", source="feishu", images=image_paths)
            start = time.time()
            while not done_event.wait(timeout=3):
                if not user_tasks.get(open_id, {}).get("running", True):
                    agent.abort()
                    card.fail("已停止")
                    break
                if time.time() - start > AGENT_TIMEOUT_SEC:
                    agent.abort()
                    card.fail("任务超时")
                    break
        except Exception as e:
            traceback.print_exc()
            card.fail(f"错误: {e}")
        finally:
            agent._turn_end_hooks.pop(hook_key, None)
            user_tasks.pop(open_id, None)

    threading.Thread(target=run_agent, daemon=True).start()


def handle_command(open_id, cmd, chat_id=None):
    agent = _get_agent(open_id)

    def _send_cmd_response(content):
        if chat_id:
            send_message(chat_id, content, receive_id_type="chat_id")
        else:
            send_message(open_id, content)

    receive_id = chat_id or open_id
    rid_type = "chat_id" if chat_id else "open_id"

    from frontends import fs_commands

    ctx = fs_commands.CommandContext(
        send_text=_send_cmd_response,
        send_file=lambda path: _send_local_file(receive_id, path, receive_id_type=rid_type),
        base_dir=PROJECT_ROOT,
        mutating_allowed=not PUBLIC_ACCESS,
    )

    def _on_stop():
        # Feishu-side side-effect for /stop: flip the running flag so the
        # long agent thread exits its wait loop. The agent.abort() inside
        # SharedCommandHandler handles cancelling the actual work.
        if open_id in user_tasks:
            user_tasks[open_id]["running"] = False

    public_note = ("注: /run /clip <text> /open 在公开访问 "
                   "(fs_allowed_users=['*']) 下被禁用。")

    handled = fs_commands.dispatch_with_shared(
        cmd, ctx, agent,
        on_stop=_on_stop,
        public_note=public_note,
    )
    if not handled:
        _send_cmd_response(f"未知命令: {cmd}")


def main():
    global client
    if not _acquire_single_instance():
        print("[Feishu] Another instance is already running; exiting.", flush=True)
        return
    if not APP_ID or not APP_SECRET:
        print("错误: 请通过 GUI Bots tab 或 `python -m launcher.config set bots.feishu.app_id ... && python -m launcher.config set bots.feishu.app_secret ...` 配置飞书应用凭据")
        sys.exit(1)
    client = create_client()
    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()
    cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("=" * 50 + "\n飞书 Agent 已启动（长连接模式）\n" + f"App ID: {APP_ID}\n等待消息...\n" + "=" * 50)
    cli.start()


if __name__ == "__main__":
    main()
