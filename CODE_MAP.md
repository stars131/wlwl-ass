# wlwl-ass · 代码地图

> 这份文档是项目的"目录索引 + 关键函数表"。配套 [`PROJECT_GUIDE.md`](./PROJECT_GUIDE.md) 食用——
> Guide 教你"想加 X 该去哪改"，Map 告诉你"那个地方具体有什么"。
>
> 行号是写文档时的快照，**仅作大致定位**，改动后会漂——以函数/类名为准。

---

## 0. 全景目录树（精简版）

```
wlwl-ass/
├── 入口与启动器
│   ├── launch.pyw            # 启动 Tauri GUI（唯一桌面入口）
│   ├── start_from_zero.cmd   # Windows 一键（建 venv + 装依赖 + 起 GUI）
│   ├── 一键启动.cmd          # 同上中文别名
│   ├── wlwl / wlwl.cmd       # CLI shim → launcher.cli_repl
│   └── pyproject.toml        # 项目元信息 + 入口点 (wlwl = launcher.cli_repl:main)
│
├── Agent 内核（根目录 .py 文件）
│   ├── agent_loop.py         # ~180 行：通用主循环 + BaseHandler 协议
│   ├── wlwl_ass.py           # ~1.8K 行：WlwlAssHandler，9 个工具的具体实现
│   ├── agentmain.py          # ~465 行：把 Handler + Loop + LLM 装配成 GeneraticAgent
│   ├── permissions.py        # 权限策略（auto / ask / read-only）
│   ├── project_context.py    # 当前项目根目录上下文加载
│   ├── memory_namespace.py   # 分层记忆 MemoryStore 实体规范化
│   ├── tokenjuice.py         # 大输出压缩（避免 token 爆炸）
│   ├── web_recipe.py         # 浏览器自动化"配方"管理
│   ├── simphtml.py           # 极简 HTML 解析（不引 BeautifulSoup 重量级依赖时用）
│   └── TMWebDriver.py        # Chrome CDP 桥；通过 assets/tmwd_cdp_bridge 扩展通信
│
├── llmcore/                  # provider-agnostic LLM 客户端 + Kernel/Worker 编排
├── launcher/                 # GUI 后端 + CLI + bot/session 进程管理
├── frontends/                # 6 个 IM Bot 适配器 + 共享 CLI 指令
├── gui/                      # Tauri + React 桌面应用（src/ 是 React，src-tauri/ 是 Rust）
├── tools/                    # 通过 code_run 调用的"重型工具"库（浏览器、视觉、MoA、MCP…）
├── memory/                   # 分层记忆 L0–L4（SOP + 可执行 skill）
├── voice/                    # 语音 loop 状态机 + intent + 唤醒词
├── voice-website/            # 配套语音前端（独立 React 子项目）
├── plugins/                  # 可选 side-effect 插件（Langfuse 追踪等）
├── reflect/                  # 反思 / 调度（autonomous + scheduler）
├── assets/                   # tool schemas、CDP 桥扩展、demo gif、技术报告 PDF
├── docs/                     # 架构概览、ADR、specs、RUNBOOK
├── tests/                    # 445+ pytest 测试 + journey 测试
└── scripts/                  # 运维 / 一次性脚本
```

---

## 1. 根目录核心 .py 文件

### `agent_loop.py` (180 行) — Agent 主循环

| 名字 | 类型 | 用途 |
|---|---|---|
| `ensure_safe_std_streams()` | func | 处理 `.pyw` 后台模式下 stdout/stderr 为 None 的情况；幂等 |
| `StepOutcome` | dataclass | 工具返回值容器：`data`, `next_prompt`, `should_exit` |
| `BaseHandler` | class | 所有 Handler 的基类。子类只需要实现 `do_<tool_name>` 方法 |
| `BaseHandler.dispatch(tool_name, args, response, index=0)` | method | 总入口：找 `do_<name>` 方法并调用，处理 before/after 回调 |
| `agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, ...)` | func | **核心 100 行循环**：感知→推理→工具→记忆 |
| `try_call_generator(func, ...)` | func | 让方法既能 yield 流式又能直接 return —— Handler 方法两种风格混用 |
| `json_default(o)` | func | JSON 序列化兜底（set → list） |

### `wlwl_ass.py` (1.8K 行) — WlwlAssHandler & 工具实现

模块级函数（独立可调用）：

| 函数 | 用途 |
|---|---|
| `code_run(code, code_type, timeout, cwd, code_cwd, stop_signal)` | **最重要**：执行 python/powershell/bash。写 tmp 文件再 subprocess |
| `ask_user(question, candidates=None)` | 通过 input() 或 GUI 拿用户输入 |
| `first_init_driver()` | 首次初始化 Chrome CDP |
| `web_scan(tabs_only, switch_tab_id, text_only)` | 抓当前标签页 DOM |
| `web_execute_js(script, switch_tab_id, no_monitor)` | 在真实浏览器跑 JS |
| `file_read(path, start, keyword, count, show_linenos)` | 读文件，支持 keyword 过滤 + 行号 |
| `file_patch(path, old_content, new_content)` | 局部替换（不是整文件重写） |
| `expand_file_refs(text, base_dir)` | 把 `@file:xx.txt` 展开成内容 |
| `smart_format(data, max_str_len, omit_str)` | 把大 dict/list 压短便于 LLM 看 |
| `consume_file(dr, file)` | （浏览器 CDP）下载文件回调 |
| `format_error(e)` | 异常 → 用户友好字符串 |
| `get_global_memory()` | 加载 `memory/global_mem.txt`（L2 全局事实） |

`WlwlAssHandler(BaseHandler)`（439–1773 行）—— **所有 LLM 可调用工具都是这里的方法**：

| 方法 | 工具名 | 干什么 |
|---|---|---|
| `do_code_run` | `code_run` | 上面 `code_run()` 的 dispatch 包装 |
| `do_ask_user` | `ask_user` | 同 |
| `do_web_scan` | `web_scan` | |
| `do_web_execute_js` | `web_execute_js` | |
| `do_file_read` | `file_read` | |
| `do_file_read_batch` | `file_read_batch` | 批量读多个文件 |
| `do_file_write` | `file_write` | |
| `do_file_patch` | `file_patch` | |
| `do_task_closure_check` | `task_closure_check` | 检查任务是否已完成 |
| `do_self_readme` | `self_readme` | 读自身 README（自我介绍） |
| `do_update_working_checkpoint` | `update_working_checkpoint` | 保存工作中状态 |
| `do_sop_search` | `sop_search` | 在 memory/ 里搜 SOP |
| `do_sop_read` | `sop_read` | 读具体 SOP |
| `do_web_search` | `web_search` | 走 `tools/web_search.py` |
| `do_wechat_send` | `wechat_send` | 走 `tools/wechat.py` |
| `do_mcp_call` | `mcp_call` | 调外部 MCP server (走 `tools/mcp_client.py`) |
| `do_process` | `process` | 管理后台子进程（走 `launcher/process_registry`） |
| `do_checkpoint` | `checkpoint` | checkpoint CRUD |
| `do_vision` | `vision` | 多模态视觉调用 |
| `do_gui_operator` | `gui_operator` | 键鼠/截图（走 `tools/gui_operator.py`） |
| `do_browser_operator` | `browser_operator` | 浏览器混合自动化 |
| `do_forum_harvest` | `forum_harvest` | LinuxDo 等论坛抓取 |
| `do_api_probe` | `api_probe` | 探活 LLM endpoint |
| `do_free_pool_ask` | `free_pool_ask` | 走免费 LLM 池 |
| `do_pm_friction_scan` | `pm_friction_scan` | PM 摩擦点挖掘 |
| `do_pm_proposal_decide` | `pm_proposal_decide` | PM 提案决策 |
| `do_mixture_of_agents` | `mixture_of_agents` | MoA 多专家融合 |
| `do_voice` | `voice` | 语音功能入口 |
| `do_image_generate` | `image_generate` | 图像生成 |
| `do_skill_propose_patch` | `skill_propose_patch` | 自我升级（提交 skill 改进） |
| `do_no_tool` | — | "本轮不调工具"的占位 |
| `do_start_long_term_update` | `start_long_term_update` | 触发长期记忆固化 |

内部方法（不暴露给 LLM）：

| 方法 | 用途 |
|---|---|
| `_get_abs_path(path)` | 项目根相对路径 → 绝对路径 |
| `_check_cross_bot_write(abs_path)` | 防止 bot 写到别的 bot 的目录 |
| `_extract_code_block(response, code_type)` | 从 LLM 响应里抠出代码块 |
| `_in_plan_mode()` / `_exit_plan_mode()` | Plan Mode 状态 |
| `_check_plan_completion()` | 看 plan 是不是完成了 |
| `_get_anchor_prompt(skip)` | 拼接当前轮次的锚定提示 |

### `agentmain.py` (465 行) — 装配层

| 关键符号 | 用途 |
|---|---|
| `GeneraticAgent` 类 | 把 LLM client、Handler、Loop、权限、工具 schema 装配起来；CLI 和 GUI 都用它 |
| `load_tool_schema(suffix='')` | 读 `assets/tools_schema.json`（中文 `_cn` / 英文 `_en`） |
| 顶部 `from plugins import langfuse_tracing` | side-effect 装载追踪插件，可选 |

### 其他根目录

| 文件 | 用途 |
|---|---|
| `permissions.py` | `PermissionPolicy`、`InteractivePermissionPrompter`、`ToolPermissionRequest`；auto/ask/read-only 三种模式 |
| `project_context.py` | `load_project_context()`：从当前 cwd 推断项目根 + 加载该项目的 settings |
| `memory_namespace.py` | `MemoryStore` 类、`normalize_entity()`；L0–L4 分层记忆的 store 接口 |
| `tokenjuice.py` | `compact_output(text)`：把工具大输出压成 LLM 看得过来的尺寸 |
| `web_recipe.py` | `RecipeManager` 类：浏览器自动化"配方"——一次成功的 JS 流程会沉淀成 recipe 复用 |
| `TMWebDriver.py` | Chrome DevTools Protocol 桥；和 `assets/tmwd_cdp_bridge/` 扩展配对 |
| `simphtml.py` | 不引 BeautifulSoup 时的极简 HTML 解析 |

---

## 2. `launcher/` — GUI 后端 + CLI + 进程管理

42 个文件，是除了 wlwl_ass.py 之外最重的目录。

### 入口

| 文件 | 用途 |
|---|---|
| `cli_repl.py` | **`wlwl` 命令入口**。一次性模式 + 交互 REPL 都在这里。`main()` |
| `api_server.py` | **2280 行，GUI 后端 HTTP 服务**。bottle 框架。所有 `/api/...` endpoint |
| `bot_manager.py` | 拉起/重启/监视 6 个 IM bot 子进程；崩溃自愈 |
| `session_worker.py` | 每个 GUI 会话独立子进程；进程间 IPC |
| `session_runtime.py` | 单会话运行时状态机 |

### 配置与凭据

| 文件 | 用途 |
|---|---|
| `config.py` | 早期 KV 配置（`bots.telegram.bot_token` 这种） |
| `config_store.py` | 新版统一 store（项目级 + 用户级） |
| `config_migrate.py` | 旧→新格式迁移 |
| `api_config.py` | LLM API key 的 CRUD |
| `api_presets.py` | 50+ 服务商预设（Claude/OpenAI/DeepSeek/Kimi/GLM…） |
| `api_endpoint_probe.py` | 探活 endpoint，测延迟 |
| `dotenv_shim.py` | 没装 python-dotenv 时的最小替代 |
| `profiles.py` | 多 profile 切换 |
| `launch_config.py` | 启动级配置（端口、日志路径） |

### Free Pool（多渠道免费 LLM 池，ADR-0006）

| 文件 | 用途 |
|---|---|
| `free_pool_router.py` | 把请求路由到合适的免费 LLM；故障转移 |
| `free_pool_vault.py` | 免费 key 的存储 |

### 会话与归档

| 文件 | 用途 |
|---|---|
| `checkpoint_manager.py` | 工作中状态保存/恢复 |
| `auto_checkpoint.py` | 定时自动 checkpoint |
| `trajectory.py` | Agent 走过的轨迹记录 |
| `activity_log.py` | 活动日志（被 GUI 的 Activity tab 消费） |
| `project_manager.py` | 多项目管理 |

### CLI 子命令

| 文件 | 暴露 |
|---|---|
| `cli_config.py` | `wlwl config list/add/use/probe/presets` |
| `cli_kb.py` | KB worker 调试 |
| `cli_tokens.py` | `wlwl tokens` |
| `cli_readme.py` | 自动生成 README 摘要 |
| `cli_workers.py` | Worker 调试 |
| `cli_concierge_llm_test.py` | 飞书 concierge bot 的 LLM 联调 |

### Worker / Kernel 周边

| 文件 | 用途 |
|---|---|
| `correlation.py` | 跨进程 trace 关联 |
| `curator.py` | 把"试验通过"的工具调用沉淀成 SOP |
| `doctor.py` | 健康检查（`wlwl doctor` / `/doctor`） |
| `metrics.py` | 指标汇总（token 用量、成本、延迟） |
| `onboarding.py` | 第一次启动的引导流程 |
| `preflight.py` | 启动前自检（ADR-0012） |
| `process_registry.py` | Agent 自己拉的后台进程登记表 |
| `skills.py` | Skill / SOP 索引（`/skills`） |
| `approval.py` | 命令白名单（哪些 shell 命令免询问） |
| `llm_binding.py` | Bot 与 LLM 绑定（哪个 bot 用哪把 key） |
| `voice_ws.py` | 语音 WebSocket endpoint（粘 `voice/orchestrator.py` 到 bottle） |
| `gui_operator_runtime.py` | GUI 自动化运行时 |
| `bootstrap_start.ps1` | Windows 启动脚本 |
| `logging_config.py` | 日志格式 / 输出位置 |

---

## 3. `llmcore/` — Provider-agnostic LLM + Kernel/Workers

### LLM Session（顶层 API）

| 文件 | 用途 |
|---|---|
| `__init__.py` | **公共 API 入口**。PEP 562 lazy 加载 |
| `base.py` | `BaseSession` 抽象 |
| `adapters/anthropic.py` | Claude 适配器 |
| `adapters/openai.py` | OpenAI 兼容协议适配器（含 DeepSeek/Kimi/GLM 等） |
| `clients.py` | `ToolClient`、`NativeToolClient` |
| `mixin.py` | `MixinSession`：多 provider 故障转移混合会话 |

### 内部支撑

| 文件 | 用途 |
|---|---|
| `_keys.py` | 从环境/配置加载 API key；`reload_mykeys()` |
| `_history.py` | 上下文历史压缩（`compress_history_tags`、`trim_messages_history`） |
| `_messages.py` | OpenAI ↔ Anthropic 消息格式转换 |
| `_mock.py` | 测试用 mock response shape |
| `_pricing.py` | 各模型单价表（算成本用） |
| `_usage.py` | Token 用量统计 |
| `_utils.py` | `auto_make_url`、`_openai_stream` 等 |
| `errors.py` | 异常类 |
| `metrics.py` | LLM 调用指标 |

### Kernel + Workers（ADR-0008/0009）

| 文件 | 用途 |
|---|---|
| `kernel.py` | **单进程单 kernel 单例**。`get_kernel()`；`dispatch`/`dispatch_stream`；circuit breaker；health probe |
| `worker.py` | Worker 协议（`__call__` / `health()` / `manifest`） |
| `capabilities.py` + `capabilities.yaml` | 能力注册表 |
| `captoken.py` | Capability token 签名/验证 |
| `forum.py` | "Forum bus"：worker 之间的消息总线 |
| `concierge_agent.py` | 飞书 concierge bot 的 agent（ADR-0011） |

### Workers 实例（`llmcore/workers/`）

| 文件 | 干什么 |
|---|---|
| `audit_worker.py` | 审计 / 合规过滤 |
| `calendar_worker.py` + `feishu_calendar_storage.py` | 日程 |
| `escalate_worker.py` | 升级到人工的兜底 |
| `inspiration_worker.py` | 灵感 / 创意生成 |
| `kb_worker.py` | 知识库检索 |
| `llm_worker.py` | 通用 LLM 包装 |
| `slot_worker.py` | 槽位填充（concierge 用） |
| `xiaomi_voice_worker.py` | 小米 MiMo 语音 STT |
| `local_whisper_worker.py` | 本地 faster-whisper 兜底 STT |
| `mock_voice_worker.py` | 测试用 mock |

---

## 4. `frontends/` — IM Bot 适配器 & CLI 共享

| 文件 | 平台 / 用途 |
|---|---|
| `tgapp.py` | Telegram |
| `fsapp.py` | 飞书（普通） |
| `fsapp_concierge.py` | 飞书 concierge bot（rule-gated + LLM hook，ADR-0011） |
| `qqapp.py` | QQ（qq-botpy WebSocket） |
| `wecomapp.py` | 企业微信 |
| `dingtalkapp.py` | 钉钉 |
| `wechatapp.py` | 个人微信（iLink 协议） |
| `chatapp_common.py` | 所有 bot 共享的聊天循环逻辑 |
| `gateway.py` + `gateway_handler.py` + `gateway_adapters/` | 多 bot 统一 gateway 抽象 |
| `cli_commands.py` | `SharedCommandHandler`：`/new` `/continue` `/skills` 等命令，CLI 和 bot 共用 |
| `continue_cmd.py` | `/continue` 命令的具体实现（列出可恢复会话 + 恢复） |
| `fs_commands.py` | 飞书专属命令扩展 |

---

## 5. `gui/` — Tauri + React 桌面应用

### 顶层

| 路径 | 用途 |
|---|---|
| `package.json` | npm 依赖 + 脚本（`npm run tauri:dev`、`tauri build`） |
| `vite.config.ts` / `tsconfig*.json` | Vite + TypeScript |
| `tailwind.config.js` / `postcss.config.js` | shadcn/ui + Tailwind |
| `src-tauri/Cargo.toml` | Rust 端依赖 |
| `src-tauri/src/lib.rs` + `main.rs` + `python_runtime.rs` | Tauri Rust 入口；**拉起 `launcher/api_server.py` 子进程** |
| `src-tauri/tauri.conf.json` | bundle 配置（`targets: "all"` 三平台） |

### 前端结构（`gui/src/`，feature-based，ADR-0004）

```
src/
├── App.tsx                # 路由 + 顶层布局
├── main.tsx               # React entrypoint
├── components/            # 跨 feature 的 UI 组件（shadcn 包装）
├── features/              # ← 每个业务 tab 一个目录
│   ├── api-configs/       # API 配置 tab
│   ├── bots/              # Bots tab
│   ├── sessions/          # 会话 tab
│   ├── settings/          # 设置 tab
│   ├── activity/          # 活动日志
│   ├── credentials/       # 凭据管理
│   ├── doctor/            # 健康检查
│   ├── gui-operator/      # GUI 自动化操作
│   ├── onboarding/        # 首次引导
│   ├── processes/         # 后台进程列表
│   ├── skills/            # Skill 浏览
│   ├── theme/             # 主题切换
│   └── token-usage/       # token 用量
├── i18n/                  # 中英双语
├── lib/                   # 公共 fetcher / utils
├── styles/                # 全局样式
├── test/                  # Vitest 测试
└── types/                 # TS 类型
```

**每个 feature 标准结构**：`components/`、`hooks/`、`api/`、`types.ts`。

---

## 6. `tools/` — 通过 `code_run` 调用的库

这些**不是**新原子工具——它们是 Python 库，由 Agent 在 `code_run` 里 import 使用，或被 `wlwl_ass.py` 里的 `do_*` 方法包装。

| 文件 | 用途 |
|---|---|
| `api_probe.py` | LLM endpoint 延迟探测 |
| `browser_hybrid_operator.py` | 浏览器混合自动化（CDP + 视觉混合） |
| `curator_propose.py` | 自动 SOP 沉淀 |
| `feishu.py` | 飞书 API 封装（消息/卡片/文件） |
| `forum_harvester.py` | LinuxDo / V2EX 等论坛抓取 |
| `free_pool_fingerprints.py` | Free pool 指纹 |
| `gui_operator.py` | 键鼠 / 截图（pyautogui 包装） |
| `image_generation.py` | 图像生成（多 provider） |
| `mcp_client.py` | MCP（Model Context Protocol）客户端 |
| `mixture_of_agents.py` | MoA 多专家投票 |
| `pm_friction_miner.py` | PM 摩擦点挖掘 |
| `session_search.py` | 跨会话搜索 |
| `skill_frontmatter.py` | SOP 文件 frontmatter 解析 |
| `skills_guard.py` | 防止误删/误改 skill |
| `sop_tools.py` | SOP 操作（搜索/读/索引） |
| `tavily_proxy.py` | Tavily 搜索代理 |
| `vision_tools.py` | 视觉调用 helper |
| `voice_tools.py` | 语音 helper |
| `web_search.py` | 通用 Web 搜索 |
| `wechat.py` | 微信操作（PC 客户端驱动） |

---

## 7. `memory/` — 分层记忆（L0–L4）

**重要**：`.gitignore` 默认忽略 `memory/*`，只白名单了少数 SOP 和工具。每台机器的 memory 是独有的。

| 路径 | 层 | 用途 |
|---|---|---|
| `global_mem.txt` | L2 | 全局事实（长期稳定知识） |
| `global_mem_insight.txt` | L1 | 索引层 |
| `autonomous_operation_sop.md` + `autonomous_operation_sop/` | L3 | 自主操作 SOP |
| `memory_management_sop.md` | L0 | 元规则 |
| `memory_cleanup_sop.md` | — | 清理流程 |
| `skill_search/` | — | 海量 skill 索引（懒加载） |
| `L4_raw_sessions/` | L4 | 会话归档（运行时积累） |
| `*_sop.md`（10+ 个） | L3 | 各种任务流程（github_contribution、code_review、deepresearch、image_gen、moa、pandoc、plan、scheduled_task…） |
| `*.py`（adb_ui、keychain、ljqCtrl、ocr_utils、procmem_scanner、ui_detect、skill_self_improve） | — | 可执行 skill 工具 |
| `pm_journeys/` | — | PM 旅程数据 |
| `__init__.py` | — | 让 memory 可作为 package import |

---

## 8. `voice/` & `voice-website/` — 语音 loop（ADR-0010）

### `voice/`（后端 5 状态机）

| 文件 | 用途 |
|---|---|
| `orchestrator.py` | `ConversationOrchestrator`：IDLE→ARMED→LISTENING→PROCESSING→RESPONDING |
| `intent.py` | `RuleIntentClassifier`、`IntentResult` |
| `wake.py` | `PhraseMatcher`：唤醒词匹配 |
| `storage.py` | `VoiceSession`、`TurnEvent`：会话存储 |

### `voice-website/`（独立 React 子项目）

| 路径 | 用途 |
|---|---|
| `src/main.tsx` + `src/App.tsx` | 入口 |
| `src/lib/ws.ts` | WebSocket 客户端 |
| `src/lib/controller.ts` | 状态控制 |
| `src/lib/audio/*` | 录音 + 播放 |
| `src/state/conversation.ts` | 对话状态 |
| `Dockerfile` + `nginx.conf` | 部署 |

跑这个前端不影响主项目；用 `npm ci && npm run dev`。

---

## 9. `assets/` — 资源

| 文件 | 用途 |
|---|---|
| `tools_schema.json` / `tools_schema_cn.json` / `tools_schema_en.json` | **所有原子工具的 OpenAI-style schema**——加新工具要改这个 |
| `code_run_header.py` | `code_run` 执行前注入的 header（设 PYTHONIOENCODING 等） |
| `tmwd_cdp_bridge/` | Chrome 扩展（CDP 桥），手动安装到浏览器 |
| `SETUP_FEISHU.md` / `SETUP_FEISHU_CONCIERGE.md` | 飞书配置文档 |
| `demo/*.gif` + `images/*.jpg` | README 用的演示图 |
| `GenericAgent_Technical_Report.pdf` | 上游论文 |

---

## 10. `docs/` — 设计文档

| 路径 | 用途 |
|---|---|
| `architecture/overview.md` | 架构总图（必读） |
| `adr/0001-0012` | 12 个架构决策记录（按需读对应模块） |
| `adr/README.md` | ADR 索引 + 写作规范 |
| `specs/feishu-concierge-bot.md` + `voice-website-prd.md` | 详细 spec |
| `CONFIG.md` | 配置文件优先级 / 高级凭据 |
| `RUNBOOK_voice.md` | 语音栈部署 runbook |
| `feishu_bot_call_process.md` | 飞书 bot 调用流程 |

---

## 11. `tests/` — pytest 测试套件

约 80 个测试文件，445+ 用例。命名约定：

| 模式 | 测什么 |
|---|---|
| `test_api_*.py` | api_server / api_config / api_presets |
| `test_bot_*.py` | bot_manager / bot 隔离 / 崩溃检测 |
| `test_fsapp*.py` / `test_concierge_*.py` | 飞书 + concierge |
| `test_llmcore_*.py` / `test_native_sessions.py` / `test_llm_*.py` | llmcore |
| `test_kernel*.py` / `test_*_worker.py` | kernel + workers |
| `test_voice*.py` / `test_orchestrator*.py` | voice loop |
| `test_cli_*.py` | CLI |
| `test_gui_*.py` | GUI 后端 |
| `test_free_pool.py` / `test_hardening.py` | free pool + 容错 |
| `test_intent_*.py` | intent 分类 |
| `test_local_whisper_worker.py` | 本地 STT 兜底 |
| `journeys/` | 端到端旅程测试（baselines + runs/） |

跑方式见 `PROJECT_GUIDE.md §6`。

---

## 12. `plugins/` & `reflect/` & `scripts/`

| 路径 | 用途 |
|---|---|
| `plugins/langfuse_tracing.py` | 可选 Langfuse 追踪。`agentmain.py` import 时 side-effect 安装；没装 SDK 静默跳过 |
| `reflect/autonomous.py` | 自主反思循环（自我升级） |
| `reflect/scheduler.py` | 定时任务调度 |
| `scripts/feishu_concierge_probe.py` + `_debug.py` | 飞书 concierge 联调脚本 |
| `scripts/regen_autonomous_reports_index.py` | 重生成 R-编号报告索引 |
| `scripts/import_sophub_sops.py` | 从 SOP Hub 导入 |
| `scripts/_copy_to_changwlwl.py` | （这次搬家用的脚本，可删） |

---

## 13. 配置文件清单

| 文件 | 用途 |
|---|---|
| `pyproject.toml` | Python 项目元信息；extras：`ui` / `all-frontends` / `voice` / `stt-local` / `wechat` / `gui-operator` / `test` |
| `.env.example` | 环境变量模板 |
| `.gitignore` | **必读**——其中 `memory/*` 段的 whitelist 很关键 |
| `gui/package.json` | npm |
| `voice-website/package.json` | npm |
| `assets/tools_schema*.json` | 工具 schema |
| `llmcore/capabilities.yaml` | 能力注册表 |

运行时生成（gitignore 了，本仓库不存在）：

| 文件 | 内容 |
|---|---|
| `temp/launcher_api_configs.json` | API 配置 |
| `temp/*.log` | 各 bot / api_server / session 的日志 |
| `~/.wlwl-ass/config.json` | 用户级配置 |
| `memory/*` 的非白名单部分 | 用户专属的 skill 库 |
| `sche_tasks/` | 已调度任务状态 |

---

## 14. 速查：去哪找

| 找 | 去 |
|---|---|
| LLM 调用入口 | `llmcore/__init__.py` re-export → `llmcore/adapters/{anthropic,openai}.py` |
| 工具实现 | `wlwl_ass.py::WlwlAssHandler.do_*` |
| 工具 schema | `assets/tools_schema*.json` |
| 主循环 | `agent_loop.py::agent_runner_loop` |
| GUI 后端 endpoint | `launcher/api_server.py` |
| Bot 进程管理 | `launcher/bot_manager.py` |
| 会话状态机 | `launcher/session_runtime.py` |
| Kernel/Worker 编排 | `llmcore/kernel.py` + `llmcore/workers/` |
| 权限策略 | `permissions.py` |
| 命令行 slash 命令 | `frontends/cli_commands.py` |
| Tauri Rust 端 | `gui/src-tauri/src/lib.rs` |
| React feature 入口 | `gui/src/features/<name>/components/<Name>Page.tsx` |
| 测试 | `tests/test_<topic>.py` |
| 设计决策 | `docs/adr/00NN-*.md` |
| 配置文件优先级 | `docs/CONFIG.md` |

---

**记住**：要找一个具体函数最快的方式是 `Grep "def my_function"` 或在你的 IDE 里全局搜符号——这份地图只是骨架，肉在代码里。
