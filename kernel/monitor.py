"""kernel.monitor：运行期健康监控（自进化安全网，阶段4 + P0 错误分类）。

职责：
- 三层指标：tick 心跳（连续失败/超时）、chat 异常（窗口内累计）、超时；
- **错误分类（P0，2026-08-12）**：基础设施故障（网络/provider/超时/认证）与
  brain 故障（确定性异常/契约失败）分开——只有 brain 故障累计触发回滚，
  infra/timeout 只审计留痕，避免“LLM 服务挂了 → 误判 brain 坏了 → 回滚”；
- 阈值外部 JSON 配置（<data>/monitor.json），文件损坏回退默认值；
- 连续异常自动触发 updater.rollback（brain 包回退到上一个可用版本），
  一次运行最多回滚一次（防循环）；
- 审计：回滚/降级/infra 失败/未分类异常写 <data>/brain/updates.log
  （action=monitor_rollback / monitor_failure / monitor_unclassified）；
- 启动自测：fake 注入验证回滚链路（见 tests.test_monitor）。

依赖方向：只依赖 stdlib；updater 由宿主注入（kernel↔宿主组合）。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_THRESHOLDS = {
    # 连续 tick 异常（异常或超时）达到该值 → 判定 tick 失速
    "tick_fail_limit": 3,
    # 60 秒窗口内 chat 异常次数达到该值 → 判定聊天链路失速
    "chat_fail_limit": 5,
    "chat_window_seconds": 60,
    # 回滚后冷却：同一次运行只自动回滚一次（防循环回滚）
    "max_auto_rollbacks": 1,
}

# 基础设施故障特征（异常类型全名 module.Name 转小写后的子串匹配）。
# denylist 方案：匹配 → infra（不累计回滚）；未匹配 → brain（累计）。
# 新异常类型未匹配时记 monitor_unclassified 审计（去重），便于扩充此表。
_INFRA_MARKERS = (
    "httpx", "urllib", "requests", "socket", "ssl", "dns",
    "timeout", "connection", "eof", "responseread", "readerror",
    "streaminterrupted", "apiconnection", "apirequest", "authentication",
    "ratelimit", "badrequest", "protocollerror", "remoteprotocol",
    "openai", "anthropic", "provider", "unexpectedeof",
)


class Monitor:
    """运行期健康监控：记录 tick/chat 指标（分错误域），超阈值自动回滚进化模块。"""

    def __init__(self, data_dir, updater: Any = None):
        self.data_dir = Path(data_dir)
        self.updater = updater  # kernel.updater.Updater（宿主注入），None=仅记录
        self.thresholds = self._load_thresholds(data_dir)
        self._tick_fail_streak = 0
        self._chat_fail_count = 0
        self._chat_window_start = time.time()
        self._rollbacks_done = 0
        self._last_action = ""  # 最近一次动作（audit 与测试断言用）
        # 未分类异常类型名（去重，防刷屏；cap 防泄漏）
        self._unclassified_logged = set()
        self._unclassified_cap = 100

    # ---------- 阈值配置（外部 JSON，损坏回退默认） ----------

    def _load_thresholds(self, data_dir):
        """读取 <data>/monitor.json 阈值；缺失/损坏/非法值全部回退默认。"""
        thresholds = dict(DEFAULT_THRESHOLDS)
        path = Path(data_dir) / "monitor.json"
        if not path.is_file():
            return thresholds
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return thresholds
            for key, default in DEFAULT_THRESHOLDS.items():
                value = raw.get(key, default)
                if isinstance(value, (int, float)) and value > 0:
                    thresholds[key] = value
        except (OSError, ValueError):
            pass  # 损坏回退默认
        return thresholds

    # ---------- 错误分类（P0） ----------

    def classify_failure(self, exc) -> str:
        """错误分类：infra（网络/provider/超时/认证）| brain（其余）| timeout（无对象）。

        启发式 denylist：未匹配的新异常类型默认 brain（宁可误回滚也不漏回滚），
        并记 monitor_unclassified 审计（按类型名去重），便于扩充特征表。
        """
        if exc is None:
            return "timeout"
        key = f"{type(exc).__module__}.{type(exc).__name__}"
        low = key.lower()
        if any(marker in low for marker in _INFRA_MARKERS):
            return "infra"
        self._audit_unclassified(key)
        return "brain"

    def _audit_unclassified(self, exc_key):
        """未匹配异常类型记审计（去重，不刷屏）。"""
        if exc_key in self._unclassified_logged:
            return
        if len(self._unclassified_logged) >= self._unclassified_cap:
            return
        self._unclassified_logged.add(exc_key)
        self._audit("monitor_unclassified", module="classify",
                    detail=f"未分类异常类型：{exc_key}（默认按 brain 处理）")

    # ---------- 指标记录 ----------

    def record_tick(self, ok: bool, failure: Optional[str] = None,
                    exc: Any = None, elapsed: float = 0.0):
        """tick 心跳：ok=False（异常或超时）累加连续失败，成功清零。

        failure 取值：None（旧语义，计入失败）/ "brain"（计入失败并审计）/
        "infra"（网络/provider 等，仅审计不累计）/ "timeout"（仅审计不累计）。
        """
        if ok:
            self._tick_fail_streak = 0
            return None
        if failure in (None, "brain"):
            if failure == "brain":
                self._audit_failure("tick", "brain", exc)
            self._tick_fail_streak += 1
            return self._maybe_recover("tick")
        # infra / timeout：不累计回滚，但留痕（诊断“为什么没回滚”）
        self._audit_failure("tick", failure, exc)
        return None

    def record_chat(self, ok: bool, failure: Optional[str] = None,
                    exc: Any = None, elapsed: float = 0.0):
        """chat 心跳：滑动窗口内累计失败（窗口过期清零）。分类语义同 record_tick。"""
        now = time.time()
        window = float(self.thresholds["chat_window_seconds"])
        if now - self._chat_window_start > window:
            self._chat_fail_count = 0
            self._chat_window_start = now
        if ok:
            return None
        if failure in (None, "brain"):
            if failure == "brain":
                self._audit_failure("chat", "brain", exc)
            self._chat_fail_count += 1
            return self._maybe_recover("chat")
        # infra / timeout：不累计回滚，但留痕
        self._audit_failure("chat", failure, exc)
        return None

    # ---------- 自动回滚 ----------

    def _maybe_recover(self, source):
        """超阈值检查：tick 连续失败 / chat 窗口内失败 → 自动回滚 brain 包。"""
        limit = int(self.thresholds["tick_fail_limit" if source == "tick" else "chat_fail_limit"])
        count = self._tick_fail_streak if source == "tick" else self._chat_fail_count
        if count < limit:
            return None
        if self.updater is None:
            self._last_action = f"{source}:fail>{limit}（无 updater，仅记录）"
            return self._last_action
        if self._rollbacks_done >= int(self.thresholds["max_auto_rollbacks"]):
            self._last_action = f"{source}:fail>{limit}（已达回滚上限，停止自动回滚）"
            return self._last_action
        # 优先回滚 brain 包（控制流进化域）；无包或回退不到更旧版本则仅记录
        rolled = None
        try:
            if "brain" in self.updater.BUILTIN_MODULES:
                rolled = self.updater.rollback("brain")
            if rolled is None:
                for name in self.updater.BUILTIN_MODULES:
                    if name == "brain":
                        continue
                    rolled = self.updater.rollback(name)
                    if rolled is not None:
                        break
        except Exception as exc:
            self._last_action = f"{source}:fail>{limit}（回滚异常：{exc}）"
            self._audit("monitor_rollback_error", detail=str(exc))
            return self._last_action
        self._rollbacks_done += 1
        if rolled is not None:
            self._last_action = f"{source}:fail>{limit}→自动回滚 brain 至 {rolled}"
            self._audit("monitor_rollback", module="brain", version=rolled,
                        detail=f"{source} 连续失败 {count} 次")
        else:
            self._last_action = f"{source}:fail>{limit}（无更旧版本可回退，仅记录）"
            self._audit("monitor_no_rollback", module="brain", detail=self._last_action)
        # 回滚后清零失败计数（等待恢复观察）
        self._tick_fail_streak = 0
        self._chat_fail_count = 0
        return self._last_action

    # ---------- 审计 ----------

    def _audit(self, action, module="brain", version="", detail=""):
        try:
            log_path = self.data_dir / "brain" / "updates.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = json.dumps(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "action": action,
                    "module": module,
                    "version": version,
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(record + "\n")
        except OSError:
            pass  # 审计失败不阻断

    def _audit_failure(self, channel, failure_type, exc=None):
        """infra/timeout/brain 失败留痕：诊断“为什么没回滚/为什么回滚了”。

        detail 用纯文本（不嵌套 JSON，避免双重转义影响可读性与检索）。
        """
        parts = [f"channel={channel}", f"failure_type={failure_type}"]
        if exc is not None:
            parts.append(f"exc_type={type(exc).__module__}.{type(exc).__name__}")
            parts.append(f"exc_msg={str(exc)[:200]}")
        self._audit("monitor_failure", module=channel, detail=" ".join(parts))

    # ---------- 启动自测 ----------

    def self_test(self, fake_updater=None):
        """启动自测：假注入 updater 验证回滚链路（不碰真实版本）。

        返回 (ok, 描述)。fake_updater 需实现 rollback(name) -> version|None
        与 BUILTIN_MODULES；不传则跳过（仅当真实 updater 存在时做无害检查）。
        """
        if fake_updater is None:
            return True, "无注入（跳过）"
        probe = Monitor(self.data_dir, updater=fake_updater)
        probe.thresholds["tick_fail_limit"] = 2
        probe.thresholds["max_auto_rollbacks"] = 5
        action = None
        for _ in range(2):
            action = probe.record_tick(False)
        if action is None or "→自动回滚" not in action:
            return False, f"自测失败：tick 连续失败未触发回滚（{action}）"
        return True, action
