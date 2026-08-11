"""kernel.runtime 行为验证：epoch 竞态 / busy 保护 / watchdog 超时 / 定时重排。

用 QCoreApplication（offscreen，无需 GUI）验证 Runtime 的关键机制与旧
HeartBeatApp 手写 QTimer+线程逻辑的行为等价性。可直接运行，也可用 pytest：
    python test_runtime.py
"""

import sys
import time

from PySide6.QtCore import QCoreApplication, QTimer

from kernel.runtime import Runtime

app = QCoreApplication(sys.argv)
results = []


def wait(ms):
    """跑事件循环直到条件满足或超时。"""
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)


# 1) 基本触发 + on_result（子线程 → 主线程回调）
rt = Runtime()
rt.add_task("t1", work=lambda epoch: f"res-{epoch}", timeout_ms=5000, on_result=lambda r: results.append(("t1", r)))
assert rt.trigger("t1"), "trigger should start"
assert rt.is_busy("t1")
wait(200)
assert ("t1", "res-1") in results, f"t1 result missing: {results}"
assert not rt.is_busy("t1"), "busy should clear after done"

# 2) busy 保护：同一任务忙碌时 trigger 返回 False
rt.add_task("t2", work=lambda epoch: time.sleep(0.3) or f"res-{epoch}", timeout_ms=5000, on_result=lambda r: results.append(("t2", r)))
assert rt.trigger("t2"), "t2 first trigger"
assert not rt.trigger("t2"), "t2 should be busy-guarded"
wait(500)
assert ("t2", "res-1") in results, f"t2 result missing: {results}"

# 3) 超时：on_timeout 触发，且超时后到达的过期结果被丢弃
rt.add_task("t3", work=lambda epoch: time.sleep(0.2) or "late", timeout_ms=50, on_result=lambda r: results.append(("t3", r)), on_timeout=lambda: results.append(("t3-timeout",)))
assert rt.trigger("t3"), "t3 trigger"
wait(400)
assert ("t3-timeout",) in results, f"t3 timeout missing: {results}"
assert not any(r[0] == "t3" for r in results), f"t3 stale result leaked: {results}"
assert not rt.is_busy("t3"), "t3 busy should clear on timeout"

# 4) 定时重排：interval 任务到点自动重排并触发（周期语义）
rt.add_task("t4", work=lambda epoch: "t4-ok", timeout_ms=5000, on_result=lambda r: results.append(("t4", r)), interval_ms=80)
rt.schedule_next("t4", 80)
wait(300)
assert sum(1 for r in results if r[0] == "t4") >= 2, f"t4 should be periodic: {results}"
rt.stop_all()  # 周期任务必须 stop_all 才停
before = sum(1 for r in results if r[0] == "t4")
wait(250)
assert sum(1 for r in results if r[0] == "t4") == before, "t4 fired after stop_all"

# 5) on_timer：提供时定时到点完全交给回调（重排/触发/状态由调用方负责）
rt2 = Runtime()
calls = []


def on_timer():
    calls.append("timer")
    rt2.schedule_next("t5", 60)
    assert rt2.trigger("t5"), "on_timer 内 trigger 应成功"


rt2.add_task("t5", work=lambda epoch: "t5-ok", timeout_ms=5000, on_result=lambda r: results.append(("t5", r)), interval_ms=60, on_timer=on_timer)
rt2.schedule_next("t5", 60)
wait(400)
assert len(calls) >= 2, f"on_timer should be called repeatedly: {calls}"
assert ("t5", "t5-ok") in results, f"t5 result missing: {results}"
rt2.stop_all()

print("RUNTIME-OK: 全部 5 项机制验证通过")
app.quit()
