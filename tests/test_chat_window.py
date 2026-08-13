"""聊天窗口气泡宽度自适应测试（offscreen）。

运行：
    QT_QPA_PLATFORM=offscreen HB_NO_MAC_TRAY=1 python -m tests.test_chat_window
"""

import sys

from PySide6.QtWidgets import QApplication

from gui import chat_window


def _make_window(on_pick_dir=None):
    app = QApplication.instance() or QApplication(sys.argv)
    w = chat_window.ChatWindow(
        "测试", on_send=lambda *a: None, on_clear=lambda: None, on_pick_dir=on_pick_dir
    )
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


def test_dir_button_picks_and_shows():
    """目录按钮：点击回调宿主选择器；set_project_dir 更新按钮提示与状态行。"""
    picked = []
    w = _make_window(on_pick_dir=lambda: picked.append(1))
    w.dir_btn.click()
    assert picked == [1]
    assert w.coding_status_label.isHidden()
    w.set_project_dir("/tmp/proj")
    assert not w.coding_status_label.isHidden()
    assert "编程目录：/tmp/proj" in w.coding_status_label.text()
    assert "/tmp/proj" in w.dir_btn.toolTip()
    w.set_project_dir("")
    assert w.coding_status_label.isHidden()
    assert "选择编程项目目录" in w.dir_btn.toolTip()


def test_coding_status_show_hide():
    """编码状态行：有文本显示，空文本隐藏。"""
    w = _make_window()
    assert w.coding_status_label.isHidden()
    w.set_coding_status("🔨 第 1 步：读取文件")
    assert not w.coding_status_label.isHidden()
    assert "第 1 步" in w.coding_status_label.text()
    w.set_coding_status("")
    assert w.coding_status_label.isHidden()


def test_stop_coding_button():
    """编码运行中显示停止按钮，点击回调宿主。"""
    calls = []
    w = _make_window()
    w.on_cancel_coding = lambda: calls.append(1)
    assert w.stop_coding_btn.isHidden()
    w.set_coding_running(True)
    assert not w.stop_coding_btn.isHidden()
    w._on_stop_coding()
    assert calls == [1]
    w.set_coding_running(False)
    assert w.stop_coding_btn.isHidden()


def test_sessions_sidebar_render_and_switch():
    """会话侧边栏：渲染/高亮/📁 标记；点击非当前会话回调宿主。"""
    w = _make_window()
    sessions = [
        {"id": "default", "name": "默认对话", "project_dir": None},
        {"id": "abc123", "name": "快排项目", "project_dir": "/tmp/proj"},
    ]
    w.set_sessions(sessions, "default")
    assert w.session_list.count() == 2
    # 当前会话高亮
    assert w.session_list.currentItem().text() == "默认对话"
    # 绑定目录的会话带 📁 前缀
    texts = [w.session_list.item(i).text() for i in range(2)]
    assert "📁 快排项目" in texts
    # 点击另一会话 → 回调宿主并传 sid
    switched = []
    w.on_switch_session = lambda sid: switched.append(sid)
    w.session_list.setCurrentRow(1)
    w._on_session_clicked(w.session_list.item(1))
    assert switched == ["abc123"]
    # 点击当前会话不重复回调
    w.set_sessions(sessions, "abc123")
    w._on_session_clicked(w.session_list.item(1))
    assert switched == ["abc123"]


def test_sessions_new_and_delete_callbacks():
    """➕/🗑 按钮回调宿主；默认会话删除弹信息框（不回调）。"""
    w = _make_window()
    new_calls = []
    del_calls = []
    w.on_new_session = lambda: new_calls.append(1)
    w.on_delete_session = lambda sid: del_calls.append(sid)
    w._on_new_session()
    assert new_calls == [1]
    # 默认会话不可删：_on_delete_session 弹 QMessageBox（offscreen 下不阻塞）——
    # 用 monkeypatch 弹窗返回 Yes 验证回调仍不触发
    import gui.chat_window as cw

    orig = cw.QMessageBox.question
    cw.QMessageBox.question = staticmethod(lambda *a, **k: cw.QMessageBox.Yes)
    try:
        w._on_delete_session()  # 当前 default → 信息框，无回调
        assert del_calls == []
    finally:
        cw.QMessageBox.question = orig
    # 非默认会话：确认 Yes → 回调 sid
    w.set_sessions(
        [{"id": "default", "name": "默认对话", "project_dir": None},
         {"id": "xyz", "name": "临时", "project_dir": None}],
        "xyz",
    )
    cw.QMessageBox.question = staticmethod(lambda *a, **k: cw.QMessageBox.Yes)
    try:
        w._on_delete_session()
    finally:
        cw.QMessageBox.question = orig
    assert del_calls == ["xyz"]


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
