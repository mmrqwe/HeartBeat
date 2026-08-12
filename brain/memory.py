"""brain.memory：记忆领域模块（Agent 组合使用）。

自进化约定（阶段 4 updater）：本模块是允许 AI 替换升级的独立模块——
只要保持公开方法签名（remember / relevant / profile / extract_facts /
followup_candidate / parse_schedule_expiry / format_memories），
Kernel 即可在版本测试通过后切换实现。

设计：以组合方式访问 Agent 共享状态（self.agent.state/db/cfg/clock），
不 import agent 模块（避免循环依赖）。
"""

import re
import time
from datetime import timedelta

FOLLOW_KEYWORDS = ("考试", "开会", "面试", "报告", "加班", "出差")

_MEMORY_SECRET_MARKERS = (
    "secret", "access secret", "api_key", "password", "passwd", "token",
    "credential", "sk-", "密钥",
)


class MemoryModule:
    """记忆领域：事实提取、画像构建、记忆跟进、向量检索、日程解析。"""

    def __init__(self, agent):
        self.agent = agent

    # ---------- 记忆写入 ----------

    def remember(self, role, text, category="misc", importance=3, source="chat",
                 expires_at=None):
        item_id = self.agent.memory.add(
            role, text, category=category, importance=importance,
            source=source, expires_at=expires_at,
        )
        if self.agent.db.vec_ready:
            vector = self.agent.embedder.embed_one(text)
            if vector:
                self.agent.db.add_embedding("memory", item_id, vector)
        return item_id

    def embed_chat(self, item_id, text):
        if not self.agent.db.vec_ready:
            return
        vector = self.agent.embedder.embed_one(text)
        if vector:
            self.agent.db.add_embedding("chat", item_id, vector)

    # ---------- 记忆读取 ----------

    def relevant(self, query, k=5):
        """向量检索相关记忆；不可用时退回最近记忆。"""
        results = []
        if self.agent.db.vec_ready:
            vector = self.agent.embedder.embed_one(query)
            if vector:
                results = self.agent.db.search_embeddings("memory", vector, k)
                results += self.agent.db.search_embeddings("chat", vector, k)
        if not results:
            results = [dict(i) for i in self.agent.memory.recent(5)]
        return results

    @staticmethod
    def format_memories(items):
        if not items:
            return "暂无"
        return "；".join(f"[{i.get('role', 'memory')}] {i['text']}" for i in items)

    def profile(self):
        """记忆画像：按类别汇总的要点（而非原始 5 条），供主动思考使用。"""
        groups = self.agent.db.memory_profile(limit_per=3, roles=("fact",))
        if not groups:
            return "还没有关于主人的记录，多聊聊天让我记住你。"
        lines = []
        for group in groups:
            items = "；".join(i["text"] for i in group["items"])
            lines.append(f"[{group['category']}] {items}")
        return "\n".join(lines)

    # ---------- 事实提取（规则，零 API 成本） ----------

    def extract_facts(self, user_text):
        """规则提取主人对白中的事实（两种大脑模式通用）。

        rules: (正则, 模板, 类别, 重要度)。模板含 {} 用捕获组填充。
        """
        rules = [
            (r"我叫([^，。！？\s]{1,12})", "主人叫{}", "identity", 4),
            (r"(?:我超爱|我最爱|我特别爱|我喜欢|我爱)(.+?)(?:[，。！？、]|$)", "主人喜欢{}", "preference", 3),
            (r"我不喜欢(.+?)(?:[，。！？、]|$)", "主人不喜欢{}", "preference", 3),
            (r"(?:我每天|我每周|我一般|我习惯)([^，。！？]{1,24})(?:[。！？]|$)", "主人习惯：{}", "habit", 3),
            (r"(?:我是|我是一名|我是做)([^，。！？\s]{1,10}(?:生|师|员|工|人|医|律师|会计))", "主人是{}", "identity", 4),
            (r"(?:我住在)([^，。！？\s]{1,12}?(?:上班|工作|上学|住))", "{}", "habit", 3),
            (r"(?:明天|后天|今天|本周|下周|周[一二三四五六日天]|周末|月底).{0,24}?(?:考试|开会|面试|报告|加班|出差|体检|生日|旅行|搬家|答辩)", "{}", "schedule", 4),
            # —— 以下为扩充（关注主人动态/健康/作息）——
            (r"(?:我|我最近|最近|我正在|正在)((?:在学|在追|在玩|在练|在准备|在看|在读|在写|在做)[^，。！？]{1,16})(?:[，。！？]|$)", "主人最近{}", "preference", 3),
            (r"(?:我养了|我家有)(?:一只|一个)?([^，。！？]{0,10}?(?:猫|狗|兔|仓鼠|鹦鹉|鱼|鸟))", "主人养了{}", "preference", 3),
            (r"(?:我一般|我习惯|我每天)([^，。！？]{2,16}?(?:睡觉|起床|上班|下班|午休|睡))", "主人习惯：{}", "habit", 3),
            (r"我在([^，。！？]{2,16}?(?:上班|工作|上学|实习))", "主人在{}", "habit", 3),
            (r"(?:我|我最近|最近)(?:今天|昨晚|这两天|有点|总是|经常)?((?:熬夜|失眠|感冒|发烧|头疼|胃疼|过敏|嗓子疼)[^，。！？]{0,8})", "主人最近{}", "habit", 3),
            (r"(?:请你|你|帮我)?(?:记住|记一下|记下来|记住一下|以后记得)(?:[：:，,]?\s*)(.{2,60})", "主人要求记住：{}", "misc", 3),
            (r"(?:我希望你|你以后要|以后你)([^，。！？]{2,60})", "主人希望：{}", "preference", 3),
        ]
        for pattern, template, category, importance in rules:
            match = re.search(pattern, user_text)
            if not match:
                continue
            if template == "{}":
                fact = match.group(0)
            else:
                fact = template.format(match.group(1))
            if fact not in [i["text"] for i in self.agent.memory.facts()]:
                self.remember(
                    "fact",
                    fact,
                    category=category,
                    importance=importance,
                    expires_at=(
                        self.parse_schedule_expiry(fact) if category == "schedule" else None
                    ),
                )
                if self.agent.stats:
                    self.agent.stats.record_fact()

    def extract_facts_watermark(self, limit=100):
        """巡视时补采水位线之后的主人对白（升级后首启 / 漏采场景也能建画像）。

        chat() 已实时提取并推进水位线，这里只处理水位线之后的消息，幂等。
        """
        scan_id = int(self.agent.state.get("fact_scan_id", 0) or 0)
        rows = self.agent.db.chat_after(scan_id, limit=limit)
        if not rows:
            return
        for row in rows:
            self.extract_facts(row["text"])
        self.agent.state["fact_scan_id"] = rows[-1]["id"]
        self.agent._save_state()

    # ---------- LLM 自主记忆分析（替代写死规则的主路径） ----------

    _ANALYZER_SYSTEM = (
        "你是记忆分析器。从对话中判断有没有值得长期记住的关于主人的事实。"
        "值得记住的包括：身份、偏好、习惯、日程、资产/投资、宠物/家人、健康、工作学习等长期信息。"
        "不要记：临时指令、一次性请求、客套话、网址、密钥/密码/Token/Access Secret。\n"
        "只输出以下格式之一：\n"
        "[FACT:类别] 一句事实（类别只能是 identity/preference/habit/schedule/finance/misc）\n"
        "[NONE]\n"
        "不要输出任何其他内容。"
    )

    @staticmethod
    def _is_sensitive_fact(text):
        low = text.lower()
        return any(m in low for m in _MEMORY_SECRET_MARKERS)

    def analyze_and_remember(self, user_text, reply=""):
        """让 LLM 自己判断这段对话有没有值得记住的事实，并入库。

        返回新记住的条数。任何异常都静默降级，不影响聊天主流程。
        """
        brain = getattr(self.agent, "brain", None)
        cfg = getattr(self.agent, "cfg", {}) or {}
        if brain is None or not (cfg.get("api") or {}).get("api_key"):
            return 0
        try:
            content = "主人说：" + (user_text or "")
            if reply:
                content += "\n桌宠回：" + reply
            raw = brain.complete(
                [
                    {"role": "system", "content": self._ANALYZER_SYSTEM},
                    {"role": "user", "content": content},
                ],
                max_tokens=200,
            ) or ""
        except Exception:
            return 0
        existing = {i["text"] for i in self.agent.memory.facts()}
        saved = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("[FACT"):
                continue
            category = "misc"
            fact = line[len("[FACT"):].strip()
            if fact.startswith(":"):
                end = fact.find("]")
                if end > 0:
                    category = fact[1:end].strip()
                    fact = fact[end + 1:].strip()
            elif fact.startswith("]"):
                fact = fact[1:].strip()
            if not fact or fact in existing or self._is_sensitive_fact(fact):
                continue
            if category not in ("identity", "preference", "habit", "schedule", "finance", "misc"):
                category = "misc"
            self.remember(
                "fact",
                fact,
                category=category,
                importance=3,
                source="chat",
                expires_at=self.parse_schedule_expiry(fact) if category == "schedule" else None,
            )
            if self.agent.stats:
                self.agent.stats.record_fact()
            existing.add(fact)
            saved += 1
        return saved

    # ---------- 记忆跟进 ----------

    def followup_candidate(self, now):
        if now.timestamp() - self.agent.state.get("last_followup_ts", 0.0) < 12 * 3600:
            return None
        for item in reversed(self.agent.memory.recent(15, roles=("fact",))):
            for keyword in FOLLOW_KEYWORDS:
                if keyword in item["text"]:
                    self.agent.state["last_followup_ts"] = now.timestamp()
                    self.agent._save_state()
                    return f"你之前说{item['text']}，进展怎么样？"
        return None

    # ---------- 日程解析 ----------

    def parse_schedule_expiry(self, text, now=None):
        """从日程类记忆文本里粗解析到期时间（用于主动提醒）。

        支持：今天/明天/后天/周X/下周X/月底 + 可选“X点”。解析不出返回 None。
        """
        now = now or self.agent.clock()
        day = now.date()
        hour = None
        hour_match = re.search(r"(\d{1,2})\s*点", text or "")
        if hour_match:
            hour = min(23, max(0, int(hour_match.group(1))))
        if "后天" in text:
            day = day + timedelta(days=2)
        elif "明天" in text or "明早" in text:
            day = day + timedelta(days=1)
        elif re.search(r"下(周|星期)([一二三四五六日天])", text):
            weekday = "一二三四五六日天".index(
                re.search(r"下(周|星期)([一二三四五六日天])", text).group(2)
            )
            day = day + timedelta(days=(weekday - day.weekday() + 7) % 7 or 7)
        elif re.search(r"(周|星期)([一二三四五六日天])", text):
            weekday = "一二三四五六日天".index(
                re.search(r"(周|星期)([一二三四五六日天])", text).group(2)
            )
            day = day + timedelta(days=(weekday - day.weekday()) % 7)
        elif "月底" in text:
            import calendar

            day = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        elif "今天" in text or "今晚" in text:
            pass
        else:
            return None
        if hour is None:
            hour = 23 if day > now.date() else now.hour
        return f"{day.strftime('%Y-%m-%d')} {hour:02d}:00"
