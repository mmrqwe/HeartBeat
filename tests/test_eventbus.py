"""kernel.eventbus 机制测试：同步/异步派发、跨线程回主线程、退订、异常隔离。

可直接运行，也可用 pytest：
    python test_eventbus.py
"""

import sys
import threading
import time

from PySide6.QtCore import QCoreApplication

from kernel.eventbus import EventBus

app = QCoreApplication.instance() or QCoreApplication(sys.argv)


def wait(ms):
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def test_sync_dispatch():
    """同步订阅者在调用线程立即执行。"""
    bus = EventBus()
    thread_ids = []
    payloads = []

    def handler(payload):
        thread_ids.append(threading.get_ident())
        payloads.append(payload)

    bus.subscribe("topic", handler)
    bus.emit("topic", {"n": 1})
    assert payloads == [{"n": 1}], payloads
    assert thread_ids == [threading.get_ident()], "sync handler 应在调用线程执行"


def test_async_dispatch_cross_thread():
    """async 订阅者：子线程 emit，handler 回到主线程（总线线程）执行。"""
    bus = EventBus()
    main_tid = threading.get_ident()
    got = []

    def handler(payload):
        got.append((threading.get_ident(), payload))

    bus.subscribe("topic", handler, async_=True)
    threading.Thread(target=lambda: bus.emit("topic", "hello"), daemon=True).start()
    wait(300)
    assert got, "async handler 应收到事件"
    tid, payload = got[0]
    assert payload == "hello"
    assert tid == main_tid, f"async handler 应在主线程执行：{tid} != {main_tid}"


def test_unsubscribe():
    bus = EventBus()
    calls = []
    handler = lambda p: calls.append(p)
    bus.subscribe("topic", handler)
    bus.emit("topic", 1)
    bus.unsubscribe("topic", handler)
    bus.emit("topic", 2)
    assert calls == [1], calls


def test_exception_isolation():
    """单个 handler 抛异常不影响其他订阅者与发布者。"""
    bus = EventBus()
    seen = []

    def bad(_payload):
        raise RuntimeError("boom")

    def good(payload):
        seen.append(payload)

    bus.subscribe("topic", bad)
    bus.subscribe("topic", good)
    bus.emit("topic", "ok")  # 不应抛异常
    assert seen == ["ok"], seen


def test_emit_without_subscribers():
    bus = EventBus()
    bus.emit("no-such-topic", 1)  # 不应报错


def test_has_subscribers():
    bus = EventBus()
    assert not bus.has_subscribers("t")
    bus.subscribe("t", lambda p: None)
    assert bus.has_subscribers("t")
    bus.subscribe("t", lambda p: None, async_=True)
    assert bus.has_subscribers("t")


def test_agent_audit_publishes_tool_executed():
    """brain 层发布点：agent._audit_tool 发布 tool.executed（注入的 eventbus）。"""
    import agent
    import core
    import db as dbmod

    # 轻量构造 Agent（mock embedder 避免模型下载）
    from tempfile import TemporaryDirectory

    from pathlib import Path

    events = []

    class FakeBus:
        def emit(self, topic, payload=None):
            events.append((topic, payload))

    with TemporaryDirectory() as tmp:
        cfg = core.load_config(Path(tmp) / "nonexistent.json")
        cfg["api"]["api_key"] = ""
        cfg["embedding_enabled"] = False  # 测试不下载嵌入模型
        database = dbmod.Database(Path(tmp) / "heartbeat.db")
        ag = agent.Agent(cfg, {}, tmp, stats=None, db=database)
        ag.eventbus = FakeBus()
        ag._audit_tool("user", "run_bash", '{"command": "ls"}', "readonly", True, True, "exit=0")
    assert len(events) == 1, events
    topic, payload = events[0]
    assert topic == "tool.executed"
    assert payload["tool"] == "run_bash" and payload["approved"] is True and payload["ok"] is True


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
