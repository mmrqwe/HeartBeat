"""test_evolve：自我进化流水线测试（brain.evolver + Agent 意图识别）。

覆盖：成功全链路（生成→安全→验证→安装→active 切换→新方法生效）、
import 白名单拒绝、失败重试带反馈、不可进化模块拒绝、意图解析与确认流程。
LLM 全部 mock（FakeBrain），不碰真实 API。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent as agent_mod
import core
import db as dbmod

ROOT = Path(__file__).parent.parent

PLANNER_SRC = (ROOT / "brain" / "planner.py").read_text(encoding="utf-8")
# 模拟 LLM 的"进化结果"：插入一个新方法（验证新版本真的带上了新功能）
_NEW_METHOD = '\n    def drink_reminder(self):\n        return "该喝水啦"\n'
PLANNER_EVOLVED = PLANNER_SRC.replace(
    "    def maybe_save_thought(self, ctx):",
    _NEW_METHOD + "    def maybe_save_thought(self, ctx):",
)
assert "drink_reminder" in PLANNER_EVOLVED
# 语法坏代码（def 缺参数括号 → SyntaxError）
BAD_SRC = PLANNER_SRC.replace(
    "    def greeting(self, now):",
    "    def broken(:\n        pass\n\n    def greeting(self, now):",
)


class FakeBrain:
    """mock core.Brain：按序返回预设响应，记录收到的 prompt。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, messages, max_tokens=None):
        self.prompts.append(messages)
        if not self.responses:
            raise AssertionError("FakeBrain 响应已耗尽")
        return self.responses.pop(0)


def _make_updater(tmp_path):
    from brain.smoke import smoke_test_module
    from kernel.updater import Updater

    upd = Updater(str(tmp_path))
    upd.smoke_runner = smoke_test_module
    upd.ensure_installed()
    return upd


def _make_agent(tmp_path, with_updater=False):
    cfg = core.load_config(tmp_path / "config.json")
    cfg["api"]["api_key"] = ""
    cfg["embedding_enabled"] = False
    cfg["tools_enabled"] = False
    database = dbmod.Database(tmp_path / "heartbeat.db")
    ag = agent_mod.Agent(
        cfg, {}, tmp_path, stats=core.Stats(database), db=database,
        brain_loader=_make_updater(tmp_path) if with_updater else None,
    )
    return ag


def _make_evolver(tmp_path, fake_brain):
    from brain.evolver import Evolver

    return Evolver(fake_brain, _make_updater(tmp_path))


def test_evolve_pipeline_success(tmp_path):
    """全链路：生成候选 → 安全/契约/冒烟验证 → v1.1 安装 → 新方法生效。"""
    ev = _make_evolver(tmp_path, FakeBrain([PLANNER_EVOLVED]))
    version = ev.evolve("planner", "每天上午9点提醒我喝水")
    assert version == "v1.1"
    # active 已切换且可加载，新方法真实存在
    module = ev.updater.load("planner")
    assert hasattr(getattr(module, "Planner"), "drink_reminder")
    # 候选目录已清理
    assert not list(ev.candidate_root.glob("*"))
    # 审计有 install 记录
    log = (ev.updater.root / "updates.log").read_text(encoding="utf-8")
    assert '"action": "install"' in log and '"version": "v1.1"' in log


def test_evolve_rejects_dangerous_import(tmp_path):
    """候选代码含禁止 import（os）→ 重试耗尽后失败，版本未安装。"""
    bad = "import os\n" + PLANNER_SRC
    ev = _make_evolver(tmp_path, FakeBrain([bad, bad, bad]))
    try:
        ev.evolve("planner", "任意需求")
    except ValueError as exc:
        assert "禁止 import：os" in str(exc)
    else:
        raise AssertionError("应当拒绝含 os import 的候选")
    assert ev.updater.list_versions("planner") == ["v1.0"]
    assert ev.updater.active_version("planner") == "v1.0"


def test_evolve_retry_with_feedback(tmp_path):
    """首次生成语法错误 → 带反馈重试 → 第二次成功。"""
    fake = FakeBrain([BAD_SRC, PLANNER_EVOLVED])
    ev = _make_evolver(tmp_path, fake)
    version = ev.evolve("planner", "每天上午9点提醒我喝水")
    assert version == "v1.1"
    # 第二次请求带上了上次失败反馈
    assert "验证失败" in fake.prompts[1][-1]["content"]


def test_evolve_rejects_non_evolvable_module(tmp_path):
    """agent 模块不可自进化（核心控制流锁定）。"""
    fake = FakeBrain([])
    ev = _make_evolver(tmp_path, fake)
    try:
        ev.evolve("agent", "任意需求")
    except ValueError as exc:
        assert "不可进化" in str(exc)
    else:
        raise AssertionError("应当拒绝 agent 模块")
    assert fake.responses == []  # 未发起任何生成


def test_evolve_requirement_too_short(tmp_path):
    ev = _make_evolver(tmp_path, FakeBrain([]))
    try:
        ev.evolve("planner", "喝水")
    except ValueError as exc:
        assert "需求描述太短" in str(exc)
    else:
        raise AssertionError("应当拒绝过短需求")


def test_evolve_intent_parse(tmp_path):
    """Agent 意图识别：关键词/模块解析/确认回调/取消/引导。"""
    ag = _make_agent(tmp_path, with_updater=True)
    # 非进化文本不拦截
    assert ag._try_evolve_intent("今天天气怎么样") is None
    # 无确认回调（CLI）→ 走到 evolve（mock 掉，避免真实 LLM）
    ag.evolver.evolve = lambda name, req, on_status=None: "v9.9"
    reply = ag._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "进化成功" in reply and "v9.9" in reply and "planner" in reply
    # memory 关键词解析
    seen = {}
    ag.evolver.evolve = lambda name, req, on_status=None: seen.update(name=name, req=req) or "v1.1"
    ag._try_evolve_intent("升级记忆：多记住一些日程")
    assert seen["name"] == "memory" and "多记住一些日程" in seen["req"]
    # 用户拒绝确认 → 取消
    ag.tool_confirm_cb = lambda desc: False
    reply = ag._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "已取消" in reply
    # 需求太短 → 引导
    reply = ag._try_evolve_intent("进化 planner")
    assert "请说具体一点" in reply
    # 无 brain_loader（测试直连）→ 引擎不可用
    ag2 = _make_agent(tmp_path, with_updater=False)
    reply = ag2._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "进化引擎不可用" in reply


if __name__ == "__main__":
    import tempfile
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = tempfile.mkdtemp(prefix="evolve_test_")
            try:
                fn(Path(tmp))
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
