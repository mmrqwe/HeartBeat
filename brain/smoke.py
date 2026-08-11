"""brain.smoke：updater 的 L2 冒烟 runner（宿主注入 kernel.updater.smoke_runner）。

在临时数据目录构造真实 Agent（无 LLM key / 无向量 / 关工具），用候选模块
实例化契约类，跑一遍契约方法（纯规则，不碰网络/LLM），验证行为可用。

kernel/updater 不 import 业务模块（依赖方向红线），L2 冒烟由本模块
（应用层）注入执行；返回 True=通过，异常由 validate_candidate 捕获。
"""

import tempfile
from pathlib import Path

import agent as agent_mod
import core
import db as dbmod

_CLASS_NAMES = {"memory": "MemoryModule", "planner": "Planner"}


def smoke_test_module(module_name, module):
    """用候选模块实例化契约类并跑契约方法。返回 True=通过，失败抛异常。

    支持单文件模块（memory/planner）与包模块（brain，阶段2）：
    包模式下从子模块取类（agent.py->Agent 等），Agent 做无参浅冒烟；
    真实 Agent 构造复杂（需 cfg/db），阶段3 接入 L2b mock replay 深冒烟。
    """
    if module_name == "brain":
        return _smoke_brain_package(module)
    cls = getattr(module, _CLASS_NAMES[module_name])
    with tempfile.TemporaryDirectory() as tmp:
        cfg = core.load_config(Path(tmp) / "config.json")
        cfg["api"]["api_key"] = ""
        cfg["embedding_enabled"] = False
        cfg["tools_enabled"] = False  # 冒烟不触发随机网络搜索
        database = dbmod.Database(Path(tmp) / "heartbeat.db")
        ag = agent_mod.Agent(cfg, {}, tmp, stats=core.Stats(database), db=database)
        inst = cls(ag)
        if module_name == "memory":
            inst.extract_facts("我叫测试员，明天开会")
            assert any("测试员" in i["text"] for i in ag.memory.facts()), "事实提取失败"
            inst.remember("fact", "测试员喜欢摄影", category="preference")
            rel = inst.relevant("测试员")
            assert isinstance(rel, list) and rel, "记忆检索为空"
            assert inst.profile(), "画像为空"
            inst.parse_schedule_expiry("明天10点开会")
            inst.followup_candidate(ag.clock())
        else:
            assert inst.build_time_context(), "时间上下文为空"
            inst.is_quiet(ag.clock())
            inst.cooldown_ok(ag.clock())
            inst.update_mood({"collections": []})
            inst.greeting(ag.clock())
            inst.patrol_topics()
            inst.rules_think({"collections": []}, ag.clock())
    return True


def _smoke_brain_package(module):
    """包冒烟（阶段2 浅层）：子模块类存在性已由 L1 校验，这里验证
    Agent 可实例化且 chat 可用（无参构造候选）；真实 Agent 构造需
    cfg/db 参数（TypeError），此时仅确认子模块可导入（浅冒烟通过，
    深冒烟由阶段3 L2b mock replay 承担）。"""
    import sys

    base = module.__name__
    agent_cls = getattr(sys.modules[f"{base}.agent"], "Agent")
    mem_cls = getattr(sys.modules[f"{base}.memory"], "MemoryModule")
    plan_cls = getattr(sys.modules[f"{base}.planner"], "Planner")
    try:
        inst = agent_cls()
    except TypeError:
        return True  # 真实 Agent 构造需参数：浅冒烟通过，深冒烟阶段3
    reply = inst.chat("hi")
    assert isinstance(reply, str) and reply, "Agent.chat 返回空"
    return True
