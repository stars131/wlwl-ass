# wlwl-ass — 30-Second Quickstart

> 30 秒装好、跑起来。完整文档参见 [README.md](./README.md) / [GETTING_STARTED.md](./GETTING_STARTED.md)。

## 1. 装依赖

```bash
git clone <this-repo> && cd <this-repo>
pip install -r requirements.txt
```

如果 `requirements.txt` 不存在，最小集合：

```bash
pip install requests beautifulsoup4
```

## 2. 跑一次配置向导

```bash
python agentmain.py
```

**首次运行会自动检测到没有 LLM 配置并启动交互式向导。** 也可以显式触发：

```bash
python -m launcher.cli_init        # 标准向导
python agentmain.py --init         # 等价
```

向导会问 4 个问题：
1. **哪家 API**（OpenAI 兼容 / Anthropic）
2. **哪个 endpoint**（官方 / DeepSeek / Kimi / GLM / OpenRouter / OAI-Free relay / 自定义 URL）
3. **API key**（粘贴）
4. **模型名**（带默认值）

之后会做一次 3 秒连接探测（可跳过），最后写到 **`.env`**。

## 3. 验证

```bash
python -m launcher.doctor          # 全量诊断（Python / 依赖 / 配置 / 权限）
python -m launcher.metrics         # 任务 / 工具指标（首次运行可能空表）
```

## 4. 跑起来

```bash
python agentmain.py                # CLI 交互模式
python launch.pyw                  # 桌面 GUI（默认 Tauri，自动 fallback Qt）
```

CLI 内置斜杠命令：`/help`、`/llm`、`/permission`、`/exit`。

---

## 配置加载优先级

```
shell env < .env < ~/.wlwl-ass/config.json < <project>/.wlwl-ass/config.json < temp/launcher_api_configs.json
```

- 99% 的用户只需要 `.env`，向导会写到那里。
- 多渠道 / mixin 故障转移 → GUI 「API 配置」 tab 写到 `temp/launcher_api_configs.json`。
- 命令行批改 → `python -m launcher.config set <path> <value>`。

## 常用一行命令

```bash
# 检查现状
python -m launcher.cli_init --check       # 是否已配置
python -m launcher.doctor                 # 全量体检（含 pip install 修复建议）
python -m launcher.config list            # 当前 config_store 全貌（secret 默认掩码）
python -m launcher.metrics --since 7d     # 近 7 天任务指标

# 排错
WLWL_ACTIVITY_LOG_OFF=1 python agentmain.py # 关闭埋点（隐私 / 调试）
python agentmain.py --no-wizard           # 跳过首次向导（默认会自动跑）
python agentmain.py --verbose             # 看每一轮 LLM 调用
```

## 常见 30 秒踩坑

| 现象 | 解决 |
|------|------|
| 向导没出现，直接报错 | `python -m launcher.cli_init --force` |
| 401 Unauthorized | key 复制时多了空格；用 `--init` 重跑向导 |
| 405 / 404 探测失败但保存继续 | 部分 relay 不支持 GET `/models`，对话依然能走 |
| `No usable LLM config found` | 还没配置——跑 `python -m launcher.cli_init` 或编辑 `.env` |
| Tauri GUI 黑屏 | `python launch.pyw --qt-legacy` |
| 老版本 `mykey.py` 想保留 | `python -m launcher.config migrate` 一次性导入到 `~/.wlwl-ass/config.json`（**该工具下个 release 会删除**） |

## 我现在该做什么

1. 跑 `python agentmain.py`，让向导带你过一遍
2. 跑一句 `wlwl-ass> 列出当前目录的 Python 文件`，确认整条链路通了
3. 跑 `python -m launcher.metrics`，看自己的第一条任务记录
