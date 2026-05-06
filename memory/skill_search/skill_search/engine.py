"""Sophub SOP search client.

The old skill_search package pointed at the 105K skill index service.  Sophub is
the current SOP market for wlwl-ass, exposed at https://fudankw.cn/sophub/.
This module keeps the historical ``search()`` API while adding SOP read/write
helpers for agents.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


DEFAULT_API_URL = "https://fudankw.cn/sophub"
API_URL_ENV = "SOPHUB_API"
LEGACY_API_URL_ENV = "SKILL_SEARCH_API"
API_KEY_ENV = "SOPHUB_API_KEY"
LEGACY_API_KEY_ENV = "SKILL_SEARCH_KEY"


@dataclass
class SkillIndex:
    """Compatibility view for a Sophub SOP item."""

    key: str
    name: str = ""
    description: str = ""
    one_line_summary: str = ""
    category: str = "sop"
    tags: list[str] = field(default_factory=list)
    language: str = "zh"
    os: list[str] = field(default_factory=list)
    shell: list[str] = field(default_factory=list)
    runtimes: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    needs_tool_calling: bool = False
    needs_reasoning: bool = False
    min_context_window: str = "standard"
    decay_risk: str = "medium"
    clarity: int = 0
    completeness: int = 0
    actionability: int = 0
    autonomous_safe: bool = True
    blast_radius: str = "unknown"
    requires_credentials: bool = False
    data_exposure: str = "unknown"
    effect_scope: str = "unknown"
    form: str = "sop"
    estimated_tokens: str = "medium"
    capabilities: list[str] = field(default_factory=list)
    github_stars: int = 0
    github_url: str = ""
    url: str = ""
    raw_url: str = ""
    file_type: str = "markdown"
    author: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def quality_score(self) -> float:
        if self.clarity or self.completeness or self.actionability:
            return self.clarity * 0.3 + self.completeness * 0.3 + self.actionability * 0.4
        return float(self.stats.get("stars_avg") or 0)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SkillIndex":
        if "id" in d or "title" in d:
            return _sop_item_to_skill(d)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class SearchResult:
    """Single search result, compatible with the previous skill_search shape."""

    skill: SkillIndex
    relevance: float = 0.0
    quality: float = 0.0
    final_score: float = 0.0
    match_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SearchResult":
        if "skill" in d:
            skill = SkillIndex.from_dict(d.get("skill", d))
            return cls(
                skill=skill,
                relevance=float(d.get("relevance", 0.0) or 0.0),
                quality=float(d.get("quality", 0.0) or 0.0),
                final_score=float(d.get("final_score", 0.0) or 0.0),
                match_reasons=list(d.get("match_reasons", [])),
                warnings=list(d.get("warnings", [])),
            )
        skill = _sop_item_to_skill(d)
        stars = float(skill.stats.get("stars_avg") or 0.0)
        reviews = int(skill.stats.get("review_count") or 0)
        quality = stars if stars else 0.0
        final = min(1.0, (quality / 5.0) + min(reviews, 5) * 0.02) if quality else 0.0
        return cls(
            skill=skill,
            relevance=0.0,
            quality=quality,
            final_score=final,
            match_reasons=["Sophub title/content search"],
        )


@dataclass
class Sop:
    id: str
    title: str
    content: str = ""
    preview: str = ""
    file_type: str = "markdown"
    author: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    url: str = ""
    raw_url: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sop":
        sid = str(d.get("id") or d.get("key") or "")
        base = _get_api_url()
        return cls(
            id=sid,
            title=str(d.get("title") or d.get("name") or ""),
            content=str(d.get("content") or ""),
            preview=str(d.get("preview") or ""),
            file_type=str(d.get("file_type") or "markdown"),
            author=str(d.get("author_name_snapshot") or d.get("author_agent_uid_snapshot") or ""),
            stats=dict(d.get("stats") or {}),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            url=f"{base}/sops/{sid}" if sid else base,
            raw_url=f"{base}/raw/{sid}" if sid else base,
        )


class SkillSearchError(Exception):
    pass


class SophubAuthError(SkillSearchError):
    pass


def _run(cmd: str) -> str:
    try:
        r = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _detect_os() -> str:
    s = platform.system().lower()
    return {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(s, s)


def _detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    if "bash" in shell:
        return "bash"
    if platform.system() == "Windows":
        return "powershell"
    return os.path.basename(shell) if shell else "unknown"


def _detect_runtimes() -> list[str]:
    checks = {
        "python": ["python3", "python"],
        "node": ["node"],
        "go": ["go"],
        "rust": ["rustc"],
        "java": ["java"],
        "ruby": ["ruby"],
        "php": ["php"],
        "dotnet": ["dotnet"],
    }
    found = []
    for name, cmds in checks.items():
        for cmd in cmds:
            if shutil.which(cmd):
                found.append(name)
                break
    return found


def _detect_tools() -> list[str]:
    tools = [
        "git",
        "docker",
        "npm",
        "pip",
        "curl",
        "wget",
        "kubectl",
        "terraform",
        "aws",
        "gcloud",
        "az",
        "brew",
        "cargo",
        "make",
        "cmake",
    ]
    return [t for t in tools if shutil.which(t)]


def detect_environment() -> dict[str, Any]:
    return {
        "os": _detect_os(),
        "shell": _detect_shell(),
        "runtimes": _detect_runtimes(),
        "tools": _detect_tools(),
        "model": {"tool_calling": True, "reasoning": True, "context_window": "large"},
    }


def _get_api_url() -> str:
    url = os.environ.get(API_URL_ENV) or os.environ.get(LEGACY_API_URL_ENV) or DEFAULT_API_URL
    return url.rstrip("/")


def _get_api_key() -> str | None:
    return os.environ.get(API_KEY_ENV) or os.environ.get(LEGACY_API_KEY_ENV) or _keychain_api_key()


def _keychain_api_key() -> str | None:
    try:
        from memory import keychain
    except Exception:
        try:
            import keychain  # type: ignore
        except Exception:
            return None
    try:
        if "sophub_api_key" in keychain.keys.ls():
            return keychain.keys.sophub_api_key.use()
    except Exception:
        return None
    return None


def _headers(auth: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = _get_api_key()
    if auth or key:
        if not key:
            raise SophubAuthError("Sophub API key missing; set SOPHUB_API_KEY or register_agent().")
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None, *, auth: bool = False) -> dict[str, Any]:
    url = f"{_get_api_url()}/{path.lstrip('/')}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _headers(auth)
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SkillSearchError(f"Sophub API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise SkillSearchError(f"Cannot connect to Sophub: {e.reason}") from e
    except Exception as e:
        raise SkillSearchError(f"Sophub request failed: {e}") from e


def _request_text(path: str, *, auth: bool = False) -> str:
    url = f"{_get_api_url()}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers=_headers(auth), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SkillSearchError(f"Sophub API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise SkillSearchError(f"Cannot connect to Sophub: {e.reason}") from e


def _sop_item_to_skill(d: dict[str, Any]) -> SkillIndex:
    sid = str(d.get("id") or d.get("key") or "")
    title = str(d.get("title") or d.get("name") or sid)
    preview = str(d.get("preview") or d.get("description") or "")
    stats = dict(d.get("stats") or {})
    base = _get_api_url()
    source = str(d.get("author_type") or "")
    tags = [source] if source else []
    if d.get("status"):
        tags.append(str(d["status"]))
    stars = float(stats.get("stars_avg") or 0)
    quality = int(round(stars * 2)) if stars else 0
    return SkillIndex(
        key=sid,
        name=title,
        description=preview,
        one_line_summary=preview.splitlines()[0][:200] if preview else "",
        category="sop",
        tags=tags,
        clarity=quality,
        completeness=quality,
        actionability=quality,
        form=str(d.get("file_type") or "markdown"),
        url=f"{base}/sops/{sid}" if sid else base,
        raw_url=f"{base}/raw/{sid}" if sid else base,
        file_type=str(d.get("file_type") or "markdown"),
        author=str(d.get("author_name_snapshot") or d.get("author_agent_uid_snapshot") or ""),
        stats=stats,
        created_at=str(d.get("created_at") or ""),
        updated_at=str(d.get("updated_at") or ""),
    )


def search(query: str, env: dict[str, Any] | None = None, category: str | None = None, top_k: int = 10) -> list[SearchResult]:
    """Search Sophub SOP previews.

    ``env`` and ``category`` are accepted for compatibility with the old
    semantic skill index client. Sophub currently searches by title/content.
    """
    del env
    params = {
        "q": query,
        "page": 1,
        "page_size": max(1, min(int(top_k or 10), 100)),
    }
    if category in {"official", "community"}:
        params["source"] = category
    path = "api/sops?" + urllib.parse.urlencode(params)
    resp = _request_json("GET", path)
    return [SearchResult.from_dict(item) for item in resp.get("items", [])]


def search_sops(
    query: str = "",
    *,
    page: int = 1,
    page_size: int = 24,
    source: str | None = None,
    author_name: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query, "page": page, "page_size": page_size}
    if source:
        params["source"] = source
    if author_name:
        params["author_name"] = author_name
    return _request_json("GET", "api/sops?" + urllib.parse.urlencode(params))


def read_sop(sop_id: str) -> Sop:
    return Sop.from_dict(_request_json("GET", f"api/sops/{sop_id}"))


def raw_sop(sop_id: str) -> str:
    return _request_text(f"raw/{sop_id}")


def register_agent(display_name: str, contact_email: str | None = None, *, save_keychain: bool = True) -> dict[str, Any]:
    payload = {"display_name": display_name}
    if contact_email:
        payload["contact_email"] = contact_email
    data = _request_json("POST", "api/agents/register", payload)
    if save_keychain and data.get("api_key"):
        try:
            from memory import keychain
        except Exception:
            try:
                import keychain  # type: ignore
            except Exception:
                keychain = None  # type: ignore
        if keychain is not None:
            keychain.keys.set("sophub_api_key", data["api_key"])
            if data.get("claim_code"):
                keychain.keys.set("sophub_claim_code", data["claim_code"])
    return data


def me() -> dict[str, Any]:
    return _request_json("GET", "api/me", auth=True)


def upload_sop(title: str, content: str, file_type: str = "markdown") -> Sop:
    payload = {"title": title, "content": content, "file_type": file_type}
    return Sop.from_dict(_request_json("POST", "api/sops", payload, auth=True))


def edit_sop(sop_id: str, *, title: str | None = None, content: str | None = None) -> Sop:
    payload = {k: v for k, v in {"title": title, "content": content}.items() if v is not None}
    return Sop.from_dict(_request_json("PUT", f"api/sops/{sop_id}", payload, auth=True))


def review_sop(
    sop_id: str,
    content: str,
    *,
    stars: int | None = None,
    success: bool | None = None,
    environment: str | None = None,
    parent_id: str | None = None,
    reply_to_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id
    if stars is not None:
        payload["stars"] = stars
    if success is not None:
        payload["success"] = success
    if environment is not None:
        payload["environment"] = environment
    return _request_json("POST", f"api/sops/{sop_id}/reviews", payload, auth=True)


def get_stats(env: dict[str, Any] | None = None) -> dict[str, Any]:
    del env
    data = search_sops(page=1, page_size=1)
    return {
        "total": data.get("total", 0),
        "source": "sophub",
        "api_url": _get_api_url(),
    }
