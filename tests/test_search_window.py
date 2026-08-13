"""搜索窗口测试。"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from gui.search_window import SearchWindow


def _make_window():
    app = QApplication.instance() or QApplication(sys.argv)
    return SearchWindow()


def test_result_item_rich_layout():
    w = _make_window()
    w._show_results([{
        "title": "标题",
        "snippet": "摘要",
        "url": "https://example.com",
    }], "")
    item = w.results.item(0)
    assert item.data(Qt.UserRole) == "https://example.com"
    widget = w.results.itemWidget(item)
    labels = [l.text() for l in widget.findChildren(QLabel)]
    assert "标题" in labels and "摘要" in labels and "https://example.com" in labels


def test_search_window_not_always_on_top():
    w = _make_window()
    from gui.search_window import Qt as search_qt

    assert w.windowFlags() & search_qt.WindowStaysOnTopHint == 0
