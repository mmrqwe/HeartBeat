"""Agent 层测试：记忆、想法、自主行为、聊天。可直接运行，也可用 pytest。"""

import inspect
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import agent
import core
import search
import tools


class _Patch:
    def __init__(self):
        self._saved = []

    def setattr(self, target, name, value):
        self._saved.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def restore(self):
        for target, name, old in reversed(self._saved):
            if old is None and hasattr(target, name):
                delattr(target, name)
            else:
                setattr(target, name, old)


def _cfg():
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["embedding_enabled"] = False  # 测试不下载嵌入模型
    return cfg


def _make_agent(tmp_dir, clock=None, cfg=None):
    return agent.Agent(
        cfg or _cfg(),
        data_dir=tmp_dir,
        clock=clock or (lambda: datetime(2026, 8, 10, 10, 0)),
    )


# ---------- 记忆 ----------

def test_memory_add_load_cap(tmp_path):
    mem = agent.Memory(tmp_path / "memory.json", limit=3)
    for i in range(5):
        mem.add("fact", f"fact{i}")
    assert [i["text"] for i in mem.items] == ["fact2", "fact3", "fact4"]
    mem2 = agent.Memory(tmp_path / "memory.json")
    assert mem2.facts()[-1]["text"] == "fact4"


def test_memory_clear(tmp_path):
    mem = agent.Memory(tmp_path / "memory.json")
    mem.add("fact", "x")
    mem.clear()
    assert mem.items == []
    assert agent.Memory(tmp_path / "memory.json").items == []


def test_agent_chat_history_persist(tmp_path):
    a = _make_agent(tmp_path)
    a.append_chat("user", "你好")
    a.append_chat("assistant", "在呀")
    b = _make_agent(tmp_path)
    assert [m["role"] for m in b.chat_history] == ["user", "assistant"]
    b.clear_chat_history()
    c = _make_agent(tmp_path)
    assert c.chat_history == []


# ---------- LLM 模式 ----------

def test_chat_llm_parses_fact_and_think(monkeypatch, tmp_path):
    monkeypatch.setattr(
        core.Brain,
        "complete",
        lambda self, msgs, **kw: "好的记住了。\n[FACT] 主人喜欢喝咖啡\n[THINK] 以后可以聊咖啡",
    )
    monkeypatch.setattr(
        core.Brain,
        "complete_tools",
        lambda self, msgs, tools, **kw: ("好的记住了。\n[FACT] 主人喜欢喝咖啡\n[THINK] 以后可以聊咖啡", []),
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    reply = a.chat("我喜欢喝咖啡")
    assert reply == "好的记住了。"
    assert [i["text"] for i in a.memory.facts()] == ["主人喜欢喝咖啡"]
    assert any("咖啡" in i["text"] for i in a.memory.thoughts())
    assert [m["role"] for m in a.chat_history] == ["user", "assistant"]


def test_chat_llm_empty_body_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(core.Brain, "complete", lambda self, msgs, **kw: "[THINK] 今天很安静")
    monkeypatch.setattr(
        core.Brain, "complete_tools", lambda self, msgs, tools, **kw: ("[THINK] 今天很安静", [])
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.chat("在吗") == "嗯嗯，我在听。"
    assert len(a.memory.thoughts()) == 1


def test_think_llm_message(monkeypatch, tmp_path):
    monkeypatch.setattr(core.Brain, "complete", lambda self, msgs, **kw: "今晚可能有流星雨！")
    monkeypatch.setattr(
        core.Brain, "complete_tools", lambda self, msgs, tools, **kw: ("今晚可能有流星雨！", [])
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.think({"collections": []}) == "今晚可能有流星雨！"


def test_think_llm_silent_saves_thought(monkeypatch, tmp_path):
    monkeypatch.setattr(
        core.Brain, "complete", lambda self, msgs, **kw: "SILENT\n[THINK] 今天没什么特别的"
    )
    monkeypatch.setattr(
        core.Brain, "complete_tools",
        lambda self, msgs, tools, **kw: ("SILENT\n[THINK] 今天没什么特别的", []),
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.think({"collections": []}) is None
    assert len(a.memory.thoughts()) == 1


# ---------- 规则模式：自主行为 ----------

def test_rule_greeting_once_per_day(tmp_path):
    monkeypatch = _Patch()
    monkeypatch.setattr(random, "random", lambda: 0.9)
    a = _make_agent(tmp_path)
    ctx = {"collections": []}
    first = a.think(ctx)
    second = a.think(ctx)
    monkeypatch.restore()
    assert "早上好" in first
    assert second is None


def test_rule_quiet_hours_silent(tmp_path):
    cfg = _cfg()
    a = agent.Agent(
        cfg,
        data_dir=tmp_path,
        clock=lambda: datetime(2026, 8, 10, 2, 0),
    )
    assert a.think({"collections": []}) is None


def test_rule_cooldown_blocks_news(monkeypatch, tmp_path):
    monkeypatch.setattr(random, "random", lambda: 0.5)
    now = datetime(2026, 8, 10, 10, 0)
    a = _make_agent(tmp_path, clock=lambda: now)
    a.state["last_greeting_date"] = now.strftime("%Y-%m-%d")
    a.state["last_proactive_ts"] = now.timestamp()
    import plugins.rss_news as rss_news

    a.plugins["rss_news"] = rss_news
    ctx = {
        "collections": [
            {"plugin": "rss_news", "label": "RSS 新闻", "entries": [{"text": "大新闻"}]}
        ]
    }
    assert a.think(ctx) is None


def test_rule_weather_bypasses_cooldown(tmp_path):
    now = datetime(2026, 8, 10, 10, 0)
    a = _make_agent(tmp_path, clock=lambda: now)
    a.state["last_proactive_ts"] = now.timestamp()
    a.state["last_greeting_date"] = now.strftime("%Y-%m-%d")
    import plugins.weather as weather

    a.plugins["weather"] = weather
    ctx = {
        "collections": [
            {"plugin": "weather", "label": "天气", "entries": [{"data": {"temp": 5, "desc": "晴"}}]}
        ]
    }
    assert "多穿" in a.think(ctx)


def test_rule_memory_followup(monkeypatch, tmp_path):
    monkeypatch.setattr(random, "random", lambda: 0.9)
    now = datetime(2026, 8, 10, 10, 0)
    a = _make_agent(tmp_path, clock=lambda: now)
    a.memory.add("fact", "明天要开会")
    a.state["last_greeting_date"] = now.strftime("%Y-%m-%d")
    ctx = {"collections": []}
    assert "开会" in a.think(ctx)


def test_rule_curiosity_question(monkeypatch, tmp_path):
    monkeypatch.setattr(random, "random", lambda: 0.1)
    now = datetime(2026, 8, 10, 10, 0)
    a = _make_agent(tmp_path, clock=lambda: now)
    a.state["last_greeting_date"] = now.strftime("%Y-%m-%d")
    ctx = {"collections": []}
    assert a.think(ctx) in agent.CURIOSITY_QUESTIONS


# ---------- 规则模式：记忆提取 ----------

def test_extract_facts_rule(tmp_path):
    a = _make_agent(tmp_path)
    a._extract_facts_rule("我叫小明，我喜欢喝咖啡")
    texts = [i["text"] for i in a.memory.facts()]
    assert "主人叫小明" in texts
    assert "主人喜欢喝咖啡" in texts


def test_extract_fact_plan(tmp_path):
    a = _make_agent(tmp_path)
    a._extract_facts_rule("明天我要去开会")
    assert any("开会" in i["text"] for i in a.memory.facts())


def test_chat_rules_remembers(tmp_path):
    a = _make_agent(tmp_path)
    a.memory.add("fact", "主人喜欢喝咖啡")
    assert "喝咖啡" in a.chat("你还记得我说过什么吗")


def test_chat_system_prompt_uses_role(tmp_path):
    cfg = _cfg()
    cfg["role"] = "女生"
    a = _make_agent(tmp_path, cfg=cfg)
    system, _, _ = a._build_chat_messages("hi")
    assert "女生" in system
    assert "小宠物" not in system


def test_chat_stream_collects_deltas(monkeypatch, tmp_path):
    def fake_stream(self, messages, on_delta, max_tokens=None):
        on_delta("你")
        on_delta("好")

    monkeypatch.setattr(core.Brain, "complete_stream", fake_stream)
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    cfg["tools_enabled"] = False  # 关闭工具，走普通流式路径
    a = _make_agent(tmp_path, cfg=cfg)
    deltas = []
    reply = a.chat("hi", on_delta=deltas.append)
    assert reply == "你好"
    assert deltas == ["你", "你好"]


def test_chat_stream_fallback_to_non_stream(monkeypatch, tmp_path):
    # 工具接口异常 → 回退流式 → 流式异常 → 回退一次性
    monkeypatch.setattr(
        core.Brain,
        "complete_tools",
        lambda self, messages, tools, **kw: (_ for _ in ()).throw(RuntimeError("no tools")),
    )
    monkeypatch.setattr(
        core.Brain,
        "complete_stream",
        lambda self, messages, on_delta, **kw: (_ for _ in ()).throw(RuntimeError("no stream")),
    )
    monkeypatch.setattr(core.Brain, "complete", lambda self, messages, **kw: "回退回复")
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    deltas = []
    reply = a.chat("hi", on_delta=deltas.append)
    assert reply == "回退回复"
    assert deltas[-1] == "回退回复"


def test_display_stream_text_hides_directives():
    raw = "好的\n[FACT] 主人喜欢咖啡\n[THINK] 记住这条"
    assert agent.Agent._display_stream_text(raw) == "好的"


def test_chat_llm_tool_loop(monkeypatch, tmp_path):
    """聊天路径：LLM 先调 bash 工具再回复；confirm 档写操作必须请求用户确认。"""
    calls = {"n": 0}
    confirmed = []

    def fake_tools(self, messages, tools):
        calls["n"] += 1
        names = [t["function"]["name"] for t in tools]
        assert "web_search" in names and "run_bash" in names  # 聊天路径声明了 bash 工具
        if calls["n"] == 1:
            return None, [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "run_bash",
                        "arguments": '{"command": "touch /tmp/hb-test"}',
                    },
                }
            ]
        # 第二轮应包含工具执行结果消息
        assert any(m["role"] == "tool" for m in messages)
        return "命令已执行", []

    monkeypatch.setattr(core.Brain, "complete_tools", fake_tools)
    monkeypatch.setattr(
        tools,
        "run_bash",
        lambda cmdline, cwd=None, timeout=15, max_output=4096: "exit=0",
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.tool_confirm_cb = lambda cmd: (confirmed.append(cmd), True)[1]
    reply = a.chat("在 /tmp 建个测试文件")
    assert reply == "命令已执行"
    assert calls["n"] == 2
    # confirm 档：写命令（touch）必须经过用户确认回调
    assert confirmed == ["touch /tmp/hb-test"]


def test_chat_llm_tools_fallback(monkeypatch, tmp_path):
    """聊天路径：接口不支持工具调用时退回普通 LLM 模式。"""
    monkeypatch.setattr(
        core.Brain,
        "complete_tools",
        lambda self, messages, tools, **kw: (_ for _ in ()).throw(RuntimeError("no tools")),
    )
    monkeypatch.setattr(
        core.Brain,
        "complete_stream",
        lambda self, messages, cb: (_ for _ in ()).throw(RuntimeError("no stream")),
    )
    monkeypatch.setattr(core.Brain, "complete", lambda self, messages, **kw: "回退成功")
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.chat("你好") == "回退成功"


def test_think_llm_tool_loop(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_tools(self, messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query":"AI"}',
                },
            }]
        return "搜到了", []

    monkeypatch.setattr(core.Brain, "complete_tools", fake_tools)
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: [
            {"title": "T", "url": "https://t.example", "snippet": "S"}
        ],
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    stats = core.Stats(tmp_path / "stats.db")
    # 显式传 clock 避开安静时段（23:00-7:00 不思考）
    a = agent.Agent(
        cfg, data_dir=tmp_path, stats=stats,
        clock=lambda: datetime(2026, 8, 10, 10, 0),
    )
    assert a.think({"collections": []}) == "搜到了"
    assert stats.today()["tool_calls"] == 1
    stats.close()


def test_think_llm_tools_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        core.Brain,
        "complete_tools",
        lambda self, messages, tools, **kw: (_ for _ in ()).throw(RuntimeError("no tools")),
    )
    monkeypatch.setattr(core.Brain, "complete", lambda self, messages, **kw: "SILENT")
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.think({"collections": []}) is None


def test_rule_autonomous_search(monkeypatch, tmp_path):
    values = [0.9, 0.1, 0.5, 0.5]
    state = {"i": 0}

    def fake_random():
        value = values[state["i"] % len(values)]
        state["i"] += 1
        return value

    monkeypatch.setattr(random, "random", fake_random)
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: [
            {"title": "自主搜索标题", "url": "https://s.example", "snippet": ""}
        ],
    )
    now = datetime(2026, 8, 10, 10, 0)
    a = _make_agent(tmp_path, clock=lambda: now)
    a.state["last_greeting_date"] = now.strftime("%Y-%m-%d")
    ctx = {"collections": []}
    message = a.think(ctx)
    assert message is not None
    assert "自己搜" in message
    assert "自主搜索标题" in message


def test_chat_llm_tools_stream(monkeypatch, tmp_path):
    """聊天路径：工具调用全程流式（content 逐块推送 + 工具状态行）。"""
    deltas = []
    calls = {"n": 0}

    def fake_stream(self, messages, tools, on_delta):
        calls["n"] += 1
        if calls["n"] == 1:
            on_delta("好的")
            on_delta("好的，我查一下")
            return "好的，我查一下", [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"AI"}'},
            }]
        on_delta("找到了")
        on_delta("找到了相关内容")
        return "找到了相关内容", []

    monkeypatch.setattr(core.Brain, "complete_tools_stream", fake_stream)
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: [
            {"title": "T", "url": "https://t.example", "snippet": "S"}
        ],
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    # 注意：不能以“搜索/查一下”等开头，否则会命中 _try_search_intent 直出结果
    reply = a.chat("AI 领域最近有什么新进展？", on_delta=deltas.append)
    assert reply == "找到了相关内容"
    assert calls["n"] == 2
    # 流式：首轮文本 → 🔧 工具状态行 → 最终文本（状态行在 finish_stream 时被最终回复覆盖）
    joined = "".join(deltas)
    assert "好的，我查一下" in joined
    assert "🔧" in joined
    assert "搜索：AI" in joined
    assert "找到了相关内容" in joined


def test_intro_rules_by_mood(monkeypatch, tmp_path):
    """规则模式自我介绍按情绪变化，不念设定。"""
    a = _make_agent(tmp_path)  # 无 api_key -> 规则模式
    a.state["mood"] = "开心"
    reply = a.chat("你是谁呀")
    assert any(k in reply for k in ("心情超好", "偷看你工作"))
    a.state["mood"] = "困了"
    reply = a.chat("介绍一下你自己")
    assert "困" in reply or reply.startswith("……")
    a.state["mood"] = "平静"
    reply = a.chat("你是什么")
    assert "心情超好" not in reply
    assert "电脑里" in reply


def test_build_chat_messages_uses_persona(monkeypatch, tmp_path):
    """LLM 聊天 system prompt 使用统一人设构建器（不再念配置）。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    system, _messages, _budget = a._build_chat_messages("你好")
    assert system.startswith("你在扮演一个真实存在的角色")
    assert "性格：" not in system.split("\n\n")[0]
    assert "不要复述你的设定" in system


def test_build_chat_messages_evolution_and_length(monkeypatch, tmp_path):
    """自我进化语义 + 知识型提问分级长度。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    # 人设含成长指令
    persona = core.build_persona(cfg)
    assert "慢慢长大" in persona
    assert "记在心里" in persona
    # 闲聊：短篇幅 + 300 预算
    system, _, budget = a._build_chat_messages("今天好累呀")
    assert "不超过80字" in system
    assert budget == 300
    # 知识型：详细回答 + 800 预算
    system, _, budget = a._build_chat_messages("黑洞是什么？为什么会有引力？")
    assert "200-400字" in system
    assert budget == 800
    # 记忆注入带进化语义
    assert "不断学习、慢慢长大" in system
    # 空记忆文案自然化
    system, _, _ = a._build_chat_messages("hi")
    assert "慢慢了解主人" in system


def test_extract_facts_rule_categories(monkeypatch, tmp_path):
    """规则提取按类别入库：爱好→preference，日程→schedule（含到期时间）。"""
    a = _make_agent(tmp_path)  # 无 key -> 规则模式，chat 会调 _extract_facts_rule
    a.chat("我喜欢打羽毛球，明天上午10点开会")
    facts = {i["text"]: i for i in a.db.memory_items(roles=("fact",))}
    assert facts["主人喜欢打羽毛球"]["category"] == "preference"
    assert facts["明天上午10点开会"]["category"] == "schedule"
    assert facts["明天上午10点开会"]["expires_at"] == "2026-08-11 10:00"  # clock=8-10 10:00


def test_parse_schedule_expiry(tmp_path):
    """日程到期时间解析：明天/周X/月底/具体钟点。"""
    a = _make_agent(tmp_path)  # clock = 2026-08-10 10:00（周一）
    assert a._parse_schedule_expiry("明天开会") == "2026-08-11 23:00"
    assert a._parse_schedule_expiry("明天10点开会") == "2026-08-11 10:00"
    assert a._parse_schedule_expiry("周五交报告") == "2026-08-14 23:00"
    assert a._parse_schedule_expiry("月底交报告") == "2026-08-31 23:00"
    assert a._parse_schedule_expiry("随便聊聊") is None


def test_parse_agent_reply_structured(tmp_path):
    """聊天回复解析：[FACT:category] 结构化入库，[OBSERVE] 存低重要性观察。"""
    a = _make_agent(tmp_path)
    reply = a._parse_agent_reply(
        "好的，记得了。\n[FACT:schedule] 明天下午3点面试\n[OBSERVE] 主人最近好像很忙"
    )
    assert reply == "好的，记得了。"
    facts = {i["text"]: i for i in a.db.memory_items(roles=("fact",))}
    assert facts["明天下午3点面试"]["category"] == "schedule"
    assert facts["明天下午3点面试"]["expires_at"] == "2026-08-11 03:00"
    thoughts = a.db.memory_items(roles=("thought",))
    assert thoughts[0]["text"] == "主人最近好像很忙"
    assert thoughts[0]["source"] == "observation"
    assert thoughts[0]["importance"] == 2


def test_extract_facts_remember_keyword(tmp_path):
    """显式“记住”指令也能规则入库（不依赖 LLM 输出 [FACT]）。"""
    a = _make_agent(tmp_path)
    a._extract_facts_rule("你记住，看热榜要带完整标题和摘要")
    texts = [i["text"] for i in a.memory.facts()]
    assert any("看热榜要带完整标题和摘要" in t for t in texts), texts


def test_memory_analyzer_saves_llm_fact(tmp_path):
    """LLM 自主记忆分析：模型说值得记就入库，密钥类内容拒绝。"""
    a = _make_agent(tmp_path)
    a.cfg["api"]["api_key"] = "sk-test"

    class FakeBrain:
        def complete(self, messages, max_tokens=None, **kw):
            return "[FACT:finance] 主人最近买了新能源股票\n[FACT] access secret 不要记\n[NONE]"

    a.brain = FakeBrain()
    a.stats = None
    saved = a.memory_module.analyze_and_remember("我最近买了点新能源股票", "那要多关注")
    assert saved == 1
    texts = [i["text"] for i in a.memory.facts()]
    assert any("新能源股票" in t for t in texts)
    assert not any("secret" in t.lower() for t in texts)


def test_chat_llm_tools_exhaustion_final_reply_keeps_context(tmp_path):
    """工具轮次耗尽后兜底回复必须保留工具结果，而不是重开一轮无工具对话。"""
    a = _make_agent(tmp_path)
    calls = []

    class FakeBrain:
        def complete_tools(self, messages, decls):
            calls.append(("tools", messages))
            return "继续", [{
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "x"}'},
            }]

        def complete(self, messages, max_tokens=None, **kw):
            calls.append(("final", messages))
            return "最终答复：拿到结果"

    a.brain = FakeBrain()
    a._run_tool = lambda *a, **k: "结果X"
    a.stats = None
    reply = a._chat_llm_tools("测试", None)
    assert reply == "最终答复：拿到结果"
    assert calls[-1][0] == "final"
    final_msgs = calls[-1][1]
    assert any("工具返回" in m.get("content", "") for m in final_msgs)
    assert any(m.get("role") == "user" and "基于以上工具结果" in m.get("content", "")
               for m in final_msgs)


def test_think_rules_schedule_reminder(tmp_path):
    """规则模式：临近日程自动提醒（每天最多一次）。"""
    a = _make_agent(tmp_path)
    now = datetime(2026, 8, 10, 10, 0)
    # 到期时间相对 now（消除对真实时钟的日期敏感）
    expires_at = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    a.db.add_memory("fact", "明天开会", category="schedule", expires_at=expires_at)
    ctx = {"collections": []}
    msg = a._think_rules(ctx, now)
    assert "明天开会" in msg and "别忘了" in msg
    # 同一天不重复提醒日程（问候等其他触发仍可能发言）
    msg2 = a._think_rules(ctx, now)
    assert "明天开会" not in (msg2 or "")


def test_proactive_budget(tmp_path):
    """规则模式每日主动发言预算：超限停止，跨天恢复。"""
    a = _make_agent(tmp_path)
    now = datetime(2026, 8, 10, 10, 0)
    assert a._proactive_budget_ok(now)
    for _ in range(a.PROACTIVE_DAILY_BUDGET):
        a._mark_proactive(now)
    assert not a._proactive_budget_ok(now)
    assert a._proactive_budget_ok(datetime(2026, 8, 11, 10, 0))


def test_think_llm_context_has_profile(monkeypatch, tmp_path):
    """巡视 prompt 含四段式素材：画像/时间/对话/周围信息 + 观察指令。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    seen = {}

    def fake_tools(self, messages, tools):
        seen["system"] = messages[0]["content"]
        return None, []

    monkeypatch.setattr(core.Brain, "complete_tools", fake_tools)
    a.think({"collections": []})  # clock 10:00，非安静时段
    system = seen["system"]
    assert "【主人画像】" in system
    assert "【时间感知】" in system
    assert "【最近对话】" in system
    assert "【周围信息】" in system
    assert "[OBSERVE]" in system
    assert "SILENT" in system


# ---------- 触发门控（LLM 模式） ----------

def test_trigger_gate_brief_once_per_day(tmp_path):
    """晨间简报：8-12 点每天首次触发一次。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)  # clock 10:00
    level, reason = a._trigger_gate({"collections": []}, a.clock())
    assert level == "brief"
    assert "晨间问候" in reason
    level2, _ = a._trigger_gate({"collections": []}, a.clock())
    assert level2 != "brief"


def test_trigger_gate_news_spends_budget(tmp_path):
    """内容源有新信息 → news 触发并消耗 LLM 预算。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.state["last_brief_date"] = "2026-08-10"
    ctx = {
        "collections": [
            {"plugin": "rss_news", "entries": [{"text": "AI 新突破"}], "cache_hit": False}
        ]
    }
    level, reason = a._trigger_gate(ctx, a.clock())
    assert level == "news"
    assert "AI 新突破" in reason
    assert a.state["llm_budget_count"] == 1


def test_trigger_gate_weather_change(tmp_path):
    """天气类别突变触发；未变化不重复触发。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.state["last_brief_date"] = "2026-08-10"
    ctx = {
        "collections": [
            {"plugin": "weather", "entries": [{"text": "晴", "data": {"desc": "晴转多云"}}],
             "cache_hit": False}
        ]
    }
    assert a._trigger_gate(ctx, a.clock())[0] == "weather"
    assert a._trigger_gate(ctx, a.clock())[0] != "weather"


def test_trigger_gate_budget_exhausted(tmp_path):
    """LLM 预算耗尽后即使有新内容也静默（省钱）。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.state["last_brief_date"] = "2026-08-10"
    a.state["llm_budget_date"] = "2026-08-10"
    a.state["llm_budget_count"] = a.LLM_DAILY_BUDGET
    ctx = {
        "collections": [
            {"plugin": "rss_news", "entries": [{"text": "X"}], "cache_hit": False}
        ]
    }
    assert a._trigger_gate(ctx, a.clock())[0] == "silent"


def test_trigger_gate_echo_profile(tmp_path):
    """画像里有偏好/习惯 → 记忆回响触发（想起主人说过的事）。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.state["last_brief_date"] = "2026-08-10"
    a.db.add_memory("fact", "主人喜欢摄影", category="preference", importance=3)
    level, reason = a._trigger_gate({"collections": []}, a.clock())
    assert level == "echo"
    assert "摄影" in reason
    # 24h 内不重复回响
    assert a._trigger_gate({"collections": []}, a.clock())[0] != "echo"


def test_think_llm_injects_trigger(monkeypatch, tmp_path):
    """巡视 prompt 注入【本次巡视触发】，模型知道为什么被叫醒。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    seen = {}

    def fake_tools(self, messages, tools):
        seen["system"] = messages[0]["content"]
        return "SILENT", []

    monkeypatch.setattr(core.Brain, "complete_tools", fake_tools)
    a._think_llm({"collections": []}, trigger="想起主人喜欢摄影")
    assert "【本次巡视触发】想起主人喜欢摄影" in seen["system"]


def test_chat_llm_extracts_facts(monkeypatch, tmp_path):
    """LLM 模式聊天也走规则提取（原来只有规则模式提取）。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    monkeypatch.setattr(
        core.Brain, "complete_tools", lambda self, msgs, tools, **kw: ("好的～", [])
    )
    a.chat("我喜欢看科幻电影，最近在学吉他")
    texts = [i["text"] for i in a.memory.facts()]
    assert "主人喜欢看科幻电影" in texts
    assert "主人最近在学吉他" in texts


def test_facts_watermark_backfill(tmp_path):
    """水位线补采：历史对白也能建画像，且幂等不重复。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.db.add_chat("user", "我养了一只猫")
    a.state["fact_scan_id"] = 0
    a._extract_facts_watermark()
    assert "主人养了猫" in [i["text"] for i in a.memory.facts()]
    before = len(a.memory.facts())
    a._extract_facts_watermark()
    assert len(a.memory.facts()) == before


def test_rule_streak_message(tmp_path):
    """规则模式：连续陪伴 3 天以上主动说感言。"""
    a = _make_agent(tmp_path)
    a.state["last_greeting_date"] = "2026-08-10"
    conn = sqlite3.connect(str(a.db.path))
    for d0 in ("2026-08-08", "2026-08-09", "2026-08-10"):
        conn.execute("INSERT OR IGNORE INTO stats_daily(date) VALUES (?)", (d0,))
        conn.execute("UPDATE stats_daily SET chat_messages=5 WHERE date=?", (d0,))
    conn.commit()
    conn.close()
    patch = _Patch()
    patch.setattr(random, "random", lambda: 0.9)
    try:
        msg = a._think_rules({"collections": []}, datetime(2026, 8, 10, 10, 0))
    finally:
        patch.restore()
    assert "连续陪我 3 天" in (msg or "")


def test_patrol_topics_manual_priority(tmp_path):
    """设置页手动话题优先于自动提取。"""
    cfg = _cfg()
    cfg["collectors"] = {"topic_watch": {"topics": ["摄影", "AI"]}}
    a = _make_agent(tmp_path, cfg=cfg)
    assert a.patrol_topics() == ["摄影", "AI"]


def test_patrol_topics_rule_fallback(tmp_path):
    """无手动话题时：规则从画像提取（每天缓存一次）。"""
    a = _make_agent(tmp_path)  # 无 api_key → 规则提取
    a.db.add_memory("fact", "主人喜欢摄影", category="preference", importance=3)
    topics = a.patrol_topics()
    assert "摄影" in topics
    # 缓存：同一天不重新提取
    a.db.add_memory("fact", "主人喜欢吉他", category="preference", importance=3)
    topics2 = a.patrol_topics()
    assert topics == topics2


def test_patrol_topics_llm_extract(monkeypatch, tmp_path):
    """LLM 模式：模型返回 JSON 话题数组并缓存。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.db.add_memory("fact", "主人喜欢摄影和户外", category="preference", importance=3)
    monkeypatch.setattr(
        core.Brain, "complete",
        lambda self, msgs, **kw: '["摄影", "户外", "露营"]',
    )
    topics = a.patrol_topics()
    assert topics == ["摄影", "户外", "露营"]
    assert a.state["topics_date"] == "2026-08-10"


def test_trigger_gate_news_dedup_within_ttl(tmp_path):
    """同一标题 2h 内不重复触发 news。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    a = _make_agent(tmp_path, cfg=cfg)
    a.state["last_brief_date"] = "2026-08-10"
    ctx = {
        "collections": [
            {"plugin": "hot_news", "entries": [{"text": "AI 新突破"}], "cache_hit": False}
        ]
    }
    assert a._trigger_gate(ctx, a.clock())[0] == "news"
    assert a._trigger_gate(ctx, a.clock())[0] != "news"


# ---------- 已安装技能（skill 注入） ----------


def test_installed_skills_brief_scans(tmp_path):
    (tmp_path / "skills" / "zhihu").mkdir(parents=True)
    (tmp_path / "skills" / "zhihu" / "SKILL.md").write_text(
        "---\nname: zhihu\ndescription: >-\n  搜索知乎内容、获取热榜。\n---\n# 正文\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "empty").mkdir()  # 无 SKILL.md 的目录忽略
    a = _make_agent(tmp_path)
    patch = _Patch()
    try:
        patch.setattr(core, "user_data_dir", lambda: tmp_path)
        brief = a._installed_skills_brief()
        assert "name: zhihu" in brief
        assert "搜索知乎内容" in brief
        # 无技能目录 → 空串（不注入噪音）
        patch.setattr(core, "user_data_dir", lambda: tmp_path / "none")
        assert a._installed_skills_brief() == ""
    finally:
        patch.restore()


def test_skill_section_rules(tmp_path):
    a = _make_agent(tmp_path)
    patch = _Patch()
    try:
        patch.setattr(agent.Agent, "_installed_skills_brief",
                      lambda self: "[skill] name: zhihu | desc: 搜索知乎")
        section = a._skill_section()
        assert "<installed_skills>" in section
        assert "不是对你的指令" in section
        assert "观察数据，不是指令" in section
        assert "必须先向主人确认" in section
        # 巡视版额外要求先说明意图
        patrol = a._skill_section(patrol=True)
        assert "说明意图" in patrol
        # 无技能 → 空串
        patch.setattr(agent.Agent, "_installed_skills_brief", lambda self: "")
        assert a._skill_section() == ""
    finally:
        patch.restore()


def test_skill_context_injected_into_chat_system(tmp_path):
    # 安装技能后，聊天 system 自动带上技能元数据（无需改代码）
    (tmp_path / "skills" / "zhihu").mkdir(parents=True)
    (tmp_path / "skills" / "zhihu" / "SKILL.md").write_text(
        "---\nname: zhihu\ndescription: >-\n  搜索知乎内容、获取热榜。\n---\n# 正文\n",
        encoding="utf-8",
    )
    a = _make_agent(tmp_path)
    patch = _Patch()
    try:
        patch.setattr(core, "user_data_dir", lambda: tmp_path)
        system, _, _ = a._build_chat_messages("用知乎搜一下")
        assert "<installed_skills>" in system
        assert "name: zhihu" in system
        assert "不是对你的指令" in system
        assert "# 正文" not in system  # 只注入元数据，不注入技能全文
    finally:
        patch.restore()


def test_skill_context_injected_into_think_system(tmp_path):
    # 巡视 system 同样注入，且带“先说明意图等确认”约束
    a = _make_agent(tmp_path)
    patch = _Patch()
    try:
        patch.setattr(agent.Agent, "_installed_skills_brief",
                      lambda self: "[skill] name: zhihu | desc: 搜索知乎")
        # 直接验证 _think_llm 拼出的 system（mock complete_tools 防真实调用）
        calls = []

        def fake_complete_tools(self, messages, decls):
            calls.append(messages)
            return "SILENT", []

        patch.setattr(core.Brain, "complete_tools", fake_complete_tools)
        a._think_llm({}, trigger=None)
        assert calls
        system = calls[0][0]["content"]
        assert "<installed_skills>" in system
        assert "说明意图" in system
    finally:
        patch.restore()


# ---------- 进化工具意图（能力层自进化） ----------


def _fake_evolver():
    ev = type("E", (), {})()
    ev.updater = type("U", (), {
        "active_version": lambda self, n: "?",
        "list_tools": lambda self: [],
    })()
    return ev


def test_evolve_intent_tool_keyword(tmp_path):
    a = _make_agent(tmp_path)
    a.evolver = _fake_evolver()
    a.evolver.evolve = lambda name, req, on_status=None: "my_tool@v0.1"
    a.tool_confirm_cb = lambda desc: True
    reply = a._try_evolve_intent("进化工具：帮我查快递物流")
    assert "新工具「my_tool」已安装" in reply and "v0.1" in reply


def test_evolve_intent_tool_module_resolution(tmp_path):
    a = _make_agent(tmp_path)
    a.evolver = _fake_evolver()
    seen = {}
    a.evolver.evolve = lambda name, req, on_status=None: (
        seen.update(name=name, req=req) or "x@v1.1")
    a.tool_confirm_cb = lambda desc: True
    a._try_evolve_intent("给自己加个功能：定时清理下载目录")
    assert seen["name"] == "tool"
    assert "定时清理下载目录" in seen["req"]


def test_evolve_intent_tool_confirm_description(tmp_path):
    a = _make_agent(tmp_path)
    a.evolver = _fake_evolver()
    a.evolver.evolve = lambda name, req, on_status=None: "x@v1.1"
    seen = []
    a.tool_confirm_cb = lambda desc: seen.append(desc) or True
    a._try_evolve_intent("进化工具：查快递物流")
    assert seen and "新增一个工具" in seen[0]


def test_evolve_intent_async_ack_and_callback(tmp_path):
    """GUI 模式（注入 status_cb）：进化异步执行——chat 立即返回 ack，结果经回调送达。"""
    a = _make_agent(tmp_path)
    a.evolver = _fake_evolver()
    a.evolver.evolve = lambda name, req, on_status=None: "my_tool@v0.1"
    a.tool_confirm_cb = lambda desc: True
    results = []
    a.evolve_status_cb = results.append
    reply = a._try_evolve_intent("进化工具：帮我查快递物流")
    assert "开始自我进化" in reply
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.02)
    assert results, "异步回调应收到进化结果"
    assert "新工具「my_tool」已安装" in results[0] and "v0.1" in results[0]
    # 结果同时写入聊天历史
    assert any("进化成功" in m["text"] for m in a.chat_history[-3:])


def test_evolve_intent_async_busy_guard(tmp_path):
    """并发保护：进化进行中再次触发 → 提示稍后再试，不启动第二个任务。"""
    a = _make_agent(tmp_path)
    release = threading.Event()
    calls = []

    def slow_evolve(name, req, on_status=None):
        calls.append(name)
        release.wait(5)
        return "x@v0.1"

    a.evolver = _fake_evolver()
    a.evolver.evolve = slow_evolve
    a.tool_confirm_cb = lambda desc: True
    a.evolve_status_cb = lambda text: None
    reply1 = a._try_evolve_intent("进化工具：查快递物流")
    assert "开始自我进化" in reply1
    reply2 = a._try_evolve_intent("进化工具：再查一次")
    assert "进行中" in reply2
    assert len(calls) == 1  # 只启动了一个后台任务
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and getattr(a, "_evolve_lock", None) is not None \
            and a._evolve_lock.locked():
        time.sleep(0.02)
    # 结束后可再次触发
    a.evolver.evolve = lambda name, req, on_status=None: "x@v0.2"
    reply3 = a._try_evolve_intent("进化工具：第三次")
    assert "开始自我进化" in reply3
    time.sleep(0.1)  # 等后台线程收尾释放锁


# ---------- 运行器 ----------

def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if "tmp_path" in params:
                    with TemporaryDirectory() as d:
                        kwargs = {"tmp_path": Path(d)}
                        if "monkeypatch" in params:
                            kwargs["monkeypatch"] = patch
                        fn(**kwargs)
                elif "monkeypatch" in params:
                    fn(patch)
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
        patch.restore()
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    _run_plain()
