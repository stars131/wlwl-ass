# 飞书机器人完整调用流程分析报告

> 分析日期：2026-05-18  
> 源码路径：`frontends/fsapp.py` (899行), `frontends/fs_commands.py` (379行),  
> `agentmain.py` (465行), `launcher/bot_manager.py` (585行)

---

## 一、整体架构：双通道 + 统一 Agent 核心

```
                         ┌──────────────────────────────┐
                         │      GeneraticAgent           │  ← agentmain.py (同一个类)
                         │  (LLM Backend + Tools + Loop) │
                         └──────┬────────────┬──────────┘
                                │            │
                     put_task() │            │ put_task()
                     source=    │            │ source=
                    "feishu"    │            │ "gui"/"cli"
                 ┌──────────────┴──┐   ┌─────┴──────────────┐
                 │    fsapp.py     │   │  session_worker.py  │
                 │ (Lark WebSocket) │   │  (Child Process)    │
                 │ _TaskCard 输出   │   │  DisplayQueue 输出  │
                 └────────┬───────┘   └────────┬────────────┘
                          │                    │
                    飞书服务器              GUI (launcher)
```

---

## 二、飞书机器人核心调用链 (fsapp.py)

### 2.1 启动流程

```
main()
  ├─ _acquire_single_instance()         # 端口锁 FEISHU_LOCK_PORT=19532
  ├─ 校验 APP_ID / APP_SECRET            # 来自 mykeys
  ├─ 校验 ALLOWED_USERS                  # 安全：拒绝空列表，允许 "*"
  ├─ create_client()                     # lark.Client
  ├─ register_p2_im_message_receive_v1(handle_message)  # 事件注册
  └─ cli.start()                         # 长连接阻塞
```

### 2.2 消息处理链

```
handle_message(event)
  ├─ 类型判断: p2p (私聊) / group_chat (群聊)
  ├─ 提取: open_id, chat_id, message_id
  ├─ _build_user_message(message, open_id)
  │   ├─ text: 直接取 content.text
  │   ├─ post: 遍历 content 块，提取文本 + image_key
  │   ├─ image/audio/file/media: 下载 → 保存到 temp/feishu_media/{sanitized_open_id}/
  │   └─ 其他类型: 占位符描述
  ├─ _get_agent(open_id)
  │   └─ _AGENT_CACHE: 每用户一个 GeneraticAgent 实例，线程安全
  ├─ put_task(user_input, source="feishu", images=image_paths)
  │   └─ 进入 agentmain.py 的 task_queue → 统一 Agent 循环
  └─ threading.Thread(target=run_agent)
       ├─ user_tasks[open_id] = {"running": True}
       ├─ _TaskCard 初始化 (飞书交互式卡片)
       ├─ drain_queue: 轮询 display_queue
       │   ├─ {"next": chunk}  → 卡片 patch (增量推送)
       │   └─ {"done": result} → 卡片 done (最终结果)
       ├─ 文件输出: 正则匹配 [FILE:path] → upload_file
       └─ 异常: 捕获 _UploadHardError
```

### 2.3 Agent 获取规则

```python
_AGENT_CACHE: dict[str, GeneraticAgent]  # key = open_id
```

- 同一用户复用，线程安全锁 `_AGENT_CACHE_LOCK`
- PUBLIC_ACCESS 模式（`allowed_users=["*"]`）：禁止突变命令
- 非公开模式：所有用户均视为管理员身份

### 2.4 _TaskCard 输出机制

```
_TaskCard(receive_id, rid_type="open_id"|"chat_id")
  ├─ _create()   → 创建飞书交互式卡片（折叠面板结构）
  ├─ step(summary, detail) → patch 卡片，追加折叠步骤
  │   └─ 每步显示: status + Turn 编号 + 折叠详情
  ├─ done(text)  → 最终更新卡片状态为"已完成"
  └─ _push()     → 调用 lark API 创建/更新卡片消息
```

输出过滤：移除 `<thinking>` `<summary>` `<tool_use>` `<file_content>` HTML 标签后展示。

### 2.5 文件输出机制

Agent 输出中的 `[FILE:path]` 正则匹配：
- 沙箱校验：只允许 `PROJECT_ROOT` 下文件
- 拒绝：`.git` `.wlwl-ass` `memory` `.env` `launcher_api_configs.json` `mykey*.py`
- 图片限制: ≤10MB；文件限制: ≤30MB
- 支持格式: png/jpg/jpeg/gif/bmp/webp/ico/tiff | opus/mp3/wav/m4a/aac | mp4/mov/avi/mkv/webm | pdf/doc/docx/xls/xlsx/ppt/pptx

---

## 三、Slash 命令体系 (fs_commands.py)

### 3.1 飞书专属命令

| 命令 | 功能 | 公开模式可用 |
|------|------|:---:|
| `/sop <query>` | 搜索 Sophub | ✅ |
| `/sop read <id>` | 拉 SOP 全文（长文落盘发文件） | ✅ |
| `/sop stats` | SOP 统计 | ✅ |
| `/screenshot [N]` | 截屏并发回（mss/PIL 双后端） | ✅ |
| `/run <cmd>` | 执行 shell 命令 (30s超时) | ❌ |
| `/clip` | 读剪贴板 | ✅ |
| `/clip <text>` | 写剪贴板 | ❌ |
| `/open <path\|url>` | 用默认程序打开 | ❌ |

### 3.2 继承命令（from cli_commands）

通过 `dispatch_with_shared()` 还继承：`/usage` `/doctor` `/skills` `/sessions` `/checkpoints` `/processes` `/approval` `/trajectory` `/curator` `/mcp` `/help` 等 CLI 全套命令。

### 3.3 命令处理流程

```
handle_message → text starts with "/"
  └─ dispatch_with_shared(cmd_line, ctx, agent)
       ├─ 先尝试 fs_commands 本地命令
       ├─ 再尝试 SharedCommandHandler (cli_commands)
       └─ ctx.send_text() / ctx.send_file() → 飞书消息 API
```

---

## 四、安全与权限模型

### 4.1 三层安全

1. **端口锁**：`FEISHU_LOCK_PORT=19532` 防多实例
2. **用户白名单**：`bots.feishu.allowed_users`
   - 空列表 = 拒绝所有人
   - `["*"]` = 公开模式（锁定突变命令）
   - 具体 open_id 列表 = 管理员模式
3. **公开模式限制**：`/run` `/clip<写入>` `/open` 被 `_require_mutating()` 拦截

### 4.2 隐私保护

- 消息预览截断：默认仅 40 字符（设 `WLWL_FSAPP_LOG_VERBOSE=1` 恢复 200 字符）
- 出站文件沙箱：禁止泄露配置/密钥/memory 目录

---

## 五、飞书机器人身份体系

本项目实际运行 **两个独立飞书机器人**，分别用不同的 App 凭证和端口：

| 机器人 | 脚本 | 端口锁 | 飞书 App | 受众 |
|--------|------|--------|----------|------|
| **Owner Bot**（主机器人） | `fsapp.py` | 19532 | Owner App | 管理员本人 |
| **Concierge Bot**（小秘书） | `fsapp_concierge.py` | 19533 | Concierge App | 管理员的访客/朋友 |

### 5.1 Owner Bot 身份注入 (`fsapp.py`)

身份通过 `extra_sys_prompt` 注入到 `GeneraticAgent` 的所有 LLM backend：

```
mykeys["fs_system_prompt"]  ─┐
mykeys["fs_user_prompts"]    ├──→ _resolve_extra_prompt(open_id)
   per-user 覆盖              │       ↓
                              │    "## Feishu Persona\n{prompt}"
                              │       ↓
                              └──→ _apply_prompt(agent, prompt)
                                      ↓
                                  agent.set_extra_sys_prompt(prompt)
                                  对所有 backend 生效
```

- `fs_system_prompt`：全局飞书 persona（来自 `mykeys`，通过 `launcher/config_store.py` 从 `~/.wlwl-ass/config.json` 加载）
- `fs_user_prompts`：按 open_id 的 per-user 覆盖（格式：`{"ou_xxx": "你是管理员Alice的助手..."}`）
- 解析优先级：`fs_user_prompts[open_id]` > `fs_system_prompt` > 空

**实际效果**：Agent 在多轮对话中始终携带此身份 prompt，飞书用户消息前会加上 "Feishu Persona" 头注入到 system prompt。

### 5.2 Concierge Bot 人设 (`fsapp_concierge.py` + `concierge_agent.py`)

Concierge Bot 不使用 `GeneraticAgent`，而是独立的 `ConciergeAgent`（`llmcore/concierge_agent.py`），人设通过 `BotPersonaConfig` 结构化管理：

**配置定义** (`ConciergeConfig.persona_config: BotPersonaConfig`)：

| 字段 | 用途 | 默认值概括 |
|------|------|-----------|
| `identity` | 角色自我介绍 | "我是小 W，Alice 的私人小秘书。我是她数字生活的管家..." |
| `project_summary` | 项目能力简介 | "Alice 使用一个叫做 GA 的 AI agent 系统来管理她的数字生活..." |
| `capability_summary` | 能力边界说明 | calendar 查询 + FAQ 知识库 + 紧急情况转接 |
| `style_guidelines` | 回复风格 | 友好、简洁、不泄露内部信息、不确定时说不知道 |
| `working_hours` | 工作时间 | 默认全天候 |
| `topics_allowed` | 允许话题 | 默认为空（全部允许） |

**人设默认值** (`_DEFAULT_PERSONA_IDENTITY` line 380-420ish)：
```
你是小 W，Alice 的私人小秘书。我是她数字生活的管家——帮她安排日程、回答朋友们的常见问题、
在紧急时帮她联系 Alice。不要暴露项目内部细节（比如 GA、agent、LLM、工具名）。
用友好轻松的语调；如果问题超出我能处理的范围，诚实地说明并建议留言。
```

**能力限制**：
- 只能查日历（只读）、查 FAQ 知识库、紧急转接
- 无代码执行、无文件读写、无浏览器、无截图
- 单轮回复，无多 turn 任务卡片
- 收到 `/` 命令当闲聊处理（无 slash command）

**加载路径**：`ConciergeConfig` → `ConciergeAgent.__init__()` → `_build_system_prompt()` 拼接 identity + project_summary + capability_summary + style_guidelines → 每次 LLM 调用作为 system message。

### 5.3 身份链路总结

```
用户发消息到飞书
    ├─ 消息进 Owner App → fsapp.py → GeneraticAgent (完整工具 + fs_system_prompt)
    │    └─ 回复格式：_TaskCard 多 turn 交互卡片（仅展示当前 turn，旧 turn 不显示）
    │
    └─ 消息进 Concierge App → fsapp_concierge.py → ConciergeAgent (受限工具 + BotPersonaConfig)
         └─ 回复格式：单条文本消息，无卡片
```

---

## 六、Agent 核心运行循环 (agentmain.py) — 飞书与 GUI 共享

```
GeneraticAgent.run()
  └─ while True:
       task = task_queue.get()
       ↓
       _handle_slash_cmd(raw_query)  → 处理内置命令 /reset /continue /stop
       ↓
       history.append([USER]: query)
       ↓
       WlwlAssHandler(agent, history, cwd, permission_policy)
       ↓
       agent_runner_loop(llmclient, sys_prompt, query, handler, TOOLS_SCHEMA)
       │   → LLM 多轮对话 (max_turns=70)
       │   → 每轮生成 → 解析 tool_call → 执行 → 结果回填
       │   → turn_end_callback → key_info 更新
       │   → display_queue.put({next: chunk})  流式输出
       ↓
       display_queue.put({done: full_resp})
       ↓
       activity_log.record(task_end, outcome, elapsed_s, turns)
```

---

## 七、飞书 vs GUI 会话对比

| 维度 | 飞书机器人 | GUI (session_worker) |
|------|-----------|---------------------|
| **Agent 实例** | `GeneraticAgent` — **同一个类** | `GeneraticAgent` — **同一个类** |
| **进程模型** | 主进程内线程 | 独立子进程 (child Python) |
| **输入通道** | Lark WebSocket (im.message.receive_v1) | API Server → WebSocket → task_queue |
| **source 标签** | `"feishu"` | `"gui"` |
| **输出方式** | `_TaskCard` (飞书交互卡片) | `DisplayQueue` → WebSocket → GUI 渲染 |
| **历史存储** | per-user (open_id), 在 GeneraticAgent 内 | per-project session, 在 session_runtime |
| **文件输出** | [FILE:path] → Lark upload_file API | 文件路径直接可访问 |
| **Agent 缓存** | `_AGENT_CACHE` per open_id | session_worker 中 agent 生命周期=session 生命周期 |
| **多用户** | 多 open_id 共享一个进程 | 每个 project session 独立进程 |
| **工具权限** | 完整工具集（同 GUI） | 完整工具集 |
| **LLM 后端** | 同一个 llmclient 配置 | 同一个 llmclient 配置 |
| **SOP 访问** | 同一个 memory/ 目录 | 同一个 memory/ 目录 |
| **work_dir** | PROJECT_ROOT/temp | project_root 或 temp |
| **最大轮数** | 70 turns | 70 turns |
| **中途停止** | `/continue stop` 命令 | GUI Stop 按钮 → cancel Event |

---

## 八、结论

**飞书机器人和 GUI 会话用的是完全相同的 Agent 核心（GeneraticAgent → agent_runner_loop → LLM + Tools），区别仅在输入/输出层面：**

- 飞书：Lark SDK 收消息 → `put_task(source="feishu")` → `_TaskCard` 回传
- GUI：API Server 收消息 → `put_task(source="gui")` → WebSocket 推增量

**对于一个消息的处理能力完全相同**：相同的 LLM 配置、相同的工具集（文件读写/代码执行/浏览器/网页搜索/截图/全功能）、相同的 SOP 库、相同的权限策略。飞书只是换了一个"壳"，核心大脑不变。