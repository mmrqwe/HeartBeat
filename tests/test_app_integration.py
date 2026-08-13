"""HeartBeatApp × kernel.runtime 接线集成测试：tick（主动思考）与 chat（聊天）全链路。

覆盖：
- tick：触发 → runtime.trigger → 子线程 work → on_result 回主线程 → busy/状态流转；
- chat：_send_chat → runtime.trigger(chat) → _chat_work → 流式 delta（含过期 epoch
  过滤）→ on_result → 状态流转。

需要 offscreen 环境：
    QT_QPA_PLATFORM=offscreen HB_NO_MAC_TRAY=1 python test_app_integration.py
"""

import json
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog

import core
import db as dbmod
import kernel
import main as mainmod
import plugins.quote as quote_mod
import plugins.rss_news as rss_mod
import plugins.weather as weather_mod

_ORIG_COLLECTS = {}


def wait(ms):
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)


def _make_app():
    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    cfg_path = Path(tmp.name) / "config.json"
    cfg = json.loads(json.dumps(kernel.boot.DEFAULT_CONFIG))
    cfg["api"]["api_key"] = ""  # 规则模式，零 LLM 调用
    cfg["embedding_enabled"] = False  # 测试不加载/不残留 ONNX 嵌入线程
    cfg["interval_minutes"] = 1
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    app = QApplication.instance()
    if app is None or not isinstance(app, QGuiApplication):
        # 若前面已有 QCoreApplication（eventbus/runtime 测试），不能复用，
        # 必须新建真正的 QApplication，否则创建 QWidget 会原生崩溃。
        app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # P3：data_dir 显式注入 tmp（Kernel 跳过真实用户目录迁移）——
    # 每个测试独立数据库/事件表，不再污染真实用户库
    hb = mainmod.HeartBeatApp(str(cfg_path), data_dir=tmp.name)
    # 屏蔽真实采集与思考（不碰网络/LLM）
    orig_gather = core.gather
    core.gather = lambda *a, **k: {"collections": [], "errors": []}
    # 规则聊天会直接调插件 collect（真实联网会残留网络线程，污染 Qt 事件循环）
    _ORIG_COLLECTS.clear()
    _ORIG_COLLECTS["weather"] = weather_mod.collect
    _ORIG_COLLECTS["rss_news"] = rss_mod.collect
    _ORIG_COLLECTS["quote"] = quote_mod.collect
    weather_mod.collect = lambda settings: [{"title": "天气", "text": "晴，适合出门"}]
    rss_mod.collect = lambda settings: [{"title": "新闻", "text": "本地测试新闻"}]
    quote_mod.collect = lambda settings: [{"title": "一言", "text": "测试一言"}]
    return tmp, hb, orig_gather


def _cleanup(tmp, hb, orig_gather):
    """统一清理：先还原 mock，再 close（关 DB/停线程），最后删临时目录。"""
    core.gather = orig_gather
    for mod, orig in (
        (weather_mod, _ORIG_COLLECTS.get("weather")),
        (rss_mod, _ORIG_COLLECTS.get("rss_news")),
        (quote_mod, _ORIG_COLLECTS.get("quote")),
    ):
        if orig is not None:
            mod.collect = orig
    try:
        hb.close()
    finally:
        tmp.cleanup()


class _ChatStub:
    """轻量聊天窗替身：记录消息与编码运行状态。"""

    def __init__(self):
        self.received = []
        self.coding_running = False

    def add_message(self, role, text, time_str=None):
        self.received.append((role, text))

    def set_thinking(self, on):
        pass

    def set_coding_status(self, text):
        pass

    def set_coding_running(self, on):
        self.coding_running = bool(on)

    def cancel_stream(self):
        pass


def test_coding_intent_routing():
    """自然语言编程意图路由：无 project_dir → 引导提示；有目录 → coding 任务；闲聊 → chat。"""
    tmp, hb, orig_gather = _make_app()
    try:
        rt = hb.kernel.runtime
        assert "coding" in rt._tasks, "coding 任务未注册"

        class _Stub:
            received = []

            def add_message(self, role, text, time_str=None):
                self.received.append((role, text))

            def set_thinking(self, on):
                pass

            def set_coding_status(self, text):
                pass

            def set_coding_running(self, on):
                pass

        hb.chat_win = _Stub()
        # 1) 编程意图 + 未选目录 → 本地引导提示，不进入任何任务
        hb._send_chat("帮我把代码改一下")
        assert any("项目目录" in text for _, text in hb.chat_win.received), (
            f"应提示选择项目目录，实际：{hb.chat_win.received}"
        )
        assert not rt.is_busy("coding")
        # 2) 选中目录后，编程意图消息自然路由到 coding 任务
        hb.cfg["project_dir"] = str(Path(tmp.name) / "proj")
        hb.chat_win.received.clear()
        hb._send_chat("写一个 html 页面")
        deadline = time.time() + 3
        while time.time() < deadline and rt.is_busy("coding"):
            wait(50)
        assert not rt.is_busy("coding")
        # coding 平行路径也必须落库用户提问，历史不能只有 assistant 回复
        coding_texts = [
            m["text"] for m in hb.agent.chat_history(session_id="default")
        ]
        assert any("写一个 html 页面" in t for t in coding_texts), (
            f"coding 用户消息缺失：{coding_texts}"
        )
        # 3) 闲聊消息不触发 coding（走 chat）
        hb._send_chat("今天天气不错")
        deadline = time.time() + 3
        while time.time() < deadline and rt.is_busy("chat"):
            wait(50)
        assert not rt.is_busy("coding")
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_tick_lifecycle():
    """一次完整 tick：触发 → 忙碌 + 状态 → 完成复位 + 结果提示。"""
    tmp, hb, orig_gather = _make_app()
    try:
        rt = hb.kernel.runtime
        assert "tick" in rt._tasks and "chat" in rt._tasks, "tick/chat 任务未注册"

        # 慢任务制造 busy 窗口
        hb.agent.live = lambda ctx: time.sleep(0.3) or None
        QTimer.singleShot(50, hb._autonomy_tick)
        wait(150)
        assert rt.is_busy("tick"), "tick 应处于忙碌状态"
        assert hb.pet._status_text == "思考中…", f"状态应为‘思考中…’，实际：{hb.pet._status_text}"

        # busy 保护：忙碌中再次触发 epoch 不递增
        started = rt.current_epoch("tick")
        hb._autonomy_tick()
        assert rt.current_epoch("tick") == started, "busy 中重复触发不应递增 epoch"

        wait(800)
        assert not rt.is_busy("tick"), "tick 完成后应复位 busy"
        assert hb.pet._status_text.endswith("想了一圈，暂无新事"), f"状态应为完成提示，实际：{hb.pet._status_text}"
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_chat_lifecycle_and_stale_delta():
    """聊天全链路：触发 → 流式 delta（过期 epoch 被过滤）→ 回复 → 状态流转。"""
    tmp, hb, orig_gather = _make_app()
    try:
        rt = hb.kernel.runtime

        deltas = []
        hb.bridge.delta.connect(lambda epoch, text, session_id: deltas.append((epoch, text)))

        def fake_chat(user_text, on_delta=None, session_id="default"):
            # 模拟流式：正确 epoch 的增量 + 过期线程的增量
            if on_delta:
                on_delta("你好")
                hb._stream_delta(999, "过期增量", "default")  # 模拟旧线程（epoch 不匹配）
            return "你好呀，我在！"

        hb.agent.chat = fake_chat
        hb._send_chat("你好")
        wait(300)

        # 流式增量：只保留最新 epoch
        assert any(e == 1 for e, _ in deltas), f"正确 epoch 的 delta 缺失：{deltas}"
        assert all(e != 999 for e, _ in deltas), f"过期 epoch 的 delta 泄漏：{deltas}"
        # 完成：busy 复位 + 状态流转
        assert not rt.is_busy("chat"), "chat 完成后应复位 busy"
        assert hb.pet._status_text == "陪我聊天中", f"状态应为‘陪我聊天中’，实际：{hb.pet._status_text}"
    finally:
        _cleanup(tmp, hb, orig_gather)




def test_events_written_for_chat_and_tick():
    """P1：chat/tick 任务埋点写入 events 时间线（trace_id 关联会话）。

    注意：_make_app 的 data_dir 是真实用户目录（Kernel 迁移逻辑），
    events 表与真实库共享——断言必须按 trace_id 过滤本测试会话。
    """
    tmp, hb, orig_gather = _make_app()
    try:
        hb.agent.live = lambda ctx: None
        hb.agent.chat = lambda user_text, on_delta=None: "ok"
        # tick 会话
        QTimer.singleShot(0, hb._autonomy_tick)
        wait(300)
        tick_trace = getattr(hb.agent, "_trace_id", "") or ""
        assert tick_trace.startswith("tick_"), f"tick trace 缺失：{tick_trace!r}"
        tick_events = hb.db.event_items(trace_id=tick_trace, limit=10)
        assert {e["type"] for e in tick_events} == {"tick.started", "tick.finished"}, tick_events
        # chat 会话
        hb._send_chat("hi")
        wait(300)
        chat_trace = getattr(hb.agent, "_trace_id", "") or ""
        assert chat_trace.startswith("chat_"), f"chat trace 缺失：{chat_trace!r}"
        chat_events = hb.db.event_items(trace_id=chat_trace, limit=10)
        assert len(chat_events) == 2, chat_events
        assert {e["type"] for e in chat_events} == {"chat.started", "chat.finished"}, chat_events
        # started/finished 同 trace
        assert chat_events[0]["trace_id"] == chat_events[1]["trace_id"] == chat_trace
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_chat_busy_queues_next_message():
    """chat 忙碌时新消息应排队，空闲后按 FIFO 继续，不静默丢弃。"""
    tmp, hb, orig_gather = _make_app()
    try:
        rt = hb.kernel.runtime
        gate = threading.Event()
        calls = []

        def fake_chat(user_text, on_delta=None, session_id="default"):
            calls.append(user_text)
            if user_text == "first":
                gate.wait(2)
            return "ok"

        hb.agent.chat = fake_chat
        hb._send_chat("first")
        wait(200)
        assert rt.is_busy("chat")

        hb._send_chat("second")
        assert hb._chat_pending == [("second", "default")]
        assert "已排队" in hb.pet._status_text

        gate.set()
        wait(600)
        assert calls == ["first", "second"], calls
        assert hb._chat_pending == []
        assert not rt.is_busy("chat")
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_session_switch_and_dir_binding():
    """多对话编排：目录↔会话一对一；切换会话同步全局编程目录；删除回落默认。"""
    tmp, hb, orig_gather = _make_app()
    try:
        d = hb.db
        assert hb.current_session_id == "default"
        # 目录绑定：find → 无则建（一对一）
        proj = str(Path(tmp.name) / "proj")
        assert d.find_session_by_project_dir(proj) is None
        sid = d.create_session("快排项目", project_dir=proj)
        assert d.find_session_by_project_dir(proj)["id"] == sid
        # 切换到绑定目录的会话 → 全局编程目录跟随
        hb._switch_session(sid)
        assert hb.current_session_id == sid
        assert hb.cfg["project_dir"] == dbmod._normalize_project_dir(str(proj))
        # 会话消息落库后切换加载（无 UI 时只验证数据面）
        hb.agent.append_chat("user", "写个快排", session_id=sid)
        hb.agent.append_chat("user", "默认会话消息", session_id="default")
        hb._switch_session("default")
        assert hb.current_session_id == "default"
        assert hb.cfg["project_dir"] == "", (
            f"切到未绑定会话应清空 project_dir，实际：{hb.cfg['project_dir']!r}"
        )
        assert [m["text"] for m in hb.agent.chat_history(session_id="default")] == ["默认会话消息"]
        # 新建会话：名称带序号
        hb._new_session()
        new_sid = hb.current_session_id
        assert new_sid != "default" and new_sid != sid
        assert hb.db.session(new_sid)["name"] == "新对话 1"
        # 删除当前会话 → 回落默认
        hb._delete_session(new_sid)
        assert hb.current_session_id == "default"
        assert hb.db.session(new_sid) is None
        # 默认会话不可删
        hb._delete_session("default")
        assert hb.db.session("default") is not None
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_pick_dir_binds_current_empty_session(tmp_path, monkeypatch):
    """新建空对话后选文件夹：直接绑定当前对话，不在左侧新增一项。"""
    tmp, hb, orig_gather = _make_app()
    try:
        hb._new_session()
        sid = hb.current_session_id
        proj = tmp_path / "proj"
        monkeypatch.setattr(
            QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *a, **k: str(proj)),
        )
        hb._pick_project_dir()
        assert hb.current_session_id == sid, "应继续使用当前空对话"
        expected_dir = dbmod._normalize_project_dir(str(proj))
        assert hb.db.session(sid)["project_dir"] == expected_dir
        assert hb.cfg["project_dir"] == expected_dir
        assert len(hb.db.list_sessions()) == 2, "不应新增会话"
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_coding_completion_uses_pet_bubble(tmp_path, monkeypatch):
    """编码完成：宠物气泡播报一句话摘要 + happy 动画，聊天窗关了也看得见。"""
    tmp, hb, orig_gather = _make_app()
    try:
        bubbles = []
        animations = []
        monkeypatch.setattr(
            hb.pet, "show_bubble",
            lambda text, seconds=8, animation="talk": bubbles.append(text),
        )
        monkeypatch.setattr(
            hb.pet, "play", lambda *args, **kwargs: animations.append(args[0])
        )
        hb._show_coding_reply(("default", "搞定了：改了 main.py\n测试通过"))
        assert any("搞定了" in b for b in bubbles)
        assert "happy" in animations
        assert hb._coding_status_text == ""
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_coding_progress_question_answered_directly(tmp_path, monkeypatch):
    """编码中问“你在干嘛”：宠物直接用当前步骤回答，不排队走聊天。"""
    tmp, hb, orig_gather = _make_app()
    try:
        hb._coding_status_text = "正在修改 main.py"
        hb.chat_win = _ChatStub()
        monkeypatch.setattr(
            hb.kernel.runtime, "is_busy", lambda name: name == "coding"
        )
        hb._send_chat("你在干嘛")
        assert any(
            "正在修改 main.py" in text
            for _, text in hb.chat_win.received
        ), hb.chat_win.received
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_cancel_coding_stops_task_and_skips_reply(tmp_path, monkeypatch):
    """手动停止：busy 释放、排队清空、在途结果不再落库为 assistant。"""
    tmp, hb, orig_gather = _make_app()
    try:
        gate = threading.Event()

        def fake_coding_task(text, on_status=None, on_delta=None, max_rounds=None,
                             session_id="default", cancel_event=None):
            gate.wait(3)
            return "done"

        hb.agent.coding_task = fake_coding_task
        hb.cfg["project_dir"] = str(Path(tmp.name) / "proj")
        hb.chat_win = _ChatStub()
        hb._send_chat("写一个 html 页面")
        rt = hb.kernel.runtime
        deadline = time.time() + 3
        while time.time() < deadline and not rt.is_busy("coding"):
            wait(20)
        assert rt.is_busy("coding")
        hb._cancel_coding()
        assert not rt.is_busy("coding")
        gate.set()
        wait(300)
        history = hb.agent.chat_history(session_id="default")
        assert not any(
            m["role"] == "assistant" and m["text"] == "done" for m in history
        ), history
        assert any(
            m["role"] == "system" and "手动停止" in m["text"] for m in history
        ), history
    finally:
        _cleanup(tmp, hb, orig_gather)


def test_coding_continues_after_session_switch(tmp_path):
    """并发策略：编码任务归属发起会话，切换会话只影响显示，不打断执行。"""
    tmp, hb, orig_gather = _make_app()
    try:
        gate = threading.Event()

        def fake_coding_task(text, on_status=None, on_delta=None, max_rounds=None,
                             session_id="default", cancel_event=None, confirm_plan=None):
            gate.wait(3)
            return "done"

        hb.agent.coding_task = fake_coding_task
        hb.cfg["project_dir"] = str(Path(tmp.name) / "proj")
        hb._send_chat("写一个 html 页面")
        rt = hb.kernel.runtime
        deadline = time.time() + 3
        while time.time() < deadline and not rt.is_busy("coding"):
            wait(20)
        origin = hb._coding_task_session
        assert origin == "default"

        other = hb.db.create_session("其他会话")
        hb._switch_session(other)
        assert hb.current_session_id == other
        assert rt.is_busy("coding"), "切换会话不应打断编码任务"
        assert hb._coding_task_session == origin

        hb._cancel_coding()
        gate.set()
        wait(300)
        assert not rt.is_busy("coding")
    finally:
        _cleanup(tmp, hb, orig_gather)


def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} TESTS FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
