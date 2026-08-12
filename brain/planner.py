"""brain.planner：规则决策原语模块（Agent 组合使用）。

自进化约定（阶段 4 updater）：本模块是允许 AI 替换升级的独立模块——
只要保持公开方法签名（rules_think / greeting / cooldown_ok /
proactive_budget_ok / mark_proactive / is_quiet / update_mood /
plugin_messages / pick_search_topic / patrol_topics / maybe_save_thought /
build_time_context / build_recent_thread），Kernel 即可切换实现。

设计：以组合方式访问 Agent 共享状态（self.agent.state/db/cfg/clock），
不 import agent 模块（避免循环依赖）。
"""

import random

import search
from brain.llm import owner_title

CURIOSITY_QUESTIONS = [
    "你今天过得怎么样？",
    "有什么新鲜事想跟我分享吗？",
    "要不要我帮你查点什么？",
    "今天有什么计划吗？",
]

PROACTIVE_DAILY_BUDGET = 15  # 规则模式每日主动发言上限（防话痨）


class Planner:
    """规则决策：主动发言规划（问候/预算/冷却/安静时段/心情/规则发言）。"""

    # 类属性别名（模块级 PROACTIVE_DAILY_BUDGET 供方法直接引用）
    PROACTIVE_DAILY_BUDGET = PROACTIVE_DAILY_BUDGET

    def __init__(self, agent):
        self.agent = agent

    def _owner(self):
        return owner_title(self.agent.cfg)

    # ---------- 上下文构建 ----------

    def build_time_context(self, now=None):
        """时间感知：现在是几点、星期几、用户可能在干嘛。"""
        now = now or self.agent.clock()
        owner = self._owner()
        hour = now.hour
        if 5 <= hour < 8:
            phase = f"清晨，{owner}可能刚起床或准备上班"
        elif 8 <= hour < 12:
            phase = f"上午，{owner}大概率在工作/学习中"
        elif 12 <= hour < 14:
            phase = f"中午，{owner}可能在午休或吃饭"
        elif 14 <= hour < 18:
            phase = f"下午，{owner}大概率在工作/学习中"
        elif 18 <= hour < 21:
            phase = f"傍晚到晚上，{owner}可能刚下班回家"
        elif 21 <= hour < 23:
            phase = f"晚上，{owner}可能在放松休息"
        else:
            phase = f"深夜，{owner}可能已经睡了，不要打扰"
        weekday = "一二三四五六日"[now.weekday()]
        return f"现在是{now.strftime('%m月%d日')}周{weekday}，{now.strftime('%H:%M')}，{phase}。"

    def build_recent_thread(self, n=3):
        """最近几轮对话脉络（不含记忆库，纯最近聊天）。"""
        recent = [
            m for m in self.agent.chat_history[-n * 2:]
            if m["role"] in ("user", "assistant") and m["text"].strip()
        ]
        if not recent:
            return ""
        owner = self._owner()
        user_label = "用户：" if owner == "你" else owner + "："
        return "\n".join(
            (user_label if m["role"] == "user" else "你：") + m["text"][:60]
            for m in recent[-n * 2:]
        )

    # ---------- 决策原语 ----------

    def is_quiet(self, now):
        try:
            start = int(self.agent.cfg.get("quiet_start", 23))
            end = int(self.agent.cfg.get("quiet_end", 7))
        except (TypeError, ValueError):
            return False
        if start == end:
            return False
        hour = now.hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def cooldown_ok(self, now):
        gap = max(10, int(self.agent.cfg.get("proactive_gap_minutes", 30)))
        return (now.timestamp() - self.agent.state.get("last_proactive_ts", 0.0)) >= gap * 60

    def proactive_budget_ok(self, now):
        date = now.strftime("%Y-%m-%d")
        if self.agent.state.get("daily_proactive_date") != date:
            return True
        return int(self.agent.state.get("daily_proactive_count", 0)) < PROACTIVE_DAILY_BUDGET

    def mark_proactive(self, now):
        date = now.strftime("%Y-%m-%d")
        if self.agent.state.get("daily_proactive_date") != date:
            self.agent.state["daily_proactive_date"] = date
            self.agent.state["daily_proactive_count"] = 0
        self.agent.state["daily_proactive_count"] = (
            int(self.agent.state.get("daily_proactive_count", 0)) + 1
        )
        self.agent.state["last_proactive_ts"] = now.timestamp()
        self.agent._save_state()

    def update_mood(self, ctx):
        mood = "平静"
        hour = self.agent.clock().hour
        for coll in ctx.get("collections", []):
            if coll["plugin"] != "weather" or not coll["entries"]:
                continue
            desc = (coll["entries"][0].get("data") or {}).get("desc", "")
            if any(k in desc for k in ("雨", "雪", "阴", "霾")):
                mood = "有点蔫"
            elif "晴" in desc:
                mood = "开心"
        if hour >= 23 or hour < 6:
            mood = "困了"
        self.agent.state["mood"] = mood
        self.agent._save_state()

    def greeting(self, now):
        date = now.strftime("%Y-%m-%d")
        if self.agent.state.get("last_greeting_date") == date:
            return None
        hour = now.hour
        if 5 <= hour < 11:
            text = "早上好～今天也要加油哦！"
        elif 11 <= hour < 14:
            text = "中午好，记得好好吃饭～"
        elif 14 <= hour < 18:
            text = "下午好，忙的话也别忘了休息。"
        elif 18 <= hour < 23:
            text = "晚上好，今天过得怎么样？"
        else:
            return None
        self.agent.state["last_greeting_date"] = date
        self.agent._save_state()
        return text

    def plugin_messages(self, ctx):
        for coll in ctx.get("collections", []):
            module = self.agent.plugins.get(coll["plugin"])
            if not module or not callable(getattr(module, "suggest", None)):
                continue
            if not coll["entries"]:
                continue
            settings = self.agent.cfg.get("collectors", {}).get(coll["plugin"], {})
            state = self.agent.brain.state.setdefault(coll["plugin"], {})
            message = module.suggest(settings, coll["entries"], state)
            if message:
                yield coll["plugin"], message

    def pick_search_topic(self, ctx):
        """从记忆/新闻/常识里选一个值得主动搜索的话题。"""
        profile = self.agent.db.memory_profile(limit_per=3, roles=("fact",))
        for group in profile:
            if group["category"] in ("preference", "habit"):
                for item in group["items"]:
                    text = item["text"]
                    owner = self._owner()
                    prefixes = (
                        owner + "喜欢", owner + "习惯",
                        "主人喜欢", "主人习惯",
                    )
                    for prefix in prefixes:
                        if not text.startswith(prefix):
                            continue
                        keyword = text[len(prefix):].strip()
                        if keyword and len(keyword) <= 20:
                            return f"{keyword} 最新消息"
                        break
        for coll in ctx.get("collections", []):
            if coll["plugin"] == "rss_news" and coll["entries"]:
                title = coll["entries"][0]["text"]
                short = title[:8].strip("，。！？、 ")
                if short:
                    return short
        return random.choice([
            "人工智能 最新进展",
            "本周科技新闻",
            "健康生活",
            "财经市场",
            "今天的热点新闻",
        ])

    # ---------- 话题（topic_watch 来源） ----------

    def patrol_topics(self, force=False):
        """巡视用兴趣话题：设置页手动 topics > 自动提取缓存（每天更新一次）。"""
        manual = (
            self.agent.cfg.get("collectors", {}).get("topic_watch", {}).get("topics") or []
        )
        manual = [str(t).strip() for t in manual if str(t).strip()]
        if manual:
            return manual
        today = self.agent.clock().strftime("%Y-%m-%d")
        if not force and self.agent.state.get("topics_date") == today:
            return list(self.agent.state.get("topics_list") or [])
        topics = self.extract_topics()
        self.agent.state["topics_date"] = today
        self.agent.state["topics_list"] = topics
        self.agent._save_state()
        return topics

    def extract_topics(self, max_topics=8):
        """从画像提取话题关键词：LLM 优先（精确），失败/无 key 时规则降级。"""
        import json
        import re

        owner = self._owner()
        profile = self.agent._build_memory_profile()
        if not profile or profile.startswith(f"还没有关于{owner}的记录") or profile.startswith("还没有关于主人的记录"):
            return []
        if self.agent.cfg["api"]["api_key"]:
            try:
                system = (
                    f"从下面关于{owner}的记录中提取 5-8 个适合搜索资讯的话题关键词。"
                    "要求：名词短语 2-4 字（如 摄影、人工智能、健身），"
                    "去掉情绪词和动词，只输出 JSON 数组如 [\"摄影\",\"AI\"]，不要其他内容。\n\n"
                    + profile
                )
                raw = self.agent.brain.complete([
                    {"role": "system", "content": system},
                    {"role": "user", "content": "提取话题"},
                ]) or ""
                parsed = json.loads(raw.strip().strip("`") or "[]")
                if isinstance(parsed, list):
                    topics = [str(t).strip()[:6] for t in parsed if str(t).strip()]
                    return topics[:max_topics]
            except Exception:
                pass  # 降级规则提取
        # 规则降级：从常见事实句式里抽关键词
        topics = []
        prefixes = sorted({owner, "主人"})
        patterns = [
            re.compile(
                rf"{re.escape(p)}喜欢(?:看|听|玩|打|读)?([^，。！？]{{1,10}})"
            )
            for p in prefixes
        ] + [
            re.compile(
                rf"{re.escape(p)}最近在(?:学|看|追|玩|读)([^，。！？]{{1,10}})"
            )
            for p in prefixes
        ] + [
            re.compile(rf"{re.escape(p)}习惯：([^，。！？]{{1,10}})")
            for p in prefixes
        ]
        for pattern in patterns:
            for group in self.agent.db.memory_profile(limit_per=10, roles=("fact",)):
                for item in group["items"]:
                    match = re.search(pattern, item["text"])
                    if match:
                        topic = match.group(1).strip()[:6]
                        if topic and topic not in topics:
                            topics.append(topic)
        return topics[:max_topics]

    # ---------- 规则发言决策 ----------

    def maybe_save_thought(self, ctx):
        if random.random() >= 0.4:
            return
        news = []
        for coll in ctx.get("collections", []):
            if coll["plugin"] == "rss_news" and coll["entries"]:
                news = coll["entries"]
                break
        if news:
            title = news[0]["text"]
            short = title if len(title) <= 40 else title[:37] + "…"
            self.agent._remember("thought", f"今天看到一条新闻：{short}，有点意思。")
        else:
            self.agent._remember("thought", "今天没发生什么特别的事，但我一直在。")
        if self.agent.stats:
            self.agent.stats.record_thought()

    def rules_think(self, ctx, now):
        # 1) 天气类紧急提醒：不受冷却限制
        for plugin_name, message in self.plugin_messages(ctx):
            if plugin_name == "weather":
                self.mark_proactive(now)
                return message

        # 2) 日程提醒：用户提过的日程临近（每天最多一次，不受冷却限制）
        schedule = self.agent.db.memory_schedule_due(
            within_hours=12, now=now.strftime("%Y-%m-%d %H:%M")
        )
        if (
            schedule
            and self.agent.state.get("last_schedule_remind_date") != now.strftime("%Y-%m-%d")
        ):
            self.agent.state["last_schedule_remind_date"] = now.strftime("%Y-%m-%d")
            self.mark_proactive(now)
            return f"你之前说{schedule[-1]['text']}，时间快到了，别忘了哦。"

        # 3) 每天一次的问候
        greeting = self.greeting(now)
        if greeting:
            return greeting

        # 4) 其他插件建议、记忆跟进、好奇心都受冷却与每日预算限制
        if not self.cooldown_ok(now) or not self.proactive_budget_ok(now):
            self.maybe_save_thought(ctx)
            return None
        for plugin_name, message in self.plugin_messages(ctx):
            self.mark_proactive(now)
            return message
        followup = self.agent._memory_followup(now)
        if followup:
            self.mark_proactive(now)
            return followup
        # 4.5) 连续陪伴感言（每 24h 最多一次）
        streak = self.agent.db.streak_days(now)
        if (
            streak >= 3
            and now.timestamp() - self.agent.state.get("last_streak_ts", 0.0) >= 24 * 3600
        ):
            self.agent.state["last_streak_ts"] = now.timestamp()
            self.agent._save_state()
            self.mark_proactive(now)
            return f"你已经连续陪我 {streak} 天了，谢谢呀～"
        if random.random() < 0.25:
            self.mark_proactive(now)
            return random.choice(CURIOSITY_QUESTIONS)
        # 4) 自主好奇搜索：桌宠自己选话题，主动调用搜索工具
        if self.agent.cfg.get("tools_enabled", True) and random.random() < 0.35:
            topic = self.pick_search_topic(ctx)
            if topic:
                try:
                    category = random.choice(["web", "news"])
                    entries = search.search_all(topic, category, 4)
                    if self.agent.stats:
                        self.agent.stats.record_tool()
                    if entries:
                        first = entries[0]
                        extra = first.get("url") or first.get("snippet") or ""
                        self.mark_proactive(now)
                        return (
                            f"我自己搜了一下“{topic}”："
                            f"{first.get('title', '')} {extra}"
                        ).strip()
                except Exception:
                    pass
        self.maybe_save_thought(ctx)
        return None
