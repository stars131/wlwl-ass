# 飞书 Agent 配置指南

> 让你的个人电脑变成飞书机器人的大脑，随时随地通过飞书对话控制你的电脑。

---

## 📋 目录

1. [前置条件](#前置条件)
2. [方案选择](#方案选择)
3. [企业用户配置](#企业用户配置)
4. [个人用户配置](#个人用户配置)
5. [项目配置](#项目配置)
6. [运行与测试](#运行与测试)
7. [常见问题](#常见问题)

---

## 前置条件

### 必需环境

- Python 3.8+
- 本项目完整代码
- LLM API 密钥（Claude/OpenAI 等，已在 `llmcore/mykeys` 中配置）

### 安装依赖

```bash
pip install lark-oapi
```

---

## 方案选择

| 你的情况           | 推荐方案                   | 预计耗时  |
| ------------------ | -------------------------- | --------- |
| 公司已有飞书企业版 | [企业用户配置](#企业用户配置) | 5-10分钟  |
| 个人用户/学习测试  | [个人用户配置](#个人用户配置) | 10-15分钟 |

---

## 企业用户配置

> 适用于：你的公司使用飞书，你有权限创建应用或联系管理员审批

### 步骤 1：创建应用

1. 访问 [飞书开放平台](https://open.feishu.cn/)
2. 登录你的企业飞书账号
3. 点击右上角「创建应用」→「企业自建应用」
4. 填写应用信息：
   - 应用名称：`我的Agent助手`（可自定义）
   - 应用描述：`个人AI助手`
   - 应用图标：可选

### 步骤 2：添加机器人能力

1. 进入应用详情页
2. 左侧菜单选择「添加应用能力」
3. 找到「机器人」，点击「添加」
4. 配置机器人信息（可保持默认）

### 步骤 3：配置权限

1. 左侧菜单「权限管理」→「API 权限」
2. 搜索并开通以下权限：
   - `im:message` - 获取与发送单聊、群组消息
   - `im:message:send_as_bot` - 以应用身份发送消息
   - `contact:user.id:readonly` - 获取用户 ID

### 步骤 4：获取凭证

1. 左侧菜单「凭证与基础信息」
2. 记录以下信息：
   - **App ID**：`cli_xxxxxxxx`
   - **App Secret**：`xxxxxxxxxxxxxxxx`

### 步骤 5：发布应用

1. 左侧菜单「版本管理与发布」
2. 点击「创建版本」
3. 填写版本信息，提交审核
4. **联系企业管理员审批**（或自己是管理员直接审批）

### 步骤 6：获取你的 Open ID

1. 应用审批通过后，在飞书中搜索你的机器人
2. 给机器人发送任意消息
3. 运行以下代码获取你的 Open ID：

```python
# 临时运行一次，获取 open_id
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

client = lark.Client.builder().app_id("你的APP_ID").app_secret("你的APP_SECRET").build()

# 监听消息，打印发送者的 open_id
def handle(data):
    print(f"你的 Open ID: {data.event.sender.sender_id.open_id}")

# ... 或者查看 frontends/fsapp.py 运行时的日志输出
```

---

## 个人用户配置

> 适用于：没有企业飞书账号，想个人测试使用

### 步骤 1：创建测试企业

1. 访问 [飞书开放平台](https://open.feishu.cn/)
2. 使用个人手机号注册/登录
3. 点击右上角头像 →「创建测试企业」
4. 填写企业名称（如：`我的测试工作区`）
5. 创建完成后，你就是这个测试企业的**管理员**

### 步骤 2：创建应用

> 与企业用户步骤相同

1. 点击「创建应用」→「企业自建应用」
2. 填写应用信息

### 步骤 3：添加机器人能力

1. 进入应用详情页
2. 「添加应用能力」→「机器人」→「添加」

### 步骤 4：配置权限

1. 「权限管理」→「API 权限」
2. 开通权限：
   - `im:message`
   - `im:message:send_as_bot`
   - `contact:user.id:readonly`

### 步骤 5：获取凭证

1. 「凭证与基础信息」
2. 复制 **App ID** 和 **App Secret**

### 步骤 6：发布应用（测试企业可自审批）

1. 「版本管理与发布」→「创建版本」
2. 提交后，进入 [飞书管理后台](https://feishu.cn/admin)
3. 「工作台」→「应用审核」→ 通过你的应用

### 步骤 7：在飞书客户端使用

1. 下载 [飞书客户端](https://www.feishu.cn/download)
2. 登录你的测试企业账号
3. 搜索你创建的机器人名称
4. 开始对话！

---

## 项目配置

### 配置飞书凭证

通过 GUI 「Bots」 tab 写入，或用命令行：

```bash
python -m launcher.config set bots.feishu.app_id "cli_xxxxxxxxxxxxxxxx"
python -m launcher.config set bots.feishu.app_secret "xxxxxxxxxxxxxxxx"
python -m launcher.config set bots.feishu.allowed_users '["ou_xxxxxxxxxxxxxxxxxxxxxxxx"]'  # 必填
```

> ⚠️ **必填**：`allowed_users` 不再支持「留空 = 允许所有人」。空列表/缺省现在等于**拒绝所有**（agent 自带 `do_code_run`，开放给所有人 = 远程代码执行）。
> - 单人使用：`["ou_yourid"]`
> - 公开使用（高风险）：必须显式写 `["*"]`
> - 多人：`["ou_a", "ou_b"]`

写入位置：`~/.wlwl-ass/config.json`，POSIX 下自动 chmod 600。

### （可选）启用飞书日历作为日程后端

语音/工具产生的日程事件默认写本地 SQLite (`temp/calendar.db`)。要切换为「写到飞书我的日历」：

1. **加权限**：飞书开放平台 →「权限管理」→ 增开 `calendar:calendar`（需要管理员重新审批）。
2. **打开开关**：
   ```bash
   python -m launcher.config set bots.feishu.use_for_calendar true
   ```
3. 重启 `python -m launcher.voice_ws`。首启时 `FeishuCalendarStorage` 会调 `primary()` 拉用户主日历的 `calendar_id` 缓存到 `bots.feishu.calendar_id`，之后所有 `calendar.create_event.v1` / `update` / `delete` / `query` 直接读写飞书云。

注意：
- 切换后端不迁移历史数据，旧 SQLite 事件 id 在飞书侧不可达。
- 关闭：`python -m launcher.config set bots.feishu.use_for_calendar false`，重启后回退本地 SQLite。
- 飞书日历事件没有原生 tags 字段；本实现把 `tags` 编进事件描述顶部 `[tags:a,b]` 行，往返保留。

### 确认 LLM 配置

确保至少有一个 LLM 配置（向导写到 `.env`，或 GUI 「API 配置」 tab 保存到 `temp/launcher_api_configs.json`）。最快路径：

```bash
python -m launcher.cli_init       # 一次性向导
python -m launcher.doctor          # 检查现状
```

---

## 运行与测试

### 启动服务

```bash
cd /path/to/pc-agent-loop
python frontends/fsapp.py
```

### 预期输出

```
==================================================
飞书 Agent 已启动（长连接模式）
App ID: cli_xxxxxxxxxxxxxxxx
等待消息...
==================================================
```

### 测试对话

1. 打开飞书客户端
2. 找到你的机器人
3. 发送：`你好`
4. 等待回复（首次可能需要几秒）

---

## 可用命令

在与机器人对话时，可以使用以下特殊命令：

| 命令 | 说明 |
| ---- | ---- |
| `/new` | 开始新对话，清除当前上下文 |
| `/stop` | 中止当前正在执行的任务 |
| `/restore <关键词>` | 恢复之前的对话上下文（根据关键词搜索历史记录） |

### 命令示例

```
/new                    # 清空对话，重新开始
/stop                   # 停止正在运行的任务
/restore 昨天的任务      # 恢复包含"昨天的任务"关键词的历史对话
```

### 消息显示说明

- ⏳ 表示任务正在执行中
- 消息会实时更新，无需等待完成
- 超长回复会自动分段发送

---

## 常见问题

### Q: 提示「应用未发布」或「无权限」

**A:** 确保应用已发布且管理员已审批。测试企业用户需要在管理后台手动审批。

### Q: 发送消息后没有回复

**A:** 检查：

1. `frontends/fsapp.py` 是否在运行
2. 终端是否有错误日志
3. LLM API 密钥是否配置正确

### Q: 提示「invalid app_id」

**A:** 检查 `~/.wlwl-ass/config.json` 中 `bots.feishu.app_id` 是否正确复制（包含 `cli_` 前缀）。可用 `python -m launcher.config get bots.feishu.app_id` 检查。

### Q: 如何获取自己的 Open ID？

**A:** 运行 `frontends/fsapp.py` 后给机器人发消息，查看终端日志中的 `open_id`

### Q: 能否多人同时使用？

**A:** 可以。`frontends/fsapp.py` 已按 `open_id` 隔离会话——每位用户拥有独立的 Agent、history 与工作记忆，互不干扰。一个飞书应用仍只能一台电脑长连接，但同一应用下多用户对话彼此独立。详见下文「多用户隔离与个性化提示词」。

---

## 架构说明

```
你的飞书 ←→ 飞书云 ←→ 长连接 ←→ frontends/fsapp.py ←→ Agent ←→ 你的电脑
                              ↑
                         运行在你电脑上
```

- 消息通过飞书云转发到你电脑上运行的 `frontends/fsapp.py`
- Agent 处理请求后，通过飞书 API 回复消息
- **你的电脑必须保持运行** `frontends/fsapp.py` 才能响应消息

---

## 多用户隔离与个性化提示词

`fsapp.py` 按飞书 `open_id` 维护一个 agent 池：每位用户首次发消息时按需创建独立的 `GeneraticAgent`，闲置 1 小时自动回收。每个 agent 拥有自己的 history、working memory、`backend.history` —— 跨用户不会互相污染。

### 全局飞书提示词（所有用户共享）

```bash
python -m launcher.config set bots.feishu.system_prompt \
  "你正在飞书 IM 中与用户对话。回复要简洁，避免大段代码块；重要文件用 [FILE:路径] 标记。"
```

### 按用户覆盖（per-open_id 个性化）

先发消息让机器人打印你的 `open_id`（终端日志会有 `收到消息 [ou_xxxx]`），然后：

```bash
python -m launcher.config set bots.feishu.user_prompts.ou_abc123 \
  "你是张三的私人助理，他在做心理学博士论文，沟通风格直接。"

python -m launcher.config set bots.feishu.user_prompts.ou_xyz789 \
  "你是李四的工作助手，主要协助 Python 开发，回复时尽量给出代码示例。"
```

优先级：**`user_prompts[open_id]` > `system_prompt` > 空**。配置改动后重启 `fsapp.py` 生效（`mykeys` 在进程启动时一次性加载）。

### 验证

```bash
# 1. 设一个明显标记
python -m launcher.config set bots.feishu.system_prompt "回复必须以 [PROMPT-OK] 开头。"
# 2. 重启 fsapp.py
# 3. 任意飞书用户发 "hello"，回复应包含 [PROMPT-OK]
```

清空覆盖：`python -m launcher.config delete bots.feishu.user_prompts.ou_abc123`

---

## 下一步

- 自定义 Agent 行为：编辑 `assets/sys_prompt.txt`
- 添加新工具：编辑 `assets/tools_schema.json`
- 查看日志：运行时观察终端输出
- **想让朋友也能跟你的助理聊天（但不给他们 `code_run` 权限）？**
  见 [`docs/specs/feishu-concierge-bot.md`](../docs/specs/feishu-concierge-bot.md)
  与 [`docs/adr/0011-feishu-concierge-bot.md`](../docs/adr/0011-feishu-concierge-bot.md)
  —— 第二个飞书 app + 受限 agent（"小秘书"）的完整设计与实施分阶段计划。

---

*文档版本：v1.1 | 更新日期：2026-03-07*

**v1.1 更新内容：**
- 新增「可用命令」章节（/new, /stop, /restore）
- 新增消息显示说明（⏳ 进行中标记、实时更新等）
