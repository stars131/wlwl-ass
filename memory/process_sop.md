---
version: 0.2.0
tags: [process, registry, lifecycle, core]
summary: When to register background processes you spawn, how to list/kill them, and why orphan PIDs are a real problem.
---
# Process Registry SOP

**触发**：你用 `code_run` 起了一个会跑超过 30 秒的进程（headless browser、长 shell 监听、文件 watcher、独立 Python 服务），并且这一轮之后还要继续操作它或者过几个回合再回收。

**不触发**：≤30 秒能跑完的子进程；只是 `subprocess.run(...)` 阻塞等结果的；用 `code_run({'timeout':...})` 自己管生命周期的。

## 为什么必须用

- pid 在你 chat history 里很快滚出去 → 几个回合之后你**找不到怎么 kill**
- Windows 的孤儿 chromium 一个吃 300MB；3 个 zombie 就把内存压住
- 用户问「现在还跑着什么」→ 没注册表你只能猜

## 工具：`process`

四个 action，都通过同一个工具调用：

```
process({"action": "register", "label": "scrape-twitter-1", "pid": 12345, "kind": "browser", "cmd": "python scraper.py"})
process({"action": "list"})                            # 看现在还活着哪些
process({"action": "kill", "label": "scrape-twitter-1"})  # 按 label 杀（一次杀光所有同 label 的）
process({"action": "kill", "pid": 12345, "force": true})  # 按 pid 强杀
process({"action": "cleanup_dead"})                    # 把已死的注册项扫掉
```

`list` 的 `alive` 字段是当场查的，不是缓存。

## 标准用法（启动→注册一气呵成）

```python
import subprocess
p = subprocess.Popen([sys.executable, "long_task.py"])
# 立刻调 process tool 注册
```
然后 `process({"action":"register", "label":"long_task", "pid": p.pid, "cmd":"python long_task.py"})`。
这样下一轮 `process({"action":"list"})` 看得见，不需要的时候 `process({"action":"kill","label":"long_task"})` 一刀干净。

## 何时调 cleanup_dead

- 长任务跑完前的最后一步：清掉所有已退出的注册项，让 list 干净
- 进入 plan 模式新阶段前

## 已 launcher 起的进程不需要再注册

ProjectManager 启动 streamlit session 和 BotManager 启动 bot subprocess 时已经自动注册了 `session:<id>` / `bot:<key>` 两类 label。你不要再手动注册它们；但可以用 `process({"action":"list"})` 看见它们。
