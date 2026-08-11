"""HeartBeatApp × kernel.runtime 接线集成测试：tick（巡视）与 chat（聊天）全链路。

覆盖：
- tick：触发 → runtime.trigger → 子线程 work → on_result 回主线程 → busy/状态流转；
- chat：_send_chat → runtime.trigger(chat) → _chat_work → 流式 delta（含过期 epoch
  过滤）→ on_result → 状态流转。

需要 offscreen 环境：
    QT_QPA_PLATFORM=offscreen HB_NO_MAC_TRAY=1 python test_app_integration.py
"""

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import core
import kernel
import main as mainmod


def wait(ms):
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)


def _make_app():
    tmp = TemporaryDirectory()
    cfg_path = Path(tmp.name) / "config.json"
    cfg = json.loads(json.dumps(kernel.boot.DEFAULT_CONFIG))
    cfg["api"]["api_key"] = ""  # 规则模式，零 LLM 调用
    cfg["interval_minutes"] = 1
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    hb = mainmod.HeartBeatApp(str(cfg_path))
    # 屏蔽真实采集与思考（不碰网络/LLM）
    core.gather = lambda *a, **k: {"collections": [], "errors": []}
    return tmp, hb


def test_tick_lifecycle():
    """一次完整 tick：触发 → 忙碌 + 状态 → 完成复位 + 结果提示。"""
    tmp, hb = _make_app()
    rt = hb.kernel.runtime
    assert "tick" in rt._tasks and "chat" in rt._tasks, "tick/chat 任务未注册"

    # 慢任务制造 busy 窗口
    hb.agent.think = lambda ctx: time.sleep(0.3) or None
    QTimer.singleShot(50, hb._autonomy_tick)
    wait(150)
    assert rt.is_busy("tick"), "tick 应处于忙碌状态"
    assert hb.pet._status_text == "巡视中…", f"状态应为‘巡视中…’，实际：{hb.pet._status_text}"

    # busy 保护：忙碌中再次触发 epoch 不递增
    started = rt.current_epoch("tick")
    hb._autonomy_tick()
    assert rt.current_epoch("tick") == started, "busy 中重复触发不应递增 epoch"

    wait(800)
    assert not rt.is_busy("tick"), "tick 完成后应复位 busy"
    assert hb.pet._status_text.endswith("巡视完，暂无新事"), f"状态应为完成提示，实际：{hb.pet._status_text}"
    hb.kernel.stop()
    tmp.cleanup()


def test_chat_lifecycle_and_stale_delta():
    """聊天全链路：触发 → 流式 delta（过期 epoch 被过滤）→ 回复 → 状态流转。"""
    tmp, hb = _make_app()
    rt = hb.kernel.runtime

    deltas = []
    hb.bridge.delta.connect(lambda epoch, text: deltas.append((epoch, text)))

    def fake_chat(user_text, on_delta=None):
        # 模拟流式：正确 epoch 的增量 + 过期线程的增量
        if on_delta:
            on_delta("你好")
            hb._stream_delta(999, "过期增量")  # 模拟旧线程（epoch 不匹配）
        return "你好呀，我在！"

    hb.agent.chat = fake_chat
    hb._send_chat("你好")
    wait(300)

    # 流式增量：只保留最新 epoch
    assert any(e == 1 for e, _ in deltas), f"正确 epoch 的 delta 缺失：{deltas}"
    assert all(e != 999 for e, _ in deltas), f"过期 epoch 的 delta 泄漏：{deltas}"
    # 完成：busy 复位 + 状态流转
    assert not rt.is_busy("chat"), "chat 完成后应复位 busy"
    assert hb.pet._status_text == "陪我聊天中", f"状态应为‘陪我聊天中’，实际：{hb.pet._status_text}"
    hb.kernel.stop()
    tmp.cleanup()


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
