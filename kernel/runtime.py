"""kernel.runtime：运行时内核（Qt 事件循环上的任务调度）。

它只做事件循环与任务管理：定时触发、线程池提交、看门狗超时、epoch 竞态保护。
不知道任务内容是什么（巡视 / 聊天 / 采集都一样处理）。

设计约束（与 Qt 主循环的关系）：
- GUI 应用的主循环是 ``app.exec()``，本模块在它之上调度，不做 while-True；
- 任务回调（on_result/on_error/on_timeout）一律回到主线程执行；
- 子线程中仅执行 work()，其结果经信号回主线程；
- epoch 竞态保护：同一任务只接受最新一次触发的结果，过期结果自动丢弃。

P1（2026-08-12）线程池化：
- ThreadPoolExecutor(max_workers=3) 替代每次 trigger 新建 daemon Thread——
  并发线程数上限 = 任务数（_busy 同名防并发），且未来任务增多不再线程爆炸；
- Future 只用于取消（超时取消排队任务）与关闭清理，结果仍走 Qt Signal；
- stop_all：shutdown(wait=False, cancel_futures=True) + _stopping 标志——
  在途任务不再 emit（防关闭后信号残留），排队任务取消执行。

Runtime 可独立于 GUI 使用（QObject 需要 QCoreApplication 存在）。
"""

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal


class Runtime(QObject):
    """任务调度内核。

    每个任务拥有：单次定时器（周期由 schedule_next 决定，手动触发不依赖定时器）、
    看门狗（超时重置任务并回调 on_timeout）、epoch（丢弃过期线程结果）、
    Future（线程池提交句柄，用于超时取消与关闭清理）。
    """

    # (task_name, epoch, result) / (task_name, epoch, error) / (task_name)
    # error 传异常对象（不 str 化）：宿主侧可分类（基础设施故障 vs brain 故障）
    task_done = Signal(str, int, object)
    task_error = Signal(str, int, object)
    task_timeout = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = {}
        self._epochs = {}
        self._busy = set()
        self._stopped = False
        # 关闭标志：stop_all 置位后，在途任务完成时不再 emit（防信号残留）
        self._stopping = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hb-task")

        self.task_done.connect(self._on_task_done)
        self.task_error.connect(self._on_task_error)
        self.task_timeout.connect(self._on_task_timeout)

    # ---------- 任务注册 ----------

    def add_task(
        self,
        name,
        *,
        work,
        timeout_ms,
        on_result=None,
        on_error=None,
        on_timeout=None,
        interval_ms=None,
        on_timer=None,
    ):
        """注册一个任务。

        - work(epoch, *args) -> result：在子线程执行，抛异常走 on_error；
        - on_result(result) / on_error(error) / on_timeout()：主线程回调，
          且只回调最新一次触发（epoch 竞态保护）；
        - interval_ms 非 None 时创建周期定时器：到点默认自动重排并触发；
          提供 on_timer 时完全交给回调（回调负责 schedule_next/trigger），
          用于保留调用方"到点→重排→busy 检查→状态提示"的既有语义。
        """
        timer = QTimer(self)
        timer.setSingleShot(True)
        if interval_ms is not None:
            timer.timeout.connect(lambda: self._on_interval(name))
        watchdog = QTimer(self)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: self.task_timeout.emit(name))
        self._tasks[name] = {
            "work": work,
            "timeout_ms": timeout_ms,
            "interval_ms": interval_ms,
            "timer": timer,
            "watchdog": watchdog,
            "on_result": on_result,
            "on_error": on_error,
            "on_timeout": on_timeout,
            "on_timer": on_timer,
        }
        self._epochs[name] = 0

    # ---------- 调度 ----------

    def schedule_next(self, name, interval_ms=None):
        """用任务间隔（或显式传入）重启单次定时器。"""
        if self._stopped:
            return
        spec = self._tasks.get(name)
        if not spec:
            return
        ms = interval_ms if interval_ms is not None else spec["interval_ms"]
        if ms is not None:
            spec["timer"].start(ms)

    def trigger(self, name, *args):
        """立即触发任务（同一任务忙碌时忽略）。返回是否真正启动。"""
        spec = self._tasks.get(name)
        if self._stopped or not spec or name in self._busy:
            return False
        self._epochs[name] += 1
        self._busy.add(name)
        spec["watchdog"].start(spec["timeout_ms"])
        try:
            spec["future"] = self._executor.submit(
                self._run, name, self._epochs[name], args
            )
        except RuntimeError:
            # 线程池已关闭（stop_all 后）：不再启动
            self._busy.discard(name)
            return False
        return True

    def current_epoch(self, name):
        """任务当前 epoch（子线程可安全读取：GIL 下 int 读原子）。"""
        return self._epochs.get(name, 0)

    def _on_interval(self, name):
        """定时器到点：有 on_timer 时交给回调（由回调负责重排/触发/状态）；

        否则默认重排再触发。即使任务忙碌（trigger 返回 False）也要重排，
        保证周期任务不停摆。
        """
        if self._stopped:
            return
        spec = self._tasks.get(name)
        if not spec:
            return
        cb = spec.get("on_timer")
        if cb is not None:
            cb()
            return
        self.schedule_next(name)
        self.trigger(name)

    def is_busy(self, name):
        return name in self._busy

    def stop_all(self):
        """停止所有定时器与看门狗；线程池关闭。

        shutdown(wait=False, cancel_futures=True)：排队未执行的任务取消，
        在途任务继续跑完但 _stopping 标志使其不再 emit（防关闭后信号残留）。
        """
        self._stopped = True
        self._stopping = True
        for spec in self._tasks.values():
            spec["timer"].stop()
            spec["watchdog"].stop()
        self._busy.clear()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # ---------- 内部 ----------

    def _run(self, name, epoch, args):
        if self._stopping:
            return  # 关闭中：在途任务不再 emit
        spec = self._tasks[name]
        try:
            result = spec["work"](epoch, *args)
        except Exception as exc:
            self.task_error.emit(name, epoch, exc)
            return
        self.task_done.emit(name, epoch, result)

    def _on_task_done(self, name, epoch, result):
        if epoch != self._epochs.get(name):
            return  # 过期结果（已有新触发或已超时）
        self._busy.discard(name)
        spec = self._tasks.get(name)
        if not spec:
            return
        spec["watchdog"].stop()
        if spec["on_result"]:
            try:
                spec["on_result"](result)
            except Exception:
                pass

    def _on_task_error(self, name, epoch, error):
        if epoch != self._epochs.get(name):
            return
        self._busy.discard(name)
        spec = self._tasks.get(name)
        if not spec:
            return
        spec["watchdog"].stop()
        if spec["on_error"]:
            try:
                spec["on_error"](error)
            except Exception:
                pass

    def _on_task_timeout(self, name):
        spec = self._tasks.get(name)
        if not spec or name not in self._busy:
            return  # 迟到的超时（任务已结束）
        self._epochs[name] += 1  # 使在途结果过期
        self._busy.discard(name)
        future = spec.get("future")
        if future is not None:
            future.cancel()  # 排队中的任务取消执行（运行中的无法中断，结果会被 epoch 丢弃）
        if spec["on_timeout"]:
            try:
                spec["on_timeout"]()
            except Exception:
                pass
