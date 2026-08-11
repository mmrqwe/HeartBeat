"""聊天窗口：Qt 气泡式多轮对话，支持流式更新。"""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import theme


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
        row1.addWidget(name_label)
        row1.addWidget(self.status_label)
        row1.addStretch(1)
        row1.addWidget(clear_btn)
        header_layout.addLayout(row1)
        header_layout.addWidget(self.stats_label)
        root.addWidget(header)

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
        bubble = QLabel(text)
        bubble.setStyleSheet(theme.bubble_style(role))
        bubble.setWordWrap(True)
        bubble.setMaximumWidth(300)
        bubble.setFixedWidth(300)  # 固定宽度：wordWrap 换行宽度确定，避免布局按 sizeHint 缩窄
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_layout.addWidget(bubble, 0, align)
        self._sync_bubble_height(bubble)

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

    def _sync_bubble_height(self, bubble):
        """按换行后的实际需求高度固定气泡高度（QLabel wordWrap 在流式频繁
        setText 下布局不会自动重算高度，会导致长文本底部被裁剪）。"""
        if bubble is None:
            return
        width = bubble.width() or 300
        height = bubble.heightForWidth(width)
        bubble.setFixedHeight(max(height, 16))

    def update_last_message(self, text):
        if self._last_bubble is not None:
            self._last_bubble.setText(text)
            self._sync_bubble_height(self._last_bubble)
            self._scroll_to_bottom()

    def finish_stream(self, text):
        """流式结束：用最终完整文本收尾，并复位流式状态。"""
        if self._last_bubble is not None:
            self._last_bubble.setText(text)
            self._sync_bubble_height(self._last_bubble)
            self._scroll_to_bottom()
        self._streaming = False

    def is_streaming(self):
        return self._streaming

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

    def closeEvent(self, event):
        if self._think_timer:
            self._think_timer.stop()
        self.hide()
        event.ignore()
