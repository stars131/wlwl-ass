"""Settings tab widget — global launch options consolidated into one page."""
from __future__ import annotations

from typing import Callable

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # graceful: launcher.qt_launcher.main() refuses without PySide6
    Qt = None
    QCheckBox = QComboBox = QFileDialog = QFormLayout = QFrame = QGroupBox = None
    QHBoxLayout = QLabel = QLineEdit = QMessageBox = QPushButton = QSpinBox = None
    QVBoxLayout = None
    QWidget = object

from launcher.launch_config import DEFAULT_OPTIONS, PERMISSION_MODES, load_options, save_options


class SettingsTab(QWidget):
    """Reads/writes launcher_options.json. Notifies caller via on_saved."""

    def __init__(self, base_dir: str, on_saved: Callable[[dict], None] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.base_dir = base_dir
        self.on_saved = on_saved
        self.options = load_options(base_dir)
        self._build_ui()
        self.load_form()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        title = QLabel("启动设置 — 全局默认值。修改后点保存；运行中的会话需重启生效。")
        title.setStyleSheet("font-weight: bold; font-size: 13px; padding: 4px;")
        outer.addWidget(title)

        defaults_group = QGroupBox("会话默认值")
        defaults = QFormLayout(defaults_group)
        self.llm_no = QSpinBox()
        self.llm_no.setRange(0, 99)
        defaults.addRow("默认 LLM 索引 (llm_no)", self.llm_no)

        self.permission = QComboBox()
        self.permission.addItems(sorted(PERMISSION_MODES))
        defaults.addRow("权限模式 (permission_mode)", self.permission)

        proj_row = QHBoxLayout()
        self.project_root = QLineEdit()
        proj_browse = QPushButton("浏览…")
        proj_browse.clicked.connect(self._browse_project_root)
        proj_row.addWidget(self.project_root)
        proj_row.addWidget(proj_browse)
        proj_holder = QWidget()
        proj_holder.setLayout(proj_row)
        defaults.addRow("默认项目根 (project_root)", proj_holder)

        self.use_context = QCheckBox("注入项目上下文（推荐）")
        defaults.addRow("", self.use_context)

        self.autonomous = QCheckBox("自主流程（autonomous_enabled）")
        defaults.addRow("", self.autonomous)

        outer.addWidget(defaults_group)

        runtime_group = QGroupBox("运行时")
        runtime = QFormLayout(runtime_group)
        self.scheduler = QCheckBox("启用 L4 调度器（scheduler）")
        runtime.addRow("", self.scheduler)
        outer.addWidget(runtime_group)

        info_group = QGroupBox("提示")
        info = QVBoxLayout(info_group)
        info.addWidget(self._info_line(
            "• Bot 凭据（fs_app_id、tg_bot_token 等）请在「Bots」标签页点「配置」编辑，"
            "保存到 ~/.wlwl-ass/config.json。"
        ))
        info.addWidget(self._info_line(
            "• API 配置 / Profile 切换在「API 配置」标签页管理。"
        ))
        info.addWidget(self._info_line(
            "• 命令行：python -m launcher.config list / set / migrate"
        ))
        info.addWidget(self._info_line(
            "• 飞书命令在公开访问 (fs_allowed_users=['*']) 下会自动禁用 /run /clip 写 /open，详见 docs/CONFIG.md。"
        ))
        outer.addWidget(info_group)

        outer.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(reset_btn)
        btn_row.addWidget(save_btn)
        outer.addLayout(btn_row)

    def _info_line(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #555;")
        return lbl

    # ── form ⇄ options ──

    def load_form(self):
        self.options = load_options(self.base_dir)
        self.llm_no.setValue(int(self.options.get("llm_no", 0)))
        mode = str(self.options.get("permission_mode") or DEFAULT_OPTIONS["permission_mode"])
        idx = self.permission.findText(mode)
        if idx >= 0:
            self.permission.setCurrentIndex(idx)
        self.project_root.setText(str(self.options.get("project_root") or ""))
        self.use_context.setChecked(bool(self.options.get("use_project_context", True)))
        self.autonomous.setChecked(bool(self.options.get("autonomous_enabled", False)))
        self.scheduler.setChecked(bool(self.options.get("scheduler", True)))

    def collect(self) -> dict:
        return {
            **self.options,
            "llm_no": int(self.llm_no.value()),
            "permission_mode": self.permission.currentText(),
            "project_root": self.project_root.text().strip(),
            "use_project_context": self.use_context.isChecked(),
            "autonomous_enabled": self.autonomous.isChecked(),
            "scheduler": self.scheduler.isChecked(),
        }

    # ── handlers ──

    def _browse_project_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择项目根目录",
                                                self.project_root.text() or self.base_dir)
        if path:
            self.project_root.setText(path)

    def _on_save(self):
        self.options = save_options(self.base_dir, self.collect())
        if self.on_saved:
            self.on_saved(self.options)
        QMessageBox.information(self, "已保存", "启动设置已保存。已运行会话需重启后生效。")

    def _on_reset(self):
        if QMessageBox.question(self, "恢复默认",
                                "把所有设置恢复为默认值？") != QMessageBox.Yes:
            return
        self.options = save_options(self.base_dir, dict(DEFAULT_OPTIONS))
        self.load_form()
        if self.on_saved:
            self.on_saved(self.options)
