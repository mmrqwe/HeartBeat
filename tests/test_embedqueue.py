"""kernel.embedqueue 测试：单 worker FIFO、worker 注入、clear/flush、异常隔离。

运行：python -m tests.test_embedqueue（或 python tests/test_embedqueue.py）
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.embedqueue import EmbedQueue


def test_enqueue_without_worker_returns_false():
    """worker 未注入：enqueue 返回 False（调用方降级同步）。"""
    q = EmbedQueue()
    assert q.enqueue("chat", 1, "hi") is False
    assert q.pending() == 0


def test_worker_receives_tasks_in_order():
    """FIFO：单 worker 按入队顺序处理。"""
    q = EmbedQueue()
    got = []
    q.set_worker(lambda kind, item_id, text: got.append((kind, item_id, text)))
    q.enqueue("chat", 1, "a")
    q.enqueue("memory", 2, "b")
    assert q.flush(timeout=2), "队列应排空"
    assert got == [("chat", 1, "a"), ("memory", 2, "b")]


def test_worker_exception_does_not_block_queue():
    """log-and-drop：单条任务异常不阻塞后续任务。"""
    q = EmbedQueue()
    got = []

    def worker(kind, item_id, text):
        if item_id == 1:
            raise RuntimeError("boom")
        got.append((kind, item_id, text))

    q.set_worker(worker)
    q.enqueue("chat", 1, "bad")
    q.enqueue("chat", 2, "good")
    assert q.flush(timeout=2), "队列应排空"
    assert got == [("chat", 2, "good")], "异常任务应被跳过，后续任务继续"


def test_clear_drops_pending():
    """clear：模型切换时清空待处理任务（reindex 补齐语义）。"""
    q = EmbedQueue()
    got = []

    def slow_worker(kind, item_id, text):
        time.sleep(0.05)
        got.append(item_id)

    q.set_worker(slow_worker)
    for i in range(10):
        q.enqueue("chat", i, str(i))
    q.clear()
    assert q.pending() == 0, "clear 后队列应为空"
    q.flush(timeout=1)
    assert len(got) < 10, "clear 后不应再消费新任务（已取出的可能完成）"


def test_flush_timeout_reports_not_empty():
    """flush 超时返回 False（worker 卡住时）。"""
    q = EmbedQueue()
    q.set_worker(lambda kind, item_id, text: time.sleep(0.5))
    q.enqueue("chat", 1, "a")
    assert q.flush(timeout=0.05) is False, "worker 慢于超时时应返回 False"
    q.flush(timeout=2)  # 收尾排空


def _run_all():
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
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
