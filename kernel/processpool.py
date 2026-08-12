"""kernel.processpool：受控后台进程池（Coding Agent 长任务执行基座，锁定层）。

为什么放 kernel 且不可进化：并发上限 / 超时强杀 / 输出缓冲上限是
资源安全基线——LLM 进化产物不得放宽（否则一个失控循环就能打满 CPU）。

职责：
- 并发上限（默认 3）：超过拒绝启动；
- 每进程超时（默认 300s，上限 1800s）：超时 SIGTERM（进程组）→ 3s 宽限 → SIGKILL；
- 增量输出捕获（drain 线程 + 字符环形上限，防止无界日志撑爆内存）；
- 环境变量过滤（复用 kernel.permission._filter_env，凭据不下沉子进程）；
- Windows 走 PowerShell，POSIX 走 bash -c（与 kernel.permission.run_bash 一致）。

依赖方向：只依赖 kernel.permission 的执行原语，不 import brain/tools/ui。
"""

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque

from kernel.permission import _filter_env

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_TIMEOUT = "timeout"
STATUS_KILLED = "killed"
STATUS_ERROR = "error"

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
MAX_CONCURRENCY = 3
MAX_OUTPUT_CHARS = 65536
KILL_GRACE_SECONDS = 3
MAX_TRACKED = 64  # 池内最多保留的任务记录（含已结束）


class PoolError(Exception):
    """启动被拒（并发满/参数非法），消息可直接回给 LLM。"""


class _BgProc:
    def __init__(self, pid, proc, command, cwd, timeout):
        self.pid = pid
        self.proc = proc
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.started = time.time()
        self._lines = deque()
        self._chars = 0
        self._lock = threading.Lock()
        self._terminated_at = None  # 超时 SIGTERM 时间
        self._canceled = False  # cancel() 主动取消
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        try:
            for raw in self.proc.stdout:
                with self._lock:
                    line = raw.decode("utf-8", errors="replace")
                    self._lines.append(line)
                    self._chars += len(line)
                    while self._chars > MAX_OUTPUT_CHARS and self._lines:
                        self._chars -= len(self._lines.popleft())
        except (OSError, ValueError):
            pass

    def _signal(self, sig):
        if os.name == "nt":
            try:
                self.proc.kill()
            except OSError:
                pass
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except OSError:
                pass

    def terminate(self):
        self._signal(signal.SIGTERM)
        self._terminated_at = time.time()

    def cancel(self):
        self._canceled = True
        self.terminate()

    def poll(self):
        exit_code = self.proc.poll()
        now = time.time()
        status = STATUS_RUNNING
        if exit_code is None and now - self.started >= self.timeout:
            if self._terminated_at is None:
                self.terminate()
            terminated_at = self._terminated_at
            if terminated_at is not None and now - terminated_at >= KILL_GRACE_SECONDS:
                self._signal(signal.SIGKILL)
        if exit_code is None:
            try:
                exit_code = self.proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                exit_code = None
        if exit_code is not None:
            if self._terminated_at is not None:
                status = STATUS_KILLED if self._canceled else STATUS_TIMEOUT
            else:
                status = STATUS_DONE
        with self._lock:
            tail = "".join(self._lines)
        return {
            "status": status,
            "exit_code": exit_code,
            "elapsed": round(now - self.started, 1),
            "output_tail": tail[-2000:],
        }


class BgPool:
    """受控后台进程池：start / poll / cancel / cancel_all。"""

    def __init__(self, max_concurrency=MAX_CONCURRENCY,
                 default_timeout=DEFAULT_TIMEOUT, max_timeout=MAX_TIMEOUT):
        self.max_concurrency = int(max_concurrency)
        self.default_timeout = int(default_timeout)
        self.max_timeout = int(max_timeout)
        self._procs = {}
        # RLock：start() 持锁期间会调用 running_count()（内部再次加锁）
        self._lock = threading.RLock()

    def running_count(self):
        with self._lock:
            return sum(1 for p in self._procs.values()
                       if p.proc.poll() is None)

    def start(self, command, cwd, timeout=None):
        """启动后台进程，返回任务 ID。并发满/非法参数抛 PoolError。"""
        timeout = int(timeout or self.default_timeout)
        timeout = max(1, min(timeout, self.max_timeout))
        with self._lock:
            if self.running_count() >= self.max_concurrency:
                raise PoolError(
                    f"后台并发进程已达上限（{self.max_concurrency}），"
                    "请等现有任务结束或先 bg_cancel"
                )
            if len(self._procs) >= MAX_TRACKED:
                self._drop_oldest_finished()
            env = _filter_env(os.environ.copy())
            if os.name == "nt":
                argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                argv = ["bash", "-c", command]
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                raise PoolError(f"进程启动失败：{exc}") from exc
            pid = uuid.uuid4().hex[:8]
            self._procs[pid] = _BgProc(pid, proc, command, str(cwd), timeout)
            return pid

    def _drop_oldest_finished(self):
        finished = sorted(
            (p for p in self._procs.values() if p.proc.poll() is not None),
            key=lambda p: p.started,
        )
        for proc in finished[: len(self._procs) - MAX_TRACKED + 1]:
            self._procs.pop(proc.pid, None)

    def poll(self, pid):
        """轮询任务：返回状态 dict；任务不存在返回 None。"""
        with self._lock:
            proc = self._procs.get(pid)
        if proc is None:
            return None
        try:
            return proc.poll()
        except OSError:
            return {"status": STATUS_ERROR, "exit_code": None,
                    "elapsed": 0.0, "output_tail": "（进程状态读取失败）"}

    def cancel(self, pid):
        """主动取消：SIGTERM 进程组，宽限后 SIGKILL（由 poll 触发强杀）。"""
        with self._lock:
            proc = self._procs.get(pid)
        if proc is None:
            return f"任务不存在：{pid}"
        proc.cancel()
        return f"已请求取消任务 {pid}"

    def cancel_all(self):
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            proc.cancel()
        return len(procs)
