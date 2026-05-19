"""
Memory Namespace — 记忆命名空间 + 实体索引
受 OpenHuman Memory Tree 启发，为 wlwl-ass 扁平记忆系统增加：
1. Namespace 隔离（user_pref / env_fact / skill_cache / ...）
2. 实体标准化（同一概念跨 SOP/L4 关联）
3. 遗忘标记（过期而非删除）

用法:
    from memory_namespace import MemoryStore
    store = MemoryStore()
    store.remember("微信", "user_pref", "用户偏好用微信发消息")
    store.remember("微信", "skill_cache", "wechat_batch SOP 缓存")
    results = store.recall("微信")  # 跨 namespace 检索
    store.forget("微信", "skill_cache")  # 标记遗忘
"""

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


# ── 命名空间常量 ──
NS_USER_PREF = "user_pref"       # 用户偏好
NS_ENV_FACT = "env_fact"         # 环境事实
NS_SKILL_CACHE = "skill_cache"   # 技能缓存
NS_TOOL_RESULT = "tool_result"  # 工具结果缓存
NS_GLOBAL = "global"            # 全局默认

ALL_NAMESPACES = [NS_GLOBAL, NS_USER_PREF, NS_ENV_FACT, NS_SKILL_CACHE, NS_TOOL_RESULT]

# ── 实体 ID 标准化 ──

_ENTITY_ALIASES: dict[str, str] = {
    # 应用别名 → 标准实体 ID
    "wechat": "app:wechat", "微信": "app:wechat", "WeChat": "app:wechat",
    "gmail": "app:gmail", "谷歌邮箱": "app:gmail",
    "notion": "app:notion", "telegram": "app:telegram", "tg": "app:telegram",
    "qq": "app:qq", "飞书": "app:feishu", "feishu": "app:feishu",
    "钉钉": "app:dingtalk", "dingtalk": "app:dingtalk",
    "git": "tool:git", "github": "tool:github",
    "adb": "tool:adb", "ocr": "tool:ocr",
    # 概念别名
    "记忆": "concept:memory", "memory": "concept:memory",
    "安全": "concept:security", "security": "concept:security",
    "浏览器": "concept:browser", "browser": "concept:browser",
}


def normalize_entity(name: str) -> str:
    """将任意名称标准化为实体 ID"""
    name = name.strip()
    if ":" in name:
        return name  # 已经是标准格式
    return _ENTITY_ALIASES.get(name.lower(), f"entity:{name.lower()}")


# ── 数据结构 ──

@dataclass
class MemoryEntry:
    entity_id: str
    namespace: str
    content: str
    source: str = ""          # 来源 (SOP名/session ID)
    created_at: float = 0.0
    forgotten: bool = False
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class EntityIndex:
    """实体索引：记录一个实体出现在哪些 namespace 和来源中"""
    canonical_id: str
    aliases: list[str] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    last_seen: float = 0.0


# ── 存储引擎 ──

class MemoryStore:
    """带命名空间和实体索引的记忆存储"""

    def __init__(self, store_path: Optional[str] = None):
        self.entries: list[MemoryEntry] = []
        self.entity_index: dict[str, EntityIndex] = {}
        self.store_path = store_path
        if store_path and os.path.exists(store_path):
            self._load()

    # ── 写入 ──

    def remember(self, entity_name: str, namespace: str, content: str,
                 source: str = "", tags: Optional[list[str]] = None) -> MemoryEntry:
        """写入一条记忆"""
        entity_id = normalize_entity(entity_name)
        entry = MemoryEntry(
            entity_id=entity_id,
            namespace=namespace,
            content=content,
            source=source,
            tags=tags or [],
        )
        self.entries.append(entry)

        # 更新实体索引
        if entity_id not in self.entity_index:
            self.entity_index[entity_id] = EntityIndex(
                canonical_id=entity_id,
                aliases=[entity_name] if entity_name != entity_id else [],
            )
        idx = self.entity_index[entity_id]
        if entity_name not in idx.aliases and entity_name != entity_id:
            idx.aliases.append(entity_name)
        if namespace not in idx.namespaces:
            idx.namespaces.append(namespace)
        idx.last_seen = time.time()

        self._autosave()
        return entry

    # ── 检索 ──

    def recall(self, query: str, namespace: Optional[str] = None,
               include_forgotten: bool = False, limit: int = 20) -> list[MemoryEntry]:
        """检索记忆，支持跨 namespace"""
        entity_id = normalize_entity(query)
        results = []
        for entry in self.entries:
            if entry.forgotten and not include_forgotten:
                continue
            if namespace and entry.namespace != namespace:
                continue
            # 匹配：实体ID精确匹配 or 内容模糊匹配
            if entry.entity_id == entity_id:
                results.append(entry)
            elif query.lower() in entry.content.lower():
                results.append(entry)
            elif entry.entity_id in query.lower() or query.lower() in entry.entity_id:
                results.append(entry)
        results.sort(key=lambda e: e.created_at, reverse=True)
        return results[:limit]

    def recall_by_namespace(self, namespace: str) -> list[MemoryEntry]:
        """获取某个命名空间的全部记忆"""
        return [e for e in self.entries if e.namespace == namespace and not e.forgotten]

    def search_entities(self, pattern: str) -> list[EntityIndex]:
        """搜索实体索引"""
        pat = pattern.lower()
        return [
            idx for idx in self.entity_index.values()
            if pat in idx.canonical_id.lower()
            or any(pat in a.lower() for a in idx.aliases)
        ]

    # ── 遗忘 ──

    def forget(self, entity_name: str, namespace: Optional[str] = None) -> int:
        """标记遗忘（不删除，只标记 forgotten=True）"""
        entity_id = normalize_entity(entity_name)
        count = 0
        for entry in self.entries:
            if entry.entity_id != entity_id:
                continue
            if namespace and entry.namespace != namespace:
                continue
            if not entry.forgotten:
                entry.forgotten = True
                count += 1
        self._autosave()
        return count

    def purge_forgotten(self) -> int:
        """真正删除已遗忘的条目"""
        before = len(self.entries)
        self.entries = [e for e in self.entries if not e.forgotten]
        self._autosave()
        return before - len(self.entries)

    # ── 持久化 ──

    def _autosave(self):
        if not self.store_path:
            return
        self._save()

    def _save(self):
        data = {
            "entries": [asdict(e) for e in self.entries],
            "entity_index": {k: asdict(v) for k, v in self.entity_index.items()},
        }
        os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self):
        with open(self.store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.entries = [MemoryEntry(**e) for e in data.get("entries", [])]
        for k, v in data.get("entity_index", {}).items():
            self.entity_index[k] = EntityIndex(**v)

    # ── 导出为 wlwl-ass L2/L3 格式 ──

    def export_to_insight(self) -> str:
        """导出为 L1 Insight 索引格式"""
        lines = []
        for entity_id, idx in sorted(self.entity_index.items()):
            active_ns = [
                ns for ns in idx.namespaces
                if any(e.entity_id == entity_id and e.namespace == ns and not e.forgotten
                       for e in self.entries)
            ]
            if active_ns:
                aliases_str = ",".join(idx.aliases[:3])
                lines.append(f"{entity_id}({aliases_str}): {'/'.join(active_ns)}")
        return "\n".join(lines)