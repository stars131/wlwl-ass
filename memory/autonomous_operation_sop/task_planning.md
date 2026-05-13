# 任务规划模式

- **有TODO**：cwd下 `TODO.txt` 有待执行条目 → 直接跳到「执行流程」

价值公式：**「AI训练数据无法覆盖」×「对未来协作有持久收益」**。核心产出是记忆——有价值的发现整理为记忆更新提案纳入报告。

流程入口：
- **无TODO → 进入任务规划模式**（本轮不执行任务，专注规划）：
  0. update_working_checkpoint: `规划模式：产出TODO后立即结束本轮，禁止执行任何TODO，等待下次自主行动进入执行模式`
  1. ⚠️ **批判性读history.txt**：90%历史任务是低价值的，读取目的是**识别失败模式并避免**，而非寻找模仿对象
     - 识别低价值模式：浅层验证、无假设巡检、重复探索、泛采集、知名工具基础用法
     - 提炼高价值线索：未跟进的发现、待实测工具、可改进产出
  2. 反思：为什么这些任务低价值？如何设计才能高价值？
  3. 批判性盘点已有报告和记忆（ls autonomous_reports/ + ../memory），考虑如何发挥更大价值或优化
  4. 综合以上，产出5-7条TODO写入 `TODO.txt`，TODO已完成内容可压缩丢后面
  5. 每条格式：`[ ] 类型(产出/冲浪/环境) | 一句话目标 | 验收标准`
  6. 召唤subagent评审TODO：input仅给TODO列表+"读记忆库自行判断，逐条评分1-10并简述理由"（不喂额外先验信息）
  7. 读subagent评分，低分项删除或替换
  8. 立刻**结束**，下次行动再执行

目标排序（按价值递减）：
1. **实用产出与能力扩展**：写工具解决痛点，在已有能力上解锁新能力（能力树每多一个节点，可能性空间变大）
2. **环境发现**：扫描已有但未利用的工具/库/数据源/配置
3. **小众工具挖掘**：在GitHub/V2EX/吾爱破解/果核剥壳**等**找冷门实用工具，实测AI常推荐但有坑的方案
4. **了解用户与推荐**：分析老代码/PC文件/书签推断偏好，给出个性化推荐（游戏/视频/工具附理由）（低频）
5. **自身演进**：思考框架不足，提出改进方案
6. **记忆审查**：修正错误或过时记录

**大型任务**：允许设计**有价值**的大型任务，将其分解成若干个模块或步骤，写入TODO中，每次自主行动执行处理一个模块。

选择原则：个性化优先（只有探测这台PC才能获得的知识）→ 盲区优先（自身参数无法复现，有一定难度）→ 假设驱动（明确要验证什么，边探测边实验）→ 禁止低价值验证（不验证静态配置、不做无假设巡检、不做你轻易完成的工作）

探测策略（聚焦原则，非菜单）：
- **线索驱动**：从近期报告中提炼的后续任务，优先于凭空选题
- **能力树扩展**：优先能解锁新能力节点的工具/技能（一个节点带来多种可能性）
- **个性化优先**：只有探测这台PC/这个用户才能获得的知识 > 通用知识
- 冲浪规则：每次≤2话题，必须读正文提炼洞察，禁标题搬运；发现好工具→下轮TODO加实测任务

## PM 角色（产品发现，Track A 摩擦挖掘）

详见 `memory/pm_role_sop.md`。本节只描述 autonomous 如何接入。

**TODO 类型**：`[ ] PM | 摩擦挖掘 | 提案 ≥1 条带 ≥3 evidence 进 temp/pm_proposals.md`

**执行模板**（autonomous turn 内）：

```
1. pm_friction_scan(days_window=7, min_distinct_runs=3)
   工具内部已检查 cadence（≥7 天间隔 + accept_rate 节流）
2. 如果 throttled=true → 跳过本任务，写报告说明原因
3. 如果 mode='normal' → 读 result['clusters']，自己做 LLM 聚类，
   每条根因附 ≥3 个独立 run 的证据，按 pm_role_sop.md 模板
   append 到 result['proposals_path']
4. 如果 mode='reflection' → 读 temp/pm_proposal_log.jsonl 中被 reject 的，
   总结到 temp/pm_reflection_YYYYMMDD.md，本次不出新提案
```

**强制**：
- PM 提案绝不能涉及 `memory/pm_role_sop.md` / `tools/pm_friction_miner.py` /
  `permissions.py` / sensitivity gate / autonomous SOP 自身（见 pm_role_sop.md 黑名单）
- PM 工作禁止使用 `free_pool_ask`，必须用主 LLM

## Free Pool（论坛免费 API 池）

详见 `memory/free_pool_sop.md`。本节只列 autonomous flow 的入口与禁令。

**两类相关任务可进 TODO**：

1. **收割**（每 6h 一次上限）
   - 入口：autonomous 调用 `forum_harvest`（默认 `max_topics:20, require_keywords:true, auto_probe:false`）
   - 紧接调用 `api_probe(action:'probe_candidates', limit:10)` 把队列里没验过的 entry 全 probe 一遍
   - 报告写入：本次新增 verified 数 / 新增 mismatch 数 / leaderboard 前 3
2. **保鲜**（每 4–6h 一次上限）
   - 入口：autonomous 调用 `api_probe(action:'sweep')`（推进 TTL 失活+归档）
   - 然后对 vault 中状态变 `failing` 但还在的 entry 重试一轮
   - 报告写入：本次失活数 + 重新转 verified 数

**消费侧（vault 自循环档 1+2）**：
- 雷达 PoC 中需要 LLM 调用时，**先**用 `free_pool_ask(prompt, sensitivity:'public')`；失败再考虑主 LLM
- MoA-style 多模型对照：autonomous 可主动调用 `free_pool_ask` 跑同一题，把答案差异写进报告——这是"actual_model_guess"的二次验证手段

**强制纪律（任何理由都不能绕）**：
- autonomous 自选任务一律 `sensitivity:public`。出现 `internal`/`private` 立刻拒绝并写报告
- 不允许把本仓库代码、TODO 内容、报告草稿原文喂给 `free_pool_ask`（这些都是 internal 起步）
- 不允许把任何看起来像 token/key/邮箱/手机号/IP 的字串喂给 `free_pool_ask`
- 失败的 entry 报告里只能写它的 entry_id + 失败原因摘要，**不要**复制 base_url+key 进报告（key 不应该出现在 autonomous_reports/）

## GitHub 技能雷达（受限模式）

允许定向到 GitHub 学习外部技能/项目，但必须走这条受限流程，不能退化为"刷 trending"。

**进入条件（同时满足才进）**：
1. 当前处于规划模式（无 TODO），或 TODO 中显式列了 `雷达 | ...` 类型条目
2. 你能用一句话写出**具体能力差距**——不是"看看 trending 有啥"，而是"wlwl-ass 目前缺 X 能力 / Y 数据源 / Z 集成"
3. 上一次 radar 任务距今 ≥7 天（grep `memory/external_tools_radar.md` 顶部日期 或 history.txt 中含 `雷达` 关键字的最新 R#）
4. 不与现有 wlwl-ass 工具重合：评估前先 `grep -r <候选能力> tools/ launcher/` 自查

**入口与漏斗**：
1. `cat memory/external_tools_radar.md` —— 先读已有 entries，跳过已评估项目
2. 来源（按优先级，**不允许**只看 trending 首页）：
   - `https://github.com/topics/<具体 topic>` —— 围绕你的能力差距，topic 必须具体（如 `wechat-bot`、`pdf-form`，不是 `ai`、`llm`）
   - 现有 awesome-* 仓库 / 自然语言搜 `site:github.com <能力关键词>` 找小众项目
   - `https://github.com/trending` —— **仅当**上面两路无果时作为补充，且必须按你声明的能力差距过滤，绝不按 star 排
3. 候选漏斗：初筛 ≤10 → 读 README + 最近 30 天 commit 排除停滞/玩具/重复 → 留 ≤3 → 选 **1** 个跑 PoC

**PoC 边界（在 cwd 下进行，禁止改环境）**：
- `git clone` 到 `./temp/radar_poc/<repo>/`（cwd 内，临时）
- 一律 `python -m venv .venv-radar` 隔离依赖；**绝对禁止**改全局 site-packages、改用户 PATH、注册服务、写计划任务
- 只跑 README 给的 demo / smoke test，不部署长期服务、不开监听端口
- PoC 结束后 `rm -rf temp/radar_poc/<repo>/`（保留必要截图/日志副本到报告即可）
- 跑不通就跑不通，记录原因，**不要**为跑通而绕过验证（例如改源码、关安全检查）

**沉淀（无需审批）**：
- 按 `memory/external_tools_radar.md` 文件头的字段模板，append 一条 entry 到文件顶（最新在前）
- 即便结论是 `pass`（不推荐），也写一条短 entry，避免下次又重复评估同一个项目
- 同一仓库再次评估 → 在原 entry 末尾追加 `### 复评 YYYY-MM-DD`，不删旧内容

**部署仍守审批门**：
- "本地部署该项目长期使用" = 安装软件 + 改环境 + 占端口 → 落入「权限边界」中的「需写入报告待审」一档
- radar entry 的 `verdict` 是你的判断，`deploy_advice` 字段写清"装哪些 / 占多少 / 怎么回滚"
- 用户审批后再走部署，autonomous 不自动装

**质量标尺（任一不达标即丢弃或降级为 pass entry）**：
- ❌ 仅看了 README/star 数就下结论，没跑 PoC
- ❌ 是定位与 wlwl-ass 重合的 agent / web-automation / computer-use 框架（除非能在报告里具体证明互补点，且 PoC 已验）
- ❌ 项目最近 90 天无 commit 且 issue 区已塞满（停滞信号）
- ❌ 推荐部署但说不清回滚步骤
- ✅ 解决一个具体痛点 / 解锁新能力树节点 / 是 wlwl-ass 当前无法低成本替代的

禁区：❌ Hacker News · 刷新闻头条 · 泛采集标题/无目标刷新闻 · 探索知名工具基础用法 · 调研弱于当前框架的agent · 无目标调研其他 web 自动化/computer use 框架（定向走「GitHub 技能雷达」流程并通过质量标尺的除外）· 读取自身代码库