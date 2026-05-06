---
version: 0.1.0
tags: [checkpoint, persistence, long-task, autonomous]
summary: When to snapshot intermediate task state to disk so a context flush / restart doesn't erase progress.
---
# Checkpoint SOP

**触发**：以下任一条件
1. 任务预计 ≥10 个回合，或者跨多次会话才能完成
2. 自主模式（autonomous）下的多步任务，且每步都有可观察的离散产物（文件已写、进度已推进）
3. 用户主动让你"先停一下"或"明天接着做"

**不触发**：≤5 步的简单查询/修改、纯对话、信息汇总（没有真"中间状态"）。

## 为什么必须用

context window 总会被 trim；token 成本攒不住；用户随时可能 ctrl+c。没有 checkpoint，10 步任务跑到第 8 步重启 = 全废。

## 工具：`checkpoint`

```
checkpoint({"action":"save", "task_id":"refactor-auth-2026-05-04", "state":{...}, "note":"after step 3 of 7"})
checkpoint({"action":"load", "task_id":"refactor-auth-2026-05-04"})              # 最新
checkpoint({"action":"load", "task_id":"...", "checkpoint_id":"20260504..."})    # 指定
checkpoint({"action":"list", "task_id":"refactor-auth-2026-05-04"})              # 该任务全部 cp
checkpoint({"action":"list_tasks"})                                              # 所有任务
checkpoint({"action":"clear", "task_id":"refactor-auth-2026-05-04"})             # 任务结束清空
```

`task_id` 由你自己取，要稳定 + 唯一。推荐格式：`<verb>-<obj>-<YYYYMMDD>`，例如 `refactor-auth-20260504`。

## 推荐 state 结构

```json
{
  "step_index": 3,
  "total_steps": 7,
  "next_action": "运行测试",
  "discovered_constraints": ["用户的 Python 是 3.10", "目标库版本必须 ≥ 2.x"],
  "completed": ["scan", "design", "scaffold"],
  "files_touched": ["a.py", "b/c.py"],
  "pending_decisions": []
}
```

不要把巨型字符串塞进 state（一段 200KB 的代码 dump 之类）。state 应当是**可以让你下次接着干的最小信息**，别的都从文件系统重读。

## 什么时候 save

- 每完成一个有意义的"步骤"（不是每个 file_patch 都存）
- 写完一段长代码后但还没测前
- 切换大方向之前（防回滚）
- 用户提到要暂停 / 你预感快撞 token 上限

## 什么时候 load

- 接到新对话且 task_id 之前出现过
- 用户说"接着昨天那个" / "你之前在做啥"
- 自主模式醒来的第一件事

## 任务结束记得 clear

防止注册表无限增长。也避免下次同名 task_id 误命中旧状态。
