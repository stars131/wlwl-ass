# R186 PS/rg 替代 Python os.walk：SOP Patch 待审批报告

**类型**：产出 / 待审批草案
**本轮 TODO**：`产出 | PS/rg替代os.walk SOP落地 | R186验证完成但SOP patch未写入memory/—写patch草案报告待审批，改RULES和code_run相关SOP`
**执行边界**：本轮只在 `temp/` 下产出报告与审批草案，不直接修改 `memory/`。
**结论**：R186 的验证结论已足够进入 SOP/记忆 patch 审批；建议将“文件搜索/遍历/统计禁用 Python os.walk，优先 PowerShell/rg”固化到 L1/L2 与 code_run 相关 SOP。

## 1. 已核验证据

### 1.1 R186 原始验证报告

来源：`temp/autonomous_reports/R186_ps_rg_validation.md`

R186 记录的关键结果：

| 场景 | 推荐命令 | 实测耗时 | Python os.walk 预估风险 |
|---|---|---:|---|
| 文件搜索 `.md` | `Get-ChildItem -Filter -Depth` / `rg --files -g` | 约 96ms | 5-60s+ |
| 日志行统计 | `Select-String -Pattern "."` / `rg -c "."` | 约 19-55ms | 3-30s |
| 目录枚举 | `Get-ChildItem -Directory` | 约 2ms | 1-10s |

R186 结论：PowerShell 原生命令与 rg 在典型搜索/统计任务中均为 `<100ms` 量级，相对 Python `os.walk` 约有 50-600x 加速，可显著降低 `code_run` 中段超时风险。

### 1.2 当前 L2 / L1 已部分记录

`memory/global_mem.txt` 已有 L2 记录：

> 文件搜索/遍历/统计禁Python os.walk，用PS或rg：Get-ChildItem/Select-String/rg均<100ms vs os.walk 5-60s+(R186验证50-600x加速)

`memory/global_mem_insight.txt` 已有 L1/RULES 记录：

> 文件遍历/内容搜索禁Python os.walk(被杀软hook>15s),用PS Select-String或rg(<100ms)

这说明记忆层已经吸收了关键结论；本轮产出重点是把可审批 patch 方案明确成文，供用户决定是否同步到更具体的 code_run 相关 SOP/规则文档。

## 2. 待审批 Patch 草案

> 以下为建议内容草案。由于 `memory/` 属长期记忆/规则区，本轮不直接写入，仅报告待用户审批。

### 2.1 建议追加到 code_run 相关 SOP 的规则块

```md
## 文件搜索 / 遍历 / 统计（R186）

在 Windows 环境中，`code_run(type="python")` 执行大目录遍历时容易被杀软 hook 或路径规模放大，导致 5-60s+ 中段超时。凡是“搜文件名、搜文本、统计行数、枚举目录”这类任务，默认不要写 Python `os.walk()` / 大范围 `glob()` / `os.scandir()` 递归脚本。

优先使用 `code_run(type="powershell")` 或 `rg`：

- 搜文件名：`Get-ChildItem -Path <root> -Filter <pattern> -File -Depth <n>`
- 搜目录：`Get-ChildItem -Path <root> -Directory -Depth <n>`
- 搜文本：`Select-String -Path <files> -Pattern <pattern>`；若 `rg` 可用则用 `rg -n "<pattern>" <root>`
- 统计匹配行：`(Select-String -Path <files> -Pattern <pattern>).Count`；若 `rg` 可用则用 `rg -c "<pattern>" <root>`
- 列文件：`rg --files <root>` 或 `Get-ChildItem ... | Select-Object -ExpandProperty FullName`

约束：

- 禁止无界 `Get-ChildItem -Recurse`；必须限制 `-Depth` 或限定根目录/过滤条件。
- 禁止为文件搜索/内容搜索启动 Python `os.walk()`，除非根目录很小且已说明边界。
- `rg` 若提示 command not found，先按 WinGet/PATH 记忆刷新当前 shell PATH，或回退到 PowerShell 原生命令。
- PowerShell 写中文容易编码异常，中文文件写入仍优先使用 `file_write`。
```

### 2.2 建议同步到 L1 极简索引的形式

当前 L1 已有同类规则，可保持不变或微调为：

```md
10. 文件遍历/内容搜索禁Python os.walk；用PS Select-String/Get-ChildItem(-Depth)或rg(<100ms)，rg缺PATH先刷新/回退PS
```

### 2.3 建议同步到 L2 Code_Run 的形式

当前 L2 已有 R186 记录，可保持不变或微调为：

```md
- 文件搜索/遍历/统计禁 Python os.walk/无界 glob；优先 PowerShell 或 rg：Get-ChildItem(-Depth)/Select-String/rg 均 <100ms，较 os.walk 5-60s+ 快 50-600x（R186）。rg 不在 PATH 时先刷新 WinGet Links PATH 或回退 PS 原生命令。
```

## 3. 本轮实际发现的边界

- 直接用 Python 导入 autonomous helper 曾超时；随后改用 `file_read` 读取 helper 源码确认路径与收尾协议，避免无信息重试。
- 本轮尝试在 PowerShell 中直接调用 `rg` 时出现 `rg : 无法将“rg”项识别为 cmdlet...`，与 L2 中“code_run 每次独立 shell，需开头刷新 PATH 才可用 winget 工具”相符。
- 因此 patch 草案中加入了“rg 缺 PATH 先刷新或回退 PowerShell 原生命令”的边界说明。

## 4. 审批请求

请用户审查后决定是否执行以下任一动作：

1. 批准将 §2.1 写入具体 code_run 相关 SOP；
2. 批准按 §2.2 / §2.3 微调 L1/L2；
3. 拒绝本 patch，保留当前 L1/L2 简略记录即可。

本轮未修改 `memory/`，无不可逆操作。

## 5. 后续建议

若批准执行，下一轮应先读 `memory_management_sop.md`，再用 `file_patch` 精确 patch 目标记忆/SOP 文件，并同步 L1 极简索引。