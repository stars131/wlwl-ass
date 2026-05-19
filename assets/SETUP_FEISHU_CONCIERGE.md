# 飞书 Concierge（朋友代聊机器人）配置指南

> 让朋友也能跟"你的助理"约时间、问问题——而**不**继承你 owner bot 的 `code_run` / `file_write` 权限。
>
> 这是 [ADR-0011](../docs/adr/0011-feishu-concierge-bot.md) + [完整 spec](../docs/specs/feishu-concierge-bot.md) 的对应运行手册。本文档假设你已经按 [`SETUP_FEISHU.md`](./SETUP_FEISHU.md) 配好了 **owner bot**（也就是给你自己用的那把）。

---

## 0. 它做什么

- 朋友在飞书 DM 这个机器人："下周想吃饭" → 机器人根据**你的日历空闲**回 2–3 个时段
- 朋友选了一个 → 机器人发**审批卡片**给你的 owner bot（一键确认 / 拒绝 / 改时间）
- 朋友问"她现在在哪上班" → 命中你**手动维护**的 KB allowlist 才答；不在的话礼貌转告
- 朋友写代码请求 / 索取身份证号 / 其他敏感问题 → 礼貌拒绝，**不**升级、**不**触达你

它**不会**：跑代码、写文件、控制浏览器、读你的私人对话记忆、主动发消息给朋友。

---

## 1. 前置条件

- 已配置好 owner bot（见 `SETUP_FEISHU.md`），有自己的 `bots.feishu.app_id` / `app_secret`
- `lark_oapi` 已装（owner bot 已经用上了，不用再装）
- 至少有一个 LLM 配置（concierge v1 用规则分类器，**不强制要 LLM**——但 v2 会扩 LLM 兜底，先有最稳）

---

## 2. 创建第二个飞书应用

去 [飞书开放平台](https://open.feishu.cn/) → 「创建应用」→ **企业自建应用**。

**应用名建议**：跟 owner bot 区分清楚。比如 owner 叫"我的 Agent"，这个就叫 **"X 的小秘书"** —— 朋友看到的对话窗口上显示的就是这个名字。

### 添加机器人能力

应用详情页 → 左侧「**添加应用能力**」→「**机器人**」→ 添加。

### 开通权限（**严格按这个列表**）

左侧「**权限管理**」→「API 权限」→ 搜索并开通：

| 权限名 | 必要性 |
|---|---|
| `im:message` | ✅ 必需，接收消息 |
| `im:message:send_as_bot` | ✅ 必需，以 bot 身份回复 |
| `contact:user.id:readonly` | ✅ 必需，解析发送者 open_id |

> **不要**给这个 app 开 `im:resource` / `im:resource:upload` / `calendar:calendar`。concierge 不需要发文件、不直接读写日历——日历由 owner bot 通过 kernel + `calendar_worker` 走。这是 ADR-0011 § 6.3 的特权隔离设计。

### 配置长连接事件

左侧「**事件与回调**」→「**事件配置**」标签页 →
顶部两个 tab 选「**使用长连接接收**」（**不**选自建回调 URL）。

下方「**添加事件**」→ 搜索 `接收消息` → 勾选 **`im.message.receive_v1`** (IM v2.0)。
**就这一个**——其他 `card.action.trigger`（owner 点确认按钮回流）是 Phase 3 的事。

### 创建版本 + 发布

「**版本管理与发布**」→「创建版本」：
- 版本号：`1.0.0`
- 可用范围：选「**部分成员**」→ 加你自己 + 测试朋友
- 提交审核

测试企业你就是管理员，到 [飞书管理后台](https://feishu.cn/admin)「应用审核」自审通过。

---

## 3. 写入凭证

```bash
python -m launcher.config set bots.feishu_concierge.app_id     "cli_xxx"
python -m launcher.config set bots.feishu_concierge.app_secret "xxx"
```

> 写到 `~/.wlwl-ass/config.json`，POSIX chmod 600；不入 git。

---

## 4. 收集 open_id

`fsapp_concierge.py` 还没起的话用探针脚本，方便看 inbound：

```bash
python scripts/feishu_concierge_probe.py
```

然后**你自己**用飞书发一条消息给新机器人，终端会打印：

```
sender open_id:  ou_yourselfxxx
```

记下来。**朋友们**同理——让他们一个个发一条，把每个人的 `ou_...` 都收集了。

写入：

```bash
# 你自己的 open_id（在 OWNER app 视角下的；同一个人在不同 app 下 open_id 不同！）
# concierge 用这个 ID 决定 escalate 卡片发给谁
python -m launcher.config set bots.feishu_concierge.owner_open_id_on_owner_app "ou_yourself_under_OWNER_APP"

# 允许的朋友列表（concierge app 视角下）
python -m launcher.config set bots.feishu_concierge.allowed_friends '["ou_friend1","ou_friend2"]'

# （可选）你自己在 CONCIERGE app 视角下的 open_id —— 配上后，concierge 会
# 静默忽略你直接 DM 给它的消息（按设计你应该用 owner bot 而非 friend bot）。
# 默认空 = 允许 owner 自测：先空着开 bot 自己 DM 调试，调好再配上让 friend
# bot 对自己装陌生人。注意这个跟上面 owner_open_id_on_owner_app 的视角不同。
python -m launcher.config set bots.feishu_concierge.owner_open_id_on_concierge_app "ou_yourself_under_CONCIERGE_APP"
```

> ⚠️ **重要**：`owner_open_id_on_owner_app` 是你在 **owner app** 视角下的 open_id（因为 escalate 卡片是通过 owner app 发的）。`owner_open_id_on_concierge_app` 是你在 **concierge app** 视角下的 open_id（inbound 事件里看到的 ID 是这个视角）。`allowed_friends` 里的朋友 ID 也是 **concierge app** 视角。这三类 ID 不要混——
> - **拿 owner 视角 open_id**：起 owner bot (`python frontends/fsapp.py`) 然后给它发消息，看终端
> - **拿 concierge 视角朋友/自己 ID**：起这个探针脚本，让朋友（或你自己）发消息，看终端

---

## 5. 可选：调参

```bash
# 你的"对外公开"工作时段（不要跟你真日历完全一致——这是朋友可以约的窗口）
python -m launcher.config set bots.feishu_concierge.working_hours \
  '{"weekday":["09:00-12:00","14:00-18:00","19:30-21:30"],"weekend":["10:00-22:00"]}'

# 默认会议时长（朋友没说时长时用）
python -m launcher.config set bots.feishu_concierge.default_meeting_minutes 60

# 会议前后缓冲（分钟）
python -m launcher.config set bots.feishu_concierge.meeting_buffer_min 15

# 允许 concierge 回答的话题白名单（必须跟你 KB 里的 topic 字段对得上）
python -m launcher.config set bots.feishu_concierge.topics_allowed \
  '["employer","contact_window","weekend_plan","city"]'

# 让 concierge 用你自定的口吻
python -m launcher.config set bots.feishu_concierge.persona \
  "你是 Alice 的助理小 W。语气友好、简洁、靠谱。当不确定时永远问 Alice。"

# 每个朋友的速率上限（防 spam）
python -m launcher.config set bots.feishu_concierge.rate_limit_per_friend \
  '{"window_s":60,"max_msgs":8}'

# ★ 启用 LLM 聊天兜底（v2.1，默认关）
# 当规则没识别出 schedule/qa/out_of_scope/escalate_now 时（也就是
# 朋友只是在闲聊"在干啥""今天好热"），用 LLM 生成自然回复。
# 规则命中的 4 条路径仍走原路——LLM 改变不了 out_of_scope 的硬判定。
python -m launcher.config set bots.feishu_concierge.llm_enabled true
```

> **关于 LLM 兜底**：开启后 concierge 启动时会从 `mykeys` 里挑第一个可用的 LLM session（跟 owner bot 同源——你在 GUI 「API 配置」/`.env` 里配的任意一把 key 都行）。每次只在朋友说了规则没匹配上的话时才调一次，每个 turn 独立，**不**累计上下文（朋友的多轮上下文活在文件 SessionStore 里，LLM 看不到——只看当前这一句 + persona system prompt）。LLM 输出会自动 (a) 剥除 `<thinking>` 等推理 tag，(b) 剪到 800 字符以内，(c) 去掉 "助理:" 类前缀。
>
> **不开 LLM 也能跑**——朋友说"在干啥"会得到一句模板回复"嗯嗯～有事儿可以跟我说"。开了 LLM 朋友看到的就是真聊天的体感。

---

## 6. 维护知识库

朋友能问到的所有"关于你"的事实都活在 `temp/concierge_kb.jsonl`。用 CLI 管：

```bash
python -m launcher.cli_kb list
python -m launcher.cli_kb add --topic employer \
    --summary "Alice 在 ABC 公司做 ML" \
    --long   "Alice 自 2025-08 起在 ABC 公司 ML 平台组" \
    --visibility friends
python -m launcher.cli_kb show --topic employer
python -m launcher.cli_kb edit --topic employer --summary "新的"
python -m launcher.cli_kb delete --topic employer
```

> **要让朋友能问到这个 topic，必须满足两个条件**：(1) 写进了 KB，(2) topic 名出现在 `bots.feishu_concierge.topics_allowed` 里。 KB 里有但没在 allowlist 里的 topic 会触发"这个我不能直接告诉你，我帮你转告？"路径——KB 是真相，allowlist 是公开决定。

---

## 7. 启动

**默认**：只要 `bots.feishu_concierge.app_id` + `app_secret` 都配齐且 `lark_oapi` 已装，**concierge 会跟 GUI/`launch.pyw`/`python -m launcher.api_server` 一起自动启动**——和 owner bot 同样的待遇。你不用手动跑命令。

**手动启动**（调试或不开 GUI 时）：

```bash
python frontends/fsapp_concierge.py
```

期望输出：

```
============================================================
飞书 Concierge 已启动（长连接模式）
  app_id:        cli_aa827b97d8b8dbc6
  owner_open_id: ou_xxxxxxxx
  allowed_friends: ['ou_friend1', 'ou_friend2']
  escalate live: yes
  press Ctrl+C to stop
============================================================
[concierge] LLM smalltalk via <你的 LLM session name>     ← 开了 llm_enabled
```

`escalate live: yes` 表示 owner app 凭证也读到了，朋友约时间的卡片会**真的发**到你的飞书 owner 客户端；`no (stub → JSONL)` 表示只写到 `temp/concierge_escalations.jsonl`——通常是 owner bot 还没配。

**首次自启动看到的提示**（凭证齐但还缺软配置）：

```
⚠️  allowed_friends 为空 —— 所有朋友消息将被静默丢弃。
    `python -m launcher.config set bots.feishu_concierge.allowed_friends '["ou_friend1"]'`
    （或 '["*"]' 公开访问；改完重启此进程）

ℹ️   owner_open_id_on_owner_app 未配 —— 朋友约时间的卡片会落到
    temp/concierge_escalations.jsonl 而非直接弹到你飞书。
    配完后 escalate live 模式自动启用（重启此进程）。
```

这是**预期行为**——concierge 已经接通飞书 WS，但因为你还没告诉它"哪些朋友允许聊"、"卡片发给谁"，所以在等你补完配置。补完后重启 concierge 进程（GUI Bots tab 点重启，或 `taskkill /F /PID <19533 占用者>` + 再启）即可生效。

---

## 8. 也可以通过 GUI 一键起停

`launcher.bot_manager` 已经注册第 7 个 BotSpec（`feishu_concierge`，lock_port 19533）——

启动 GUI（`python launch.pyw`）→ **Bots** tab → 你应该看到第 7 行「**飞书·小秘书**」。绿灯起、红灯停。

**默认自启动**：`auto_start=True`。意思是只要 GUI/`launch.pyw` 启动时 `app_id` + `app_secret` 都齐，**concierge 会自动起**，不需要你点开关。其他 IM bot（telegram/qq/owner 飞书等）的行为完全一致。

---

## 9. 排错

| 现象 | 原因 / 解决 |
|---|---|
| 启动报 `fs_concierge_app_id 未配置` | `python -m launcher.config get bots.feishu_concierge.app_id` 看看，可能是粘贴错了 |
| 启动报 `Another instance is already running` | 19533 被占用——`taskkill /F /PID <占用 19533 的 pid>` 或重启 |
| 朋友发消息没回应 | 看 stdout 是否打 `[concierge] inbound`。打了说明事件通；没打说明事件订阅没起作用，回控制台检查长连接 tab + im.message.receive_v1 + 版本发布 |
| 朋友发了但被 `drop ... not in allowed_friends` | `allowed_friends` 没加这位的 open_id |
| 朋友说"约下周吃饭"没出 slot | 你的日历是空的、且 `working_hours` 没覆盖那段时间 |
| 朋友选了 slot 但你没收到飞书卡片 | `escalate live: no` —— 缺 `fs_app_id` / `fs_app_secret`（owner app）；或 owner_open_id_on_owner_app 没配对。看 `temp/concierge_escalations.jsonl` 兜底 |
| 朋友问 employer 但答"我不能告诉你" | KB 有但 `topics_allowed` 没加 `employer`——补一下 |
| 朋友问 employer 但答"我不太清楚" | KB 里没 `employer` topic——`cli_kb add` 一下 |

---

## 10. v2 / v3 还会做什么

- **owner 点卡片按钮回流**（Phase 3）：owner 在飞书直接点「确认」，concierge 自动写日历 + 通知朋友"已确认"。当前 v1/v2 需要 owner 手动到 owner bot 文本 confirm。
- **GUI Concierge tab**：审计日志可视化 + KB 编辑器
- **群聊**（v3）
- **跨平台 concierge**：Telegram / 钉钉 / 微信复用 ConciergeAgent

---

*文档版本：v1.0 | 更新：2026-05-17 | 对应代码：Phase 2 of ADR-0011*
