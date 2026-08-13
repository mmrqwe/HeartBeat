"""聊天窗口：Qt 气泡式多轮对话，支持流式更新。"""

import time

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui import theme


BUBBLE_MIN_W = 72    # 气泡最小宽度（短消息窄气泡，按内容自适应）
BUBBLE_MARGIN = 10   # 气泡内边距（与 documentMargin 一致）


def _make_action_icon(kind):
    """侧边栏操作按钮的小图标：new=加号，rename=铅笔，delete=垃圾桶。"""
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#666666"), 2)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    if kind == "new":
        painter.drawLine(10, 4, 10, 16)
        painter.drawLine(4, 10, 16, 10)
    elif kind == "rename":
        painter.setBrush(QColor("#666666"))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(15, 3),
            QPointF(18, 6),
            QPointF(6, 18),
            QPointF(3, 15),
        ]))
    elif kind == "delete":
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(5, 7, 10, 11)
        painter.drawLine(4, 6, 16, 6)
        painter.drawLine(9, 9, 9, 16)
        painter.drawLine(13, 9, 13, 16)
    painter.end()
    return QIcon(pm)


def _make_bubble(role):
    """Markdown 渲染气泡（QTextBrowser 原生支持）。"""
    bubble = QTextBrowser()
    bubble.setStyleSheet(theme.bubble_style(role))
    bubble.setFrameShape(QFrame.NoFrame)
    bubble.setOpenExternalLinks(True)
    bubble.viewport().setAutoFillBackground(False)  # QSS 背景作用于 frame，viewport 需透明
    bubble.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    bubble.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    # 宽度不固定：由 _sync_bubble_height 按内容自然宽度 + 窗口宽度自适应。
    # 超长无空格单词（URL/代码串）按任意位置断行，避免横向溢出被裁剪；
    # 正常文本仍按词边界/中文逐字换行，不受影响。
    bubble.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    bubble.document().setDocumentMargin(BUBBLE_MARGIN)  # 内边距（QSS padding 对 QTextEdit 无效）
    return bubble


class ChatInput(QTextEdit):
    def __init__(self, on_send):
        super().__init__()
        self.on_send = on_send
        self.setFixedHeight(64)
        self.setPlaceholderText("说点什么…（Enter 发送 / Shift+Enter 换行）")

    def keyPressEvent(self, event: QKeyEvent):
        if (
            event.key() == Qt.Key_Return
            and not (event.modifiers() & Qt.ShiftModifier)
        ):
            self.on_send()
            event.accept()
            return
        super().keyPressEvent(event)


class ChatWindow(QWidget):
    def __init__(self, pet_name, on_send, on_clear, on_pick_dir=None,
                 on_new_session=None, on_switch_session=None,
                 on_delete_session=None, on_rename_session=None,
                 on_cancel_coding=None):
        super().__init__()
        self.pet_name = pet_name
        self.on_send = on_send
        self.on_clear = on_clear
        self.on_pick_dir = on_pick_dir
        self.on_new_session = on_new_session
        self.on_switch_session = on_switch_session
        self.on_delete_session = on_delete_session
        self.on_rename_session = on_rename_session
        self.on_cancel_coding = on_cancel_coding
        self.messages = []
        self._bubble_items = []  # (bubble, text, markdown)：窗口缩放时按记录重排
        self._last_bubble = None
        self._streaming = False
        self._think_dots = 0
        self._think_timer = None
        self.coding_status_label = None
        self.current_session_id = "default"
        self._session_ids = []

        self.setWindowTitle(f"和{pet_name}聊天")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.resize(680, 600)
        self.setMinimumSize(560, 420)
        self._build()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧：会话列表（多对话：新增/删除/切换；📁 表示绑定了编程目录）
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(164)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(6)
        side_header = QHBoxLayout()
        side_title = QLabel("对话")
        side_title.setObjectName("Hint")
        new_btn = QPushButton(_make_action_icon("new"), "")
        new_btn.setToolTip("新建对话")
        new_btn.setFixedWidth(30)
        new_btn.clicked.connect(self._on_new_session)
        del_btn = QPushButton(_make_action_icon("delete"), "")
        del_btn.setToolTip("删除选中的对话")
        del_btn.setFixedWidth(30)
        del_btn.clicked.connect(self._on_delete_session)
        rename_btn = QPushButton(_make_action_icon("rename"), "")
        rename_btn.setToolTip("重命名选中的对话")
        rename_btn.setFixedWidth(30)
        rename_btn.clicked.connect(self._on_rename_session)
        self.new_session_btn = new_btn
        self.rename_session_btn = rename_btn
        self.delete_session_btn = del_btn
        side_header.addWidget(side_title)
        side_header.addStretch(1)
        side_header.addWidget(new_btn)
        side_header.addWidget(rename_btn)
        side_header.addWidget(del_btn)
        side_layout.addLayout(side_header)
        self.session_list = QListWidget()
        self.session_list.setFrameShape(QFrame.NoFrame)
        self.session_list.itemClicked.connect(self._on_session_clicked)
        side_layout.addWidget(self.session_list, 1)
        root.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        header = QFrame()
        header.setObjectName("Card")
        header.setFixedHeight(64)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(12, 6, 12, 6)
        row1 = QHBoxLayout()
        name_label = QLabel(self.pet_name)
        name_label.setObjectName("Title")
        self.status_label = QLabel("在线")
        self.status_label.setObjectName("Hint")
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("Hint")
        clear_btn = QPushButton("清空记录")
        clear_btn.setToolTip("清空当前对话的消息")
        clear_btn.clicked.connect(self._confirm_clear)
        # 选择编程项目目录（选目录后，编程任务自然路由到 coding，无需切换模式）
        self.dir_btn = QPushButton("📁 目录")
        self.dir_btn.setToolTip("选择编程项目目录；选好后，编程任务会自动在该目录里执行")
        self.dir_btn.clicked.connect(self._on_pick_dir)
        row1.addWidget(name_label)
        row1.addWidget(self.status_label)
        row1.addStretch(1)
        row1.addWidget(self.dir_btn)
        row1.addWidget(clear_btn)
        header_layout.addLayout(row1)
        header_layout.addWidget(self.stats_label)
        main.addWidget(header)

        coding_row = QHBoxLayout()
        coding_row.setContentsMargins(12, 2, 12, 2)
        self.coding_status_label = QLabel("")
        self.coding_status_label.setObjectName("CodingStatus")
        self.coding_status_label.setWordWrap(True)
        self.coding_status_label.hide()
        self.stop_coding_btn = QPushButton("■ 停止")
        self.stop_coding_btn.setToolTip("停止当前编码任务（改过的文件都有备份）")
        self.stop_coding_btn.hide()
        self.stop_coding_btn.clicked.connect(self._on_stop_coding)
        coding_row.addWidget(self.coding_status_label, 1)
        coding_row.addWidget(self.stop_coding_btn)
        main.addLayout(coding_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(4)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        main.addWidget(self.scroll, 1)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(12, 8, 12, 10)
        input_row = QHBoxLayout()
        self.input_text = ChatInput(self._send)
        send_btn = QPushButton("发送")
        send_btn.setObjectName("Primary")
        send_btn.clicked.connect(self._send)
        input_row.addWidget(self.input_text, 1)
        input_row.addWidget(send_btn)
        bottom_layout.addLayout(input_row)
        main.addWidget(bottom)

        root.addLayout(main, 1)

    # ---------- 会话 ----------

    def set_sessions(self, sessions, current_session_id):
        """刷新会话列表（宿主 db.list_sessions 结果）；当前会话高亮。"""
        self.current_session_id = current_session_id
        self._session_ids = []
        self.session_list.blockSignals(True)
        self.session_list.clear()
        for s in sessions:
            label = s.get("name") or "会话"
            item = QListWidgetItem(label)
            item.setToolTip(
                f"目录：{s['project_dir']}" if s.get("project_dir") else "未绑定编程目录"
            )
            self.session_list.addItem(item)
            self._session_ids.append(s["id"])
            if s["id"] == current_session_id:
                self.session_list.setCurrentItem(item)
        self.session_list.blockSignals(False)

    def _on_session_clicked(self, item):
        idx = self.session_list.row(item)
        if 0 <= idx < len(self._session_ids):
            sid = self._session_ids[idx]
            if sid != self.current_session_id and self.on_switch_session is not None:
                self.on_switch_session(sid)

    def _on_new_session(self):
        if self.on_new_session is not None:
            self.on_new_session()

    def _on_delete_session(self):
        item = self.session_list.currentItem()
        if item is None:
            return
        idx = self.session_list.row(item)
        if not (0 <= idx < len(self._session_ids)):
            return
        sid = self._session_ids[idx]
        if sid == "default":
            QMessageBox.information(self, "删除对话", "默认对话不能删除")
            return
        answer = QMessageBox.question(
            self, "删除对话", "确定删除该对话及其全部消息吗？"
        )
        if answer == QMessageBox.Yes and self.on_delete_session is not None:
            self.on_delete_session(sid)

    def _on_rename_session(self):
        item = self.session_list.currentItem()
        if item is None:
            return
        idx = self.session_list.row(item)
        if not (0 <= idx < len(self._session_ids)):
            return
        sid = self._session_ids[idx]
        if self.on_rename_session is not None:
            self.on_rename_session(sid)

    # ---------- 消息 ----------

    def add_message(self, role, text, time_str=None):
        time_str = time_str or time.strftime("%H:%M")
        self.messages.append((role, text, time_str))
        self._render_message(role, text, time_str)
        self._scroll_to_bottom()

    def _render_message(self, role, text, time_str):
        if role == "system":
            label = QLabel(text)
            label.setObjectName("Hint")
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignCenter)
            self.content_layout.addWidget(label)
            return None

        align = Qt.AlignRight if role == "user" else Qt.AlignLeft
        bubble = _make_bubble(role)
        bubble.setMarkdown(text)
        self._bubble_items.append((bubble, text, True))
        self.content_layout.addWidget(bubble, 0, align)
        self._sync_bubble_height(bubble, text, markdown=True)

        time_label = QLabel(time_str)
        time_label.setObjectName("Hint")
        self.content_layout.addWidget(time_label, 0, align)

        if role == "assistant":
            self._last_bubble = bubble
        return bubble

    def begin_stream(self):
        """开始流式输出：创建一条独立的 assistant 占位气泡。

        关键：必须新建气泡而非复用历史消息，否则流式 delta 会改写上一条
        历史回复（旧 bug：回复重复显示 + 历史被篡改）。
        """
        if self._streaming:
            return
        self._streaming = True
        self._last_bubble = None
        self._render_message("assistant", "", time.strftime("%H:%M"))
        self._scroll_to_bottom()

    def _natural_bubble_width(self, bubble, text, markdown):
        """按内容自然宽度（不换行渲染）计算气泡宽度，clamp 到 [MIN, 窗口自适应上限]。

        短消息（“你好”/“在吗”）只占一小段，长消息撑到上限后换行，
        避免固定宽度气泡在界面里显得生硬。流式半成品用纯文本估算会
        略偏宽（md 符号计入宽度），finish_stream 用 markdown 重算自愈。
        """
        probe = QTextDocument()
        opt = QTextOption()
        opt.setWrapMode(QTextOption.NoWrap)
        probe.setDefaultTextOption(opt)
        probe.setDefaultFont(bubble.font())
        probe.setDocumentMargin(bubble.document().documentMargin())
        if markdown:
            probe.setMarkdown(text)
        else:
            probe.setPlainText(text)
        probe.adjustSize()
        natural = int(probe.size().width()) + 2  # 边框缓冲
        return max(BUBBLE_MIN_W, min(natural, self._max_bubble_width()))

    def _max_bubble_width(self):
        """气泡最大宽度跟随聊天区宽度（约 78%，留出两侧边距），不写死。"""
        viewport = self.scroll.viewport().width()
        base = viewport if viewport and viewport > 0 else self.width()
        return max(BUBBLE_MIN_W, int(base * 0.78) - 24)

    def _set_bubble_item(self, bubble, text, markdown):
        for i, (b, _t, _m) in enumerate(self._bubble_items):
            if b is bubble:
                self._bubble_items[i] = (bubble, text, markdown)
                return
        self._bubble_items.append((bubble, text, markdown))

    def _relayout_bubbles(self):
        """窗口尺寸变化后按新上限重算所有气泡宽高。"""
        for bubble, text, markdown in list(self._bubble_items):
            self._sync_bubble_height(bubble, text, markdown)

    def _estimate_height(self, bubble, text, markdown, width):
        """用独立 QTextDocument 探针估算气泡高度。

        不直接改真实 document 的 textWidth（会污染 QTextEdit 内部布局）。
        探针与真实气泡同字体/同内边距/同换行模式；textWidth 按当前气泡
        宽度保守偏窄，保证估算高度 ≥ 真实需要、内容不被裁剪。
        """
        probe = QTextDocument()
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        probe.setDefaultTextOption(opt)
        probe.setDefaultFont(bubble.font())
        probe.setDocumentMargin(bubble.document().documentMargin())
        if markdown:
            probe.setMarkdown(text)
        else:
            probe.setPlainText(text)
        probe.setTextWidth(width - 2 * probe.documentMargin() - 4)
        probe.adjustSize()
        return max(int(probe.size().height()) + 8, 24)

    def _sync_bubble_height(self, bubble, text, markdown=True):
        """按内容自适应宽度 + 估算高度固定气泡（流式高频更新同样依赖此机制）。

        宽度 = 内容自然宽度（上限跟随窗口），高度用独立探针估算；
        已显示且有真实布局高度时取两者较大值，再延迟一帧用真实布局修正，
        消除字体替换/宽度差异导致的估算偏差（任何环境下都不裁剪内容）。
        """
        if bubble is None:
            return
        width = self._natural_bubble_width(bubble, text, markdown)
        bubble.setMaximumWidth(self._max_bubble_width())
        bubble.setFixedWidth(width)
        est = self._estimate_height(bubble, text, markdown, width)
        real = bubble.document().size().height()
        bubble.setFixedHeight(max(int(real) if real > 0 else est, est, 24))
        # 气泡自身的 isVisible 在 addWidget 后布局映射前为 False，须用顶层窗口判断
        if bubble.window() is not None and bubble.window().isVisible():
            QTimer.singleShot(0, lambda: self._fix_bubble_height(bubble))

    def _fix_bubble_height(self, bubble):
        """事件循环跑过后 document 已按真实字体/宽度 layout，修正为精确高度。

        布局未就绪（real=0）时重试一帧——QTextEdit 的文档 relayout 与
        singleShot 回调的调度顺序不保证，必须等到真实高度可用。
        """
        try:
            if bubble is None or not bubble.window() or not bubble.window().isVisible():
                return
            real = bubble.document().size().height()
        except RuntimeError:
            return  # 窗口已销毁：不再触碰已删除的 C++ 对象
        if real > 0:
            bubble.setFixedHeight(max(int(real) + 4, 24))
            self._scroll_to_bottom()
        else:
            QTimer.singleShot(0, lambda: self._fix_bubble_height(bubble))

    def update_last_message(self, text):
        if self._last_bubble is not None:
            # 流式中用纯文本：markdown 半成品（未闭合 **、``` 等）会闪烁/误渲染
            self._last_bubble.setPlainText(text)
            self._set_bubble_item(self._last_bubble, text, False)
            self._sync_bubble_height(self._last_bubble, text, markdown=False)
            self._scroll_to_bottom()

    def finish_stream(self, text):
        """流式结束：用最终完整文本收尾（Markdown 渲染），并复位流式状态。"""
        if self._last_bubble is not None:
            self._last_bubble.setMarkdown(text)
            self._set_bubble_item(self._last_bubble, text, True)
            self._sync_bubble_height(self._last_bubble, text, markdown=True)
            self._scroll_to_bottom()
        self._streaming = False

    def is_streaming(self):
        return self._streaming

    def cancel_stream(self):
        """取消流式：移除当前占位气泡并复位状态（超时/中断等场景）。"""
        if self._last_bubble is not None:
            self._bubble_items = [
                item for item in self._bubble_items if item[0] is not self._last_bubble
            ]
            self.content_layout.removeWidget(self._last_bubble)
            self._last_bubble.deleteLater()
            self._last_bubble = None
        self._streaming = False

    def clear(self):
        self.messages = []
        self._bubble_items = []
        self._last_bubble = None
        self._streaming = False
        while self.content_layout.count() > 1:
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_bubbles()

    def _confirm_clear(self):
        answer = QMessageBox.question(
            self, "清空记录", "确定清空当前对话的聊天记录吗？"
        )
        if answer == QMessageBox.Yes:
            self.on_clear()

    # ---------- 输入与状态 ----------

    def _send(self):
        text = self.input_text.toPlainText().strip()
        if not text:
            return
        self.input_text.clear()
        self.add_message("user", text)
        self.set_thinking(True)
        self.on_send(text)

    def _on_pick_dir(self):
        """点击目录按钮 → 回调宿主弹目录选择器。"""
        if self.on_pick_dir is not None:
            self.on_pick_dir()

    def set_project_dir(self, path):
        """宿主设置项目目录后回传：更新按钮提示与状态行。"""
        if not path:
            self.dir_btn.setToolTip("选择编程项目目录；选好后，编程任务会自动在该目录里执行")
            self.set_coding_status("")
            return
        self.dir_btn.setToolTip(f"编程目录：{path}\n编程任务会自动在该目录里执行（点击更换）")
        self.set_coding_status(f"编程目录：{path}")

    def set_coding_status(self, text):
        """编码任务状态行（步骤进度 / 完成提示）；空文本隐藏。"""
        if self.coding_status_label is None:
            return
        if text:
            self.coding_status_label.setText(text)
            self.coding_status_label.show()
        else:
            self.coding_status_label.hide()

    def set_coding_running(self, running):
        """编码任务运行期间显示“停止”按钮。"""
        self.stop_coding_btn.setVisible(bool(running))

    def _on_stop_coding(self):
        if self.on_cancel_coding is not None:
            self.on_cancel_coding()

    def set_thinking(self, on):
        if on:
            self._think_dots = 0
            self._think_timer = QTimer(self)
            self._think_timer.timeout.connect(self._animate_thinking)
            self._think_timer.start(400)
            self._animate_thinking()
        else:
            if self._think_timer:
                self._think_timer.stop()
                self._think_timer = None
            self.status_label.setText("在线")

    def _animate_thinking(self):
        dots = "." * (self._think_dots % 4)
        self.status_label.setText("正在思考" + dots)
        self._think_dots += 1

    def set_mood(self, mood):
        self.status_label.setText(f"心情：{mood}")

    def set_daily_stats(self, text):
        self.stats_label.setText(text)

    def _scroll_to_bottom(self):
        bar = self.scroll.verticalScrollBar()

        def _set():
            try:
                if bar is not None:
                    bar.setValue(bar.maximum())
            except RuntimeError:
                pass  # 窗口已销毁：定时器晚到，不再触碰已删除的 C++ 对象

        QTimer.singleShot(0, _set)
        QTimer.singleShot(50, _set)

    def closeEvent(self, event):
        if self._think_timer:
            self._think_timer.stop()
        self.hide()
        event.ignore()
