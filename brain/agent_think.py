"""brain.agent_think：Agent 自主思考链路（ThinkMixin）。

从 brain/agent.py 拆出（阶段2 包化：单文件 LLM 可重写粒度 ≤500 行）。
本文件是 Agent 的混入：巡视入口 / 触发门控（纯规则决策要不要花钱问
LLM）/ LLM 巡视（含工具调用）/ 工具执行与审计。

约束（与 agent.py 一致）：
- 不 import kernel（依赖方向红线）；自进化已移除（2026-08-13），无版本管理
- 共享状态经 self（Agent 主类实例）访问
"""

import random
import re
import time

import core
import db
import search
import tools
from brain import context as context_mgr


class ThinkMixin:
    """自主思考混入：Agent 主类继承本类获得巡视/思考能力。"""

    # ---------- 生活循环（替代巡视） ----------

    _LIFE_ACTIONS = {
        "search": "web",
        "news": "news",
        "stock": "stock",
        "weather": "weather",
        "wiki": "wiki",
    }

    @staticmethod
    def _looks_complete(text):
        """一句话是否以中文/英文句末标点收尾（防止把截断的半句发给用户）。"""
        return bool(
            re.search(r"[。！？!?…~～]+[\"”』】\)]?$", str(text or "").strip())
        )

    def live(self, ctx):
        """生活循环：睡眠/体力检查 → 唤醒 → 内心思考 → 欲望行动 → 角色化说话。"""
        now = self.clock()
        if self.stats:
            self.stats.record_tick()
        try:
            self.memory_module.cleanup(now=now.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
        self._update_mood(ctx)
        self._extract_facts_watermark()

        if self._is_quiet(now):
            self.state["sleep_mode"] = True
            self._save_state()
            self._log_activity("sleep", "进入睡眠，不主动消耗体力")
            return None
        self.state["sleep_mode"] = False
        self._save_state()

        if not (self.cfg.get("api") or {}).get("api_key"):
            # 无 LLM 时继续走规则发言（问候/日程/天气等），不涉及体力
            return self._think_rules(ctx, now)

        if not self._proactive_energy_ok(now):
            self._log_activity("rest", "体力不足，保持安静")
            return None

        wake = self._wake_greeting(now)
        if wake:
            self._log_activity("wake", wake, energy=1)
            if self.stats:
                self.stats.record_proactive()
            return wake

        plan = self._inner_thought(ctx, now)
        if not plan:
            self._log_activity("think", "内心思考后选择安静", energy=1)
            return None
        if plan.get("type") == "speak":
            self._log_activity("speak", plan.get("text", ""), energy=1)
            if self.stats:
                self.stats.record_proactive()
            return plan.get("text")
        if plan.get("type") == "think":
            text = plan.get("text", "")
            self._remember("thought", text, source="self_thought")
            self._record_desire(text, status="thought")
            self._log_activity("think", text, energy=1)
            return None
        if plan.get("type") == "action":
            self._record_desire(
                f"想查{plan.get('query', '')}", status="done"
            )
            message = self._do_life_action(plan, now)
            if message and self.stats:
                self.stats.record_proactive()
            return message
        return None

    def _record_desire(self, text, status="active"):
        """把内心思考产生的欲望/计划记进状态（cap 10 条）。"""
        desires = list(self.state.get("desires") or [])
        desires.append({
            "id": f"{int(time.time() * 1000)}-{len(desires)}",
            "text": str(text or "")[:120],
            "status": status,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        self.state["desires"] = desires[-10:]
        self._save_state()

    def _wake_greeting(self, now):
        """每天醒来一次的角色化主动问候，不是新闻汇报。"""
        if not self.cfg.get("wake_greeting_enabled", True):
            return None
        day = self._energy_day(now)
        if self.state.get("last_wake_date") == day:
            return None
        if self._energy_remaining(now) < 1:
            return None
        owner = core.owner_title(self.cfg)
        system = core.build_persona(self.cfg) + (
            "\n\n你现在刚睡醒。像真正的角色一样主动跟"
            + owner
            + "打招呼，可以有情绪、有语气，但不要汇报新闻、不要列清单，不超过40字。"
        )
        try:
            raw = self.brain.complete(
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": (
                            f"现在时间：{now.strftime('%Y-%m-%d %H:%M')}；"
                            f"当前情绪：{self.state.get('mood', '平静')}。"
                            "说一句主动唤醒的话。"
                        ),
                    },
                ],
                max_tokens=200,
            ) or ""
            reply = self._parse_agent_reply(raw).strip()
        except Exception:
            reply = ""
        if reply and not self._looks_complete(reply):
            reply = ""
        if not reply or "SILENT" in reply.upper():
            reply = self.planner.greeting(now) or ""
        reply = reply.strip()
        if reply:
            self.state["last_wake_date"] = day
            self._save_state()
        return reply or None

    def greet(self):
        """用户主动打招呼/摸摸头时，用角色语气回应一句（计入体力）。"""
        owner = core.owner_title(self.cfg)
        system = core.build_persona(self.cfg) + (
            "\n\n用户刚刚主动跟你打招呼/摸摸头。用你的角色语气回应一句，"
            "要有情绪、像活物，不要汇报新闻，不超过40字。"
        )
        try:
            raw = self.brain.complete(
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": (
                            f"现在时间：{time.strftime('%Y-%m-%d %H:%M')}；"
                            f"当前情绪：{self.state.get('mood', '平静')}。"
                            f"回应{owner}的招呼。"
                        ),
                    },
                ],
                max_tokens=200,
            ) or ""
            reply = self._parse_agent_reply(raw).strip()
        except Exception:
            reply = ""
        if reply and not self._looks_complete(reply):
            reply = ""
        return reply or "我在呀～想我了？"

    def _inner_thought(self, ctx, now):
        """内心思考：输出想说的话 / 想做的事 / 私下的想法 / 保持安静。"""
        owner = core.owner_title(self.cfg)
        profile = self.memory_module.profile()
        recent = self._build_recent_thread()
        context = self.brain._context_text(ctx)
        desires = self.state.get("desires") or []
        desire_text = "；".join(
            f"{d.get('id', '?')}:{d.get('text', '')}"
            for d in desires[-5:]
        ) or "（暂无）"
        system = (
            core.build_persona(self.cfg)
            + "\n\n你是一个有生命的角色，有自己的精力、情绪和想做的事。"
            "你会主动思考，而不是机械巡视。\n"
            "只输出以下格式之一：\n"
            "SPEAK 想对"
            + owner
            + "说的一句话\n"
            "ACTION <search|news|stock|weather|wiki> 想查的关键词\n"
            "THINK 私下的想法\n"
            "SILENT\n"
            "不要说废话，不要复述设定。"
        )
        user = (
            f"[当前状态] 时间：{now.strftime('%Y-%m-%d %H:%M')}；"
            f"情绪：{self.state.get('mood', '平静')}；"
            f"剩余体力：{self._energy_remaining(now)}。\n"
            f"[用户画像]\n{profile}\n"
            f"[最近对话]\n{recent or '（暂无）'}\n"
            f"[周围信息]\n{context or '（暂无）'}\n"
            f"[我的欲望]\n{desire_text}\n\n"
            "现在你在主动生活：想做什么、想说什么、还是保持安静？"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        max_tokens = int(self.cfg.get("max_context_tokens", 400000) or 400000)
        ratio = float(self.cfg.get("context_compress_ratio", 0.75) or 0.75)
        messages, _ = context_mgr.truncate_messages(
            messages, int(max_tokens * ratio), keep_recent=10
        )
        try:
            # 输出很短但推理模型会把隐藏推理计入预算：初始给足，
            # 仍触发空内容时由 brain.complete 的 finish=length 重试兑底
            raw = self.brain.complete(messages, max_tokens=2000) or ""
        except Exception:
            return None
        plan = self._parse_life_reply(raw)
        if plan and plan.get("type") == "speak":
            text = str(plan.get("text") or "").strip()
            if not self._looks_complete(text):
                # 输出被截断成半句话：宁可用完整问候，也不把残句发给主人
                plan["text"] = self.planner.greeting(now) or text
        return plan

    def _parse_life_reply(self, raw):
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper == "SILENT":
                continue
            if upper.startswith("SPEAK"):
                text = line[5:].strip()
                if text:
                    return {"type": "speak", "text": text}
            if upper.startswith("THINK"):
                text = line[5:].strip()
                if text:
                    return {"type": "think", "text": text}
            if upper.startswith("ACTION"):
                rest = line[6:].strip()
                parts = rest.split(None, 1)
                tool = parts[0].lower() if parts else ""
                query = parts[1].strip() if len(parts) > 1 else ""
                if tool in self._LIFE_ACTIONS and query:
                    return {"type": "action", "tool": tool, "query": query}
        return None

    def _do_life_action(self, plan, now):
        """执行一个低打扰的主动行动：查完记想法，只有值得说才角色化说话。"""
        tool = plan.get("tool", "")
        query = plan.get("query", "")
        category = self._LIFE_ACTIONS.get(tool, "web")
        try:
            entries = search.search_all(query, category, 4)
        except Exception as exc:
            self._log_activity("action", f"{tool} {query} 失败：{exc}")
            return None
        if not entries:
            self._remember(
                "thought", f"我查了{query}，暂时没有新结果。", source="self_action"
            )
            self._log_activity("action", f"{tool} {query} 无结果")
            return None
        title = str(entries[0].get("title") or entries[0].get("text") or "")[:80]
        self._remember(
            "thought", f"我自己查了{query}：{title}", source="self_action"
        )
        self._log_activity("action", f"{tool} {query} -> {title}")
        return self._phrase_action_result(query, title)

    def _phrase_action_result(self, query, title):
        """把主动查到的结果用角色语气说成一句话，而不是新闻播报。"""
        owner = core.owner_title(self.cfg)
        system = core.build_persona(self.cfg) + (
            "\n\n你刚刚主动查了一个东西。用你的角色语气，像随口跟"
            + owner
            + "分享一样说一句话，不超过50字，不要用列表、标题或“报告”口吻。"
        )
        try:
            raw = self.brain.complete(
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": f"我查了：{query}\n结果：{title}\n说一句话。",
                    },
                ],
                max_tokens=200,
            ) or ""
            reply = self._parse_agent_reply(raw).strip()
        except Exception:
            reply = ""
        if reply and not self._looks_complete(reply):
            reply = ""
        return reply or f"我刚好去看了看{query}，{title}。"

    # ---------- 自主思考 ----------

    def think(self, ctx):
        now = self.clock()
        if self.stats:
            self.stats.record_tick()
        try:
            self.memory_module.cleanup(now=now.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
        self._update_mood(ctx)
        # 记忆补采（含安静时段，不打扰用户）：把水位线之后的对白扫一遍
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
        owner = core.owner_title(self.cfg)

        # 天气类别快照：每次巡视更新，突变才触发
        wc = self._weather_class(ctx)
        prev_wc = self.state.get("last_weather_class", "")
        self.state["last_weather_class"] = wc
        self._save_state()

        # T0 晨间简报（每天一次，8-12 点）
        if 8 <= now.hour < 12 and self.state.get("last_brief_date") != today:
            self.state["last_brief_date"] = today
            self._save_state()
            return "brief", f"新的一天，给{owner}一句简短的晨间问候，可以提今天的天气或日程。"

        # T0 日程临近（每天最多一次）
        schedule = self.db.memory_schedule_due(within_hours=12)
        if schedule and self.state.get("last_schedule_remind_date") != today:
            self.state["last_schedule_remind_date"] = today
            self._save_state()
            return "schedule", f"{owner}之前说{schedule[-1]['text']}，时间快到了，可以提醒一下。"

        # T0 天气突变（类别变化）
        if wc and wc != prev_wc:
            return "weather", f"天气变成了{wc}，值得说的可以跟{owner}提一句。"

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

        # T1 画像记忆回响（每 12h 最多一次：想起用户说过的事）
        if now.timestamp() - self.state.get("last_echo_ts", 0.0) >= 12 * 3600:
            top = self._profile_hint()
            if top:
                self.state["last_echo_ts"] = now.timestamp()
                self._save_state()
                _spend()
                return "echo", f"想起{owner}之前说过：{top}，可以关心一下进展。"

        # T2 冷却 + 概率自主（更积极：60% 机会主动探索）
        if not self._cooldown_ok(now) or random.random() >= 0.6:
            return "silent", ""
        _spend()
        return "curious", "自主巡视：有新鲜有趣的事可以分享，没有就 SILENT。"

    def _think_llm(self, ctx, trigger=None):
        if not self.cfg.get("tools_enabled", True):
            return self._think_llm_simple(ctx, trigger=trigger)
        context = self.brain._context_text(ctx)
        owner = core.owner_title(self.cfg)
        owner_section = "【你的画像】" if owner == "你" else f"【{owner}画像】"
        system = (
            core.build_persona(self.cfg, mood=self.state.get("mood"))
            + "\n\n"
            f"你会每隔一段时间主动巡视，思考要不要跟{owner}说话。\n"
            "值得说话的情况（按优先级）：\n"
            f"1) {owner}的日程临近（考试/会议/出差/体检等）——主动提醒\n"
            f"2) {owner}关心的话题有值得说的新信息（可用工具搜索确认）\n"
            "3) 天气/环境有明显变化\n"
            f"4) 想起{owner}说过的事，想跟进问问\n"
            "5) 有新鲜有趣的事想分享\n"
            + (
                "\n【本次巡视触发】" + trigger + "\n"
                if trigger
                else "\n【本次巡视触发】自由巡视\n"
            )
            + owner_section + "\n"
            + self._build_memory_profile()
            + "\n\n【时间感知】\n"
            + self._build_time_context()
            + "\n\n【最近对话】\n"
            + (self._build_recent_thread() or "（暂无）")
            + "\n\n【周围信息】\n"
            + (context or "（暂无采集到信息）")
            + "\n\n"
            "如果确实没什么值得说的，只输出 SILENT，不要硬聊。\n"
            f"如果你对{owner}有了新观察（比如他最近常在忙什么），另起一行写 [OBSERVE] 一句，不会显示给{owner}。\n"
            "你也可以另起一行写 [THINK] 记下自己的想法。\n"
            "输出要说的话不超过60字，自然口语，不要列表和标题。\n"
            "优先说对用户有用的话；使用工具前想清楚是否必要，限流或失败不要反复重试。"
        )
        system += self._skill_section(patrol=True)
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
                    "content": self._trim_tool_result(result),
                })
            messages = self._compact_tool_rounds(messages)
        return None

    def _think_llm_simple(self, ctx, trigger=None):
        context = self.brain._context_text(ctx)
        owner = core.owner_title(self.cfg)
        owner_section = "【你的画像】" if owner == "你" else f"【{owner}画像】"
        system = (
            core.build_persona(self.cfg, mood=self.state.get("mood"))
            + "\n\n"
            f"你会每隔一段时间主动巡视，思考要不要跟{owner}说话。\n"
            "值得说话的情况（按优先级）：\n"
            f"1) {owner}的日程临近（考试/会议/出差/体检等）——主动提醒\n"
            f"2) {owner}关心的话题有值得说的新信息\n"
            "3) 天气/环境有明显变化\n"
            f"4) 想起{owner}说过的事，想跟进问问\n"
            "5) 有新鲜有趣的事想分享\n"
            + (
                "\n【本次巡视触发】" + trigger + "\n"
                if trigger
                else "\n【本次巡视触发】自由巡视\n"
            )
            + owner_section + "\n"
            + self._build_memory_profile()
            + "\n\n【时间感知】\n"
            + self._build_time_context()
            + "\n\n【最近对话】\n"
            + (self._build_recent_thread() or "（暂无）")
            + "\n\n【周围信息】\n"
            + (context or "（暂无采集到信息）")
            + "\n\n"
            "如果确实没什么值得说的，只输出 SILENT，不要硬聊。\n"
            f"如果你对{owner}有了新观察，另起一行写 [OBSERVE] 一句，不会显示给{owner}。\n"
            "你也可以另起一行写 [THINK] 记下自己的想法。\n"
            "输出要说的话不超过60字，自然口语，不要列表和标题。\n"
            "优先说对用户有用的话，避免空泛寒暄。"
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

    # ---------- 工具执行 ----------

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
            project_dir=self.cfg.get("project_dir", ""),
        )

    def _audit_tool(self, source, tool, detail, mode, approved, ok, summary):
        try:
            self.db.log_tool(
                source, tool,
                tools.redact_secrets(str(detail)),
                mode, approved, ok,
                tools.redact_secrets(str(summary)),
            )
        except Exception:
            pass
        # P1 事件时间线：tool.called（与 tool_logs 并存，按 trace_id 聚合调试）
        try:
            self.db.log_event(
                db.EventType.TOOL_CALLED, "brain.tool",
                {"tool": tool, "source": source, "ok": ok, "approved": approved},
                getattr(self, "_trace_id", ""),
            )
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
