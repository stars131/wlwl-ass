"""
Web Recipe — 应用专属提取脚本系统
受 OpenHuman Recipe 系统启发，为 wlwl-ass 的 web_scan/web_execute_js 提供：
1. 按URL自动匹配应用专属提取规则
2. 每个应用一个 recipe（manifest + extract JS）
3. 与 SOP 融合，可增量扩展

用法:
    from web_recipe import RecipeManager
    mgr = RecipeManager()
    recipe = mgr.match("https://mail.google.com/mail/u/0/")
    if recipe:
        js = recipe.extract_js()  # 拿到注入脚本
        # 通过 web_execute_js 执行
"""

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


# ── Recipe 数据结构 ──

@dataclass
class RecipeField:
    """提取字段定义"""
    name: str                 # 字段名
    selector: str             # CSS 选择器
    attribute: str = "textContent"  # 提取属性 (textContent/href/src/...)
    multiple: bool = False    # 是否提取多个
    transform: str = ""       # 后处理 (trim/number/date/url)


@dataclass
class RecipeAction:
    """可执行动作定义"""
    name: str                 # 动作名 (click_reply/send_message/...)
    selector: str             # 目标 CSS 选择器
    action_type: str = "click"  # click/focus/scroll/input
    value: str = ""           # input 动作的值


@dataclass
class WebRecipe:
    """单个应用的提取配方"""
    id: str                         # 唯一ID (gmail/wechat_web/notion/...)
    name: str                       # 显示名
    url_patterns: list[str]         # URL 匹配正则列表
    fields: list[RecipeField]       # 提取字段
    actions: list[RecipeAction] = field(default_factory=list)
    wait_for: str = ""              # 页面就绪等待的 CSS 选择器
    notes: str = ""                 # 使用提示

    def matches(self, url: str) -> bool:
        """URL 是否匹配此 recipe"""
        for pat in self.url_patterns:
            if re.search(pat, url):
                return True
        return False

    def extract_js(self) -> str:
        """生成提取数据的 JS 代码，供 web_execute_js 注入"""
        field_extracts = []
        for f in self.fields:
            attr = f.attribute
            if f.multiple:
                field_extracts.append(
                    f'  "{f.name}": Array.from(document.querySelectorAll("{f.selector}")).map(el => el.{attr})'
                )
            else:
                field_extracts.append(
                    f'  "{f.name}": document.querySelector("{f.selector}")?.{attr} ?? null'
                )

        lines = [
            "(function() {",
            "  const result = {",
        ]
        # 加 wait_for 检查
        if self.wait_for:
            lines.append(f'  _ready: !!document.querySelector("{self.wait_for}"),')
        lines.append(",\n".join(field_extracts))
        lines.append("  };")
        # 加 transform
        lines.append("  return JSON.stringify(result, null, 2);")
        lines.append("})()")
        return "\n".join(lines)


# ── 内置 Recipes ──

BUILTIN_RECIPES: list[WebRecipe] = [
    # Gmail
    WebRecipe(
        id="gmail",
        name="Gmail",
        url_patterns=[r"mail\.google\.com"],
        fields=[
            RecipeField("current_folder", ".nZ a.ao", "textContent"),
            RecipeField("emails", "tr.zA", "textContent", multiple=True),
            RecipeField("selected_subject", ".hP", "textContent"),
            RecipeField("selected_body", ".a3s", "textContent"),
            RecipeField("send_button", "div[role='button'][aria-label*='Send']", "textContent"),
        ],
        actions=[
            RecipeAction("compose", ".T-I.T-I-KE", "click"),
            RecipeAction("reply", ".aaq", "click"),
            RecipeAction("send", "div[role='button'][aria-label*='Send']", "click"),
        ],
        wait_for=".nZ",
        notes="Gmail 需要 wait_for 加载完成后再提取",
    ),
    # Notion
    WebRecipe(
        id="notion",
        name="Notion",
        url_patterns=[r"notion\.so", r"notion\.site"],
        fields=[
            RecipeField("page_title", ".notion-page-title", "textContent"),
            RecipeField("blocks", ".notion-text-block", "textContent", multiple=True),
            RecipeField("sidebar", ".notion-sidebar", "textContent"),
        ],
        actions=[
            RecipeAction("new_page", ".notion-sidebar-new-page", "click"),
        ],
        wait_for=".notion-page-title",
        notes="Notion 是 SPA，需要等待渲染",
    ),
    # GitHub
    WebRecipe(
        id="github",
        name="GitHub",
        url_patterns=[r"github\.com"],
        fields=[
            RecipeField("repo_name", "strong[itemprop='name'] a", "textContent"),
            RecipeField("readme", "#readme", "textContent"),
            RecipeField("file_tree", ".js-navigation-item", "textContent", multiple=True),
            RecipeField("issues", ".js-issue-row", "textContent", multiple=True),
            RecipeField("pr_title", ".gh-header-title", "textContent"),
        ],
        actions=[
            RecipeAction("create_issue", "[data-hotkey='c']", "click"),
        ],
        wait_for="main",
    ),
    # ChatGPT
    WebRecipe(
        id="chatgpt",
        name="ChatGPT",
        url_patterns=[r"chat\.openai\.com", r"chatgpt\.com"],
        fields=[
            RecipeField("input_box", "#prompt-textarea, #chat-input", "textContent"),
            RecipeField("messages", "[data-message-author-role]", "textContent", multiple=True),
            RecipeField("last_response", "[data-message-author-role='assistant']:last-child", "textContent"),
        ],
        actions=[
            RecipeAction("send", "[data-testid='send-button']", "click"),
        ],
        wait_for="#prompt-textarea, #chat-input",
    ),
    # Twitter/X
    WebRecipe(
        id="twitter",
        name="Twitter/X",
        url_patterns=[r"(twitter\.com|x\.com)"],
        fields=[
            RecipeField("timeline", "[data-testid='tweetText']", "textContent", multiple=True),
            RecipeField("compose", "[data-testid='tweetTextarea_0']", "textContent"),
        ],
        actions=[
            RecipeAction("tweet", "[data-testid='tweetButtonInline']", "click"),
        ],
        wait_for="[data-testid='tweetText']",
    ),
    # Claude
    WebRecipe(
        id="claude",
        name="Claude",
        url_patterns=[r"claude\.ai"],
        fields=[
            RecipeField("input_box", "div[contenteditable='true']", "textContent"),
            RecipeField("messages", ".font-user-message, .font-claude-message", "textContent", multiple=True),
        ],
        actions=[
            RecipeAction("send", "button[aria-label='Send Message']", "click"),
        ],
        wait_for="div[contenteditable='true']",
    ),
]


# ── Recipe Manager ──

class RecipeManager:
    """管理所有 recipes，支持自动匹配和增量扩展"""

    def __init__(self, extra_dir: Optional[str] = None):
        self.recipes: list[WebRecipe] = list(BUILTIN_RECIPES)
        self.extra_dir = extra_dir
        if extra_dir and os.path.isdir(extra_dir):
            self._load_extras()

    def match(self, url: str) -> Optional[WebRecipe]:
        """根据 URL 自动匹配 recipe"""
        for recipe in self.recipes:
            if recipe.matches(url):
                return recipe
        return None

    def get(self, recipe_id: str) -> Optional[WebRecipe]:
        """按 ID 获取 recipe"""
        for r in self.recipes:
            if r.id == recipe_id:
                return r
        return None

    def add(self, recipe: WebRecipe):
        """添加自定义 recipe"""
        # 如果同 ID 已存在，替换
        self.recipes = [r for r in self.recipes if r.id != recipe.id]
        self.recipes.append(recipe)
        if self.extra_dir:
            self._save_recipe(recipe)

    def list_all(self) -> list[dict]:
        """列出所有 recipes 的摘要"""
        return [
            {"id": r.id, "name": r.name, "patterns": r.url_patterns, "fields": [f.name for f in r.fields]}
            for r in self.recipes
        ]

    def generate_scan_js(self, url: str) -> Optional[str]:
        """便捷方法：给定 URL，返回提取 JS（无匹配返回 None）"""
        recipe = self.match(url)
        if recipe:
            return recipe.extract_js()
        return None

    # ── 持久化 ──

    def _save_recipe(self, recipe: WebRecipe):
        os.makedirs(self.extra_dir, exist_ok=True)
        path = os.path.join(self.extra_dir, f"{recipe.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(recipe), f, ensure_ascii=False, indent=2)

    def _load_extras(self):
        for fname in os.listdir(self.extra_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.extra_dir, fname)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            fields = [RecipeField(**fd) for fd in data.get("fields", [])]
            actions = [RecipeAction(**ad) for ad in data.get("actions", [])]
            recipe = WebRecipe(
                id=data["id"], name=data["name"],
                url_patterns=data["url_patterns"],
                fields=fields, actions=actions,
                wait_for=data.get("wait_for", ""),
                notes=data.get("notes", ""),
            )
            self.add(recipe)