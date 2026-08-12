"""聊天窗口：Qt 气泡式多轮对话，支持流式更新。"""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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
BUBBLE_MAX_W = 380   # 气泡最大宽度（超过后换行，受窗口宽度约束）
BUBBLE_MARGIN = 10   # 气泡内边距（与 documentMargin 一致）


def _make_bubble(role):
    """Markdown 渲染气泡（QTextBrowser 原生支持）。"""
    bubble = QTextBrowser()
    bubble.setStyleSheet(theme.bubble_style(role))
    bubble.setFrameShape(QFrame.NoFrame)
    bubble.setOpenExternalLinks(True)
    bubble.viewport().setAutoFillBackground(False)  # QSS 背景作用于 frame，viewport 需透明
    bubble.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    bubble.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    # 宽度不固定：由 _sync_bubble_height 按内容自然宽度自适应（短消息窄气泡），
    # 超长内容最多撑到 BUBBLE_MAX_W 后换行，避免超宽单词横向溢出。
    bubble.setMaximumWidth(BUBBLE_MAX_W)
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
    def __init__(self, pet_name, on_send, on_clear):
        super().__init__()
        self.pet_name = pet_name
        self.on_send = on_send
        self.on_clear = on_clear
        self.messages = []
        self._last_bubble = None
        self._streaming = False
        self._think_dots = 0
        self._think_timer = None
        self.coding_mode = False
        self.coding_status_label = None

        self.setWindowTitle(f"和{pet_name}聊天")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.resize(420, 600)
        self.setMinimumSize(340, 420)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

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
        clear_btn.clicked.connect(self._confirm_clear)
        self.coding_btn = QPushButton("🔨 编码")
        self.coding_btn.setCheckable(True)
        self.coding_btn.setToolTip("开启后，消息将作为编程任务在项目目录里执行")
        self.coding_btn.toggled.connect(self._on_coding_toggled)
        row1.addWidget(name_label)
        row1.addWidget(self.status_label)
        row1.addStretch(1)
        row1.addWidget(self.coding_btn)
        row1.addWidget(clear_btn)
        header_layout.addLayout(row1)
        header_layout.addWidget(self.stats_label)
        root.addWidget(header)

        self.coding_status_label = QLabel("")
        self.coding_status_label.setObjectName("CodingStatus")
        self.coding_status_label.setWordWrap(True)
        self.coding_status_label.hide()
        root.addWidget(self.coding_status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(4)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        root.addWidget(self.scroll, 1)

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
        root.addWidget(bottom)

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
        """按内容自然宽度（不换行渲染）计算气泡宽度，clamp 到 [MIN, MAX]。

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
        return max(BUBBLE_MIN_W, min(natural, BUBBLE_MAX_W))

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

        宽度 = 内容自然宽度（上限 BUBBLE_MAX_W），高度用独立探针估算；
        已显示且有真实布局高度时取两者较大值，再延迟一帧用真实布局修正，
        消除字体替换/宽度差异导致的估算偏差（任何环境下都不裁剪内容）。
        """
        if bubble is None:
            return
        width = self._natural_bubble_width(bubble, text, markdown)
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
        if bubble is None or not bubble.window() or not bubble.window().isVisible():
            return
        real = bubble.document().size().height()
        if real > 0:
            bubble.setFixedHeight(max(int(real) + 4, 24))
            self._scroll_to_bottom()
        else:
            QTimer.singleShot(0, lambda: self._fix_bubble_height(bubble))

    def update_last_message(self, text):
        if self._last_bubble is not None:
            # 流式中用纯文本：markdown 半成品（未闭合 **、``` 等）会闪烁/误渲染
            self._last_bubble.setPlainText(text)
            self._sync_bubble_height(self._last_bubble, text, markdown=False)
            self._scroll_to_bottom()

    def finish_stream(self, text):
        """流式结束：用最终完整文本收尾（Markdown 渲染），并复位流式状态。"""
        if self._last_bubble is not None:
            self._last_bubble.setMarkdown(text)
            self._sync_bubble_height(self._last_bubble, text, markdown=True)
            self._scroll_to_bottom()
        self._streaming = False

    def is_streaming(self):
        return self._streaming

    def cancel_stream(self):
        """取消流式：移除当前占位气泡并复位状态（超时/中断等场景）。"""
        if self._last_bubble is not None:
            self.content_layout.removeWidget(self._last_bubble)
            self._last_bubble.deleteLater()
            self._last_bubble = None
        self._streaming = False

    def clear(self):
        self.messages = []
        self._last_bubble = None
        self._streaming = False
        while self.content_layout.count() > 1:
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _confirm_clear(self):
        answer = QMessageBox.question(
            self, "清空记录", "确定清空聊天记录吗？"
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

    def _on_coding_toggled(self, checked):
        self.coding_mode = bool(checked)
        self.coding_btn.setText("🔨 编码中" if checked else "🔨 编码")
        if checked:
            self.set_coding_status(
                "编码模式已开启：下一条消息将作为编程任务执行。"
                "请在设置里确认项目目录（project_dir）。"
            )
        else:
            self.set_coding_status("")
        self.input_text.setPlaceholderText(
            "描述要完成的编程任务…（Enter 发送）"
            if checked
            else "说点什么…（Enter 发送 / Shift+Enter 换行）"
        )

    def set_coding_status(self, text):
        """编码任务状态行（步骤进度 / 完成提示）；空文本隐藏。"""
        if self.coding_status_label is None:
            return
        if text:
            self.coding_status_label.setText(text)
            self.coding_status_label.show()
        else:
            self.coding_status_label.hide()

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
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        QTimer.singleShot(50, lambda: bar.setValue(bar.maximum()))

    def closeEvent(self, event):
        if self._think_timer:
            self._think_timer.stop()
        self.hide()
        event.ignore()
