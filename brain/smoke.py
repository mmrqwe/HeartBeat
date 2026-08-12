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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        database.close()
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
        return _smoke_agent_deep(agent_cls)  # 真实 Agent：L2b replay + L2c
    reply = inst.chat("hi")
    assert isinstance(reply, str) and reply, "Agent.chat 返回空"
    return True


# ---------- 阶段3：L2b mock LLM replay + L2c headless 冒烟 ----------


class _ReplayBrain:
    """mock core.Brain：按剧本回放 LLM 响应（零网络零 API）。

    剧本条目 (kind, payload)：
      ("tools", (content, tool_calls))   —— complete_tools / complete_tools_stream
      ("interrupt", partial_text)        —— complete_stream 抛 StreamInterrupted
      ("plain", content)                 —— complete
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _pop(self, kind):
        if not self.script:
            raise AssertionError("ReplayBrain 剧本耗尽")
        entry = self.script.pop(0)
        assert entry[0] == kind, f"剧本类型不符：期望 {kind} 实际 {entry[0]}"
        self.calls.append(entry)
        return entry[1]

    def complete(self, messages, max_tokens=None, **kw):
        return self._pop("plain")

    def complete_tools(self, messages, decls, **kw):
        return self._pop("tools")

    def complete_tools_stream(self, messages, decls, cb, **kw):
        content, tool_calls = self._pop("tools")
        if content:
            cb(content)
        return content, tool_calls

    def complete_stream(self, messages, handle, max_tokens=None, **kw):
        payload = self._pop("interrupt") if self.script and self.script[0][0] == "interrupt" else self._pop("plain")
        if isinstance(payload, tuple) and payload[0] == "interrupt":
            raise core.StreamInterrupted(payload[1])
        handle(payload)
        return None


def _tool_call(name, args, call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _smoke_agent_deep(agent_cls):
    """L2b + L2c：用候选 Agent 类构造真实实例，mock LLM replay 聊天链路
    5 场景 + headless 3 轮对话与心跳。失败抛异常（由 validate 捕获）。"""
    import tempfile
    import db as dbmod

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cfg = core.load_config(Path(tmp) / "config.json")
        cfg["api"]["api_key"] = "mock"
        cfg["embedding_enabled"] = False
        cfg["tools_enabled"] = False
        database = dbmod.Database(Path(tmp) / "heartbeat.db")
        # 候选 Agent 类实例化（字段由候选 __init__ 自建）
        ag = agent_cls(cfg, {}, tmp, stats=core.Stats(database), db=database)

        # 场景 1：单轮工具 → 最终回复
        ag.brain = _ReplayBrain([
            ("tools", ("", [_tool_call("web_search", '{"query":"天气"}')])),
            ("tools", ("今天天气不错～", [])),
        ])
        ag._run_tool = lambda *a, **k: "mock 搜索结果"
        reply = ag._chat_llm_tools("查天气", None)
        assert reply == "今天天气不错～", f"场景1 回复不符：{reply}"

        # 场景 2：两轮工具（多轮循环上限内）
        ag.brain = _ReplayBrain([
            ("tools", ("", [_tool_call("web_search", '{"query":"a"}')])),
            ("tools", ("", [_tool_call("web_search", '{"query":"b"}')])),
            ("tools", ("多轮搞定", [])),
        ])
        reply = ag._chat_llm_tools("多轮", None)
        assert reply == "多轮搞定", f"场景2 回复不符：{reply}"

        # 场景 3：流式中断（部分内容接受）
        ag.brain = _ReplayBrain([
            ("interrupt", "部分内容"),
        ])
        deltas = []
        reply = ag._chat_llm_stream("流式", lambda d: deltas.append(d))
        assert reply == "部分内容", f"场景3 回复不符：{reply}"

        # 场景 4：工具异常隔离（不中断对话）
        ag.brain = _ReplayBrain([
            ("tools", ("", [_tool_call("web_search", "{}")])),
            ("tools", ("依然回复", [])),
        ])
        def boom(*a, **k):
            raise RuntimeError("工具炸了")

        ag._run_tool = boom
        reply = ag._chat_llm_tools("异常", None)
        assert reply == "依然回复", f"场景4 回复不符：{reply}"

        # 场景 5：无工具直接回复（问候）
        ag.brain = _ReplayBrain([
            ("tools", ("你好呀！", [])),
        ])
        reply = ag._chat_llm_tools("你好", None)
        assert reply == "你好呀！", f"场景5 回复不符：{reply}"

        # L2c：headless 3 轮对话无异常 + 心跳（规则模式，零网络）
        ag.brain = core.Brain(cfg, {}, core.Stats(database))
        cfg["api"]["api_key"] = ""
        for text in ("第一轮", "第二轮", "第三轮"):
            r = ag.chat(text)
            assert isinstance(r, str) and r, f"L2c 第 {text} 轮回复为空"
        heartbeat = ag.think({"collections": []})
        assert heartbeat is None or isinstance(heartbeat, str), "L2c 心跳异常"
        database.close()
        return True
