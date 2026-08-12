"""test_monitor：运行期健康监控测试（kernel.monitor）。

覆盖：阈值 JSON 加载（缺失/损坏/非法值回退默认）、tick 连续失败触发
回滚、chat 窗口失败触发回滚、回滚上限防循环、成功心跳清零、启动自测。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.monitor import DEFAULT_THRESHOLDS, Monitor


class FakeUpdater:
    """假 updater：记录 rollback 调用，返回固定版本。"""

    def __init__(self, builtin=("memory", "planner", "brain")):
        self.BUILTIN_MODULES = builtin
        self.rollback_calls = []
        self.next_rollback = "v1.0"

    def rollback(self, name):
        self.rollback_calls.append(name)
        return self.next_rollback


def _monitor(tmp, **kw):
    m = Monitor(tmp, updater=kw.get("updater", FakeUpdater()))
    m.thresholds["tick_fail_limit"] = kw.get("tick_limit", 3)
    m.thresholds["chat_fail_limit"] = kw.get("chat_limit", 5)
    return m


def test_thresholds_default_when_missing(tmp_path):
    m = Monitor(tmp_path)
    assert m.thresholds == DEFAULT_THRESHOLDS


def test_thresholds_load_from_json(tmp_path):
    (tmp_path / "monitor.json").write_text(
        '{"tick_fail_limit": 7, "chat_fail_limit": 9}', encoding="utf-8"
    )
    m = Monitor(tmp_path)
    assert m.thresholds["tick_fail_limit"] == 7
    assert m.thresholds["chat_fail_limit"] == 9
    assert m.thresholds["chat_window_seconds"] == DEFAULT_THRESHOLDS["chat_window_seconds"]


def test_thresholds_corrupt_json_falls_back(tmp_path):
    (tmp_path / "monitor.json").write_text("{bad json", encoding="utf-8")
    m = Monitor(tmp_path)
    assert m.thresholds == DEFAULT_THRESHOLDS


def test_thresholds_invalid_values_falls_back(tmp_path):
    (tmp_path / "monitor.json").write_text(
        '{"tick_fail_limit": -3, "chat_fail_limit": "x"}', encoding="utf-8"
    )
    m = Monitor(tmp_path)
    assert m.thresholds == DEFAULT_THRESHOLDS


def test_tick_success_resets_streak(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd)
    m.record_tick(False)
    m.record_tick(False)
    m.record_tick(True)  # 成功清零
    m.record_tick(False)
    m.record_tick(False)
    assert upd.rollback_calls == [], "成功心跳后不应触发回滚"


def test_tick_fail_chain_triggers_rollback(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, tick_limit=3)
    for _ in range(3):
        m.record_tick(False)
    assert upd.rollback_calls == ["brain"], f"应回滚 brain：{upd.rollback_calls}"
    assert "回滚" in m._last_action


def test_chat_fail_window_triggers_rollback(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=2)
    m.record_chat(False)
    m.record_chat(False)
    assert upd.rollback_calls == ["brain"], f"应回滚 brain：{upd.rollback_calls}"


def test_chat_window_expiry_resets(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=3)
    m.record_chat(False)
    m.record_chat(False)
    m.thresholds["chat_window_seconds"] = 0.001  # 窗口过期
    m._chat_window_start = 0
    m.record_chat(False)
    assert upd.rollback_calls == [], "窗口过期后失败计数应清零"


def test_max_auto_rollbacks_prevents_loop(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, tick_limit=2)
    m.thresholds["max_auto_rollbacks"] = 1
    m.record_tick(False)
    m.record_tick(False)
    assert upd.rollback_calls == ["brain"]
    m.record_tick(False)
    m.record_tick(False)
    assert upd.rollback_calls == ["brain"], "已达回滚上限，不应再次回滚"
    assert "上限" in m._last_action


def test_no_updater_only_records(tmp_path):
    m = _monitor(tmp_path, updater=None, tick_limit=2)
    m.record_tick(False)
    m.record_tick(False)
    assert "仅记录" in m._last_action


def test_audit_written_on_rollback(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, tick_limit=2)
    m.record_tick(False)
    m.record_tick(False)
    log = (tmp_path / "brain" / "updates.log").read_text(encoding="utf-8")
    assert '"action": "monitor_rollback"' in log
    assert '"version": "v1.0"' in log


def test_self_test_fake_injection(tmp_path):
    upd = FakeUpdater()
    m = Monitor(tmp_path, updater=upd)
    ok, desc = m.self_test(fake_updater=upd)
    assert ok, desc
    assert "回滚" in desc


def test_self_test_detects_broken_updater(tmp_path):
    class BrokenUpdater:
        BUILTIN_MODULES = ("brain",)

        def rollback(self, name):
            raise RuntimeError("updater 坏了")

    m = Monitor(tmp_path, updater=BrokenUpdater())
    ok, desc = m.self_test(fake_updater=BrokenUpdater())
    assert not ok, "自测应检测到回滚链路故障"


# ---------- P0：错误分类（基础设施故障不触发回滚，留痕审计） ----------


class _FakeHttpxError(Exception):
    """名字含 httpx 的异常（模拟 provider 网络故障）。"""


def test_chat_infra_failures_do_not_trigger_rollback(tmp_path):
    """网络/provider 等基础设施故障只审计，不累计回滚。"""
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=3)
    for _ in range(6):
        m.record_chat(False, failure="infra")
    assert upd.rollback_calls == [], "infra 失败不应触发回滚"
    log = (tmp_path / "brain" / "updates.log").read_text(encoding="utf-8")
    assert '"action": "monitor_failure"' in log
    assert "failure_type=infra" in log


def test_tick_infra_failures_do_not_trigger_rollback(tmp_path):
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, tick_limit=3)
    for _ in range(6):
        m.record_tick(False, failure="infra")
    assert upd.rollback_calls == [], "tick infra 失败不应触发回滚"


def test_chat_brain_failures_trigger_rollback(tmp_path):
    """brain 故障（显式分类）仍累计触发回滚，并审计异常详情。"""
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=3)
    for _ in range(3):
        m.record_chat(False, failure="brain", exc=ValueError("契约损坏"))
    assert upd.rollback_calls == ["brain"]
    log = (tmp_path / "brain" / "updates.log").read_text(encoding="utf-8")
    assert "failure_type=brain" in log
    assert "契约损坏" in log


def test_timeout_failures_audited_not_counted(tmp_path):
    """超时归基础设施故障：留痕不累计（不回滚）。"""
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=3)
    for _ in range(6):
        m.record_chat(False, failure="timeout")
    assert upd.rollback_calls == []
    log = (tmp_path / "brain" / "updates.log").read_text(encoding="utf-8")
    assert "failure_type=timeout" in log


def test_infra_failures_do_not_dilute_brain_count(tmp_path):
    """infra 失败不稀释 brain 计数：5 次 infra + 3 次 brain 应触发回滚。"""
    upd = FakeUpdater()
    m = _monitor(tmp_path, updater=upd, chat_limit=3)
    for _ in range(5):
        m.record_chat(False, failure="infra")
    for _ in range(3):
        m.record_chat(False, failure="brain")
    assert upd.rollback_calls == ["brain"]


def test_classify_failure_domains(tmp_path):
    """分类：网络/provider → infra；确定性异常 → brain；无对象 → timeout。"""
    m = Monitor(tmp_path)
    assert m.classify_failure(TimeoutError("connect")) == "infra"
    assert m.classify_failure(_FakeHttpxError("429")) == "infra"
    assert m.classify_failure(ValueError("brain bug")) == "brain"
    assert m.classify_failure(None) == "timeout"


def test_classify_unclassified_audited_once(tmp_path):
    """未匹配异常类型记审计（按类型去重，不刷屏），默认按 brain 处理。"""
    m = Monitor(tmp_path)
    assert m.classify_failure(ValueError("x")) == "brain"
    assert m.classify_failure(ValueError("y")) == "brain"  # 同类型不重复审计
    log = (tmp_path / "brain" / "updates.log").read_text(encoding="utf-8")
    assert log.count("monitor_unclassified") == 1


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = Path(tempfile.mkdtemp(prefix="monitor_test_"))
            try:
                fn(tmp)
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                import traceback

                traceback.print_exc()
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
