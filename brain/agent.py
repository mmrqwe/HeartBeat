"""Agent：桌宠的记忆、想法、自主行为。记忆/聊天/状态全部存 SQLite。"""

import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import core
import rag
import search
import tools
from db import Database, Memory

from brain.memory import FOLLOW_KEYWORDS, MemoryModule
from brain.planner import CURIOSITY_QUESTIONS, Planner, PROACTIVE_DAILY_BUDGET


class Agent:
    """桌宠的自主层：SQLite 记忆 + 向量检索 + 想法 + 主动行为。"""

    def __init__(self, cfg, plugins=None, data_dir=None, clock=None, stats=None, db=None, brain_loader=None):
        self.cfg = cfg
        self.plugins = plugins or {}
        self.data_dir = Path(data_dir) if data_dir else Path(".")
        self.db = db or Database(self.data_dir / "heartbeat.db")
        self.stats = stats
        self.embedder = rag.default_embedder(cfg, self.data_dir)
        self._embed_sig = (cfg.get("embedding_enabled"), cfg.get("embedding_model"))
        self._reindex_pending = False
        self.brain = core.Brain(cfg, self.plugins, stats)
        self.tool_confirm_cb: Optional[Callable[[str], bool]] = None  # GUI 注入：confirm 档写命令的用户确认回调
        self.eventbus = None  # GUI 注入：kernel.eventbus（工具执行旁路通知）
        self.brain_loader = brain_loader  # 自进化加载器（kernel.updater），None=用内置实现
        self.memory = Memory(self.db)
        self.chat_history = self.db.chat_items(100)
        self.state = self._load_state()
        self.clock = clock or datetime.now
        # 领域模块（组合）：记忆 / 规划 —— brain 层可独立升级的进化单元。
        # 注入 brain_loader（kernel.updater）时按 active 版本动态加载，
        # 未注入（测试/CLI 直连）时 fallback 内置实现。
        if self.brain_loader is not None:
            self.memory_module = self.brain_loader.create("memory", self)
            self.planner = self.brain_loader.create("planner", self)
        else:
            self.memory_module = MemoryModule(self)
            self.planner = Planner(self)

    # ---------- 状态 ----------

    def _load_state(self):
        defaults = {
            "last_greeting_date": "",
            "last_proactive_ts": 0.0,
            "last_followup_ts": 0.0,
            "last_schedule_remind_date": "",
            "daily_proactive_date": "",
            "daily_proactive_count": 0,
            "mood": "平静",
            "fact_scan_id": 0,          # 记忆补采水位线（chat_messages.id）
            "last_brief_date": "",      # 晨间简报日期（每天一次）
            "last_echo_ts": 0.0,        # 上次画像回响时间
            "last_streak_ts": 0.0,      # 上次连续陪伴感言时间
            "llm_budget_date": "",     # LLM 主动发言预算（防白花钱）
            "llm_budget_count": 0,
            "last_weather_class": "",  # 上次巡视的天气类别（突变检测）
            "merge_seen": {},           # 跨源标题去重指纹（TTL 2h）
            "topics_date": "",         # 兴趣话题自动提取日期（每天一次）
            "topics_list": [],          # 自动提取的话题列表
        }
        return {
            key: self.db.get_state(key, default)
            for key, default in defaults.items()
        }

    def _save_state(self):
        for key, value in self.state.items():
            self.db.set_state(key, value)

    def reload(self, cfg, plugins=None):
        self.cfg = cfg
        if plugins is not None:
            self.plugins = plugins
        self.brain = core.Brain(cfg, self.plugins, self.stats)
        # embedder 仅在模型配置变化时重建，避免每次保存都重新加载/下载模型
        sig = (cfg.get("embedding_enabled"), cfg.get("embedding_model"))
        if sig != self._embed_sig:
            self._embed_sig = sig
            self.embedder = rag.default_embedder(cfg, self.data_dir)
        # 补索引挪到后台线程执行（reindex_async），避免保存设置卡 UI
        self._reindex_pending = True
        # 领域模块重载：updater 切换版本后 reload 即生效（准热切换）；
        # 加载失败保持旧模块（升级不破坏运行中会话）
        if self.brain_loader is not None:
            try:
                self.memory_module = self.brain_loader.create("memory", self)
                self.planner = self.brain_loader.create("planner", self)
            except Exception:
                pass

    def reindex_async(self):
        """后台补向量索引（保存设置后调用，不阻塞 UI）。"""
        if not getattr(self, "_reindex_pending", False):
            return
        self._reindex_pending = False
        try:
            if getattr(self.embedder, "ready", False):
                self.db.reindex(self.embedder, "memory")
                self.db.reindex(self.embedder, "chat")
        except Exception:
            pass

    # ---------- 聊天记录 ----------

    def append_chat(self, role, text):
        item_id = self.db.add_chat(role, text)
        entry = {
            "id": item_id,
            "role": role,
            "text": text,
            "time": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.chat_history.append(entry)
        self.chat_history = self.chat_history[-100:]
        if role == "user":
            self._embed_chat(item_id, text)
        return entry

    def clear_chat_history(self):
        self.db.clear_chat()
        self.chat_history = []

    # ---------- 聊天 ----------

    def chat(self, user_text, on_delta=None):
        user_text = user_text.strip()
        entry = self.append_chat("user", user_text)
        # 两种大脑模式统一采集事实（规则提取，零 API 成本）
        self._extract_facts_rule(user_text)
        self.state["fact_scan_id"] = entry["id"]
        self._save_state()
        reply = self._try_search_intent(user_text)
        if reply is not None:
            if on_delta:
                on_delta(reply)
        elif self.cfg["api"]["api_key"]:
            if self.cfg.get("tools_enabled", True):
                reply = self._chat_llm_tools(user_text, on_delta)
            elif on_delta and self.cfg.get("stream", True):
                reply = self._chat_llm_stream(user_text, on_delta)
            else:
                reply = self._chat_llm(user_text)
        else:
            reply = self._chat_rules(user_text)
        self.append_chat("assistant", reply)
        if self.stats:
            self.stats.record_chat(2)
        return reply

    def _try_search_intent(self, user_text):
        """识别“搜索/新闻/股票/天气”意图并直接给出结果，两种大脑模式通用。"""
        patterns = [
            (re.compile(r"^(?:搜索|搜一下|帮我搜|帮我查|查一下|搜搜)\s*(?:关于)?\s*(.+)$"), "web"),
            (re.compile(r"^(?:新闻|查新闻|搜新闻)\s*(?:关于)?\s*(.+)$"), "news"),
            (re.compile(r"^(?:股票|股价|行情)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,12})$"), "stock"),
            (re.compile(r"^(?:天气|气温)\s*([\u4e00-\u9fa5A-Za-z]{1,12})$"), "weather"),
        ]
        for pattern, kind in patterns:
            match = pattern.match(user_text.strip())
            if not match:
                continue
            query = match.group(1).strip()
            try:
                if kind == "stock":
                    entries = search.search_all(query, "stock")
                    return search.format_results(entries, "股票")
                if kind == "weather":
                    entries = search.search_all(query, "weather")
                    return search.format_results(entries, "天气")
                if kind == "news":
                    entries = search.search_all(query, "news")
                    return search.format_results(entries, "新闻")
                entries = search.search_all(query, "web")
                return search.format_results(entries, "搜索")
            except Exception as exc:
                return f"搜索没成功：{exc}"
        return None

    def _chat_llm(self, user_text):
        system, messages = self._build_chat_messages(user_text)
        reply = self._parse_agent_reply(self.brain.complete(messages))
        return reply or "嗯嗯，我在听。"

    def _chat_llm_tools(self, user_text, on_delta):
        """聊天路径：带工具调用的 LLM 对话（搜索 / bash 等，最多 4 轮）。

        流式模式（on_delta 且 stream 配置开启）下，每轮模型 content 逐块推送，
        工具执行阶段插入 🔧 状态行；接口不支持工具时回退普通流式。
        """
        system, messages = self._build_chat_messages(user_text)
        decls = tools.tool_declarations(self.cfg)
        use_stream = bool(on_delta) and self.cfg.get("stream", True)
        shown = ""        # 已推送给 UI 的可见文本（不含 [FACT]/[THINK]）
        pending_note = ""  # 工具执行状态行，追加在流式文本之后
        for _ in range(4):
            try:
                if use_stream:
                    def cb(raw):
                        nonlocal shown
                        shown = self._display_stream_text(raw)
                        on_delta(shown + pending_note)

                    content, tool_calls = self.brain.complete_tools_stream(
                        messages, decls, cb
                    )
                else:
                    content, tool_calls = self.brain.complete_tools(messages, decls)
            except Exception:
                # 接口不支持工具调用时退回普通流式
                return self._chat_llm_stream(user_text, on_delta)
            if not tool_calls:
                reply = self._parse_agent_reply(content or "")
                if not reply:
                    reply = "嗯嗯，我在听。"
                if not use_stream and on_delta:
                    on_delta(reply)
                return reply
            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                arguments = function.get("arguments", "")
                if use_stream:
                    pending_note = "\n🔧 " + tools.human_brief(name, arguments)
                    on_delta(shown + pending_note)
                result = self._run_tool(name, arguments, source=tools.SOURCE_USER)
                if self.stats:
                    self.stats.record_tool()
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                })
        return self._chat_llm(user_text)

    def _chat_llm_stream(self, user_text, on_delta):
        system, messages = self._build_chat_messages(user_text)
        parts = []

        def handle(delta):
            parts.append(delta)
            on_delta(self._display_stream_text("".join(parts)))

        try:
            self.brain.complete_stream(messages, handle)
        except Exception:
            # 服务端不支持流式时退回一次性调用
            reply = self._chat_llm(user_text)
            if on_delta:
                on_delta(reply)
            return reply
        reply = self._parse_agent_reply("".join(parts))
        if not reply:
            reply = "嗯嗯，我在听。"
            on_delta(reply)
        return reply

    def _build_chat_messages(self, user_text):
        relevant = self._relevant_memories(user_text, 5)
        recent = [m for m in self.chat_history[-8:] if m["role"] in ("user", "assistant")]
        system = (
            core.build_persona(self.cfg, mood=self.state.get("mood"))
            + "\n\n"
            "你记得关于主人的事："
            + self._format_memories(relevant)
            + "。"
            "规则：像朋友一样自然聊天，一般不超过80字，不要用列表和标题。"
            "如果主人说了值得记住的事，在回复末尾另起一行写 [FACT] 简短描述。"
            "如果你想私下记下自己的念头，另起一行写 [THINK] 一行。"
            "[FACT] 和 [THINK] 这两行不会显示给主人。"
        )
        messages = [{"role": "system", "content": system}]
        messages += [{"role": m["role"], "content": m["text"]} for m in recent]
        messages.append({"role": "user", "content": user_text})
        return system, messages

    @staticmethod
    def _display_stream_text(raw):
        """流式展示时隐藏 [FACT]/[THINK] 指令行。"""
        lines = raw.splitlines()
        visible = [
            line
            for line in lines
            if not (line.startswith("[FACT]") or line.startswith("[THINK]"))
        ]
        return "\n".join(visible)

    def _chat_rules(self, user_text):
        text = user_text.lower()
        if any(k in text for k in ("记得", "说过什么", "还记得", "我之前说了")):
            facts = self.memory.facts()
            if facts:
                return "你跟我说过：" + "；".join(i["text"] for i in facts)
            return "我还没记住什么重要的事，你可以多跟我聊聊。"
        if any(k in text for k in ("你是谁", "介绍你", "自我介绍", "你是什么", "介绍一下")):
            return self._intro_rules()
        return self.brain.chat(text)

    def _intro_rules(self):
        """规则模式自我介绍：按当前情绪选不同说法，不念设定。"""
        mood = self.state.get("mood", "平静")
        name = self.cfg.get("pet_name", "小跳")
        role = self.cfg.get("role", "小宠物")
        if mood == "开心":
            pool = [
                f"我是{name}呀，你电脑里的小{role}，今天心情超好～",
                f"嘿嘿，{role}一只，住在你电脑里，天天偷看你工作。",
            ]
        elif mood in ("有点蔫", "困了"):
            pool = [
                f"……我是{name}，你电脑里的小{role}。",
                f"我是{role}……有点困。",
            ]
        else:
            pool = [
                f"我是{name}，你电脑里的小{role}。",
                f"我？住在你电脑里的小家伙，{role}一只。",
            ]
        return random.choice(pool)

    # ---------- 自主思考 ----------

    def think(self, ctx):
        now = self.clock()
        if self.stats:
            self.stats.record_tick()
        self._update_mood(ctx)
        # 记忆补采（含安静时段，不打扰主人）：把水位线之后的主人对白扫一遍
        self._extract_facts_watermark()
        if self._is_quiet(now):
            return None
        if self.cfg["api"]["api_key"]:
            # LLM 模式：先过纯规则触发门控，有料才花钱调模型
            level, reason = self._trigger_gate(ctx, now)
            if level == "silent":
                return None
            message = self._think_llm(ctx, trigger=reason)
        else:
            message = self._think_rules(ctx, now)
        if message and self.stats:
            self.stats.record_proactive()
        return message

    # ---- 触发门控（LLM 模式的“要不要花钱问模型”纯规则决策） ----

    LLM_DAILY_BUDGET = 40  # LLM 主动发言每日预算（用户明示不吝调用，仍防刷屏）
    WEATHER_CLASSES = ("雨", "雪", "晴", "阴", "多云")

    def _weather_class(self, ctx):
        """把天气描述归为粗类别：雨/雪/晴/阴/多云/空（用于突变检测）。"""
        for coll in ctx.get("collections", []):
            if coll["plugin"] != "weather" or not coll["entries"]:
                continue
            data = coll["entries"][0].get("data") or {}
            desc = str(data.get("desc") or coll["entries"][0].get("text") or "")
            for key in ("雨", "雪"):
                if key in desc:
                    return key
            for key in ("晴",):
                if key in desc:
                    return key
            if "阴" in desc:
                return "阴"
            if "云" in desc:
                return "多云"
        return ""

    def _fresh_collections(self, ctx):
        """自上次巡视以来内容有变化的采集项（新闻/名言等，天气除外）。"""
        return [
            c for c in ctx.get("collections", [])
            if c.get("cache_hit") is False and c["entries"]
            and c["plugin"] != "weather"
        ]

    def _profile_hint(self, limit=1):
        """画像里最近一条值得跟进的偏好/习惯（用于记忆回响触发）。"""
        groups = self.db.memory_profile(limit_per=limit, roles=("fact",))
        for group in groups:
            if group["category"] in ("preference", "habit", "identity"):
                for item in group["items"]:
                    text = item["text"][:40]
                    if text:
                        return text
        return None

    def _trigger_gate(self, ctx, now):
        """纯规则触发门控：返回 (level, reason)。

        T0 紧急（不计预算）：晨间简报 / 日程临近 / 天气突变
        T1 有料（计入预算）：内容源有新信息 / 画像记忆回响
        T2 自主（计入预算）：冷却通过 + 概率
        T3 静默：不调 LLM
        """
        today = now.strftime("%Y-%m-%d")

        # 天气类别快照：每次巡视更新，突变才触发
        wc = self._weather_class(ctx)
        prev_wc = self.state.get("last_weather_class", "")
        self.state["last_weather_class"] = wc
        self._save_state()

        # T0 晨间简报（每天一次，8-12 点）
        if 8 <= now.hour < 12 and self.state.get("last_brief_date") != today:
            self.state["last_brief_date"] = today
            self._save_state()
            return "brief", "新的一天，给主人一句简短的晨间问候，可以提今天的天气或日程。"

        # T0 日程临近（每天最多一次）
        schedule = self.db.memory_schedule_due(within_hours=12)
        if schedule and self.state.get("last_schedule_remind_date") != today:
            self.state["last_schedule_remind_date"] = today
            self._save_state()
            return "schedule", f"主人之前说{schedule[-1]['text']}，时间快到了，可以提醒一下。"

        # T0 天气突变（类别变化）
        if wc and wc != prev_wc:
            return "weather", f"天气变成了{wc}，值得说的可以跟主人提一句。"

        # 预算：T1/T2 共用，耗尽即静默
        if self.state.get("llm_budget_date") != today:
            self.state["llm_budget_date"] = today
            self.state["llm_budget_count"] = 0
            self._save_state()
        if int(self.state.get("llm_budget_count", 0)) >= self.LLM_DAILY_BUDGET:
            return "silent", ""

        def _spend():
            self.state["llm_budget_count"] = int(self.state.get("llm_budget_count", 0)) + 1
            self._save_state()

        # T1 内容源有新信息（跨源汇聚：去重 + 优先级 + top_k）
        merged, self.state["merge_seen"] = core.merge_entries(
            ctx.get("collections", []),
            self.state.get("merge_seen", {}),
            top_k=2,
        )
        if merged:
            _spend()
            return (
                "news",
                "看到几条新信息："
                + "；".join(t[:40] for t in merged)[:120],
            )

        # T1 画像记忆回响（每 12h 最多一次：想起主人说过的事）
        if now.timestamp() - self.state.get("last_echo_ts", 0.0) >= 12 * 3600:
            top = self._profile_hint()
            if top:
                self.state["last_echo_ts"] = now.timestamp()
                self._save_state()
                _spend()
                return "echo", f"想起主人之前说过：{top}，可以关心一下进展。"

        # T2 冷却 + 概率自主（更积极：60% 机会主动探索）
        if not self._cooldown_ok(now) or random.random() >= 0.6:
            return "silent", ""
        _spend()
        return "curious", "自主巡视：有新鲜有趣的事可以分享，没有就 SILENT。"

    def _think_llm(self, ctx, trigger=None):
        if not self.cfg.get("tools_enabled", True):
            return self._think_llm_simple(ctx, trigger=trigger)
        context = self.brain._context_text(ctx)
        system = (
            core.build_persona(self.cfg, mood=self.state.get("mood"))
            + "\n\n"
            "你会每隔一段时间主动巡视，思考要不要跟主人说话。\n"
            "值得说话的情况（按优先级）：\n"
            "1) 主人的日程临近（考试/会议/出差/体检等）——主动提醒\n"
            "2) 主人关心的话题有值得说的新信息（可用工具搜索确认）\n"
            "3) 天气/环境有明显变化\n"
            "4) 想起主人说过的事，想跟进问问\n"
            "5) 有新鲜有趣的事想分享\n"
            + (
                "\n【本次巡视触发】" + trigger + "\n"
                if trigger
                else "\n【本次巡视触发】自由巡视\n"
            )
            + "【主人画像】\n"
            + self._build_memory_profile()
            + "\n\n【时间感知】\n"
            + self._build_time_context()
            + "\n\n【最近对话】\n"
            + (self._build_recent_thread() or "（暂无）")
            + "\n\n【周围信息】\n"
            + (context or "（暂无采集到信息）")
            + "\n\n"
            "如果确实没什么值得说的，只输出 SILENT，不要硬聊。\n"
            "如果你对主人有了新观察（比如他最近常在忙什么），另起一行写 [OBSERVE] 一句，不会显示给主人。\n"
            "你也可以另起一行写 [THINK] 记下自己的想法。\n"
            "输出要说的话不超过60字，自然口语，不要列表和标题。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "（巡视中）请决定是否要主动说话。"},
        ]
        for _ in range(4):
            try:
                content, tool_calls = self.brain.complete_tools(
                    messages, tools.tool_declarations(self.cfg)
                )
            except Exception:
                # 接口不支持工具调用时退回普通模式
                return self._think_llm_simple(ctx)
            if not tool_calls:
                return self._parse_think_reply(content)
            messages.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                function = call.get("function") or {}
                try:
                    result = self._run_tool(
                        function.get("name", ""),
                        function.get("arguments", ""),
                        source=tools.SOURCE_AUTO,
                    )
                except Exception as exc:
                    result = f"工具执行失败：{exc}"
                if self.stats:
                    self.stats.record_tool()
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                })
        return None

    def _think_llm_simple(self, ctx, trigger=None):
        context = self.brain._context_text(ctx)
        system = (
            core.build_persona(self.cfg, mood=self.state.get("mood"))
            + "\n\n"
            "你会每隔一段时间主动巡视，思考要不要跟主人说话。\n"
            "值得说话的情况（按优先级）：\n"
            "1) 主人的日程临近（考试/会议/出差/体检等）——主动提醒\n"
            "2) 主人关心的话题有值得说的新信息\n"
            "3) 天气/环境有明显变化\n"
            "4) 想起主人说过的事，想跟进问问\n"
            "5) 有新鲜有趣的事想分享\n"
            + (
                "\n【本次巡视触发】" + trigger + "\n"
                if trigger
                else "\n【本次巡视触发】自由巡视\n"
            )
            + "【主人画像】\n"
            + self._build_memory_profile()
            + "\n\n【时间感知】\n"
            + self._build_time_context()
            + "\n\n【最近对话】\n"
            + (self._build_recent_thread() or "（暂无）")
            + "\n\n【周围信息】\n"
            + (context or "（暂无采集到信息）")
            + "\n\n"
            "如果确实没什么值得说的，只输出 SILENT，不要硬聊。\n"
            "如果你对主人有了新观察，另起一行写 [OBSERVE] 一句，不会显示给主人。\n"
            "你也可以另起一行写 [THINK] 记下自己的想法。\n"
            "输出要说的话不超过60字，自然口语，不要列表和标题。"
        )
        reply = self._parse_agent_reply(self.brain.complete([
            {"role": "system", "content": system},
            {"role": "user", "content": "（巡视中）请决定是否要主动说话。"},
        ]))
        return self._parse_think_reply(reply)

    def _parse_think_reply(self, reply):
        """解析自主思考回复：先提取 [THINK]/[FACT]/[OBSERVE] 指令行，
        剩余正文判断 SILENT 是否发言。"""
        reply = self._parse_agent_reply(reply or "")
        reply = reply.strip()
        if not reply:
            return None
        if re.fullmatch(r"SILENT[.。!！]?", reply, re.IGNORECASE):
            return None
        if "SILENT" in reply.upper():
            return None
        return reply

    def _run_tool(self, name, arguments, source=tools.SOURCE_AUTO):
        """执行工具调用（含安全分级 / 用户确认 / 审计）。"""
        mode = self.cfg.get("shell_tools_mode", tools.SHELL_MODE_CONFIRM)
        if mode not in tools.SHELL_MODES:
            mode = tools.SHELL_MODE_CONFIRM
        return tools.execute(
            name,
            arguments,
            mode=mode,
            source=source,
            confirm_cb=self.tool_confirm_cb,
            cwd=tools.resolve_workdir(self.cfg),
            audit=self._audit_tool,
        )

    def _audit_tool(self, source, tool, detail, mode, approved, ok, summary):
        try:
            self.db.log_tool(source, tool, detail, mode, approved, ok, summary)
        except Exception:
            pass
        # 旁路事件通知（不阻塞工具循环）：审计日志 / 统计 / UI 各自订阅。
        # eventbus 由宿主注入（HeartBeatApp），测试/CLI 可为 None。
        bus = getattr(self, "eventbus", None)
        if bus is not None:
            bus.emit(
                "tool.executed",
                {
                    "source": source,
                    "tool": tool,
                    "detail": detail,
                    "mode": mode,
                    "approved": approved,
                    "ok": ok,
                    "summary": summary,
                },
            )

    def _think_rules(self, ctx, now):
        """规则模式发言决策（委托 brain.planner）。"""
        return self.planner.rules_think(ctx, now)

    def _parse_schedule_expiry(self, text, now=None):
        """日程到期解析（委托 brain.memory）。"""
        return self.memory_module.parse_schedule_expiry(text, now)

    def _build_time_context(self, now=None):
        """时间感知（委托 brain.planner）。"""
        return self.planner.build_time_context(now)

    def _build_memory_profile(self):
        """记忆画像（委托 brain.memory）。"""
        return self.memory_module.profile()

    # ---------- 兴趣话题（topic_watch 来源） ----------

    def patrol_topics(self, force=False):
        """巡视用兴趣话题（委托 brain.planner）：手动 topics > 自动提取缓存。"""
        return self.planner.patrol_topics(force)

    def _extract_topics(self, max_topics=8):
        """话题关键词提取（委托 brain.planner）。"""
        return self.planner.extract_topics(max_topics)

    def _build_recent_thread(self, n=3):
        """最近对话脉络（委托 brain.planner）。"""
        return self.planner.build_recent_thread(n)

    def _pick_search_topic(self, ctx):
        """自主搜索话题选择（委托 brain.planner）。"""
        return self.planner.pick_search_topic(ctx)

    def _plugin_messages(self, ctx):
        """插件建议（委托 brain.planner）。"""
        yield from self.planner.plugin_messages(ctx)

    def _greeting(self, now):
        """每日问候（委托 brain.planner）。"""
        return self.planner.greeting(now)

    def _memory_followup(self, now):
        """记忆跟进（委托 brain.memory）。"""
        return self.memory_module.followup_candidate(now)

    def _cooldown_ok(self, now):
        """发言冷却检查（委托 brain.planner）。"""
        return self.planner.cooldown_ok(now)

    PROACTIVE_DAILY_BUDGET = Planner.PROACTIVE_DAILY_BUDGET  # 规则模式每日主动发言上限（防话痨）

    def _proactive_budget_ok(self, now):
        """每日主动发言预算检查（委托 brain.planner）。"""
        return self.planner.proactive_budget_ok(now)

    def _mark_proactive(self, now):
        """记录一次主动发言（委托 brain.planner）。"""
        self.planner.mark_proactive(now)

    def _is_quiet(self, now):
        """安静时段检查（委托 brain.planner）。"""
        return self.planner.is_quiet(now)

    def _update_mood(self, ctx):
        """心情更新（委托 brain.planner）。"""
        self.planner.update_mood(ctx)

    def _maybe_save_thought(self, ctx):
        """随机记录想法（委托 brain.planner）。"""
        self.planner.maybe_save_thought(ctx)

    # ---------- 记忆与向量 ----------

    def _remember(self, role, text, category="misc", importance=3, source="chat",
                  expires_at=None):
        """记忆写入（委托 brain.memory.MemoryModule）。"""
        return self.memory_module.remember(
            role, text, category=category, importance=importance,
            source=source, expires_at=expires_at,
        )

    def _embed_chat(self, item_id, text):
        """聊天向量索引（委托 brain.memory）。"""
        self.memory_module.embed_chat(item_id, text)

    def _relevant_memories(self, query, k=5):
        """向量检索相关记忆（委托 brain.memory）。"""
        return self.memory_module.relevant(query, k)

    def _format_memories(self, items):
        """记忆格式化（委托 brain.memory）。"""
        return self.memory_module.format_memories(items)

    # ---------- 工具 ----------

    def _parse_agent_reply(self, raw):
        """拆出 [FACT]/[THINK] 指令行，其余是给主人看的话。"""
        if not raw:
            return ""
        body = []
        for line in raw.splitlines():
            text = line.strip()
            if text.startswith("[FACT]"):
                fact = text[6:].strip()
                if fact and fact not in [i["text"] for i in self.memory.facts()]:
                    self._remember("fact", fact, importance=4)
                    if self.stats:
                        self.stats.record_fact()
            elif text.startswith("[FACT:"):
                # [FACT:category] 结构化事实（LLM 可自行分类，如 [FACT:schedule]）
                category = text[6:text.find("]")].strip()
                fact = text[text.find("]") + 1:].strip()
                if category and fact and fact not in [i["text"] for i in self.memory.facts()]:
                    self._remember(
                        "fact",
                        fact,
                        category=category,
                        importance=4,
                        expires_at=self._parse_schedule_expiry(fact),
                    )
                    if self.stats:
                        self.stats.record_fact()
            elif text.startswith("[OBSERVE]"):
                # 巡视时的观察记录（低重要性，不显示给主人）
                observe = text[9:].strip()
                if observe:
                    self._remember("thought", observe, importance=2, source="observation")
                    if self.stats:
                        self.stats.record_thought()
            elif text.startswith("[THINK]"):
                thought = text[7:].strip()
                if thought:
                    self._remember("thought", thought)
                    if self.stats:
                        self.stats.record_thought()
            else:
                body.append(line)
        return "\n".join(body).strip()

    def _extract_facts_watermark(self, limit=100):
        """巡视补采水位线之后的主人对白（委托 brain.memory）。"""
        self.memory_module.extract_facts_watermark(limit)

    def _extract_facts_rule(self, user_text):
        """规则事实提取（委托 brain.memory）。"""
        self.memory_module.extract_facts(user_text)
