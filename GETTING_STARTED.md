# 🚀 新手上手指南

> 完全没接触过编程也没关系，跟着做就行。Mac / Windows 都适用。
>
> 如果你已经有 Python 环境，直接跳到[第 2 步](#2-配置-api-key)。

---

## 1. 安装 Python

### Mac

打开「终端」（启动台搜索 "终端" 或 "Terminal"），粘贴这行命令然后回车：

```bash
brew install python
```

如果提示 `brew: command not found`，说明还没装 Homebrew，先粘贴这行：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

装完后再执行 `brew install python`。

### Windows

1. 打开 [python.org/downloads](https://www.python.org/downloads/)，点黄色大按钮下载
2. 运行安装包，**底部的 "Add Python to PATH" 一定要勾上**
3. 点 "Install Now"

### 验证

终端 / 命令提示符里输入：

```bash
python3 --version
```

看到 `Python 3.x.x` 就 OK。Windows 上也可以试 `python --version`。

> ⚠️ **版本提示**：推荐 **Python 3.11 或 3.12**。不要使用 3.14（与 pywebview 等依赖不兼容）。

---

## 2. 配置 API Key

### 下载项目

1. 打开 [GitHub 仓库页面](https://github.com/lsdefine/GenericAgent)（上游）
2. 点绿色 **Code** 按钮 → **Download ZIP**
3. 解压到你喜欢的位置

### 配置 API Key

wlwl-ass 的首次配置走 `.env` 文件 + 交互式向导，**不用编辑 Python 代码**：

```bash
python -m launcher.cli_init
```

向导会问 4 个问题（API 提供商 / endpoint / API key / 模型名），探测一下连通性，然后写到 `.env`。

> 多渠道 / mixin 故障转移 / Claude / Kimi / MiniMax / CRS 等高级配置走 GUI 「API 配置」 tab（保存到 `temp/launcher_api_configs.json`，含全部高级字段如 `reasoning_effort`、`thinking_type`、`fake_cc_system_prompt`）。配置加载优先级见 [docs/CONFIG.md](docs/CONFIG.md)。

### 配置示例（手编 .env）

**最常见的用法：**

```bash
# .env
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=http://你的API地址:端口/v1
OPENAI_MODEL=模型名称
```

```bash
# Anthropic 官方或 CC 透传
ANTHROPIC_API_KEY=sk-ant-你的密钥          # 真 sk-ant- → 自动 x-api-key
# ANTHROPIC_API_KEY=sk-user-...            # 透传渠道 → 自动 fake_cc 指纹
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_MODEL=claude-opus-4-7
```

> MiniMax / Moonshot / DeepSeek / GLM / OpenRouter 都是 OpenAI 兼容，只换 `OPENAI_BASE_URL` + `OPENAI_MODEL` 即可。

**多渠道 / 故障转移**：先用向导跑通一个，然后打开 GUI 在「API 配置」 tab 里加几个 native_oai / native_claude 条目，再加一条 `kind=mixin`、`llm_nos=[...]` 的 mixin 条目编排顺序。所有保存的条目同时生效。

---

## 3. 初次启动

终端里进入项目文件夹，运行：

```bash
cd 你的解压路径
python3 agentmain.py
```

这就是**命令行模式**，已经可以用了。你会看到一个输入提示符，直接打字发送任务即可。

试试你的第一个任务：

```
帮我在桌面创建一个 hello.txt，内容是 Hello World
```

> 💡 Windows 上如果 `python3` 不识别，换成 `python agentmain.py`。

---

## 4. 让 Agent 自己装依赖

Agent 启动后，只需要一句话，它就会自己搞定所有依赖：

```
请查看你的代码，安装所有用得上的 python 依赖
```

Agent 会自己读代码、找出需要的包、全部装好。

> ⚠️ 如果遇到网络问题导致 Agent 无法调用 API，可能需要先手动装一个包：
> ```bash
> pip install requests
> ```

### 升级到图形界面

依赖装完后，就可以用 GUI 模式了：

```bash
python3 launch.pyw
```

启动后会出现 wlwl-ass 主窗口，含 4 个标签页：

| 标签 | 用途 |
| --- | --- |
| **会话** | 多会话管理：新建/启动/停止/打开 Streamlit、置顶、删除 |
| **Bots** | 6 个聊天平台 bot（Telegram/QQ/飞书/企业微信/钉钉/微信）的状态 + 启停 |
| **API 配置** | 多渠道凭据 CRUD + Profile 切换（cc-switch 风格一键换档） |
| **设置** | 全局默认值（默认 LLM、权限模式、项目根、L4 调度等） |

> 第一次启动建议先到 **API 配置** 标签页加一组凭据，然后到 **会话** 新建一个项目开聊。
>
> 老版的 webview 浮窗仍可通过 `python launch.pyw --legacy-shell` 启动（向后兼容）。

### 可选：让 Agent 帮你做的事

```
请帮我建立 git 连接，方便以后更新代码
```

Agent 会自动配好。如果你电脑上没有 Git，它也会帮你下载 portable 版。

```
请帮我在桌面创建一个 launch.pyw 的快捷方式
```

这样以后双击桌面图标就能启动，不用再开终端了。

---

## 5. 能力解锁

环境跑起来之后，你可以逐步解锁更多能力。每一项都只需要**对 Agent 说一句话**：

### 基础能力

| 能力 | 对 Agent 说 | 说明 |
|------|-----------|------|
| **PowerShell 脚本执行** | `帮我解锁当前用户的 PowerShell ps1 执行权限` | Windows 默认禁止运行 .ps1 脚本 |
| **全局文件搜索** | `安装并配置 Everything 命令行工具进 PATH` | 毫秒级全盘文件搜索 |

### 浏览器自动化

| 能力 | 对 Agent 说 | 说明 |
|------|-----------|------|
| **Web 工具解锁** | `执行 web setup sop，解锁 web 工具` | 注入浏览器插件，使 Agent 能直接操控网页 |

解锁后，Agent 可以在**保留你登录态**的真实浏览器中操作：

```
打开淘宝，搜索 iPhone 16，按价格排序
去 B 站，查看我最近看过的历史视频
```

### 进阶能力

| 能力 | 对 Agent 说 | 说明 |
|------|-----------|------|
| **OCR** | `用rapidocr配置你的ocr能力并存入记忆` | 让 Agent 能"看到"屏幕文字 |
| **屏幕视觉** | `仿造你的llmcore，写个调用vision的能力并存入记忆` | 让 Agent 能"看到"屏幕内容 |
| **移动端控制** | `配置 ADB 环境，准备连接安卓设备` | 通过 USB/WiFi 控制 Android 手机 |

### 聊天平台接入（可选）

接入后可以随时随地通过手机给电脑上的 Agent 发指令。

对 Agent 说：`看你的代码，帮我配置 XX 平台的机器人接入`

支持的平台：**微信个人Bot** / QQ / 飞书 / 企业微信 / 钉钉 / Telegram

> Agent 会自动读取代码、引导你完成配置。

### 高级模式

以下模式全部**自文档化**——不用查手册，直接问 Agent 即可：

| 模式 | 对 Agent 说 |
|------|------------|
| **Reflect（反射）** | `查看你的代码，告诉我你的 reflect 模式怎么启用` |
| **计划任务** | `查看你的代码，告诉我你的计划任务模式怎么启用` |
| **Plan（规划）** | `查看你的代码，告诉我你的 plan 模式怎么启用` |
| **SubAgent（子代理）** | `查看你的代码，告诉我你的 subagent 模式怎么启用` |
| **自主探索** | `查看你的代码，告诉我你的自主探索模式怎么启用` |

> 💡 这就是 wlwl-ass 的核心设计理念：**代码即文档**。Agent 能读懂自己的源码，所以任何功能你都可以直接问它。

---

## 💡 使用越久越强

wlwl-ass 不预设技能，而是**靠使用进化**。每完成一个新任务，它会自动将执行路径固化为 Skill，下次遇到类似任务直接调用。

你不需要管理这些 Skill，Agent 会自动处理。使用时间越长，积累的技能越多，最终形成一棵完全属于你的专属技能树。

> 💡 如果你觉得某些重要信息 Agent 没有记住，可以直接告诉它：`把这个记到你的记忆里`，它会主动记忆。

**其他 Claw 的 Skill 也可以直接复用：**

- 让 Agent 搜索：`帮我找个做 XXX 的 skill` → 完成后 → `加入你的记忆中`
- 直接指定来源：`访问 XXX 文件夹/URL，按照这个 skill 做 XXX`

**保持更新：**

对 Agent 说：`git 更新你的代码，然后看看 commit 有什么新功能`

> Agent 会自动 pull 最新代码并解读 commit log，告诉你新增了什么能力。

> 更多细节请参阅 [README.md](README.md) 或 [详细版图文教程](https://my.feishu.cn/wiki/CGrDw0T76iNFuskmwxdcWrpinPb)。