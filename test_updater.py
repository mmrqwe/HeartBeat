"""test_updater.py：自进化（kernel.updater）全链路测试。

覆盖：首启安装 / 幂等 / 加载 / L0L1 验证拒绝与通过 / 安装版本递增 /
回滚 / 切换 / 启动级回滚 / 端到端自进化闭环（增强版 memory 安装后
Agent 实测新行为生效，回滚后行为恢复）。

跑法：python test_updater.py（无 GUI / 无网络 / 无模型下载依赖）。
"""

import json
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import agent as agent_mod
import core

from brain.smoke import smoke_test_module
from kernel.updater import Updater

ROOT = Path(__file__).parent


def _make_updater(data_dir):
    upd = Updater(data_dir)
    upd.smoke_runner = smoke_test_module
    upd.ensure_installed()
    return upd


def _cfg():
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["api"]["api_key"] = ""
    cfg["embedding_enabled"] = False
    cfg["tools_enabled"] = False
    return cfg


def _make_agent(tmp_dir, brain_loader=None):
    return agent_mod.Agent(
        _cfg(),
        data_dir=tmp_dir,
        clock=lambda: datetime(2026, 8, 10, 10, 0),
        brain_loader=brain_loader,
    )


def _evolved_memory_source():
    """基于内置 memory.py 生成增强版：extract_facts 规则表首行注入‘进化验证’规则。"""
    src = (ROOT / "brain" / "memory.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if '"identity"' in line and "我叫" in line:
            lines.insert(i, '            (r"(进化验证)", "检测到{}", "evolve-test", 5),')
            break
    else:
        raise AssertionError("内置 memory.py 未找到注入锚点")
    return "\n".join(lines) + "\n"


def _write_candidate(tmp_dir, name, source):
    cand = Path(tmp_dir) / f"cand_{name}"
    cand.mkdir(parents=True, exist_ok=True)
    (cand / f"{name}.py").write_text(source, encoding="utf-8")
    return cand


def _has_evolved_fact(ag):
    return any(i.get("category") == "evolve-test" for i in ag.memory.facts())


# ---------- 首启安装 / 幂等 / 加载 ----------


def test_ensure_installed_first_run(tmp_path):
    upd = Updater(tmp_path)
    upd.ensure_installed()
    for name in ("memory", "planner"):
        assert upd.active_version(name) == "v1.0"
        assert (tmp_path / "brain" / name / "v1.0" / f"{name}.py").is_file()
    mod = upd.load("memory")
    assert callable(getattr(mod, "MemoryModule", None))


def test_ensure_installed_idempotent(tmp_path):
    upd = Updater(tmp_path)
    upd.ensure_installed()
    first = sorted(p.name for p in (tmp_path / "brain" / "memory").glob("v*"))
    upd.ensure_installed()
    second = sorted(p.name for p in (tmp_path / "brain" / "memory").glob("v*"))
    assert first == second == ["v1.0"]


def test_create_instantiates_contract(tmp_path):
    upd = _make_updater(tmp_path)
    ag = _make_agent(tmp_path)
    mm = upd.create("memory", ag)
    assert type(mm).__name__ == "MemoryModule"
    pl = upd.create("planner", ag)
    assert type(pl).__name__ == "Planner"


# ---------- 候选验证 ----------


def test_validate_rejects_bad_syntax(tmp_path):
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", "def broken(:\n")
    ok, errors = upd.validate_candidate("memory", cand)
    assert not ok
    assert any("L0" in e for e in errors)


def test_validate_rejects_missing_method(tmp_path):
    upd = _make_updater(tmp_path)
    src = (ROOT / "brain" / "memory.py").read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if "def profile" not in ln]
    cand = _write_candidate(tmp_path, "memory", "\n".join(lines) + "\n")
    ok, errors = upd.validate_candidate("memory", cand)
    assert not ok
    assert any("L1" in e and "profile" in e for e in errors)


def test_validate_accepts_builtin_copy(tmp_path):
    upd = _make_updater(tmp_path)
    src = (ROOT / "brain" / "memory.py").read_text(encoding="utf-8")
    cand = _write_candidate(tmp_path, "memory", src)
    ok, errors = upd.validate_candidate("memory", cand)
    assert ok, errors


# ---------- 安装 / 切换 / 回滚 ----------


def test_install_new_version_and_active(tmp_path):
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    version = upd.install_candidate("memory", cand)
    assert version == "v1.1"
    assert upd.active_version("memory") == "v1.1"
    mod = upd.load("memory")
    assert "进化验证" in Path(mod.__file__).read_text(encoding="utf-8")


def test_install_rejects_invalid_no_change(tmp_path):
    upd = _make_updater(tmp_path)
    before = upd.list_versions("memory")
    bad = _write_candidate(tmp_path, "memory", "def broken(:\n")
    try:
        upd.install_candidate("memory", bad)
        raise AssertionError("应当拒绝坏候选")
    except ValueError:
        pass
    assert upd.list_versions("memory") == before
    assert upd.active_version("memory") == "v1.0"


def test_rollback(tmp_path):
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)
    assert upd.active_version("memory") == "v1.1"
    assert upd.rollback("memory") == "v1.0"
    assert upd.active_version("memory") == "v1.0"


def test_switch_and_missing(tmp_path):
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)
    upd.switch("memory", "v1.0")
    assert upd.active_version("memory") == "v1.0"
    upd.switch("memory", "v1.1")
    assert upd.active_version("memory") == "v1.1"
    try:
        upd.switch("memory", "v9.9")
        raise AssertionError("应当拒绝不存在的版本")
    except ValueError:
        pass


# ---------- 启动级回滚 ----------


def test_switch_emits_event(tmp_path):
    """热切换接线：install/switch/rollback 后广播 brain.switched 事件。"""
    from kernel.eventbus import EventBus

    upd = _make_updater(tmp_path)
    bus = EventBus()
    upd.eventbus = bus
    received = []
    bus.subscribe("brain.switched", lambda payload: received.append(payload))

    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)
    assert received == [("memory", "v1.1")], received

    upd.switch("memory", "v1.0")
    assert received[-1] == ("memory", "v1.0")

    upd.rollback("memory")  # 已是 v1.0，无更旧版本 → 不切换不广播
    assert len(received) == 2, received


def test_audit_log_written(tmp_path):
    """升级审计：install/switch/rollback 写入 updates.log（可追溯）。"""
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)
    upd.rollback("memory")

    log_path = tmp_path / "brain" / "updates.log"
    assert log_path.is_file()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    actions = [json.loads(ln) for ln in lines]
    assert [a["action"] for a in actions] == ["install", "rollback"]
    assert actions[0]["module"] == "memory"
    assert actions[0]["version"] == "v1.1"
    assert actions[0]["detail"]  # 记录候选来源路径
    assert all(a.get("ts") for a in actions)


def test_startup_rollback_corrupt_active(tmp_path):
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)
    # 模拟 v1.1 在下次启动前损坏（同步失败/写入中断）
    (tmp_path / "brain" / "memory" / "v1.1" / "memory.py").write_text(
        "def broken(:\n", encoding="utf-8"
    )
    upd.ensure_installed()  # 启动预检 → 自动回滚 v1.0
    assert upd.active_version("memory") == "v1.0"
    upd.load("memory")  # 不抛错


def test_startup_rebuild_when_no_older(tmp_path):
    """兜底重建：active=v1.0 损坏且无更旧版本 → ensure_installed 重建 v1.0。"""
    upd = _make_updater(tmp_path)
    # 只有 v1.0，把它写坏（模拟半写/同步中断）
    (tmp_path / "brain" / "memory" / "v1.0" / "memory.py").write_text(
        "def broken(:\n", encoding="utf-8"
    )
    upd.ensure_installed()  # rollback 无旧版本 → 兜底重建
    assert upd.active_version("memory") == "v1.0"
    upd.load("memory")  # 源码已恢复，可加载


# ---------- 端到端：自进化闭环 ----------


def test_end_to_end_evolve_cycle(tmp_path):
    """安装增强版 memory → Agent 实测新规则生效 → 回滚 → 新 Agent 恢复旧行为。"""
    upd = _make_updater(tmp_path)
    cand = _write_candidate(tmp_path, "memory", _evolved_memory_source())
    upd.install_candidate("memory", cand)

    # 进化后：Agent 走 updater 加载 → 新规则生效（独立数据目录，隔离历史）
    ag = _make_agent(Path(tempfile.mkdtemp()), brain_loader=upd)
    assert type(ag.memory_module).__name__ == "MemoryModule"
    ag.memory_module.extract_facts("进化验证")
    assert _has_evolved_fact(ag), "增强版规则未生效"

    # 回滚后：新 Agent 恢复内置行为（不再识别进化标记）
    upd.rollback("memory")
    ag2 = _make_agent(Path(tempfile.mkdtemp()), brain_loader=upd)
    ag2.memory_module.extract_facts("进化验证")
    assert not _has_evolved_fact(ag2), "回滚后增强规则应消失"

    # 未注入 brain_loader（测试/CLI 直连）→ 始终内置实现
    ag3 = _make_agent(Path(tempfile.mkdtemp()))
    ag3.memory_module.extract_facts("进化验证")
    assert not _has_evolved_fact(ag3)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = tempfile.mkdtemp(prefix="updater_test_")
            try:
                fn(Path(tmp))
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
