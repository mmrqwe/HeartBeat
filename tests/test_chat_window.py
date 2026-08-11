"""聊天窗口气泡宽度自适应测试（offscreen）。

运行：
    QT_QPA_PLATFORM=offscreen HB_NO_MAC_TRAY=1 python -m tests.test_chat_window
"""

import sys

from PySide6.QtWidgets import QApplication

from gui import chat_window


def _make_window():
    app = QApplication.instance() or QApplication(sys.argv)
    w = chat_window.ChatWindow("测试", on_send=lambda *a: None, on_clear=lambda: None)
    return w


def _bubble_width(w, index=0):
    """取 content_layout 中第 index 个气泡的宽度。

    setFixedWidth(w) 会同时把 maximumWidth 设为 w（未设置时为 16777215），
    故用 maximumWidth 读取固定后的宽度，不依赖布局映射。
    """
    count = 0
    for i in range(w.content_layout.count()):
        item = w.content_layout.itemAt(i)
        if item.widget() is None:
            continue
        if isinstance(item.widget(), chat_window.QTextBrowser):
            if count == index:
                return item.widget().maximumWidth()
            count += 1
    raise AssertionError("未找到气泡")


def test_short_message_narrow():
    """短消息：气泡收窄到最小宽度（不再是固定 300）。"""
    w = _make_window()
    w.add_message("user", "在吗")
    w.add_message("assistant", "在的！")
    assert _bubble_width(w, 0) == chat_window.BUBBLE_MIN_W
    assert _bubble_width(w, 1) == chat_window.BUBBLE_MIN_W


def test_medium_message_adaptive():
    """中等长度：气泡宽度介于 MIN 与 MAX 之间（按内容自适应）。"""
    w = _make_window()
    w.add_message("user", "今天天气怎么样呀，要不要一起出去走走")
    width = _bubble_width(w, 0)
    assert chat_window.BUBBLE_MIN_W < width < chat_window.BUBBLE_MAX_W


def test_long_message_capped():
    """长消息：撑到最大宽度后换行，不横向溢出。"""
    w = _make_window()
    w.add_message("user", "这是一段特别长的消息内容" * 20)
    assert _bubble_width(w, 0) == chat_window.BUBBLE_MAX_W


def test_stream_grows_then_caps():
    """流式：文字变长 → 气泡宽度递增，超过上限后封顶。"""
    w = _make_window()
    w.begin_stream()
    w.update_last_message("你好")
    w1 = _bubble_width(w, 0)
    w.update_last_message("你好，这是一段比较长的回复" * 2)
    w2 = _bubble_width(w, 0)
    w.update_last_message("你好，这是一段比较长的回复" * 50)
    w3 = _bubble_width(w, 0)
    assert w1 < w2 < w3 or w1 == w2 == chat_window.BUBBLE_MAX_W
    assert w3 == chat_window.BUBBLE_MAX_W
    w.finish_stream("你好，这是一段比较长的回复" * 50)


def test_super_long_word_capped():
    """超长无空格串（URL）：clamp 到上限，不横向溢出。"""
    w = _make_window()
    w.add_message("assistant", "https://example.com/" + "a" * 400)
    assert _bubble_width(w, 0) == chat_window.BUBBLE_MAX_W


def _run_plain():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_plain() else 0)
