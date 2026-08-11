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
    """用候选模块实例化契约类并跑契约方法。返回 True=通过，失败抛异常。"""
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
            assert inst.profile(), "画像为空"
            inst.parse_schedule_expiry("明天10点开会")
            inst.followup_candidate(ag.clock())
        else:
            inst.is_quiet(ag.clock())
            inst.cooldown_ok(ag.clock())
            inst.update_mood({"collections": []})
            inst.greeting(ag.clock())
            inst.patrol_topics()
            inst.rules_think({"collections": []}, ag.clock())
    return True
