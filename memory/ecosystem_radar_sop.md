---
version: 0.1.0
tags: [skill, radar, ecosystem, autonomous]
summary: 行业雷达 — 持续监听 AI 编码 agent / CLI / 工具链生态，分级飞书推送，高分项目走 PoC 漏斗后沉淀。
---

# Ecosystem Radar SOP

⚠️ **路径警告**：本 SOP 描述的工具入口是 `do_ecosystem_radar`（agent 可调）或 \
`python -m memory.ecosystem_radar --mode ...`（CLI / scheduler 调）。报告输出固定到 \
`autonomous_reports/`（temp/ 下），沉淀写到 `memory/external_tools_radar.md`。

## 这个工具解决什么

业内 agent/CLI 工具发布节奏极快（Codex 终端更新、Gemini CLI、Lark CLI、Claude Code 更新等）。\
现有 `memory/autonomous_operation_sop/task_planning.md §GitHub 技能雷达` 是被动的——agent 闲时\
才挑话题做评估。这个工具把它**主动化**：通过 `reflect/scheduler.py` 定时调用，把信号分级，\
高优级即时飞书推送，热度信号攒到每日 digest，trending 项目自动走 PoC 漏斗。

## 触发入口

| 入口 | 模式 | 频率 |
|---|---|---|
| `sche_tasks/ecosystem_watch.json` | `watch` | `every_2h` |
| `sche_tasks/ecosystem_trending.json` | `poc` | 工作日 09:13 |
| `sche_tasks/ecosystem_digest.json` | `digest` | 每天 21:07 |
| Agent 主动 | `do_ecosystem_radar({"mode":"..."})` | 手动 |
| CLI | `python -m memory.ecosystem_radar --mode watch` | 手动 |

## 启停 / 运维

雷达通过 **独立 scheduler 进程** `scripts/radar_runner.py` 驱动，**不**依赖 `agentmain.py`——这意味着即使没有配 LLM provider key，雷达也能跑（free pool / 启发式兜底）。

### 启动 / 停止 / 状态

```cmd
start_radar.cmd                   # 启动（idempotent，不会重复启）
stop_radar.cmd                    # 停止
python scripts/start_radar.py --status   # 看 PID + log tail
```

或纯 Python（任何 shell 都能用，git-bash / PowerShell / cmd 行为一致）：

```bash
python scripts/start_radar.py            # 起
python scripts/start_radar.py --stop     # 停
python scripts/start_radar.py --status   # 看状态
```

PID 文件：`temp/radar_runner.pid` · 单例锁端口：`127.0.0.1:45765` · 日志：`temp/logs/radar_runner.log`

### 冷启动（避免一上来推 30 条历史 release 刷屏）

第一次部署或清掉 seen.json 后，**必须**先跑一次 dry-run 把当前所有 release 标为 "已读"：

```bash
python -m memory.ecosystem_radar --mode watch --dry-run
```

之后 watch 才会只推送时间点之后产生的**新** release。

### 状态文件

| 文件 | 含义 |
|---|---|
| `temp/ecosystem_radar_seen.json` | 30d TTL 去重集合 — 防同一条信号反复推 |
| `temp/ecosystem_radar_buffer.jsonl` | 当前 24h 内全部 scored item — digest 从这里取 |
| `temp/ecosystem_radar_archive/<YYYY-MM>/buffer_<YYYY-MM-DD>.jsonl` | 历史 digest 后归档 |
| `temp/ecosystem_radar.log` | orchestrate 内部日志 |
| `temp/logs/radar_runner.log` | scheduler 进程日志 |
| `temp/radar_pending_poc.md` | poc 模式产出的 prompt，等用户手动执行 |
| `sche_tasks/done/<YYYY-MM-DD_HHMM>_<tid>.md` | 任务执行 stub（scheduler 用此判断冷却） |

### poc 候选的人工执行

scheduler 跑 `poc` 模式时**只生成 prompt + 飞书提醒**，不直接 PoC（因为 PoC 需要 `code_run` 等 agent 工具）。当你收到 `🧪 [雷达·PoC 候选]` 飞书消息时：

1. 进 wlwl REPL 或 GUI 唤起 agent
2. 让它跑：`ecosystem_radar mode=poc force_repo=<owner/repo>` —— agent 收到的 `next_prompt` 会自动驱动它走完整个 PoC 漏斗
3. 完成后 agent 把结果 append 到 `memory/external_tools_radar.md`，再推一条飞书简短结论

也可以直接读 `temp/radar_pending_poc.md` 内容，复制到任意 agent / 你自己手动跑。

## 三模式

### `watch`（核心循环）

```python
from memory.ecosystem_radar import orchestrate
orchestrate("watch")
```

1. 并发跑 4 个采集器（`tools.ecosystem_sources`）：`github_releases`（Atom，无需 PAT）\
   + `github_trending`（HTML+BS4）+ `hn_ai`（Algolia API）+ `grok_live`（若 XAI_API_KEY 配了）
2. 用 `temp/ecosystem_radar_seen.json`（30d TTL）去重
3. 调 LLM 一次性分级（优先 free_pool_ask sensitivity=public，失败兜底主 LLM，再失败启发式）
4. **推送门**：
   - `tier=critical` → 立即 `feishu_send` 单条
   - `tier=notable` → 仅 buffer，等 digest
   - `tier∈(trending,news)` → 仅 buffer
5. 静默时段（`WLWL_RADAR_QUIET_HOURS=22-8`，默认开）：critical 也延后到 digest
6. 全部条目写入 `temp/ecosystem_radar_buffer.jsonl`

### `poc`（自动 PoC 漏斗）

```python
orchestrate("poc")                          # 自动选 top1
orchestrate("poc", force_repo="HKUDS/CLI-Anything")   # 手动指定
```

**这个模式不直接跑 PoC**——它返回一段 prompt，由 scheduler 喂给 autonomous agent loop。\
PoC 必须在 agent 主循环里跑，因为要用 `code_run` / `file_write` 这些原子工具。

选择逻辑：
1. 读 `temp/ecosystem_radar_buffer.jsonl` 近 24h 条目
2. 过滤 `tier ≥ trending` 且 `repo` 非空
3. 排除 `memory/external_tools_radar.md` 已有 entry 的 repo（用 grep）
4. 按 score desc 取 top 1

返回的 prompt 模板会带 agent 走以下 PoC 流程 ↓

## PoC 流程（agent 在 poc 模式被唤起后执行）

⚠️ **这一段是 agent 收到 `[生态雷达 PoC]` prompt 后必须遵守的步骤**。

### 步骤

1. **登记**：`update_working_checkpoint` 写本次目标 + 报告路径。
2. **clone**：
   ```bash
   git clone <url> temp/radar_poc/<repo_safe>/
   ```
3. **探测 build system**（按顺序，命中即停）：
   | 探测文件 | 装法 |
   |---|---|
   | `pyproject.toml` | `cd temp/radar_poc/<repo_safe> && python -m venv .venv-radar && .venv-radar\Scripts\activate && pip install -e .` |
   | `setup.py` | 同上 |
   | `requirements.txt` | `python -m venv .venv-radar && pip install -r requirements.txt` |
   | `package.json` | `npm i`（在子目录内） |
   | `Cargo.toml` | `cargo build`（cwd 内编译，不全局 install） |
   | `go.mod` | `go build ./...`（cwd 内） |
   | 无以上 | 读 README "Install" / "Quickstart" 段，按其命令装到 venv |
4. **smoke**：读 README 找最小 demo / `Usage` 段；跑一次确认 exit code 0 + 输出非空。
   - 找不到 demo → 看 GitHub Actions yaml 取官方 CI 命令复刻。
   - smoke 失败 → 还要再写一份诊断（错误日志、缺什么、改了什么、为什么失败）。
5. **评估 verdict**：
   - `adopt`：明显补能力差距，PoC 跑通，建议装到主环境（**用户审批后**才装，**不要**自动）
   - `revisit`：有潜力但 PoC 不稳定/依赖太重/接口不对路
   - `pass`：能力重合 / 半成品 / 文档不全 / 项目已停滞
6. **append entry** 到 `memory/external_tools_radar.md` 头部：
   ```markdown
   ## <repo>  
   - **repo**: `owner/repo`
   - **evaluated_at**: 2026-05-19
   - **capability_gap**: <一句话说补什么能力>
   - **verdict**: adopt | revisit | pass
   - **deploy_advice**: <装哪些 / 占多少 / 怎么回滚>，pass 也写"为何 pass"
   - **notes**: <PoC 关键坑 / 关键命令 / smoke 输出片段>

   ---
   ```
7. **飞书简短结论**：用 `code_run` 调 `tools.feishu.feishu_send` 推一句：
   ```
   ✅ 雷达 PoC | <repo> | verdict=adopt | <一句话>
   ```
8. **清理**：`shutil.rmtree("temp/radar_poc/<repo_safe>/")`。保留报告里需要的关键日志/截图副本。
9. **报告**：写到入参里的 `report_path`，按 `autonomous_operation_sop.md` 的报告规范。

### PoC 边界（绝对禁止）

- ❌ 改全局 site-packages（`pip install <pkg>` 不带 venv 激活）
- ❌ 占用 < 49152 的端口（PoC 项目要监听端口的，verdict 强制 `pass`，写"需用户审批后改主环境"）
- ❌ 注册 Windows 服务 / crontab / systemd unit
- ❌ 改全局 PATH / 改用户 PATH（写入 .bashrc/.zshrc/profile）
- ❌ 为了跑通改源码（除非是改一两行常量做演示，且写在 notes 里）
- ❌ 跑 90 天无 commit 且 issue 区已塞满 的项目（停滞信号 → 直接 pass）
- ❌ 跑定位与 wlwl-ass 重合的 agent 框架（除非能在报告里具体证明互补点）

### 质量标尺

✅ **该评 adopt**：
- 解决一个具体痛点（用户已经吐过槽，或者 TODO.txt 里挂着）
- 解锁新能力树节点（不是现有工具的更花哨版）
- wlwl-ass 当前无法低成本替代

❌ **必判 pass**：
- 只看了 README/star 数就下结论
- 是另一个 "agent / web-automation / computer-use" 框架（定位重合）
- 没跑过 smoke / 跑不通也不诊断
- 推荐 adopt 但说不清回滚步骤

## `digest`（每日总结）

```python
orchestrate("digest")
```

1. 读 `temp/ecosystem_radar_buffer.jsonl` 近 24h
2. LLM 聚类总结 → top 5（不足 5 条就给几条）
3. 一条飞书卡片推用户
4. 把 buffer 文件归档到 `temp/ecosystem_radar_archive/<YYYY-MM>/buffer_<YYYY-MM-DD>.jsonl`
5. 顺手清理 30 天前的 seen 条目

## 配置

`.env.example` 段（也支持 GUI / `launcher.config_store`）：

```bash
# WLWL_RADAR_WATCHLIST=openai/codex,anthropics/claude-code,google-gemini/gemini-cli,larksuite/lark-cli,sst/opencode,block/goose
# WLWL_RADAR_NOTIFY_TO=自己的飞书用户名或 ou_xxxx
# WLWL_RADAR_QUIET_HOURS=22-8           # 22:00-08:00 不推 critical，攒到 digest
# GITHUB_TOKEN=ghp_...                  # 可选；提速率限制
```

## 飞书推送格式

### critical 卡片

```
🚨 [行业雷达·critical] {title}
来源: github_release
仓库: openai/codex
链接: https://github.com/openai/codex/releases/tag/v0.13
理由: <LLM 给的一句话>
能补什么: <LLM 给的能力差距>
```

### digest 卡片

```
📰 [行业雷达·日报 2026-05-19]

[critical] Codex 终端 0.13 新增 X — https://...
[notable]  Gemini CLI 支持本地 plugin — https://...
[notable]  Anthropic 发新 MCP 规范 — https://...
[trending] HKUDS/CLI-Anything 上 trending — https://...
[news]     HN 讨论：Agent 路由的成本控制 — https://...
```

### PoC 卡片

```
✅ 雷达 PoC | HKUDS/CLI-Anything | verdict=adopt | 给 wlwl-ass 多源 CLI 能力，需手动装 npm
```

## 与现有 autonomous SOP 的关系

- 这个 SOP **覆盖** `memory/autonomous_operation_sop/task_planning.md §GitHub 技能雷达` 的「主动雷达」部分。
- 「受限模式」的质量标尺 / PoC 边界 **完全继承不变**——本 SOP 把它们重写在 §PoC 边界 段。
- autonomous 在执行 `[生态雷达 PoC]` prompt 时**不需要**再走 `task_planning.md` 的 「能力差距声明」 + 「≥7 天间隔」检查——orchestrator 已经在 buffer/notes 双重去重做过了。

## 故障与降级

| 现象 | 自动行为 | 用户行动 |
|---|---|---|
| 单个采集器 timeout | 其他源照常出，错误写 `temp/ecosystem_radar.log` | 看 log；persistent 失败考虑禁掉源 |
| LLM 评分整体失败 | 走启发式分级，仍然推送 | 检查 `mykeys` / free pool 状态 |
| 飞书 send 失败 | log 记 `[feishu_send error]`，不影响其他流程 | 检查 fs_app_id/secret，调 `feishu_refresh_users` |
| BS4 解析 trending 页面失败 | 该源返回 0 条 | GitHub 改版了——更新 selector |
| Buffer 文件损坏 | 跳过坏行，继续读 | 必要时手动归档 |

## 调试

```bash
# 单源 smoke
python -m tools.ecosystem_sources --source github_releases --watchlist openai/codex
python -m tools.ecosystem_sources --source github_trending
python -m tools.ecosystem_sources --source hn_ai
python -m tools.ecosystem_sources --source grok_live

# 评分（dry-run，不发飞书）
python -m tools.ecosystem_scorer --no-llm

# 编排器 dry-run
python -m memory.ecosystem_radar --mode watch --dry-run
python -m memory.ecosystem_radar --mode digest --dry-run

# 强制 PoC 某个 repo
python -m memory.ecosystem_radar --mode poc --force-repo HKUDS/CLI-Anything
```

## 不在本 SOP 范围内

- 邮件/Telegram/Discord 推送 —— 用户只要飞书
- 全自动装服务 / 改主环境 —— 永远在审批门后
- X/Twitter timeline 直接抓 —— 用 Grok live_search 的 x 源
- 写 GUI 面板 —— v1 仅在飞书消费
