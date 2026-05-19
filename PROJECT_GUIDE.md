# wlwl-ass · AI 协作开发指南

> 这份文档是为**和 AI 一起在这个项目上加功能/改代码**而准备的入口指南。
>
> 用户视角的"这是什么 / 怎么装 / 怎么用"看 [`README.md`](./README.md)、[`项目介绍.md`](./项目介绍.md)、[`QUICKSTART.md`](./QUICKSTART.md)。
> 这份文档只回答一个问题：**"我要在这个项目里加一个新功能，从哪里下手？"**
>
> 想看每个目录/文件/函数详细说明，配套读 [`CODE_MAP.md`](./CODE_MAP.md)。

---

## 1. 这个项目本质上是什么

一句话：**给任意 LLM 装一个标准化的"动手层"——9 个原子工具 + ~100 行主循环 + 分层记忆，让 LLM 能真的操作本机（执行代码、读写文件、控浏览器、问用户）**。

它有两层：

```
┌───────────────────────────────────────────────────────────────┐
│  外层：用户界面层                                              │
│   ├─ Tauri+React 桌面 GUI (gui/)         ← 主要入口            │
│   ├─ wlwl 命令行 REPL (launcher/cli_repl.py)                  │
│   └─ IM Bot 前端 (frontends/fsapp.py 等 6 个)                  │
├───────────────────────────────────────────────────────────────┤
│  内层：Agent 内核                                              │
│   ├─ agent_loop.py  ← 通用循环（任何 Handler 都跑这个）         │
│   ├─ wlwl_ass.py    ← WlwlAssHandler：9 个工具的实现           │
│   ├─ llmcore/       ← 多 provider LLM 客户端 + kernel/workers │
│   ├─ memory/        ← L0–L4 分层记忆（运行时会扩展）           │
│   └─ tools/         ← 高级工具（浏览器、视觉、MoA、MCP…）       │
└───────────────────────────────────────────────────────────────┘
```

**关键性质**：
- Agent Loop 是 **provider 无关**的——同一份 `agent_runner_loop` 既能跑 Claude、又能跑 OpenAI、还能跑 DeepSeek/Kimi/GLM。切 LLM 不用动 loop。
- Handler（`WlwlAssHandler`）持有所有 `do_<tool_name>` 方法。**加新工具 = 在 Handler 上加一个方法 + 在 `assets/tools_schema.json` 加一条 schema**。
- 记忆是 **文件系统**（`memory/*.md` + `memory/*.py`），不是数据库。Agent 自己读写文件来"学习"。
- 上下文窗口故意压在 **<30K**——靠分层记忆按需召回，不是一次性塞满 prompt。

---

## 2. 30 秒跑起来

```bash
# 1) 装 Python 依赖（3.10–3.13，别用 3.14）
pip install -e ".[ui]"

# 2) Windows 一键
start_from_zero.cmd      # 同：一键启动.cmd

# 或手动起 GUI
python launch.pyw

# 或纯 CLI（不要 GUI）
pip install -e .
wlwl                     # 进 REPL
wlwl "列出当前目录的 python 文件"   # 一次性
```

**第一次起来是空窗口**——进 GUI 的 **API 配置** tab，填一把 API key（任意一家：Claude/OpenAI/DeepSeek/Kimi/GLM/MiniMax/OpenRouter…）就能用。

CLI 和 GUI **共享同一份配置** (`temp/launcher_api_configs.json` + `~/.wlwl-ass/config.json`)。

---

## 3. AI 协作开发：典型修改的"入手点"清单

下面这张表是这份文档的核心。把它读熟，绝大部分功能改动你能立刻定位到要改哪里。

| 我想做什么 | 改哪里 | 配套要碰的 |
|---|---|---|
| **加一个新原子工具**（比如 `screenshot`） | `wlwl_ass.py` 加 `do_screenshot()` 方法 | `assets/tools_schema.json` 加 schema 条目；`memory/wlwl_ass_deps_sop.md` 文档化 |
| **改一个已有工具的行为**（比如 `code_run` 加沙箱） | `wlwl_ass.py` 找 `do_code_run` / `code_run()` | 跑 `tests/test_code_run*.py` 验回归 |
| **接一个新的 LLM 厂商** | `llmcore/adapters/` 加一个新文件（仿 `anthropic.py` / `openai.py`） | `llmcore/_keys.py` 加 provider 探测；`llmcore/__init__.py` re-export |
| **加一个新的 IM Bot 前端**（比如 Discord） | `frontends/` 仿 `tgapp.py` 加一个 | `launcher/bot_manager.py` 加进程管理；`launcher/config_store.py` 加凭据 schema |
| **改 Agent 主循环**（比如加个回调点） | `agent_loop.py`（只有 ~250 行） | 改完跑全套 `tests/` |
| **加 GUI 一个新 tab** | `gui/src/features/<name>/` 新建 feature 目录 | `gui/src/App.tsx` 注册路由；`launcher/api_server.py` 加对应 REST endpoint |
| **加一个 worker**（kernel 编排的子能力） | `llmcore/workers/` 仿 `kb_worker.py` 加 | `llmcore/capabilities.yaml` 加能力声明；`llmcore/kernel.py` 注册 |
| **改命令行 slash 指令**（`/skills` `/usage` 等） | `frontends/cli_commands.py` 找 `HELP_COMMANDS` 表 | 顺手加 `tests/test_cli_*.py` |
| **改记忆系统**（L0/L1/L2/L3/L4） | `memory_namespace.py` + `memory/*_sop.md` | 看 `docs/architecture/overview.md` 关于分层记忆的部分 |
| **加一种新的"会话存档"** | `launcher/auto_checkpoint.py` + `launcher/session_runtime.py` | `tests/test_session_*.py` |
| **改语音 loop**（端到端 voice） | `voice/orchestrator.py`（5 状态机） + `launcher/voice_ws.py`（WS endpoint） | `voice-website/src/` 是配套前端；`docs/RUNBOOK_voice.md` |
| **加 token usage / 成本上报** | `llmcore/_usage.py` + `llmcore/_pricing.py` | `launcher/metrics.py` 汇总；GUI `gui/src/features/token-usage/` |
| **加 plugin / Hook**（比如 Langfuse） | `plugins/` 仿 `langfuse_tracing.py` | `agentmain.py` 顶部有 import side-effect 加载点 |

---

## 4. 必须先理解的 5 个心智模型

### 4.1 Handler ≠ Loop

```
agent_loop.py  →  agent_runner_loop(handler, ...)
                       │
                       └─ 调用 handler.dispatch("code_run", args)
                                          │
                                          └─ 进 WlwlAssHandler.do_code_run
```

- **Loop 是死的**：感知→推理→工具→记忆→循环，永远 ~100 行。
- **Handler 是活的**：每个工具方法都在 Handler 上。要加新能力，**几乎永远**是加 Handler 方法 + 加 schema，**不**是改 loop。

### 4.2 9 个原子工具，其他都是"沉淀"

| 工具 | 干什么 | 文件位置 |
|---|---|---|
| `code_run` | 执行任意 python/powershell/bash | `wlwl_ass.py::code_run` |
| `file_read` | 读文件（支持图片/PDF） | `wlwl_ass.py::WlwlAssHandler.do_file_read` |
| `file_write` | 写文件 | 同上 |
| `file_patch` | 局部修改文件 | 同上 |
| `web_scan` | 看网页（Chrome 扩展抓 DOM） | 借助 `TMWebDriver.py` + `assets/tmwd_cdp_bridge/` |
| `web_execute_js` | 在真实浏览器跑 JS | 同上 |
| `ask_user` | 问用户（人机协作） | `wlwl_ass.py::WlwlAssHandler.do_ask_user` |
| `update_working_checkpoint` | 保存中间状态 | `launcher/checkpoint_manager.py` |
| `start_long_term_update` | 触发长期记忆更新 | `wlwl_ass.py::WlwlAssHandler.do_start_long_term_update` |

`tools/` 下的 `gui_operator`、`vision_tools`、`mcp_client`、`mixture_of_agents` 等都是**通过 `code_run` 调用的库**，不是新原子工具。

### 4.3 LLM 是 provider-agnostic 的

`llmcore/` 提供统一的 `LLMSession` / `ClaudeSession` / `NativeOAISession`。所有 provider 差异都封装在 `llmcore/adapters/{anthropic,openai}.py` 里。

加新 provider 的正确方式：
1. 在 `adapters/` 加一个新文件
2. `llmcore/__init__.py` re-export 新 session class
3. `_keys.py` 加 key 探测逻辑
4. `assets/tools_schema*.json` 通常**不需要动**——它是 OpenAI tool-use 兼容格式，自动 round-trip

### 4.4 记忆是文件，不是数据库

```
memory/
├── *.md                       ← SOPs (skill 沉淀)
├── *.py                       ← 可执行的 skill 工具（adb_ui.py, ocr_utils.py 等）
├── autonomous_operation_sop/  ← 自主操作流程
├── skill_search/              ← 海量 skill 索引（懒加载）
└── L4_raw_sessions/           ← 长程会话归档
```

`.gitignore` 默认**忽略 `memory/*`**，只白名单了少量 SOP 文件——这意味着每台机器的 memory 是**该机器独有的**。AI 改代码时通常不需要碰 memory/，除非是改 memory 管理本身。

### 4.5 GUI 的 Tauri shell 自己拉 Python 后端

`launch.pyw` 启动 Tauri。Tauri 的 Rust 层（`gui/src-tauri/src/lib.rs`）会自动**启动 `launcher/api_server.py` 子进程**。

- **前端**（gui/src/）通过 HTTP REST 调 `api_server`
- **api_server** 又用 `bot_manager` 拉起飞书/QQ/TG/微信等 bot 子进程
- 每个会话是 **`launcher/session_worker.py` 单独子进程**

所以"GUI 改 X" 通常涉及**两端**：React 改 fetcher，Python 改 endpoint。

---

## 5. 改代码前必看的设计决策（ADR）

`docs/adr/` 下有 12 个架构决策记录。改对应模块前**强烈建议**先翻一眼：

| 改这个 | 先读 |
|---|---|
| Free Pool / 故障转移 / 多 LLM 编排 | `docs/adr/0006-*.md` 系列 |
| Kernel + Worker 设计 | `0008-*.md`, `0009-*.md` |
| Voice loop | `0010-*.md` |
| 飞书 concierge bot | `0011-*.md` |
| Bot 进程隔离与崩溃自愈 | `0012-*.md` |

`docs/architecture/overview.md` 是一张总图。

---

## 6. 跑测试的标准流程

```bash
# 装测试依赖
pip install -e ".[test]"

# 跑全套（445+ 测试，几分钟）
pytest

# 改了 LLM 相关
pytest tests/test_llmcore_*.py tests/test_kernel*.py

# 改了 bot
pytest tests/test_bot_*.py tests/test_*app*.py

# 改了 GUI 后端
pytest tests/test_api_server*.py

# 改了 voice
pytest tests/test_voice*.py tests/test_orchestrator*.py
```

**重要**：`pytest.ini_options` 里设了 `cache_dir = "temp/.pytest_cache"`——测试缓存写到 `temp/`，gitignore 了，不会污染。

---

## 7. 常见坑

| 现象 | 原因 |
|---|---|
| `import llmcore` 报循环依赖 | `llmcore/__init__.py` 用了 PEP 562 lazy 加载——**别**在新文件顶部就 `from llmcore import *`，用局部 import |
| 加新工具后 LLM "不知道" | `assets/tools_schema.json`（中文版 `tools_schema_cn.json`）没同步加 schema 条目 |
| Bot 启动失败但 GUI 不报错 | 看 `temp/bot_<name>.log`；多半是 SDK 没装（`pip install lark-oapi` 等） |
| `wlwl` 命令找不到 | `pip install -e .` 没装；或者 venv 的 `Scripts/`/`bin/` 不在 PATH |
| GUI 不显示 | Tauri build 没编：`cd gui && npm ci && npm run tauri:dev` |
| Python 子进程 hang 死 | 八成是 `code_run` 调 PowerShell 卡了——`launcher/process_registry.py` 兜底 |
| 改了 `tools_schema.json` 没生效 | `agentmain.py` 在 import 时一次性 `load_tool_schema()`，**重启进程**生效，不是热加载 |

---

## 8. 一个最小修改示例：加一个 `current_time` 工具

```python
# 1) wlwl_ass.py 里 WlwlAssHandler 加方法
def do_current_time(self, args, response):
    from datetime import datetime
    tz = args.get('tz', 'local')
    now = datetime.now().isoformat()
    return StepOutcome(data={"now": now, "tz": tz})
```

```json
// 2) assets/tools_schema.json 加条目
{
  "type": "function",
  "function": {
    "name": "current_time",
    "description": "Return current local time in ISO format.",
    "parameters": {
      "type": "object",
      "properties": {"tz": {"type": "string"}},
      "required": []
    }
  }
}
```

```python
# 3) tests/test_current_time.py
def test_current_time(handler):
    out = handler.dispatch('current_time', {}, response=None)
    out = list(out) if hasattr(out, '__iter__') else [out]
    assert 'now' in out[-1].data
```

重启 `wlwl` 或 GUI，让 Agent 说"现在几点"，它会调到这个工具。

---

## 9. 哪些文件你**几乎永远不要碰**

| 文件 | 为什么 |
|---|---|
| `agent_loop.py::agent_runner_loop` | 它是契约；改它影响所有 handler/provider |
| `assets/tools_schema*.json` 的字段顺序 | LLM 习惯按顺序读 schema，乱序会影响命中率 |
| `wlwl_ass.py::code_run` | 这是"母工具"——Agent 通过它装其他所有能力。改它的输出格式 = 几百条 SOP 全失效 |
| `memory_namespace.py::MemoryStore` 的对外 API | L0–L4 都依赖它 |
| `.gitignore` 里 `memory/*` 的 whitelist 段 | 弄错了会把用户技能库提交到 git |

---

## 10. 进一步阅读

| 我想 | 看 |
|---|---|
| 看完整功能列表 + 同类对比 | `README.md` |
| 看图文教程 | `GETTING_STARTED.md` |
| 看架构总图 | `docs/architecture/overview.md` |
| 看每个目录 / 关键函数说明 | **`CODE_MAP.md`** |
| 看历史设计决策 | `docs/adr/*.md` |
| 看每个外部借用的归属 | `ATTRIBUTION.md`（**改动碰外部代码时必须更新**） |
| 看上游论文 | https://arxiv.org/abs/2604.17091 |

---

**最后一句话**：这个项目的设计哲学是 **"少即是多 / 用进化代替预设"**。加新功能时，先问自己——"能不能让 Agent 通过现有的 9 个工具自己学会？"如果可以，写个 SOP 放 `memory/` 就够了；只有真的是底层能力（新 LLM provider、新 IM 平台、新执行环境），才动核心代码。
