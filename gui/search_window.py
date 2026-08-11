"""搜索窗口：Qt 界面，支持网页 / 新闻 / 股票 / 天气，点击结果在浏览器打开。"""

import threading
import webbrowser

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import search

CATEGORIES = [
    ("综合", "web"),
    ("新闻", "news"),
    ("热点", "hot"),
    ("股票", "stock"),
    ("天气", "weather"),
    ("百科", "wiki"),
    ("学术", "arxiv"),
]


class SearchBridge(QObject):
    results = Signal(list, str)


class SearchWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HeartBeat 搜索")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.resize(520, 520)
        self.setMinimumSize(420, 380)
        self._bridge = SearchBridge()
        self._bridge.results.connect(self._show_results)
        self._build()
        self.query_edit.setFocus()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        bar = QHBoxLayout()
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText(
            "搜索内容：人工智能 / 600519 / 北京 / 量子计算…"
        )
        self.query_edit.returnPressed.connect(self._do_search)
        self.category_combo = QComboBox()
        for label, value in CATEGORIES:
            self.category_combo.addItem(label, value)
        search_btn = QPushButton("搜索")
        search_btn.setObjectName("Primary")
        search_btn.clicked.connect(self._do_search)
        bar.addWidget(self.query_edit, 1)
        bar.addWidget(self.category_combo)
        bar.addWidget(search_btn)
        root.addLayout(bar)

        self.status_label = QLabel("输入关键词，回车或点搜索。")
        self.status_label.setObjectName("Hint")
        root.addWidget(self.status_label)

        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda item: self._open_url(item))
        root.addWidget(self.results, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        open_btn = QPushButton("在浏览器打开")
        open_btn.clicked.connect(self._open_selected)
        copy_btn = QPushButton("复制链接")
        copy_btn.clicked.connect(self._copy_selected)
        actions.addWidget(open_btn)
        actions.addWidget(copy_btn)
        root.addLayout(actions)

    def _do_search(self):
        query = self.query_edit.text().strip()
        if not query:
            return
        category = self.category_combo.currentData()
        self.results.clear()
        self.status_label.setText("搜索中…")
        threading.Thread(
            target=self._worker, args=(query, category), daemon=True
        ).start()

    def _worker(self, query, category):
        try:
            entries = search.search_all(query, category, limit=8)
            self._bridge.results.emit(entries, "")
        except Exception as exc:
            self._bridge.results.emit([], str(exc))

    def _show_results(self, entries, error):
        self.results.clear()
        if error:
            self.status_label.setText(f"搜索失败：{error}")
            return
        if not entries:
            self.status_label.setText("没有找到结果。")
            return
        self.status_label.setText(f"找到 {len(entries)} 条结果，双击可打开。")
        for entry in entries:
            title = entry.get("title", "")
            snippet = entry.get("snippet", "")
            url = entry.get("url", "")
            text = title
            if snippet:
                text += "\n" + snippet
            if url:
                text += "\n" + url
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, url)
            item.setToolTip(url or title)
            self.results.addItem(item)

    def _open_selected(self):
        item = self.results.currentItem()
        if item:
            self._open_url(item)

    def _open_url(self, item):
        url = item.data(Qt.UserRole) if item else ""
        if url:
            webbrowser.open(url)

    def _copy_selected(self):
        item = self.results.currentItem()
        if not item:
            return
        url = item.data(Qt.UserRole) or ""
        if url:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(url)
            self.status_label.setText("链接已复制。")
