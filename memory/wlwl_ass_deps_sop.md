# wlwl-ass 依赖/运行环境要点

- 本仓库启动脚本默认走系统 Python，不依赖 `.venv`；若补依赖，先看 `start_*.cmd` 和实际 `sys.executable`，不要先入为主装到 `.venv`。
- 前端/辅助模块除 site-packages 外还依赖仓库根目录 `.deps`；做裸 `import` 烟测时，至少补 `repo root`、`frontends`、`memory`、`plugins`、`reflect` 到 `PYTHONPATH`。
- `reflect/scheduler.py` 里的 `compress_session` 来自 `memory/L4_raw_sessions/compress_session.py`；单独导入 scheduler 时还要补 `memory/L4_raw_sessions` 到路径。
- `scheduler` 若报 `sche_tasks/scheduler.log` 不存在，优先检查 `sche_tasks/`（及 `done/`）目录；这是运行目录问题，不是缺 Python 包。