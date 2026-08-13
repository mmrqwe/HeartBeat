"""设置窗口：Qt 页签式，包含基本设置、内容源、外观、统计。"""

import copy
import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui import skins

class _ApiTestBridge(QObject):
    done = Signal(str, bool)


from gui.pet_window import draw_grid


def grid_to_pixmap(grid, palette, scale=4):
    image = QImage(20 * scale, 20 * scale, QImage.Format_RGB32)
    image.fill(QColor("#f2f0ea"))
    painter = QPainter(image)
    draw_grid(painter, grid, palette, 0, 0, scale)
    painter.end()
    return QPixmap.fromImage(image)


class SettingsWindow(QDialog):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.cfg = copy.deepcopy(controller.cfg)
        self.plugins = controller.plugins
        self.stats = controller.stats
        self.setWindowTitle("HeartBeat 设置")
        self.setWindowFlags(Qt.Window)
        self.resize(660, 640)
        self.setMinimumSize(560, 520)
        self._api_test_bridge = _ApiTestBridge()
        self._api_test_bridge.done.connect(self._show_api_test_result)
        self._build()
        self._refresh_stats_tab()

    @staticmethod
    def _wrap_scroll(widget):
        """把 tab 内容包进滚动区（设置项多，窗口固定尺寸放不下时滚轮可达）。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_basic_tab(), "基本设置")
        self.tabs.addTab(self._build_plugin_tab(), "内容源")
        self.tabs.addTab(self._build_skin_tab(), "外观")
        self.tabs.addTab(self._build_memory_tab(), "记忆")
        self.tabs.addTab(self._build_stats_tab(), "统计")
        root.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        root.addLayout(buttons)

    # ---------- 基本设置 ----------

    def _section_form(self, title):
        """设置分组卡片：标题 + 表单行。"""
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        head = QLabel(title)
        head.setObjectName("Title")
        layout.addWidget(head)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        layout.addLayout(form)
        return card, form

    def _build_basic_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)
        self._basic = {}

        def add_text(form, key, label, secret=False):
            edit = QLineEdit(str(self.cfg.get(key, "")))
            if secret:
                edit.setEchoMode(QLineEdit.Password)
            form.addRow(label, edit)
            self._basic[key] = edit

        def add_spin(form, key, label, minimum, maximum):
            spin = QSpinBox()
            spin.setRange(minimum, maximum)
            spin.setValue(int(self.cfg.get(key, minimum)))
            form.addRow(label, spin)
            self._basic[key] = spin

        card, form = self._section_form("角色与性格")
        add_text(form, "pet_name", "宠物名字")
        add_text(form, "owner_title", "对你的称呼（留空自动）")
        add_text(form, "role", "自我认知（角色）")
        add_text(form, "personality", "性格底色")
        add_text(form, "speaking_style", "说话方式（可选）")
        outer.addWidget(card)

        card, form = self._section_form("生活循环与精力")
        add_spin(form, "interval_minutes", "生活循环间隔（分钟）", 1, 1440)
        add_spin(form, "quiet_start", "安静时段开始（小时）", 0, 23)
        add_spin(form, "quiet_end", "安静时段结束（小时）", 0, 23)
        add_spin(form, "daily_energy_budget", "每日体力（LLM 调用次数）", 1, 1000000)
        add_spin(form, "proactive_energy_daily_cap", "主动思考每日上限", 0, 1000000)
        outer.addWidget(card)

        card, form = self._section_form("模型与 API")
        add_text(form, "base_url", "API 地址")
        add_text(form, "api_key", "API Key", secret=True)
        add_text(form, "model", "模型")
        api = self.cfg["api"]
        self._basic["base_url"].setText(api.get("base_url", ""))
        self._basic["api_key"].setText(api.get("api_key", ""))
        self._basic["model"].setText(api.get("model", ""))
        test_btn = QPushButton("测试连接")
        test_btn.clicked.connect(self._test_api_connection)
        form.addRow(test_btn)
        add_spin(form, "max_context_tokens", "上下文上限（token）", 1000, 1000000)
        add_spin(form, "max_output_tokens", "输出上限（token）", 1000, 1000000)
        self._basic["stream"] = QCheckBox("流式输出回复（逐字显示）")
        self._basic["stream"].setChecked(bool(self.cfg.get("stream", True)))
        form.addRow(self._basic["stream"])
        self._basic["thinking_enabled"] = QCheckBox("LLM 思考/推理模式")
        self._basic["thinking_enabled"].setChecked(
            bool(self.cfg.get("thinking_enabled", True))
        )
        form.addRow(self._basic["thinking_enabled"])
        self._basic["thinking_effort"] = QComboBox()
        for label, value in [
            ("低（low）", "low"),
            ("中（medium）", "medium"),
            ("高（high）", "high"),
        ]:
            self._basic["thinking_effort"].addItem(label, value)
        idx = self._basic["thinking_effort"].findData(
            self.cfg.get("thinking_effort", "medium")
        )
        self._basic["thinking_effort"].setCurrentIndex(max(idx, 0))
        form.addRow("思考强度", self._basic["thinking_effort"])
        outer.addWidget(card)

        card, form = self._section_form("能力与安全")
        self._basic["embedding_enabled"] = QCheckBox("启用本地向量记忆（RAG）")
        self._basic["embedding_enabled"].setChecked(
            bool(self.cfg.get("embedding_enabled", True))
        )
        form.addRow(self._basic["embedding_enabled"])
        add_text(form, "embedding_model", "向量模型")
        self._basic["tools_enabled"] = QCheckBox("允许桌宠主动思考时调用工具")
        self._basic["tools_enabled"].setChecked(
            bool(self.cfg.get("tools_enabled", True))
        )
        form.addRow(self._basic["tools_enabled"])
        self._basic["workspace_enabled"] = QCheckBox(
            "启用桌宠自己的工作区（默认文件夹：收集数据、生成网页/仪表盘）"
        )
        self._basic["workspace_enabled"].setChecked(
            bool(self.cfg.get("workspace_enabled", True))
        )
        form.addRow(self._basic["workspace_enabled"])
        add_spin(form, "patrol_tool_rounds", "主动思考工具轮数", 1, 30)
        add_spin(form, "patrol_tool_budget", "主动思考单次工具调用预算", 2, 30)
        self._basic["shell_tools_mode"] = QComboBox()
        for label, value in [
            ("关闭（off）", "off"),
            ("只读（readonly）", "readonly"),
            ("写操作需确认（confirm）", "confirm"),
            ("全部自动（full）", "full"),
        ]:
            self._basic["shell_tools_mode"].addItem(label, value)
        idx = self._basic["shell_tools_mode"].findData(
            self.cfg.get("shell_tools_mode", "confirm")
        )
        self._basic["shell_tools_mode"].setCurrentIndex(max(idx, 0))
        form.addRow("Shell 工具", self._basic["shell_tools_mode"])
        self._basic["shell_workdir"] = QLineEdit(
            str(self.cfg.get("shell_workdir", "") or "")
        )
        self._basic["shell_workdir"].setPlaceholderText("留空 = 用户主目录")
        form.addRow("Shell 工作目录", self._basic["shell_workdir"])
        self._basic["project_dir"] = QLineEdit(
            str(self.cfg.get("project_dir", "") or "")
        )
        self._basic["project_dir"].setPlaceholderText("留空 = 未启用编码模式")
        project_row = QWidget()
        project_layout = QHBoxLayout(project_row)
        project_layout.setContentsMargins(0, 0, 0, 0)
        project_layout.addWidget(self._basic["project_dir"], 1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_project_dir)
        project_layout.addWidget(browse_btn)
        form.addRow("编码项目目录", project_row)
        outer.addWidget(card)

        outer.addStretch(1)
        return self._wrap_scroll(tab)

    def _test_api_connection(self):
        """用当前表单里的 API 字段发一条最小请求验证连通性。"""
        from brain.llm import Brain

        base = self._basic["base_url"].text().strip()
        key = self._basic["api_key"].text().strip()
        model = self._basic["model"].text().strip()
        if not base or not key or not model:
            QMessageBox.warning(
                self, "测试连接", "请先填写 API 地址、API Key 和模型。"
            )
            return
        cfg = copy.deepcopy(self.cfg)
        cfg["api"].update({"base_url": base, "api_key": key, "model": model})

        def worker():
            try:
                brain = Brain(cfg, self.plugins, None)
                reply = brain.complete(
                    [{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=15,
                )
                ok = bool(reply)
                msg = "连接成功，模型可响应" if ok else "连接成功但模型无响应"
            except Exception as exc:
                ok = False
                msg = f"连接失败：{exc}"
            self._api_test_bridge.done.emit(msg, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _show_api_test_result(self, msg, ok):
        if ok:
            QMessageBox.information(self, "测试连接", msg)
        else:
            QMessageBox.warning(self, "测试连接", msg)

    def _browse_project_dir(self):
        current = str(self._basic["project_dir"].text() or "").strip()
        start = current if current and Path(current).is_dir() else str(Path.home())
        path = QFileDialog.getExistingDirectory(
            self, "选择编码协作项目目录", start
        )
        if path:
            self._basic["project_dir"].setText(path)

    # ---------- 内容源 ----------

    def _build_plugin_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        hint = QLabel("内容源都是插件：把新的 .py 放进 plugins 目录即可扩展，重启后生效。")
        hint.setObjectName("Hint")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        cards = QVBoxLayout(content)
        cards.setContentsMargins(4, 4, 4, 4)
        self._plugin_widgets = {}
        for name, module in self.plugins.items():
            card = QFrame()
            card.setObjectName("Card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            label = module.META.get("label", name) if hasattr(module, "META") else name
            enabled = QCheckBox(f"启用 · {label}（{name}）")
            settings = self.cfg.get("collectors", {}).get(name, {})
            default_enabled = (
                module.META.get("default_enabled", True)
                if hasattr(module, "META")
                else True
            )
            enabled.setChecked(bool(settings.get("enabled", default_enabled)))
            card_layout.addWidget(enabled)
            fields = self._render_plugin_fields(card_layout, module, settings)
            cards.addWidget(card)
            self._plugin_widgets[name] = {"enabled": enabled, "fields": fields}
        cards.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return tab

    def _render_plugin_fields(self, layout, module, settings):
        fields = {}
        for spec in getattr(module, "SETTINGS", []):
            key = spec["key"]
            kind = spec.get("type", "text")
            row = QHBoxLayout()
            row.addWidget(QLabel(spec.get("label", key)))
            if kind == "text":
                edit = QLineEdit(str(settings.get(key, spec.get("default", ""))))
                row.addWidget(edit, 1)
                fields[key] = edit
            elif kind == "number":
                spin = QDoubleSpinBox()
                spin.setRange(-1_000_000, 1_000_000)
                spin.setDecimals(4)
                spin.setValue(float(settings.get(key, spec.get("default", 0))))
                row.addWidget(spin, 1)
                fields[key] = spin
            elif kind == "list":
                box = QListWidget()
                for item in settings.get(key, spec.get("default", [])):
                    box.addItem(str(item))
                edit = QLineEdit()
                add_btn = QPushButton("添加")
                add_btn.clicked.connect(
                    lambda checked=False, e=edit, b=box: self._add_list_item(e, b)
                )
                del_btn = QPushButton("删除")
                del_btn.clicked.connect(
                    lambda checked=False, b=box: self._delete_list_item(b)
                )
                row.addWidget(box, 2)
                side = QVBoxLayout()
                side.addWidget(edit)
                side.addWidget(add_btn)
                side.addWidget(del_btn)
                row.addLayout(side)
                fields[key] = box
            layout.addLayout(row)
        return fields

    def _add_list_item(self, edit, box):
        value = edit.text().strip()
        if value:
            box.addItem(value)
            edit.clear()

    def _delete_list_item(self, box):
        for item in box.selectedItems():
            box.takeItem(box.row(item))

    # ---------- 外观 ----------

    def _build_skin_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.dark_mode = QCheckBox("深色模式（保存后立即生效）")
        self.dark_mode.setChecked(bool(self.cfg.get("dark_mode", False)))
        layout.addWidget(self.dark_mode)
        self.sync_role = QCheckBox("切换皮肤时同步人设（角色/性格/说话方式，开箱即用）")
        self.sync_role.setChecked(True)
        layout.addWidget(self.sync_role)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(10)
        self._skin_buttons = {}
        for index, (name, skin) in enumerate(skins.SKINS.items()):
            card = QFrame()
            card.setObjectName("Card")
            card_layout = QVBoxLayout(card)
            preview = QLabel()
            preview.setPixmap(
                grid_to_pixmap(
                    skins.render_frame(skin, "idle", 0), skin["palette"], scale=4
                )
            )
            preview.setAlignment(Qt.AlignCenter)
            card_layout.addWidget(preview)
            card_layout.addWidget(QLabel(skin["label"]), 0, Qt.AlignCenter)
            card_layout.addWidget(QLabel(skin.get("role", "")), 0, Qt.AlignCenter)
            if name == self.cfg.get("skin", skins.DEFAULT_SKIN):
                btn = QPushButton("当前皮肤")
                btn.setEnabled(False)
            else:
                btn = QPushButton("使用此皮肤")
                btn.setObjectName("Primary")
            # 所有按钮都连接信号：_refresh_skin_tab 只改外观/enabled，
            # 若只在"非当前"分支连接，切走后原"当前皮肤"按钮将永远无响应
            btn.clicked.connect(
                lambda checked=False, n=name: self._apply_skin(n)
            )
            card_layout.addWidget(btn)
            grid.addWidget(card, index // 2, index % 2)
            self._skin_buttons[name] = btn
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return tab

    def _apply_skin(self, name):
        self.controller.apply_skin(name, sync_role=self.sync_role.isChecked())
        self.cfg = copy.deepcopy(self.controller.cfg)
        # 人设同步后刷新输入框（未自定义的字段跟随皮肤人设）
        for key in ("role", "personality", "speaking_style"):
            if key in self._basic:
                self._basic[key].setText(str(self.cfg.get(key, "")))
        self._refresh_skin_tab()
        self._refresh_stats_tab()

    def _refresh_skin_tab(self):
        for name, btn in self._skin_buttons.items():
            current = name == self.cfg.get("skin", skins.DEFAULT_SKIN)
            btn.setEnabled(not current)
            btn.setText("当前皮肤" if current else "使用此皮肤")
            btn.setObjectName("" if current else "Primary")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # ---------- 统计 ----------

    def _build_memory_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 12, 14, 12)
        hint = QLabel(
            "桌宠在聊天和生活中自动记住的关于你的事。"
            "记住得越多，它主动说话时越有话题。可删除单条。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        filter_row = QHBoxLayout()
        filter_label = QLabel("筛选：")
        self.memory_category = QComboBox()
        self.memory_category.addItem("全部", None)
        for cat in ("identity", "preference", "habit", "schedule", "finance", "misc"):
            self.memory_category.addItem(cat, cat)
        self.memory_category.currentIndexChanged.connect(self._refresh_memory_tab)
        filter_row.addWidget(filter_label)
        filter_row.addWidget(self.memory_category)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)
        self.memory_list = QListWidget()
        self.memory_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.memory_list, 1)
        buttons = QHBoxLayout()
        delete_btn = QPushButton("删除选中")
        delete_btn.clicked.connect(self._delete_selected_memory)
        clear_btn = QPushButton("清空全部")
        clear_btn.setObjectName("Danger")
        clear_btn.clicked.connect(self._clear_all_memory)
        export_btn = QPushButton("导出")
        export_btn.clicked.connect(self._export_memory)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_memory_tab)
        buttons.addWidget(delete_btn)
        buttons.addWidget(clear_btn)
        buttons.addWidget(export_btn)
        buttons.addWidget(refresh_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._refresh_memory_tab()
        return self._wrap_scroll(tab)

    def _refresh_memory_tab(self):
        self.memory_list.clear()
        self._memory_ids = []
        self._memory_all = []
        agent = getattr(self.controller, "agent", None)
        if agent is None:
            return
        try:
            items = agent.memory.recent(500)
        except Exception:
            items = []
        category = self.memory_category.currentData()
        if category:
            items = [it for it in items if it.get("category") == category]
        self._memory_all = items
        if not items:
            self.memory_list.addItem("（当前筛选下没有记忆）")
            return
        category_names = {
            "identity": "身份",
            "preference": "喜好",
            "habit": "习惯",
            "schedule": "日程",
            "finance": "财务",
            "misc": "其他",
        }
        for it in items:
            role_label = "想法" if it.get("role") == "thought" else "记忆"
            cat = category_names.get(it.get("category") or "misc", it.get("category") or "其他")
            self.memory_list.addItem(f"[{role_label}·{cat}] {it['text']}")
            self._memory_ids.append(it["id"])

    def _delete_selected_memory(self):
        row = self.memory_list.currentRow()
        if row < 0 or row >= len(getattr(self, "_memory_ids", [])):
            return
        agent = getattr(self.controller, "agent", None)
        if agent is None:
            return
        mid = self._memory_ids[row]
        agent.db.delete_memory(mid)
        self._refresh_memory_tab()

    def _clear_all_memory(self):
        agent = getattr(self.controller, "agent", None)
        if agent is None:
            return
        answer = QMessageBox.question(
            self,
            "清空记忆",
            "确定清空全部记忆吗？此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            agent.db.clear_memory()
            self._refresh_memory_tab()

    def _export_memory(self):
        agent = getattr(self.controller, "agent", None)
        if agent is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出记忆", "heartbeat-memory.json", "JSON (*.json)"
        )
        if not path:
            return
        items = agent.db.memory_items(limit=None)
        payload = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M"),
            "count": len(items),
            "items": items,
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        QMessageBox.information(self, "导出完成", f"已导出 {len(items)} 条记忆")

    def _build_stats_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 12, 14, 12)
        buttons = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_stats_tab)
        clear_btn = QPushButton("清空统计")
        clear_btn.setObjectName("Danger")
        clear_btn.clicked.connect(self._clear_stats)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(clear_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.stats_summary = QLabel("")
        self.stats_summary.setWordWrap(True)
        self.stats_summary.setObjectName("Hint")
        layout.addWidget(self.stats_summary)

        self.stats_plugin_table = QTableWidget(0, 6)
        self.stats_plugin_table.setHorizontalHeaderLabels(
            ["插件", "成功", "失败", "条目", "缓存命中", "缓存率"]
        )
        self.stats_plugin_table.horizontalHeader().setStretchLastSection(True)
        self.stats_plugin_table.setMaximumHeight(180)
        layout.addWidget(self.stats_plugin_table)

        layout.addWidget(QLabel("近 7 天"))
        self.stats_days_table = QTableWidget(0, 6)
        self.stats_days_table.setHorizontalHeaderLabels(
            ["日期", "模型调用", "总 Token", "对话条数", "思考", "信息条数"]
        )
        self.stats_days_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.stats_days_table, 1)
        return self._wrap_scroll(tab)

    def _refresh_stats_tab(self):
        today = self.stats.today()
        collectors = today.get("collectors", {})
        total_entries = sum(c.get("entries", 0) for c in collectors.values())
        total_chars = sum(c.get("chars", 0) for c in collectors.values())
        prompt = today.get("prompt_tokens", 0)
        cached = today.get("cached_tokens", 0)
        llm_rate = cached / prompt * 100 if prompt else 0.0
        uptime = int(today.get("uptime_seconds", 0))
        self.stats_summary.setText(
            f"今日 {time.strftime('%Y-%m-%d')}\n"
            f"LLM：调用 {today['llm_calls']} 次（错误 {today['llm_errors']}）｜"
            f"输入 {prompt:,} token｜输出 {today['completion_tokens']:,}｜"
            f"缓存 {cached:,}（缓存率 {llm_rate:.1f}%）\n"
            f"行为：聊天 {today['chat_messages']} 条｜主动说话 {today['proactive_messages']} 次｜"
            f"想法 {today['thoughts']} 条｜记住事实 {today['facts']} 条｜"
            f"主动思考 {today['ticks']} 次｜调用工具 {today.get('tool_calls', 0)} 次\n"
            f"采集：共 {total_entries} 条 / {total_chars:,} 字｜"
            f"在线 {uptime // 3600} 小时 {uptime % 3600 // 60} 分钟"
        )

        self.stats_plugin_table.setRowCount(len(collectors))
        for row, (name, coll) in enumerate(sorted(collectors.items())):
            fetches = coll.get("fetches", 0)
            rate = coll.get("cache_hits", 0) / fetches * 100 if fetches else 0.0
            values = [
                name,
                str(coll.get("fetches", 0)),
                str(coll.get("fails", 0)),
                str(coll.get("entries", 0)),
                str(coll.get("cache_hits", 0)),
                f"{rate:.0f}%",
            ]
            for col, value in enumerate(values):
                self.stats_plugin_table.setItem(row, col, QTableWidgetItem(value))

        days = self.stats.days(7)
        self.stats_days_table.setRowCount(len(days))
        for row, day in enumerate(days):
            info = sum(c.get("entries", 0) for c in day.get("collectors", {}).values())
            tokens = day.get("prompt_tokens", 0) + day.get("completion_tokens", 0)
            values = [
                day.get("date", ""),
                str(day.get("llm_calls", 0)),
                str(tokens),
                str(day.get("chat_messages", 0)),
                str(day.get("ticks", 0)),
                str(info),
            ]
            for col, value in enumerate(values):
                self.stats_days_table.setItem(row, col, QTableWidgetItem(value))

    def _clear_stats(self):
        if QMessageBox.question(self, "清空统计", "确定清空所有统计数据和缓存标记吗？") == QMessageBox.Yes:
            self.stats.clear()
            self._refresh_stats_tab()

    # ---------- 保存 ----------

    def _save(self):
        cfg = self.cfg
        try:
            for key in ("pet_name", "role", "personality", "speaking_style"):
                cfg[key] = self._basic[key].text().strip() or cfg[key]
            for key in (
                "interval_minutes", "quiet_start", "quiet_end",
                "daily_energy_budget", "proactive_energy_daily_cap",
                "max_context_tokens", "max_output_tokens",
                "patrol_tool_rounds", "patrol_tool_budget",
            ):
                cfg[key] = self._basic[key].value()
            api = cfg["api"]
            api["base_url"] = self._basic["base_url"].text().strip() or api["base_url"]
            api["api_key"] = self._basic["api_key"].text().strip()
            api["model"] = self._basic["model"].text().strip() or api["model"]
            cfg["embedding_enabled"] = self._basic["embedding_enabled"].isChecked()
            cfg["embedding_model"] = (
                self._basic["embedding_model"].text().strip()
                or cfg["embedding_model"]
            )
            cfg["stream"] = self._basic["stream"].isChecked()
            cfg["thinking_enabled"] = self._basic["thinking_enabled"].isChecked()
            cfg["thinking_effort"] = self._basic["thinking_effort"].currentData()
            cfg["tools_enabled"] = self._basic["tools_enabled"].isChecked()
            cfg["workspace_enabled"] = self._basic["workspace_enabled"].isChecked()
            cfg["shell_tools_mode"] = self._basic["shell_tools_mode"].currentData()
            cfg["shell_workdir"] = self._basic["shell_workdir"].text().strip()
            cfg["project_dir"] = self._basic["project_dir"].text().strip()
            cfg["dark_mode"] = self.dark_mode.isChecked()

            for name, data in self._plugin_widgets.items():
                module = self.plugins[name]
                settings = cfg.setdefault("collectors", {}).setdefault(name, {})
                settings["enabled"] = data["enabled"].isChecked()
                for spec in getattr(module, "SETTINGS", []):
                    key = spec["key"]
                    kind = spec.get("type", "text")
                    widget = data["fields"].get(key)
                    if widget is None:
                        continue
                    if kind == "text":
                        settings[key] = widget.text().strip()
                    elif kind == "number":
                        value = widget.value()
                        settings[key] = int(value) if value.is_integer() else value
                    elif kind == "list":
                        settings[key] = [
                            widget.item(i).text() for i in range(widget.count())
                        ]
            self.controller.save_settings(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.accept()
