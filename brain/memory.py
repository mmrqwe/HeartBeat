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

from brain.llm import owner_title

FOLLOW_KEYWORDS = ("考试", "开会", "面试", "报告", "加班", "出差")

_MEMORY_SECRET_MARKERS = (
    "secret", "access secret", "api_key", "password", "passwd", "token",
    "credential", "sk-", "密钥",
)

DEFAULT_DISTANCE_THRESHOLD = 1.2  # 向量召回阈值：超过视为无关，不进 prompt
DEDUP_DISTANCE_THRESHOLD = 0.35   # 语义近重复阈值：低于视为同一条事实

# 值得花一次 LLM 分析的事实线索（规则未命中时才触发）
_ANALYZE_CUES = (
    "我", "喜欢", "讨厌", "爱", "买了", "买", "明天", "下周", "最近",
    "工作", "学习", "住", "养了", "考试", "面试", "出差", "记得",
    "希望", "生病", "疼", "加班", "开会", "体检", "生日", "旅行",
)

_FORGET_RE = re.compile(
    r"(?:忘掉|忘记|删掉|删除|不要记住|别再记|别记)[着]?[：:，,]?\s*(.{1,40}?)(?:了|[。！？!?]|$)"
)
_CONTRADICT_RE = re.compile(
    r"(?:不喜欢|不再喜欢|讨厌|不爱)([^，。！？]{1,20}?)(?:了|$)"
)


class MemoryModule:
    """记忆领域：事实提取、画像构建、记忆跟进、向量检索、日程解析。"""

    def __init__(self, agent):
        self.agent = agent

    def _owner(self):
        return owner_title(self.agent.cfg)

    # ---------- 记忆写入 ----------

    def remember(self, role, text, category="misc", importance=3, source="chat",
                 expires_at=None):
        text = (text or "").strip()
        if not text:
            return None
        if role == "fact":
            existing_id = self._dedup_fact_id(text)
            if existing_id is not None:
                try:
                    self.agent.db.mark_memory_used(existing_id)
                except Exception:
                    pass
                return existing_id
            self._retire_contradictions(text, category)
        item_id = self.agent.memory.add(
            role, text, category=category, importance=importance,
            source=source, expires_at=expires_at,
        )
        if self.agent.db.vec_ready:
            vector = self.agent.embedder.embed_one(text)
            if vector:
                self.agent.db.add_embedding("memory", item_id, vector)
        return item_id

    def _dedup_fact_id(self, text):
        """精确 + 语义去重：去重范围是全量事实，不再只看最近 20 条。"""
        for item in self.agent.memory.all_facts():
            if item["text"] == text:
                return item["id"]
        if not (self.agent.db.vec_ready and self.agent.embedder.ready):
            return None
        try:
            vector = self.agent.embedder.embed_one(text)
            if not vector:
                return None
            hits = self.agent.db.search_embeddings(
                "memory", vector, k=1, roles=("fact",),
                min_distance=DEDUP_DISTANCE_THRESHOLD,
            )
            return hits[0]["id"] if hits else None
        except Exception:
            return None

    def _retire_contradictions(self, text, category):
        """新事实与旧事实矛盾时，把旧事实标为过期（如“不喜欢 X”覆盖“喜欢 X”）。"""
        if category not in ("preference", "habit"):
            return 0
        match = _CONTRADICT_RE.search(text)
        if not match:
            return 0
        keyword = match.group(1).strip("的了 ")
        if not keyword:
            return 0
        try:
            past = (self.agent.clock() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
            return self.agent.db.retire_memory_like(
                keyword, category=category, now=past
            )
        except Exception:
            return 0

    def _handle_forget(self, user_text):
        """显式“忘掉/删掉 X”：删除匹配事实（含向量）。"""
        text = (user_text or "").strip()
        if not text:
            return False
        deleted = 0
        for match in _FORGET_RE.finditer(text):
            keyword = match.group(1).strip("的了 ")
            if keyword:
                try:
                    deleted += self.agent.db.delete_memory_like(keyword)
                except Exception:
                    pass
        return deleted > 0

    def embed_chat(self, item_id, text):
        if not self.agent.db.vec_ready:
            return
        vector = self.agent.embedder.embed_one(text)
        if vector:
            self.agent.db.add_embedding("chat", item_id, vector)

    # ---------- 记忆读取 ----------

    def relevant(self, query, k=5):
        """召回相关记忆：向量（距离阈值+过期过滤）→ 关键词 → 最近记忆。
        返回合并去重后的最多 k 条，并记录 last_used_at。"""
        now = time.strftime("%Y-%m-%d %H:%M")
        results = []
        if self.agent.db.vec_ready:
            vector = self.agent.embedder.embed_one(query)
            if vector:
                mem = self.agent.db.search_embeddings(
                    "memory", vector, k * 2, now=now,
                    min_distance=DEFAULT_DISTANCE_THRESHOLD,
                    roles=("fact", "thought"),
                )
                chat = self.agent.db.search_embeddings("chat", vector, k * 2, now=now)
                results = self._merge_pool(mem, chat, k=k)
        if not results:
            kw = self.agent.db.search_memory_keywords(query, k=k, now=now)
            recent = [dict(i) for i in self.agent.memory.recent(k)]
            results = self._merge_pool(kw, recent, k=k)
        for item in results:
            if item.get("id") and item.get("role") in ("fact", "thought"):
                try:
                    self.agent.db.mark_memory_used(item["id"])
                except Exception:
                    pass
        return results

    @staticmethod
    def _merge_pool(*groups, k=5):
        """合并多路召回：按 id 去重，距离优先、importance 微调。"""
        seen = set()
        merged = []
        for group in groups:
            for item in group or []:
                key = (item.get("role"), item.get("id")) if item.get("id") else item.get("text")
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        def score(item):
            distance = float(item.get("distance") or 1.0)
            importance = int(item.get("importance") or 3)
            return distance - importance * 0.02

        return sorted(merged, key=score)[:k]

    def format_memories(self, items):
        if not items:
            return "暂无"
        owner = self._owner()
        parts = []
        for item in items:
            text = str(item.get("text") or "")
            if text.startswith("主人") and owner != "主人":
                text = owner + text[len("主人"):]
            if len(text) > 200:
                text = text[:200] + "…"
            parts.append(f"[{item.get('role', 'memory')}] {text}")
        return "；".join(parts)

    def profile(self):
        """记忆画像：按类别汇总的要点（而非原始 5 条），供主动思考使用。"""
        owner = self._owner()
        groups = self.agent.db.memory_profile(limit_per=3, roles=("fact",))
        if not groups:
            return f"还没有关于{owner}的记录，多聊聊天让我记住你。"
        lines = []
        for group in groups:
            items = "；".join(
                (
                    (owner + i["text"][len("主人"):])
                    if i["text"].startswith("主人") and owner != "主人"
                    else i["text"]
                )
                for i in group["items"]
            )
            lines.append(f"[{group['category']}] {items}")
        return "\n".join(lines)

    # ---------- 事实提取（规则，零 API 成本） ----------

    def extract_facts(self, user_text):
        """规则提取用户对白中的事实（两种大脑模式通用）。

        rules: (正则, 模板, 类别, 重要度)。模板含 {} 用捕获组填充。
        返回本次新记住的条数；显式“忘记”优先处理。
        """
        if self._handle_forget(user_text):
            return 0
        owner = self._owner()
        rules = [
            (r"我叫([^，。！？\s]{1,12})", f"{owner}叫{{}}", "identity", 4),
            (r"(?:我超爱|我最爱|我特别爱|我喜欢|我爱)(.+?)(?:[，。！？、]|$)", f"{owner}喜欢{{}}", "preference", 3),
            (r"我不喜欢(.+?)(?:[，。！？、]|$)", f"{owner}不喜欢{{}}", "preference", 3),
            (r"(?:我每天|我每周|我一般|我习惯)([^，。！？]{1,24})(?:[。！？]|$)", f"{owner}习惯：{{}}", "habit", 3),
            (r"(?:我是|我是一名|我是做)([^，。！？\s]{1,10}(?:生|师|员|工|人|医|律师|会计))", f"{owner}是{{}}", "identity", 4),
            (r"(?:我住在)([^，。！？\s]{1,12}?(?:上班|工作|上学|住))", "{}", "habit", 3),
            (r"(?:明天|后天|今天|本周|下周|周[一二三四五六日天]|周末|月底).{0,24}?(?:考试|开会|面试|报告|加班|出差|体检|生日|旅行|搬家|答辩)", "{}", "schedule", 4),
            # —— 以下为扩充（关注用户动态/健康/作息）——
            (r"(?:我|我最近|最近|我正在|正在)((?:在学|在追|在玩|在练|在准备|在看|在读|在写|在做)[^，。！？]{1,16})(?:[，。！？]|$)", f"{owner}最近{{}}", "preference", 3),
            (r"(?:我养了|我家有)(?:一只|一个)?([^，。！？]{0,10}?(?:猫|狗|兔|仓鼠|鹦鹉|鱼|鸟))", f"{owner}养了{{}}", "preference", 3),
            (r"(?:我一般|我习惯|我每天)([^，。！？]{2,16}?(?:睡觉|起床|上班|下班|午休|睡))", f"{owner}习惯：{{}}", "habit", 3),
            (r"我在([^，。！？]{2,16}?(?:上班|工作|上学|实习))", f"{owner}在{{}}", "habit", 3),
            (r"(?:我|我最近|最近)(?:今天|昨晚|这两天|有点|总是|经常)?((?:熬夜|失眠|感冒|发烧|头疼|胃疼|过敏|嗓子疼)[^，。！？]{0,8})", f"{owner}最近{{}}", "habit", 3),
            (r"(?:请你|你|帮我)?(?:记住|记一下|记下来|记住一下|以后记得)(?:[：:，,]?\s*)(.{2,60})", f"{owner}要求记住：{{}}", "misc", 3),
            (r"(?:我希望你|你以后要|以后你)([^，。！？]{2,60})", f"{owner}希望：{{}}", "preference", 3),
        ]
        existing = {i["text"] for i in self.agent.memory.all_facts()}
        saved = 0
        for pattern, template, category, importance in rules:
            match = re.search(pattern, user_text)
            if not match:
                continue
            if template == "{}":
                fact = match.group(0)
            else:
                fact = template.format(match.group(1))
            if fact not in existing:
                item_id = self.remember(
                    "fact",
                    fact,
                    category=category,
                    importance=importance,
                    expires_at=(
                        self.parse_schedule_expiry(fact) if category == "schedule" else None
                    ),
                )
                if item_id is not None:
                    existing.add(fact)
                    saved += 1
                    if self.agent.stats:
                        self.agent.stats.record_fact()
        return saved

    def extract_facts_watermark(self, limit=100):
        """巡视时补采水位线之后的对白（升级后首启 / 漏采场景也能建画像）。

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
        "你是记忆分析器。从对话中判断有没有值得长期记住的关于{owner}的事实。"
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
        self._handle_forget(user_text)
        brain = getattr(self.agent, "brain", None)
        cfg = getattr(self.agent, "cfg", {}) or {}
        if brain is None or not (cfg.get("api") or {}).get("api_key"):
            return 0
        owner = self._owner()
        try:
            content = f"{owner}说：" + (user_text or "")
            if reply:
                content += "\n桌宠回：" + reply
            raw = brain.complete(
                [
                    {
                        "role": "system",
                        "content": self._ANALYZER_SYSTEM.format(owner=owner),
                    },
                    {"role": "user", "content": content},
                ],
                max_tokens=200,
            ) or ""
        except Exception:
            return 0
        existing = {i["text"] for i in self.agent.memory.all_facts()}
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
            item_id = self.remember(
                "fact",
                fact,
                category=category,
                importance=3,
                source="chat",
                expires_at=self.parse_schedule_expiry(fact) if category == "schedule" else None,
            )
            if item_id is not None:
                existing.add(fact)
                saved += 1
                if self.agent.stats:
                    self.agent.stats.record_fact()
        return saved

    def should_analyze(self, user_text, rule_saved=0):
        """LLM 记忆分析门控：规则已命中、太短、无事实线索时省钱跳过。"""
        text = (user_text or "").strip()
        if rule_saved or len(text) < 6:
            return False
        return any(cue in text for cue in _ANALYZE_CUES)

    def cleanup(self, now=None):
        """记忆生命周期清理（过期 + 上限淘汰），返回 (expired, capped)。"""
        cap = int(self.agent.cfg.get("memory_cap", 500) or 500)
        return self.agent.db.cleanup_memory(now=now, cap=cap)

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
