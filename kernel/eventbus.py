"""kernel.eventbus：事件总线（发布/订阅，kernel 级通信通道）。

设计约束：
- 订阅者声明线程偏好：async_=False（默认，调用方线程同步执行）或
  async_=True（排队到总线所在线程=主线程执行，跨线程自动 QueuedConnection）；
- emit() 同步分流：sync 订阅者立即执行，async 订阅者投递主线程；
  发布者从不等待订阅者完成（tool_call 等关键循环保持同步直连，不经事件总线，
  总线只做旁路通知——见 brain 层 tools.execute / agent._run_tool 的注释）；
- 订阅者异常隔离：单个 handler 抛异常不影响其他订阅者；
- 用途：kernel 系统事件（config.saved / module.reloaded）与 brain 旁路通知
  （tool.executed 等），为 updater / planner 提供解耦通道。
"""

from PySide6.QtCore import QObject, Signal


class EventBus(QObject):
    """发布/订阅事件总线。必须在有 QCoreApplication 的线程创建（主线程）。

    topic 为字符串；payload 为任意 Python 对象（跨线程时按引用传递，
    订阅者不得修改发布者持有的可变对象）。
    """

    # (topic, payload)：跨线程投递到总线所在线程的事件循环
    _dispatch = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sync = {}   # topic -> [handler]
        self._async = {}  # topic -> [handler]
        self._dispatch.connect(self._on_dispatch)

    # ---------- 订阅 / 退订 ----------

    def subscribe(self, topic, handler, async_=False):
        """订阅事件。async_=True 时 handler 保证在总线线程（主线程）执行。"""
        table = self._async if async_ else self._sync
        table.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic, handler):
        for table in (self._sync, self._async):
            handlers = table.get(topic)
            if handlers and handler in handlers:
                handlers.remove(handler)
                if not handlers:
                    table.pop(topic, None)
                return

    def has_subscribers(self, topic):
        return bool(self._sync.get(topic) or self._async.get(topic))

    # ---------- 发布 ----------

    def emit(self, topic, payload=None):
        """发布事件：sync 订阅者在调用线程立即执行；async 订阅者投递主线程。

        任何线程都可调用；发布者不阻塞（async 投递即返回，sync 执行是
        订阅者自己的开销）。
        """
        for handler in list(self._sync.get(topic, ())):
            try:
                handler(payload)
            except Exception:
                pass  # 异常隔离：不影响其他订阅者与发布者
        if self._async.get(topic):
            self._dispatch.emit(topic, payload)

    # ---------- 内部 ----------

    def _on_dispatch(self, topic, payload):
        """主线程执行 async 订阅者（QueuedConnection 保证在总线线程）。"""
        for handler in list(self._async.get(topic, ())):
            try:
                handler(payload)
            except Exception:
                pass
