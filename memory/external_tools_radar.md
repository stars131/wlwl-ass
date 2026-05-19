# External tools radar — 沉淀池

来源：`memory.ecosystem_radar.orchestrate(mode='poc')` 和 autonomous 雷达任务。每条 entry 顶在前（最新在前）。同一 repo 二次评估在原 entry 末尾追加 `### 复评 YYYY-MM-DD` 而不是新建。

字段：
- `repo`：owner/repo
- `evaluated_at`：YYYY-MM-DD
- `capability_gap`：这个工具补什么能力差距
- `verdict`：adopt | revisit | pass
- `deploy_advice`：装哪些 / 占多少 / 怎么回滚（pass 也写"为何 pass"）
- `notes`：跑 PoC 时踩的坑、关键命令、smoke 输出片段

---
