"""kernel.embedqueue：向量索引异步队列（单 worker FIFO，宿主注入 worker）。

背景（P0，2026-08-12）：embedding 原本同步执行——append_chat/remember 在
聊天/记忆写入路径里串行跑 ONNX 推理（512 维 ~0.5s），拖长聊天响应。
本模块把 embedding 挪到后台单 worker，聊天/记忆写入只入队、立即返回。

设计（与 kernel.runtime 同哲学：机制/内容分离）：
- 本模块只做队列调度，不知道 embedding 是什么；worker 由宿主注入
  set_worker(fn(kind, item_id, text) -> None)（main.py 绑定 embedder + db）；
- 单 daemon worker + FIFO：同一文本的向量按入队顺序写库（并发 embed 同模型
  不安全且无序）；进程退出时未完成任务丢弃，由 reindex 补齐；
- worker 异常 log-and-drop：单条失败不阻塞队列、不重试（持续故障时重试
  会堵死队列；缺失向量由下次 reindex 补齐）；
- 软上限 MAX_PENDING：embedder 长期故障时队列不无限膨胀（丢最旧 + 记日志）；
- clear()：模型切换时清空待处理任务（向量表已清空，reindex 全表补齐）；
- flush(timeout)：测试/退出前等待队列排空。

依赖方向：只依赖 stdlib（queue/threading/logging）。
"""

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# 队列软上限：超出丢弃最旧任务（防 embedder 长期故障时无限膨胀）
MAX_PENDING = 1000


class EmbedQueue:
    """单 worker 异步向量索引队列。worker 未注入时 enqueue 返回 False。"""

    def __init__(self):
        self._q = queue.Queue(maxsize=MAX_PENDING)
        self._worker = None  # fn(kind, item_id, text) -> None（宿主注入）
        self._thread = None
        self._started = False
        self._lock = threading.Lock()
        self._processing = 0    # worker 正在处理的任务数（flush 需等它归零）
        self._counter_lock = threading.Lock()

    # ---------- 注入与生命周期 ----------

    def set_worker(self, worker):
        """注入 worker 并启动单 worker 线程（幂等）。"""
        self._worker = worker
        self._ensure_started()

    def _ensure_started(self):
        with self._lock:
            if self._started or self._worker is None:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="hb-embed"
            )
            self._thread.start()

    # ---------- 入队 ----------

    def enqueue(self, kind, item_id, text) -> bool:
        """入队一个向量索引任务。返回是否真正入队（worker 未注入返回 False）。"""
        if self._worker is None:
            return False
        self._ensure_started()
        try:
            self._q.put_nowait((kind, item_id, text))
            return True
        except queue.Full:
            # 软上限：丢弃最旧任务，防无限膨胀（embedder 长期故障场景）
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((kind, item_id, text))
            except queue.Full:
                return False
            logger.warning("embed 队列满，已丢弃最旧任务（kind=%s id=%s）", kind, item_id)
            return True

    # ---------- worker ----------

    def _loop(self):
        while True:
            kind, item_id, text = self._q.get()
            worker = self._worker
            if worker is None:
                continue
            with self._counter_lock:
                self._processing += 1
            try:
                worker(kind, item_id, text)
            except Exception as exc:
                # log-and-drop：不重试不阻塞队列（缺失向量由 reindex 补齐）
                logger.warning(
                    "embed 任务失败（kind=%s id=%s）：%s", kind, item_id, exc
                )
            finally:
                with self._counter_lock:
                    self._processing -= 1

    # ---------- 控制 ----------

    def clear(self):
        """清空待处理任务（模型切换时调用：向量表已清空，reindex 全表补齐）。"""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def flush(self, timeout=5.0) -> bool:
        """等待队列排空（测试/退出用）。返回是否排空。

        队列空且 worker 空闲（处理中任务为 0）才算排空。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._counter_lock:
                if self._q.empty() and self._processing == 0:
                    return True
            time.sleep(0.01)
        with self._counter_lock:
            return self._q.empty() and self._processing == 0

    def pending(self) -> int:
        return self._q.qsize()
