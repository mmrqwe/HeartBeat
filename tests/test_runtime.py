"""kernel.runtime 行为验证：epoch 竞态 / busy 保护 / watchdog 超时 / 定时重排。

用 QApplication（offscreen，无需真实窗口）验证 Runtime 的关键机制与旧
HeartBeatApp 手写 QTimer+线程逻辑的行为等价性。可直接运行，也可用 pytest：
    python test_runtime.py
"""

import sys
import time

from PySide6.QtWidgets import QApplication

from kernel.runtime import Runtime

_app = None


def _ensure_app():
    global _app
    if _app is None:
        # 用 QApplication 而非 QCoreApplication：若本文件先跑，
        # 不能留下“非 GUI 单例”挡住后续 app_integration 的 QWidget。
        _app = QApplication.instance() or QApplication(sys.argv)
    return _app


def wait(ms):
    """跑事件循环直到条件满足或超时。"""
    app = _ensure_app()
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


def test_basic_trigger_and_callback():
    """基本触发 + on_result（子线程 → 主线程回调）"""
    rt = Runtime()
    results = []
    rt.add_task(
        "t1",
        work=lambda epoch: f"res-{epoch}",
        timeout_ms=5000,
        on_result=lambda r: results.append(("t1", r)),
    )
    assert rt.trigger("t1"), "trigger should start"
    assert rt.is_busy("t1")
    wait(200)
    assert ("t1", "res-1") in results, f"t1 result missing: {results}"
    assert not rt.is_busy("t1"), "busy should clear after done"


def test_busy_guard():
    """busy 保护：同一任务忙碌时 trigger 返回 False"""
    rt = Runtime()
    results = []
    rt.add_task(
        "t2",
        work=lambda epoch: time.sleep(0.3) or f"res-{epoch}",
        timeout_ms=5000,
        on_result=lambda r: results.append(("t2", r)),
    )
    assert rt.trigger("t2"), "t2 first trigger"
    assert not rt.trigger("t2"), "t2 should be busy-guarded"
    wait(500)
    assert ("t2", "res-1") in results, f"t2 result missing: {results}"


def test_timeout_drops_stale_result():
    """超时：on_timeout 触发，且超时后到达的过期结果被丢弃"""
    rt = Runtime()
    results = []
    rt.add_task(
        "t3",
        work=lambda epoch: time.sleep(0.2) or "late",
        timeout_ms=50,
        on_result=lambda r: results.append(("t3", r)),
        on_timeout=lambda: results.append(("t3-timeout",)),
    )
    assert rt.trigger("t3"), "t3 trigger"
    wait(400)
    assert ("t3-timeout",) in results, f"t3 timeout missing: {results}"
    assert not any(r[0] == "t3" for r in results), f"t3 stale result leaked: {results}"
    assert not rt.is_busy("t3"), "t3 busy should clear on timeout"


def test_interval_auto_reschedule_and_stop():
    """定时重排：interval 任务到点自动重排并触发；stop_all 后不再触发"""
    rt = Runtime()
    results = []
    rt.add_task(
        "t4",
        work=lambda epoch: "t4-ok",
        timeout_ms=5000,
        on_result=lambda r: results.append(("t4", r)),
        interval_ms=80,
    )
    rt.schedule_next("t4", 80)
    wait(300)
    assert sum(1 for r in results if r[0] == "t4") >= 2, f"t4 should be periodic: {results}"
    rt.stop_all()  # 周期任务必须 stop_all 才停
    wait(80)  # 冲刷 stop 前已在途的结果，避免误判为 stop 后触发
    before = sum(1 for r in results if r[0] == "t4")
    wait(250)
    assert sum(1 for r in results if r[0] == "t4") == before, "t4 fired after stop_all"


def test_on_timer_custom_callback():
    """on_timer：提供时定时到点完全交给回调（重排/触发/状态由调用方负责）"""
    rt = Runtime()
    results = []
    calls = []

    def on_timer():
        calls.append("timer")
        rt.schedule_next("t5", 60)
        assert rt.trigger("t5"), "on_timer 内 trigger 应成功"

    rt.add_task(
        "t5",
        work=lambda epoch: "t5-ok",
        timeout_ms=5000,
        on_result=lambda r: results.append(("t5", r)),
        interval_ms=60,
        on_timer=on_timer,
    )
    rt.schedule_next("t5", 60)
    wait(400)
    assert len(calls) >= 2, f"on_timer should be called repeatedly: {calls}"
    assert ("t5", "t5-ok") in results, f"t5 result missing: {results}"
    rt.stop_all()


def test_error_callback_receives_exception_object():
    """on_error 收到异常对象（P0：宿主侧可分类基础设施/brain 故障）。"""
    rt = Runtime()
    errors = []
    rt.add_task(
        "t6",
        work=lambda epoch: (_ for _ in ()).throw(ValueError("boom")),
        timeout_ms=5000,
        on_error=lambda e: errors.append(e),
    )
    assert rt.trigger("t6"), "t6 trigger"
    wait(200)
    assert len(errors) == 1 and isinstance(errors[0], ValueError), f"errors: {errors}"
    assert str(errors[0]) == "boom"


def test_cancel_clears_busy_and_drops_result():
    """主动取消：busy 立即释放，在途结果被 epoch 丢弃，不触发 on_timeout。"""
    rt = Runtime()
    results = []
    timeouts = []
    rt.add_task(
        "c1",
        work=lambda epoch: time.sleep(0.2) or "late",
        timeout_ms=5000,
        on_result=lambda r: results.append(r),
        on_timeout=lambda: timeouts.append(1),
    )
    assert rt.trigger("c1")
    assert rt.cancel("c1") is True
    assert not rt.is_busy("c1")
    assert rt.cancel("c1") is False
    wait(400)
    assert results == []
    assert timeouts == []


def test_stop_all_cancels_queued_and_silences_inflight():
    """线程池化（P1）：stop_all 取消排队任务；在途任务完成后不再 emit。"""
    rt = Runtime()
    results = []

    def slow(epoch):
        time.sleep(0.2)
        return "late"

    # 占满 3 个 worker（其中一个立即完成）后提交排队任务
    rt.add_task("a", work=slow, timeout_ms=5000, on_result=lambda r: results.append(("a", r)))
    rt.add_task("b", work=slow, timeout_ms=5000, on_result=lambda r: results.append(("b", r)))
    rt.add_task("c", work=slow, timeout_ms=5000, on_result=lambda r: results.append(("c", r)))
    rt.add_task("d", work=lambda epoch: "d-ok", timeout_ms=5000,
                on_result=lambda r: results.append(("d", r)))
    assert rt.trigger("a") and rt.trigger("b") and rt.trigger("c")
    assert rt.trigger("d"), "d 排队中"
    rt.stop_all()
    wait(600)
    # d 被取消（排队未执行）；a/b/c 在途执行但 _stopping 后不再 emit
    assert not any(r[0] == "d" for r in results), f"排队任务应被取消: {results}"
    assert all(r[0] in ("a", "b", "c") for r in results), f"在途任务应被静默: {results}"


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
