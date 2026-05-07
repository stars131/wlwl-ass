# wlwl-ass — 30-Second Quickstart

> 30 秒装好、跑起来。完整文档参见 [README.md](./README.md) / [GETTING_STARTED.md](./GETTING_STARTED.md)。

> **2026-05 起，唯一的启动入口是 Tauri GUI**。CLI / Qt / 旧 webview shell / 桌面宠物 / Streamlit 等历史界面已全部移除。所有配置（API key、bot 凭据、权限）都在 GUI 里完成。

## 1. 一键安装并启动

Windows：

```
start_from_zero.cmd
```

或中文别名：

```
一键启动.cmd
```

它会自动：

1. 创建 `.venv` 并装好 Python 依赖（需要 Python 3.10–3.13）
2. 安装 GUI 所需的 Node 依赖（需要 Node.js ≥ 20.9 + Rust cargo）
3. 拉起 Tauri GUI 主窗口

第一次启动时 GUI 是空白的——打开 **API 配置** tab，添加一个 API key（OpenAI 兼容 / Anthropic / DeepSeek / Kimi / GLM / OpenRouter / 自建 relay 都行），就能开始对话。

## 2. 已装好后日常启动

```
python launch.pyw
```

等价于直接运行 GUI；不会重新装依赖。

## 3. 自动 bot

只要你在 GUI 的 **API 配置** 或 `.env` 里填了某个 IM bot 的凭据（例如飞书需要 `fs_app_id` + `fs_app_secret`），并安装了对应 SDK（`pip install lark_oapi`），下次启动 GUI 时这个 bot 会**自动在线**——不需要在任何地方勾选开关。

GUI 里的 **Bots** tab 显示每个 bot 的 configured / SDK / running 状态；如果不想让某个 bot 跑，在那里点「停止」即可。

## 4. 验证

```
python -m launcher.doctor          # 全量诊断（Python / 依赖 / 配置 / 权限）
python -m launcher.metrics         # 任务 / 工具指标
```

## 5. 配置加载优先级

```
shell env  <  .env  <  ~/.wlwl-ass/config.json  <  <project>/.wlwl-ass/config.json  <  temp/launcher_api_configs.json
```

99% 的用户在 GUI 的 **API 配置** tab 里加一条 → 自动写到 `temp/launcher_api_configs.json`。

## 常见 30 秒踩坑

| 现象 | 解决 |
|------|------|
| `start_from_zero.cmd` 报 "Tauri toolchain not ready" | 装 Node.js ≥20.9（`winget install OpenJS.NodeJS.LTS`）+ Rust（`https://rustup.rs/`） |
| 401 Unauthorized | API key 复制时多了空格；在 GUI **API 配置** 里检查 |
| 405 / 404 探测失败但保存继续 | 部分 relay 不支持 GET `/models`，对话依然能走 |
| 飞书/TG/QQ 没自动起 | 在 GUI **Bots** tab 看哪个状态：`未配置` 缺凭据；`SDK 缺失` 跑 `pip install lark_oapi`/`telegram`/`botpy`/… |
| GUI 窗口已开，第二次点击没反应 | 单例锁端口 `19736` — 切到已开的那个窗口 |
| 想跑 scheduler 后台任务 | GUI **设置** tab 里 `scheduler` 默认开；关掉就不再起 `reflect/scheduler.py` |

## 我现在该做什么

1. 跑 `start_from_zero.cmd`，等 Tauri 窗口出来
2. 进 **API 配置** tab，加一条 API key
3. 在主聊天里发一句 `列出当前目录的 Python 文件`，确认整条链路通了
