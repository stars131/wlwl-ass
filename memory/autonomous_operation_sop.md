# 自主行动 SOP

⚠️ **路径警告**：autonomous_reports 在 temp/ 下，用`./autonomous_reports/`访问，**不是**`../memory/autonomous_reports/`或`../autonomous_reports/`！TODO在cwd下。
报告存于 `./autonomous_reports/`，文件名 `RXX_简短描述.md`（XX从 history.txt 推断自增）。

授权你进行自主行动，只要不对环境造成副作用都可进行。

## 启动（第一步）
- update_working_checkpoint: `自主行动｜收尾时重读SOP | from autonomous_operation_sop.helper import *; set_todo()/complete_task(tasktitle, historyline, report_path)`

第二步：
```python
from autonomous_operation_sop.helper import *
print(get_history(40))  # 了解历史避免重复
print(get_todo())       # 查看待办
```

## 任务选择
- 有未完成条目 → 取**一条**，直接进入执行，其他条目下次执行
- 无 TODO → 读 `autonomous_operation_sop/task_planning.md` 规划，下次执行
- 不连续两次选相同子任务
- 价值公式：**「AI训练数据无法覆盖」×「对未来协作有持久收益」**
- **外部学习/部署**：若任务是"看 GitHub 学新东西"或"部署外部项目"，必须走 `task_planning.md` § GitHub 技能雷达 流程，沉淀到 `memory/external_tools_radar.md`；本地部署仍属审批门后
- **免费 API 池**：使用论坛免费中转 API（linux.do）必须经 `forum_harvest`→`api_probe`→vault；消费一律走 `free_pool_ask` 且 sensitivity 默认 `public`，规则见 `memory/free_pool_sop.md`
- **PM 角色（产品发现）**：周期性挖掘"用户在哪里卡住 / 缺什么功能"，提案写到 `temp/pm_proposals.md` 等用户审。规则见 `memory/pm_role_sop.md`，工具入口 `pm_friction_scan`。PM 提案不允许碰自身运行规则 / 权限门 / autonomous SOP 自身
- **Phase 2 GUI 视觉回归**：每天至少跑一次 `python -m tests.journeys.runner`。失败 → 写报告附 diff 路径（`tests/journeys/runs/<run_id>/`），不要自行修 baseline。详情见下方「Phase 2 GUI 视觉回归」段。

## 执行
- 选定任务后 update_working_checkpoint，将选中的TODO条目和执行注意事项 append 到 checkpoint 中
- 调用code_run准备结尾回调, script为handler._done_hooks.append("重读自主任务sop，检查你刚刚的收尾工作是否正确，不正确则改正")，inline_eval=True （秘密参数）
- ≤30回合，小步快跑，边探测边实验
- 用临时脚本验证假设；禁只读即下结论，完整验证再写报告
- 即使失败也记录实验过程和结果，失败报告同样有价值
- 用户不在线，遇到需要决策的问题写入报告待审，不要卡住

**收尾（三件事缺一不可）**：
0. 重读本sop
1. 在cwd写报告（文件名任意），若有记忆更新建议，附在报告末尾
2. `from/import helper; complete_task(tasktitle, historyline, report_path)` → 自动编号+移报告到 autonomous_reports/+prepend history（historyline 格式：`类型 | 主题 | 结论`，严格单行）
3. `set_todo()` 获取TODO路径 → 将已完成条目标记为 `[x]`

## 权限边界
- 无需批准：只读探测、cwd内写操作/脚本实验
- 需写入报告待审：修改 global_mem / memory下SOP、安装软件、外部API调用、删除非临时文件
- 绝对禁止：读取密钥、修改核心代码库、不可逆危险操作

## 等待用户审查
- 用户归来后审查报告，决定批准、修改或拒绝方案

## Phase 2 GUI 视觉回归

每日一次（autonomous loop 命中或定时器命中即可）跑 5 条 baseline journey，确认 GUI 没被改动打坏。

### 流程
1. **前置**：GUI 必须已在 `http://127.0.0.1:1420`（或 `WLWL_GUI_URL`）跑。若未起，跳过本次回归并在报告中记一行 "GUI 未运行，跳过 Phase 2"。
2. **跑全套**：
   ```
   python -m tests.journeys.runner
   ```
   首次跑或 GUI 主动改样式后，用 `--capture-baseline` 重建基线：
   ```
   python -m tests.journeys.runner --capture-baseline
   ```
3. **结果**：
   - exit 0 = 全过；归档 `tests/journeys/runs/<run_id>/report.json`（无需写报告）。
   - exit 1 = 有失败；**写一份 autonomous 报告**，正文复制 `report.json` 里所有 `verdict != PASS` 的 step（含 `screenshot_path` / `baseline_path` / `pixel_diff_pct` / `vision_reason`）。**不要自己改 baseline**——baseline 只能由用户审过后才更新。
   - exit 2 = runner 自己崩了；写报告附 stderr。

### 边界
- 单次跑预算约 5 × （3-5 张截图）× 视觉模型调用 ≈ $0.05-$0.10。计入 `temp/cost_ledger.jsonl` `source=vision`。
- pixel diff ≤ 0.25% 直接 PASS，不调视觉模型——这是设计上的省钱阀。
- 若发现某条 journey 长期 `UNCLEAR`，写一条 PM 提案修 journey 描述或 selector，不要让它一直噪音化。

### 跟代码改动协作
- 用户改了 GUI 样式后会主动说"这次改动会让 X journey 的截图变"，在 journey YAML 的对应步骤 `expected_change` 字段写明（中文一句话即可）。视觉模型会忽略该方向的差异。