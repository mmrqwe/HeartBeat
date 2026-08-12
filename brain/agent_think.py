"""brain.agent_think：Agent 自主思考链路（ThinkMixin）。

从 brain/agent.py 拆出（阶段2 包化：单文件 LLM 可重写粒度 ≤500 行）。
本文件是 Agent 的混入：巡视入口 / 触发门控（纯规则决策要不要花钱问
LLM）/ LLM 巡视（含工具调用）/ 工具执行与审计。

约束（与 agent.py 一致）：
- 不 import kernel（依赖方向红线），经 self.brain_loader 访问版本管理
- 共享状态经 self（Agent 主类实例）访问
"""

import random
import re

import core
import tools


class ThinkMixin:
    """自主思考混入：Agent 主类继承本类获得巡视/思考能力。"""

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
