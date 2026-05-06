"""Qt launcher — single unified entry: 会话 / Bots / API 配置 / 设置 tabs."""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from launcher.api_config import (
    load_api_configs,
    save_api_configs,
)
from launcher.bot_manager import BotManager
from launcher.launch_config import load_options, project_options, save_options
from launcher.project_manager import ProjectManager

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
APP_VERSION = "0.1.0"

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStatusBar,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
    from launcher.qt_bots import BotsTab
    from launcher.qt_settings import SettingsTab
except ImportError:
    Qt = QTimer = None
    QApplication = None
    QAction = None
    QCheckBox = QComboBox = QDialog = QDialogButtonBox = QFormLayout = QHBoxLayout = None
    QInputDialog = QLabel = QLineEdit = QListWidget = QListWidgetItem = None
    QMessageBox = QPushButton = QSplitter = QStatusBar = QTabWidget = None
    QTextEdit = QVBoxLayout = None
    QMainWindow = QWidget = object  # so subclass declarations still parse
    BotsTab = SettingsTab = None  # type: ignore[assignment]


# ════════════════════════════════════════════════════════════════════
# 会话 Tab — project list (left) + project detail (right)
# ════════════════════════════════════════════════════════════════════


class SessionsTab(QWidget):
    REFRESH_MS = 3000

    def __init__(self, pm: ProjectManager, get_options, parent: QWidget | None = None):
        super().__init__(parent)
        self.pm = pm
        self.get_options = get_options  # callback returning current launch options
        self.projects: list[dict] = []
        self.current_project_id: str | None = None
        self._build_ui()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self.REFRESH_MS)

    def _build_ui(self):
        outer = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter)

        # Left: project list + actions
        left = QWidget()
        left_v = QVBoxLayout(left)
        left_v.addWidget(QLabel("会话列表"))
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self.on_project_selected)
        left_v.addWidget(self.project_list, 1)

        row1 = QHBoxLayout()
        for text, fn in (("新建", self.create_project),
                         ("启动", self.start_project),
                         ("停止", self.stop_project),
                         ("打开", self.open_project)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            row1.addWidget(btn)
        left_v.addLayout(row1)

        row2 = QHBoxLayout()
        for text, fn in (("激活", self.activate_project),
                         ("重命名", self.rename_project),
                         ("置顶", self.toggle_pin),
                         ("删除", self.delete_project)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            row2.addWidget(btn)
        left_v.addLayout(row2)

        # Right: project detail
        right = QWidget()
        right_v = QVBoxLayout(right)
        right_v.addWidget(QLabel("项目详情"))
        self.project_title = QLabel("未选择")
        self.project_title.setWordWrap(True)
        right_v.addWidget(self.project_title)
        self.project_url = QLineEdit()
        self.project_url.setReadOnly(True)
        right_v.addWidget(self.project_url)
        self.project_status = QTextEdit()
        self.project_status.setReadOnly(True)
        right_v.addWidget(self.project_status, 1)

        actions = QHBoxLayout()
        open_btn = QPushButton("打开 Streamlit")
        open_btn.clicked.connect(self.open_project)
        log_btn = QPushButton("刷新日志")
        log_btn.clicked.connect(self.show_project_log)
        actions.addWidget(open_btn)
        actions.addWidget(log_btn)
        right_v.addLayout(actions)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([320, 520])

    # ── data ──

    def selected_project(self) -> dict | None:
        if not self.current_project_id:
            return None
        return self.pm.get(self.current_project_id)

    def refresh(self):
        selected = self.current_project_id
        data = self.pm.list()
        self.projects = data["projects"]
        self.project_list.blockSignals(True)
        self.project_list.clear()
        for project in self.projects:
            marker = "★" if project.get("pinned") else " "
            status = "运行" if project.get("running") else "停止"
            item = QListWidgetItem(f"{marker} {project['name']}  [{status}] :{project.get('port')}")
            item.setData(Qt.UserRole, project["id"])
            self.project_list.addItem(item)
            if project["id"] == selected:
                self.project_list.setCurrentItem(item)
        self.project_list.blockSignals(False)
        if selected and any(p["id"] == selected for p in self.projects):
            self.current_project_id = selected
        elif self.projects:
            self.current_project_id = self.projects[0]["id"]
            self.project_list.setCurrentRow(0)
        self._update_detail()

    def on_project_selected(self, current, _previous):
        self.current_project_id = current.data(Qt.UserRole) if current else None
        self._update_detail()

    def _update_detail(self):
        project = self.selected_project()
        if not project:
            self.project_title.setText("未选择")
            self.project_url.clear()
            self.project_status.clear()
            return
        url = f"http://127.0.0.1:{project['port']}/"
        self.project_title.setText(f"{project['name']}    ID: {project['id']}")
        self.project_url.setText(url if project.get("running") else "未运行")
        lines = [
            f"状态: {'运行中' if project.get('running') else '已停止'}",
            f"端口: {project.get('port')}",
            f"PID: {project.get('pid') or '-'}",
            f"置顶: {'是' if project.get('pinned') else '否'}",
            f"最近活跃: {project.get('last_active')}",
            f"日志: {project.get('log_path') or '-'}",
        ]
        if project.get("last_error"):
            lines.append(f"错误: {project['last_error']}")
        self.project_status.setPlainText("\n".join(lines))

    # ── actions ──

    def create_project(self):
        name, ok = QInputDialog.getText(self, "新建会话", "名称:")
        if not ok:
            return
        opts = project_options(self.get_options())
        self.pm.create(name, auto_start=False, options=opts)
        self.refresh()

    def start_project(self):
        project = self.selected_project()
        if not project:
            return
        try:
            self.pm.start(project["id"])
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", str(exc))
        self.refresh()

    def stop_project(self):
        project = self.selected_project()
        if project:
            self.pm.stop(project["id"])
            self.refresh()

    def open_project(self):
        project = self.selected_project()
        if project and project.get("running"):
            self.pm.touch(project["id"])
            webbrowser.open(f"http://127.0.0.1:{project['port']}/")
            self.refresh()

    def activate_project(self):
        project = self.selected_project()
        if project:
            self.pm.set_active(project["id"])
            self.refresh()

    def rename_project(self):
        project = self.selected_project()
        if not project:
            return
        name, ok = QInputDialog.getText(self, "重命名", "名称:", text=project["name"])
        if ok:
            self.pm.rename(project["id"], name)
            self.refresh()

    def toggle_pin(self):
        project = self.selected_project()
        if project:
            self.pm.pin(project["id"], not bool(project.get("pinned")))
            self.refresh()

    def delete_project(self):
        project = self.selected_project()
        if not project:
            return
        if QMessageBox.question(self, "删除会话", f"删除 {project['name']}？") == QMessageBox.Yes:
            self.pm.delete(project["id"])
            self.current_project_id = None
            self.refresh()

    def show_project_log(self):
        project = self.selected_project()
        if not project:
            return
        path = project.get("log_path")
        if not path or not os.path.isfile(path):
            self.project_status.append("\n暂无日志")
            return
        text = Path(path).read_text(encoding="utf-8", errors="replace")[-4000:]
        self.project_status.setPlainText(text)


# ════════════════════════════════════════════════════════════════════
# API 配置 Tab — config CRUD + Profile bar (cc-switch style)
# ════════════════════════════════════════════════════════════════════


class ApiConfigTab(QWidget):
    def __init__(self, base_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.base_dir = base_dir
        self.api_configs: list[dict] = []
        self.current_api_index: int | None = None
        self._build_ui()
        self.refresh_api_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("API 配置 — 多渠道凭据"))

        self.api_list = QListWidget()
        self.api_list.currentRowChanged.connect(self.on_api_selected)
        layout.addWidget(self.api_list, 1)

        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(["native_oai", "native_claude", "mixin"])
        self.name = QLineEdit()
        self.apikey = QLineEdit()
        self.apikey.setEchoMode(QLineEdit.Password)
        self.apibase = QLineEdit()
        self.model = QLineEdit()
        self.api_mode = QLineEdit()
        self.stream = QCheckBox("stream")
        self.max_tokens = QLineEdit()
        self.timeout = QLineEdit()
        self.llm_nos = QLineEdit()
        form.addRow("类型", self.kind)
        form.addRow("名称", self.name)
        form.addRow("API Key", self.apikey)
        form.addRow("API Base", self.apibase)
        form.addRow("Model", self.model)
        form.addRow("api_mode", self.api_mode)
        form.addRow("stream", self.stream)
        form.addRow("max_tokens", self.max_tokens)
        form.addRow("timeout", self.timeout)
        form.addRow("mixin llm_nos", self.llm_nos)
        layout.addLayout(form)

        actions = QHBoxLayout()
        for text, fn in (("新增", self.new_api),
                         ("保存", self.save_api),
                         ("删除", self.delete_api),
                         ("刷新模型", self.refresh_llms)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            actions.addWidget(btn)
        layout.addLayout(actions)

        self.llm_status = QTextEdit()
        self.llm_status.setReadOnly(True)
        self.llm_status.setMaximumHeight(110)
        layout.addWidget(self.llm_status)

    # ── api list ──

    def refresh_api_list(self):
        self.api_configs = load_api_configs(self.base_dir)
        self.api_list.blockSignals(True)
        self.api_list.clear()
        for config in self.api_configs:
            self.api_list.addItem(f"   {config.get('kind')} · {config.get('name')} · {config.get('model', '')}")
        self.api_list.blockSignals(False)
        if self.api_configs:
            self.api_list.setCurrentRow(0)
        else:
            self.current_api_index = None
            self.new_api()

    def on_api_selected(self, row):
        self.current_api_index = row if 0 <= row < len(self.api_configs) else None
        if self.current_api_index is None:
            return
        self.load_api_form(self.api_configs[self.current_api_index])

    def load_api_form(self, config):
        self.kind.setCurrentText(str(config.get("kind") or "native_oai"))
        self.name.setText(str(config.get("name") or ""))
        self.apikey.setText(str(config.get("apikey") or ""))
        self.apibase.setText(str(config.get("apibase") or ""))
        self.model.setText(str(config.get("model") or ""))
        self.api_mode.setText(str(config.get("api_mode") or ""))
        self.stream.setChecked(bool(config.get("stream", True)))
        self.max_tokens.setText(str(config.get("max_tokens") or ""))
        self.timeout.setText(str(config.get("connect_timeout") or ""))
        self.llm_nos.setText(",".join(map(str, config.get("llm_nos") or [])))

    def form_config(self):
        config = {
            "kind": self.kind.currentText(),
            "name": self.name.text().strip(),
            "apikey": self.apikey.text().strip(),
            "apibase": self.apibase.text().strip(),
            "model": self.model.text().strip(),
            "api_mode": self.api_mode.text().strip(),
            "stream": self.stream.isChecked(),
            "max_tokens": self.max_tokens.text().strip(),
            "connect_timeout": self.timeout.text().strip(),
            "read_timeout": self.timeout.text().strip(),
            "llm_nos": self.llm_nos.text().strip(),
        }
        return {k: v for k, v in config.items() if v not in ("", None)}

    def new_api(self):
        self.current_api_index = None
        self.load_api_form({"kind": "native_oai", "stream": True})

    def save_api(self):
        config = self.form_config()
        configs = list(self.api_configs)
        if self.current_api_index is None:
            configs.append(config)
        else:
            configs[self.current_api_index] = config
        try:
            save_api_configs(self.base_dir, configs)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.refresh_api_list()
        self.refresh_llms()
        QMessageBox.information(self, "已保存", "API 配置已保存")

    def delete_api(self):
        if self.current_api_index is None:
            return
        configs = list(self.api_configs)
        configs.pop(self.current_api_index)
        save_api_configs(self.base_dir, configs)
        self.refresh_api_list()

    def refresh_llms(self):
        try:
            from agentmain import GeneraticAgent
            agent = GeneraticAgent()
            lines = [f"{i}. {name}" for i, name, _cur in agent.list_llms()]
            self.llm_status.setPlainText("可用模型:\n" + ("\n".join(lines) if lines else "无"))
        except Exception as exc:
            self.llm_status.setPlainText(f"模型加载失败: {exc}")


# ════════════════════════════════════════════════════════════════════
# QtLauncher 主窗口
# ════════════════════════════════════════════════════════════════════


class QtLauncher(QMainWindow):
    def __init__(self, base_dir=BASE_DIR):
        super().__init__()
        self.base_dir = base_dir
        self.pm = ProjectManager(base_dir)
        self.bot_manager = BotManager(base_dir)
        self.launch_options = load_options(base_dir)
        self.scheduler_proc = None

        self.setWindowTitle(f"wlwl-ass  v{APP_VERSION}")
        self.resize(1180, 760)

        self._build_ui()
        self._build_menu()
        self._build_statusbar()
        self.start_scheduler_if_enabled()

        self._auto_start_configured_bots()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_statusbar)
        self._status_timer.start(3000)

    # ── ui ──

    def _build_ui(self):
        self.tabs = QTabWidget()
        self.sessions_tab = SessionsTab(self.pm, lambda: self.launch_options)
        self.bots_tab = BotsTab(self.bot_manager)
        self.api_tab = ApiConfigTab(self.base_dir)
        self.settings_tab = SettingsTab(self.base_dir, on_saved=self._on_settings_saved)

        self.tabs.addTab(self.sessions_tab, "会话")
        self.tabs.addTab(self.bots_tab, "Bots")
        self.tabs.addTab(self.api_tab, "API 配置")
        self.tabs.addTab(self.settings_tab, "设置")
        self.setCentralWidget(self.tabs)

    def _build_menu(self):
        menubar = self.menuBar()

        m_file = menubar.addMenu("文件")
        act_new = QAction("新建会话", self)
        act_new.triggered.connect(self.sessions_tab.create_project)
        m_file.addAction(act_new)
        m_file.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_view = menubar.addMenu("视图")
        for label, idx in (("会话", 0), ("Bots", 1), ("API 配置", 2), ("设置", 3)):
            act = QAction(label, self)
            act.triggered.connect(lambda _=False, i=idx: self.tabs.setCurrentIndex(i))
            m_view.addAction(act)
        m_view.addSeparator()
        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self._refresh_all)
        m_view.addAction(act_refresh)

        m_help = menubar.addMenu("帮助")
        act_docs = QAction("配置文档", self)
        act_docs.triggered.connect(self._open_config_docs)
        m_help.addAction(act_docs)
        act_about = QAction("关于", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _build_statusbar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._refresh_statusbar()

    # ── settings save / scheduler / status ──

    def _on_settings_saved(self, options: dict):
        self.launch_options = options
        self.start_scheduler_if_enabled()
        self._refresh_statusbar()

    def start_scheduler_if_enabled(self):
        if not self.launch_options.get("scheduler", True):
            if self.scheduler_proc and self.scheduler_proc.poll() is None:
                self.scheduler_proc.kill()
            self.scheduler_proc = None
            return
        if self.scheduler_proc and self.scheduler_proc.poll() is None:
            return
        self.scheduler_proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(self.base_dir, "agentmain.py"),
                "--reflect",
                os.path.join(self.base_dir, "reflect", "scheduler.py"),
                "--llm_no",
                str(self.launch_options.get("llm_no", 0)),
            ],
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def _auto_start_configured_bots(self):
        """启动时按 launch_options 中的 bot flag 自动启已配置的 bot。"""
        for key in ("tg", "qq", "feishu", "wecom", "dingtalk", "wechat"):
            if not self.launch_options.get(key):
                continue
            ok, msg = self.bot_manager.start(key)
            print(f"[QtLauncher] auto-start {key}: {msg}")
        # 让 BotsTab 立即反映
        if hasattr(self, "bots_tab") and self.bots_tab is not None:
            self.bots_tab.refresh()

    def _refresh_statusbar(self):
        running_sessions = sum(1 for p in self.pm.list()["projects"] if p.get("running"))
        running_bots = sum(1 for st in self.bot_manager.status_all().values() if st.running)
        sched = "在线" if (self.scheduler_proc and self.scheduler_proc.poll() is None) else "关闭"
        self.status.showMessage(
            f"运行中 {running_sessions} 会话 · {running_bots}/6 Bot · L4 调度器 {sched} · v{APP_VERSION}"
        )

    def _refresh_all(self):
        self.sessions_tab.refresh()
        self.bots_tab.refresh()
        self.api_tab.refresh_api_list()
        self.settings_tab.load_form()
        self._refresh_statusbar()

    # ── menu actions ──

    def _open_config_docs(self):
        path = os.path.join(self.base_dir, "docs", "CONFIG.md")
        if not os.path.isfile(path):
            QMessageBox.information(self, "配置文档", f"未找到 {path}")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", str(exc))

    def _show_about(self):
        QMessageBox.about(self, "关于 wlwl-ass",
                          f"<h3>wlwl-ass</h3>"
                          f"<p>Version {APP_VERSION}</p>"
                          f"<p>Self-evolving autonomous agent — minimal core, layered memory.</p>"
                          f"<p>Project root: {self.base_dir}</p>")

    # ── close ──

    def closeEvent(self, event):
        self.pm.detach_all()
        self.bot_manager.stop_all()
        if self.scheduler_proc and self.scheduler_proc.poll() is None:
            self.scheduler_proc.kill()
        event.accept()


# ════════════════════════════════════════════════════════════════════


def main():
    if QApplication is None:
        raise SystemExit("PySide6 is required. Install with: pip install PySide6")
    app = QApplication(sys.argv)
    win = QtLauncher()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
