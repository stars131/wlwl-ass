"""Bots tab widget — render BotManager status + edit credentials inline."""
from __future__ import annotations

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # graceful: launcher.qt_launcher.main() refuses to run without PySide6
    Qt = QTimer = None
    QDialog = QDialogButtonBox = None
    QFormLayout = QHBoxLayout = QHeaderView = None
    QLabel = QLineEdit = QMessageBox = QPlainTextEdit = QPushButton = None
    QTableWidget = QTableWidgetItem = QVBoxLayout = None
    QWidget = object  # so the class statement below still parses

from launcher.bot_manager import BOT_SPECS, BotManager


# Map bot key → (config_store name, [(store_field, label, is_secret, is_list), ...]).
# Keep this in sync with launcher/api_server.py:_FIELD_TO_STORE — both are
# views into the same logical schema. If you change the bot field set,
# update both. We don't share a constant because the API server uses
# flat-key naming (``fs_app_id``) while the UI talks store-native
# (``app_id``); the human label here is what the form shows.
_BOT_FIELDS: dict[str, tuple[str, list[tuple[str, str, bool, bool]]]] = {
    "tg":       ("telegram", [
        ("bot_token",     "Bot Token",      True,  False),
        ("allowed_users", "允许用户列表",     False, True),
    ]),
    "qq":       ("qq", [
        ("app_id",        "App ID",          False, False),
        ("app_secret",    "App Secret",      True,  False),
        ("allowed_users", "允许用户列表",     False, True),
    ]),
    "feishu":   ("feishu", [
        ("app_id",        "App ID (cli_…)",  False, False),
        ("app_secret",    "App Secret",      True,  False),
        ("allowed_users", "允许用户列表 (ou_…)", False, True),
    ]),
    "wecom":    ("wecom", [
        ("bot_id",        "Bot ID",          False, False),
        ("secret",        "Secret",          True,  False),
        ("allowed_users", "允许用户列表",     False, True),
        ("welcome_message", "欢迎语",        False, False),
    ]),
    "dingtalk": ("dingtalk", [
        ("client_id",     "Client ID",       False, False),
        ("client_secret", "Client Secret",   True,  False),
        ("allowed_users", "允许用户列表",     False, True),
    ]),
    # WeChat: no credentials needed (relies on QR-code login).
    "wechat":   ("wechat", []),
}


# ─── Editor dialog ───────────────────────────────────────────────────────


class BotConfigDialog(QDialog):
    """Edit one bot's credentials. Reads/writes via launcher.config_store."""

    def __init__(self, bot_key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.bot_key = bot_key
        self.store_name, self.fields = _BOT_FIELDS.get(bot_key, ("", []))
        self.setWindowTitle(f"配置 {BOT_SPECS[bot_key].display_name}")
        self.setMinimumWidth(520)
        self._inputs: dict[str, QWidget] = {}
        self._original_secrets: dict[str, str] = {}
        self._build_ui()
        self._load()

    # -- UI --

    def _build_ui(self):
        outer = QVBoxLayout(self)
        if not self.fields:
            outer.addWidget(QLabel("此 bot 不需要凭据配置（依赖二维码登录）。"))
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject)
            outer.addWidget(buttons)
            return

        info = QLabel(
            f"凭据保存到 <b>~/.wlwl-ass/config.json</b>，自动 chmod 600。<br>"
            f"留空字段会被删除；含 <code>***</code> 的字段表示保留原值。"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #555; padding: 4px;")
        outer.addWidget(info)

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setLabelAlignment(Qt.AlignRight)
        for store_field, label, is_secret, is_list in self.fields:
            if is_list:
                w = QPlainTextEdit()
                w.setFixedHeight(70)
                w.setPlaceholderText("每行一个 ID")
            else:
                w = QLineEdit()
                if is_secret:
                    w.setEchoMode(QLineEdit.Password)
            self._inputs[store_field] = w
            row = QHBoxLayout()
            row.addWidget(w, 1)
            if is_secret:
                show_btn = QPushButton("显示")
                show_btn.setCheckable(True)
                show_btn.setFixedWidth(50)
                show_btn.toggled.connect(
                    lambda checked, ww=w: ww.setEchoMode(
                        QLineEdit.Normal if checked else QLineEdit.Password))
                row.addWidget(show_btn)
            holder = QWidget()
            holder.setLayout(row)
            form.addRow(label, holder)
        outer.addWidget(form_widget)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel | QDialogButtonBox.Reset
        )
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.Reset).setText("清空")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Reset).clicked.connect(self._on_clear)
        outer.addWidget(buttons)

    # -- load / save --

    def _load(self):
        from launcher import config_store
        store = config_store.default_store()
        existing = store.get_bot(self.store_name) or {}
        for store_field, _label, is_secret, is_list in self.fields:
            v = existing.get(store_field, "")
            if is_list:
                if isinstance(v, list):
                    text = "\n".join(str(x) for x in v)
                elif isinstance(v, str):
                    text = v
                else:
                    text = ""
                self._inputs[store_field].setPlainText(text)
            else:
                if is_secret:
                    self._original_secrets[store_field] = str(v or "")
                    # Show masked initially — real value preserved unless user types.
                    self._inputs[store_field].setText("***" if v else "")
                else:
                    self._inputs[store_field].setText(str(v or ""))

    def _on_clear(self):
        for store_field, _label, _is_secret, is_list in self.fields:
            w = self._inputs[store_field]
            if is_list:
                w.setPlainText("")
            else:
                w.setText("")

    def _on_save(self):
        from launcher import config_store
        store = config_store.default_store()
        new_fields: dict = {}
        deletes: list[str] = []
        for store_field, _label, is_secret, is_list in self.fields:
            w = self._inputs[store_field]
            if is_list:
                raw = w.toPlainText().strip()
                if not raw:
                    deletes.append(store_field)
                    continue
                items = [line.strip() for line in raw.splitlines() if line.strip()]
                new_fields[store_field] = items
            else:
                v = w.text().strip()
                if is_secret and v == "***":
                    # User didn't change the masked value — preserve original.
                    original = self._original_secrets.get(store_field, "")
                    if original:
                        new_fields[store_field] = original
                    continue
                if not v:
                    deletes.append(store_field)
                    continue
                new_fields[store_field] = v

        # Apply: merge sets, then prune deletes.
        if new_fields:
            store.set_bot(self.store_name, new_fields, layer="user", merge=True)
        if deletes:
            existing = store.get_bot(self.store_name)
            for f in deletes:
                existing.pop(f, None)
            if existing:
                store.set_bot(self.store_name, existing, layer="user", merge=False)
            else:
                store.delete_bot(self.store_name, layer="user")
        QMessageBox.information(
            self, "已保存",
            f"{BOT_SPECS[self.bot_key].display_name} 凭据已保存到 ~/.wlwl-ass/config.json。"
            f"\n已运行的 bot 进程需重启后生效。"
        )
        self.accept()


# ─── BotsTab ─────────────────────────────────────────────────────────────


class BotsTab(QWidget):
    REFRESH_MS = 3000

    def __init__(self, manager: BotManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        self._buttons: dict[str, dict[str, QPushButton]] = {}
        self._build_ui()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self.REFRESH_MS)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel("Bot 管理 — 启动/停止聊天平台 bot 后台进程")
        title.setStyleSheet("font-weight: bold; font-size: 13px; padding: 4px;")
        layout.addWidget(title)

        info = QLabel(
            "凭据从 <b>~/.wlwl-ass/config.json</b> 读取。点「配置」按钮编辑。<br>"
            "状态每 3 秒刷新。🟢 = 本 launcher 启的 / 🟡 = 外部进程 / ⚪ = 未运行"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #666; padding: 0 4px 6px 4px;")
        layout.addWidget(info)

        self.table = QTableWidget(len(BOT_SPECS), 4, self)
        self.table.setHorizontalHeaderLabels(["Bot", "配置", "状态", "操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        for row, key in enumerate(BOT_SPECS):
            spec = BOT_SPECS[key]
            self.table.setItem(row, 0, QTableWidgetItem(spec.display_name))

            ok_item = QTableWidgetItem("…")
            ok_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, ok_item)

            status_item = QTableWidgetItem("…")
            self.table.setItem(row, 2, status_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(2, 2, 2, 2)
            actions_layout.setSpacing(4)
            edit_btn = QPushButton("配置")
            start_btn = QPushButton("启动")
            stop_btn = QPushButton("停止")
            log_btn = QPushButton("日志")
            for btn in (edit_btn, start_btn, stop_btn, log_btn):
                btn.setFixedWidth(48)
                actions_layout.addWidget(btn)
            edit_btn.clicked.connect(lambda _=False, k=key: self._on_edit(k))
            start_btn.clicked.connect(lambda _=False, k=key: self._on_start(k))
            stop_btn.clicked.connect(lambda _=False, k=key: self._on_stop(k))
            log_btn.clicked.connect(lambda _=False, k=key: self._on_log(k))
            self._buttons[key] = {
                "edit": edit_btn, "start": start_btn,
                "stop": stop_btn, "log": log_btn,
            }
            self.table.setCellWidget(row, 3, actions)

        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        refresh_btn = QPushButton("立即刷新")
        refresh_btn.clicked.connect(self.refresh)
        bottom.addWidget(refresh_btn)
        bottom.addStretch(1)
        self.summary = QLabel("…")
        self.summary.setStyleSheet("color: #444;")
        bottom.addWidget(self.summary)
        layout.addLayout(bottom)

    # ── refresh ──

    def refresh(self):
        statuses = self.manager.status_all()
        running = 0
        for row, key in enumerate(BOT_SPECS):
            st = statuses[key]

            if not st.configured:
                cfg_text, tip = "❌", "缺字段: " + ", ".join(st.missing_fields)
            elif not st.sdk_installed:
                cfg_text, tip = "⚠️", "缺 SDK: " + ", ".join(st.missing_modules)
            else:
                cfg_text, tip = "✅", "已配置"
            cfg_item = self.table.item(row, 1)
            cfg_item.setText(cfg_text)
            cfg_item.setToolTip(tip)
            cfg_item.setTextAlignment(Qt.AlignCenter)

            if st.running_self:
                state_text = "🟢 运行中（本 launcher）"
            elif st.running_external:
                state_text = "🟡 外部进程占端口"
            else:
                state_text = "⚪ 已停"
            self.table.item(row, 2).setText(state_text)

            btns = self._buttons[key]
            startable = st.configured and st.sdk_installed and not st.running
            btns["start"].setEnabled(startable)
            btns["stop"].setEnabled(st.running_self)
            btns["log"].setEnabled(True)
            # Edit button always enabled — even unconfigured bots need
            # somewhere to enter the first credentials.
            btns["edit"].setEnabled(True)
            if st.running:
                running += 1

        self.summary.setText(f"运行中 {running}/{len(BOT_SPECS)} 个 bot")

    # ── handlers ──

    def _on_edit(self, key: str):
        dlg = BotConfigDialog(key, self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh()

    def _on_start(self, key: str):
        ok, msg = self.manager.start(key)
        self.summary.setText(f"[{BOT_SPECS[key].display_name}] {msg}")
        self.refresh()

    def _on_stop(self, key: str):
        ok, msg = self.manager.stop(key)
        self.summary.setText(f"[{BOT_SPECS[key].display_name}] {msg}")
        self.refresh()

    def _on_log(self, key: str):
        if not self.manager.open_log(key):
            self.summary.setText(f"[{BOT_SPECS[key].display_name}] 日志暂无")
