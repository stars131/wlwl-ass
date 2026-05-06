# 配置文件层级与覆盖规则

wlwl-ass 启动时按下列来源合并凭据，**后者覆盖前者**：

1. **Shell 环境变量**（最低优先级）
   - 已经 `export` 的变量会被 `.env` 中的值覆盖（除非你显式写 `WLWL_*` 前缀）。

2. **`.env`**（可选，零依赖，stdlib 解析）
   - 复制 `.env.example` 为 `.env`，填入 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY` 即可第一次跑通。
   - 由 `launcher/dotenv_shim.py` 解析（不需要 `python-dotenv`）。
   - 加载结果同步到 `os.environ`。

3. **`~/.wlwl-ass/config.json` + `<project>/.wlwl-ass/config.json`**（中等优先级）
   - 由 `launcher.config_store` 管理。结构化的 hierarchical 凭据 + 设置存储。
   - 写入路径：
     - 一次性向导：`python -m launcher.cli_init` → 写到 `.env`（不进 config_store）。
     - 命令行：`python -m launcher.config set providers.openai.api_key sk-...`
     - GUI 「API 配置」/ 「Bots」 tab → 通过 `/api/configs` 与 `/api/credentials` 写 launcher_api_configs.json 与 ~/.wlwl-ass/config.json
   - schema：

     ```jsonc
     {
       "version": 1,
       "providers": {
         "openai":   {"api_key": "sk-...", "base_url": "...", "model": "..."},
         "anthropic":{"api_key": "sk-ant-...", "base_url": "...", "model": "..."}
       },
       "bots": {
         "telegram": {"bot_token": "...", "allowed_users": [...]},
         "feishu":   {"app_id": "cli_...", "app_secret": "...", "allowed_users": [...]}
       },
       "settings": {
         "permission_mode": "auto",
         "langfuse": {"public_key": "pk-lf-...", "secret_key": "sk-lf-...", "host": "https://cloud.langfuse.com"}
       }
     }
     ```
   - POSIX 下写入时自动 chmod 600；可选 `pip install keyring` 把 secret 写进 OS keychain。

4. **`temp/launcher_api_configs.json`**（最高优先级）
   - GUI 的 「API 配置」 tab 维护的 API 凭据全集（含 mixin、reasoning_effort、fake_cc_system_prompt 等高级字段）。
   - 所有条目同时生效（profile 机制已退役；多渠道用 `mixin_config` 实现）。
   - 不应手编（GUI round-trip 会保留 `***` 掩码值）；要么用 GUI，要么删掉重写。

合并代码见 `llmcore/_keys.py:_load_mykeys()`。环境变量与 `~/.wlwl-ass/config.json` 的 user 层永远全局可见（不受 `WLWL_PROJECT_ROOT` 影响），因为切项目不应丢凭据。

## 变量命名决定 Session 类型

`agentmain.py` 启动时只扫描变量名包含 `api` / `config` / `cookie` 的条目，并按变量名里的关键字决定使用哪种 Session：

| 变量名包含                     | Session 类             | 适用场景                       |
| ------------------------------ | ---------------------- | ------------------------------ |
| `native` + `claude`            | `NativeClaudeSession`  | Claude 原生工具协议（推荐）    |
| `native` + `oai`               | `NativeOAISession`     | OpenAI 原生工具协议（推荐）    |
| `claude`（不含 `native`）      | `ClaudeSession`        | 文本协议（deprecated）         |
| `oai`（不含 `native`）         | `LLMSession`           | 文本协议（deprecated）         |
| `mixin`                        | `MixinSession`         | 多渠道故障转移                 |

> 改一个变量名就会切换协议——这是设计上的约定，请勿随意改名。

GUI 「API 配置」 tab 保存条目时会按 `kind` 字段自动生成符合上述规则的变量名（`native_oai_config_<slug>` / `native_claude_config_<slug>` / `mixin_config_<slug>`）。

## 配置入口选择

| 你想做的事                                  | 推荐路径                                 |
| ------------------------------------------- | ---------------------------------------- |
| 第一次配置一个 API key                      | `python -m launcher.cli_init`（写 .env） |
| 接入多渠道、思考预算、CC 透传等高级配置     | GUI 「API 配置」 tab（写 launcher_api_configs.json） |
| 命令行批改凭据 / 设置                       | `python -m launcher.config set <path> <value>` |
| 容器化 / CI                                 | 注入环境变量 / 挂载 .env 即可            |
| 启用 Langfuse tracing                       | `python -m launcher.config set settings.langfuse '{"public_key":"pk-lf-...","secret_key":"sk-lf-...","host":"https://cloud.langfuse.com"}'` |
| 从老版本 mykey.py 迁过来                    | `python -m launcher.config migrate`（一次性，**会在下个 release 删除**） |

## 飞书命令

`frontends/fs_commands.py` 提供一组斜杠命令，飞书侧直接发就生效，**不走 LLM**，响应即时。

| 命令 | 用途 |
| --- | --- |
| `/sop <query>` | 在 Sophub 检索别人分享的 SOP（top 5） |
| `/sop read <id>` | 拉取完整 SOP；超过 3KB 落盘成 `.md` 文件发送 |
| `/sop stats` | Sophub 库统计 |
| `/screenshot [N]` | 截屏发回；`N` 为显示器序号（0=全屏，1/2…=单屏）。需 `pip install mss`（推荐）或 `pip install pillow` |
| `/run <cmd>` | 执行 shell 命令，30s 超时，stdout+stderr 截断到 4000 字符 |
| `/clip` | 读剪贴板 |
| `/clip <text>` | 写剪贴板 |
| `/open <path\|url>` | 用默认程序打开文件或 URL |

**安全门禁**：当 `bots.feishu.allowed_users = ['*']`（公开访问）时，**写类命令** `/run` `/clip <text>` `/open` 自动禁用，防止陌生人远程操控。`/sop /screenshot /clip`（读）始终可用。

## SOP 检索作为 wlwl-ass 工具

除飞书命令外，**LLM 在 reasoning 时也能直接调** `sop_search` / `sop_read` 两个工具（在 `assets/tools_schema.json` 中注册）。当 wlwl-ass 遇到陌生任务时，工具描述会引导它先去 Sophub 看有没有现成参考，避免从零摸索。

工具实现位于 `tools/sop_tools.py`，复用 `memory/skill_search/` 的 Sophub 客户端。设置 `SOPHUB_API_KEY` 或通过 `python -m skill_search --register-agent <name>` 注册一个匿名 agent，即可获得读权限（写权限需要邮箱认证）。

## 安全说明

- `~/.wlwl-ass/config.json` POSIX 下写入时自动 `0o600`（仅当前用户可读写）；Windows 上靠用户目录 ACL 默认保护。
- 安装 `pip install keyring` 后可用 `python -m launcher.config set --keyring providers.openai.api_key sk-...` 把 secret 写进 OS keychain（Windows Credential Manager / macOS Keychain / Linux libsecret）。JSON 里只留 `{"@keyring": "wlwl-ass.providers.openai.api_key"}` marker。
- `temp/launcher_api_configs.json` 与 `~/.wlwl-ass/config.json` 项目层 (`<project>/.wlwl-ass/`) 都已写入 `.gitignore`。
- 强烈建议不要把这些文件拷贝到协作目录、共享硬盘或随项目打 zip 分发。

## 启动器分工

| 启动器                              | 用途                                               |
| ----------------------------------- | -------------------------------------------------- |
| `python launch.pyw`（默认推荐）     | Tauri / Qt 主窗口：会话 / Bots / API 配置 / 设置  |
| `python launch.pyw --legacy-shell`  | 旧的 webview + Streamlit 默认会话流（向后兼容）    |
| `python agentmain.py`               | 纯 CLI / REPL 模式（headless / SSH）               |
| `python frontends/qtapp.py`         | 单文件 Qt 聊天面板（独立聊天窗，不带 launcher）    |

CLI flag（`--feishu` `--tg` `--qq` `--wecom` `--dingtalk` `--wechat` `--sched`/`--no-sched` `--llm_no`）会写入 `temp/launcher_options.json`，启动后由 launcher 自动读取并启用。下次不带 flag 运行也保持启用，直到通过设置标签页或反向 flag 关闭。

## Qt 主窗口 4 标签页

- **会话**：左列项目列表 + 右列项目详情；按钮含新建 / 启动 / 停止 / 打开 Streamlit / 激活 / 重命名 / 置顶 / 删除。多会话同时跑互不干扰。
- **Bots**：6 行表格，列出每个聊天平台 bot 的"配置 ✅/❌/⚠️ SDK 未装"、"状态 🟢 本进程 / 🟡 外部进程 / ⚪ 已停"，每行 [启动] [停止] [日志] 三个按钮；状态每 3 秒刷新。
- **API 配置**：增删改查 LLM 凭据。所有保存的条目同时生效，多渠道用 `kind=mixin` 的条目编排故障转移。
- **设置**：全局默认 LLM 索引、权限模式、项目根、context/autonomous 默认开关、L4 调度器开关。修改后点保存生效；运行中的会话需重启才能采用新默认值。

菜单栏含 文件（新建会话 / 退出）、视图（切换标签 / 立即刷新）、帮助（配置文档 / 关于）。状态栏显示运行中会话数、Bot 数、L4 调度器状态、版本号。
