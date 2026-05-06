---
version: 0.1.0
tags: [skill, self-improve, sop, meta]
summary: 用完一个 SOP 后发现可以改进时，提交 patch 提案给用户审核（不直接改文件）。
---
# Skill Self-Improvement SOP

**触发**：你**刚刚完整跑完一个 SOP**（按它执行了任务），并且**真的发现一处可改进**：
- 步骤遗漏（应该有但缺的检查）
- 描述错误（路径变了、命令名变了、依赖名变了）
- 表述不清（你执行时卡了一下，新 agent 也会卡）
- 新的边界场景（你刚踩的坑，没在 SOP 里）

**不触发**：
- 你没真的执行 SOP，只是读了一遍
- 你想改的只是风格 / 措辞偏好（"我觉得这样写更好"）
- 你想加的是"我自己的笔记"（用 update_working_checkpoint，不要改公共 SOP）
- SOP 没问题，是你的环境问题（用户的 Python 版本太老等）

## 工具：`skill_propose_patch`

```
skill_propose_patch({
  "skill_id": "plan",                           # 或 plan_sop / plan_sop.md
  "diff": "...unified diff or full new file...",
  "reason": "在 step 3 里漏了检查 lockfile，按现状跑会在 ci 报错。"
})
```

返回 `{"proposal": {"id": "prop_...", "status": "open", ...}}`。

**重要**：这个工具不直接改文件；只是把提案写进 `memory/skills_proposals.jsonl` 让用户在 GUI Skills tab 里审。用户点「采纳」才真正应用 diff，并自动备份原文件到 `memory/skill_backups/`。

## diff 格式

两种都接受：

**A. unified diff（推荐）** —— 改动小时
```
--- a/memory/plan_sop.md
+++ b/memory/plan_sop.md
@@ -42,3 +42,4 @@
 步骤2：
 ...
+- ⚠️ 必须先检查 lockfile：`cat package-lock.json` 或 `cat poetry.lock`
```

**B. 整文件新内容** —— 改动大时（重写超过半篇）
直接把整篇新 SOP 内容塞进 `diff` 字段。工具检测到不是 unified diff 就走 whole-file replace。

## reason 写法

差的：`"改进了一下"` / `"修了 bug"`
好的：`"step 3 漏了 lockfile 检查；今天我跑这个 SOP 在 React 项目踩了，npm ci 失败。补上检查后流程顺。"`

reason 是用户决定要不要采纳的核心信息。给具体场景 + 你撞的什么墙。

## 何时该提，何时不该提

✅ 提：
- 一个 SOP 你跑了 3 次都在同一处卡 → 该 SOP 缺步骤
- SOP 用了已弃用的 API（库重大升级后）
- 工具表新增了功能但 SOP 还在教旧办法

❌ 不提：
- 你这次失败是因为环境特殊（用户用了非主流 OS）→ 加注释而非改主线
- 改动超过 50% → 这不是改进是重写，应该 propose 新 SOP（用户还没决定是否要新增）
- 你不确定改动是否更好 → 把不确定写进 reason 让用户判断

## 已知限制

- `accept` 走系统 `patch` 命令；某些 Windows 环境没有，会走 whole-file fallback
- 同一 SOP 可以同时挂多个 open 提案；用户审完一个剩下的不会自动失效，需要手动决断
