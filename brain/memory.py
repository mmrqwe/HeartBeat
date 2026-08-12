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

import db
from brain.llm import owner_title

FOLLOW_KEYWORDS = ("考试", "开会", "面试", "报告", "加班", "出差")

_MEMORY_SECRET_MARKERS = (
    "secret", "access secret", "api_key", "password", "passwd", "token",
    "credential", "sk-", "密钥",
)

DEFAULT_DISTANCE_THRESHOLD = 1.2  # 向量召回阈值：超过视为无关，不进 prompt
DEDUP_DISTANCE_THRESHOLD = 0.35   # 语义近重复阈值：低于视为同一条事实
# 纠错向量定位阈值（与去重一致：只有语义近重复才敢原地更新旧记忆）
CORRECTION_VEC_DISTANCE = 0.35

# 值得花一次 LLM 分析的事实线索（规则未命中时才触发）
_ANALYZE_CUES = (
    "我", "喜欢", "讨厌", "爱", "买了", "买", "明天", "下周", "最近",
    "工作", "学习", "住", "养了", "考试", "面试", "出差", "记得",
    "希望", "生病", "疼", "加班", "开会", "体检", "生日", "旅行",
)

_FORGET_RE = re.compile(
    r"(?:忘掉|忘记|删掉|删除|不要记住|别再记|别记)[着]?[：:，,]?\s*(.{1,40}?)(?:了|[。！？!?，,、]|$)"
)
_CONTRADICT_RE = re.compile(
    r"(?:不喜欢|不再喜欢|讨厌|不爱)([^，。！？]{1,20}?)(?:了|$)"
)

# 纠错：不是X，是Y / 是Y，不是X / 我说错了是Y……X 是旧说法、Y 是正确说法。
# 规则层用“原地替换旧词”实现更新；无 X 的纠错（“错了，是Y”）由 LLM 路径定位。
_CORRECT_SWAP_RE = re.compile(
    r"(?:不叫|不是)([^是叫，。！？\s]{1,16})[，,、\s]*(?:我)?"
    r"(?:而是|应该是|其实是|是|叫)([^是叫，。！？\s]{1,16})"
)
# 反向语序：“是Y，不是X”“Y，不是X”（Y 是正确说法在前、X 是旧说法在后）。
# 反向句式对普通陈述句（“我是学生，不是老师”“这个功能不是这样的”）
# 更易误触发，因此：规则替换要求词长 ≥3（专名级）且 Y 不以指示代词
# 开头；短词纠错由 LLM 路径兜底。
_CORRECT_SWAP_REV_RE = re.compile(
    r"(?:(?:^|[，。！？、；\s])|(?:应该是|其实是|而是|是|叫))"
    r"([^，。！？是叫\s]{1,16})[，,、\s]*(?:不叫|不是)([^，。！？是叫\s]{1,16})"
)
# 纠错触发词（LLM 分析门控 + 注入现有记忆清单的判定）
_CORRECT_CUES = ("错了", "记错", "纠正", "更正", "搞错", "不对")

# —— 纠错定位（更新而非新增）——
_SAME_EVENT_JACCARD = 0.4  # 2-gram Jaccard 下限：覆盖“只差专名”的纠错对
_SAME_EVENT_RATIO = 0.6    # LCS 相似比下限：覆盖“措辞改写”的纠错对
# old_text 定位旧记忆时剔除的泛词（长词在前，先替换避免子串残片）
_FACT_GENERIC_TERMS = (
    "买了的", "主人", "买了", "买的", "持有", "关注", "喜欢", "讨厌",
    "最近", "习惯", "一般", "每天", "住在", "养了", "我叫", "我是",
    "这只", "那只", "股票", "基金",
)


def _char_bigrams(text):
    """去标点后的 2-gram 字符集合（中文近似切词，供结构相似度用）。"""
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _lcs_ratio(a, b):
    """最长公共子序列相似比（手写 DP，避免 difflib 依赖进化白名单）。"""
    a = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", a or "")
    b = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", b or "")
    if not a or not b:
        return 0.0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return 2 * prev[-1] / (len(a) + len(b))


def _texts_same_event(a, b):
    """两段文本是否描述同一件事的不同版本（纠错定位的结构校验）。

    校准样本：只差专名对（“主人持有长江电力”vs“主人持有长电科技”）
    Jaccard=0.4 → 接受；不同动作+不同专名（“关注长江电力”vs“持有长电
    科技”）Jaccard≈0.08、ratio≈0.5 → 拒绝（ratio 阈值 0.6），防止误
    覆盖无关记忆。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b or a == b:
        return False
    bigrams_a = _char_bigrams(a)
    bigrams_b = _char_bigrams(b)
    union = bigrams_a | bigrams_b
    if union and len(bigrams_a & bigrams_b) / len(union) >= _SAME_EVENT_JACCARD:
        return True
    return _lcs_ratio(a, b) >= _SAME_EVENT_RATIO


def _fact_terms(text):
    """提取文本中可定位旧记忆的核心词：剔除泛词后 ≥2 字连续汉字段，
    按长度降序（长词更精准，先匹配长词）。"""
    s = text or ""
    for g in sorted(_FACT_GENERIC_TERMS, key=len, reverse=True):
        s = s.replace(g, " ")
    return sorted(
        {t for t in re.findall(r"[\u4e00-\u9fa5]{2,}", s)},
        key=len, reverse=True,
    )


class MemoryModule:
    """记忆领域：事实提取、画像构建、记忆跟进、向量检索、日程解析。"""

    def __init__(self, agent):
        self.agent = agent

    def _owner(self):
        return owner_title(self.agent.cfg)

    def _enqueue_embed(self, kind, item_id, text):
        """异步向量入队：Agent 注入 embed_queue（main.py）时入队返回 True；
        否则返回 False（测试/CLI 直连保持同步旧行为）。"""
        q = getattr(self.agent, "embed_queue", None)
        if q is None:
            return False
        try:
            return bool(q.enqueue(kind, item_id, text))
        except Exception:
            return False

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
        # P1 事件时间线：memory.created（仅真正新增时；去重命中不记录）
        try:
            self.agent.db.log_event(
                db.EventType.MEMORY_CREATED, "brain.memory",
                {"role": role, "category": category, "id": item_id},
                getattr(self.agent, "_trace_id", ""),
            )
        except Exception:
            pass
        if self.agent.db.vec_ready and not self._enqueue_embed("memory", item_id, text):
            vector = self.agent.embedder.embed_one(text)
            if vector:
                self.agent.db.add_embedding("memory", item_id, vector)
        return item_id

    def _dedup_fact_id(self, text):
        """精确 + 语义去重：先 SQL 精确匹配（O(log N)，走索引），
        未命中再向量近重复（O(N) 向量扫描兜底）。"""
        exact = self.agent.db.find_fact_by_text(text)
        if exact is not None:
            return exact
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

    # ---------- 记忆纠错（更新而非新增） ----------

    def _has_correction_cue(self, user_text):
        """这句话是否在纠正之前的说法（规则/LLM 路径共同的触发判定）。

        覆盖：明确触发词（错了/记错/纠正…）、“不是X，是Y”正向句式、
        “是Y，不是X”反向句式（反向句式单独出现也触发，如“长电科技，
        不是长江电力”——不含触发词时必须靠句式识别）。
        """
        text = (user_text or "").strip()
        if any(c in text for c in _CORRECT_CUES):
            return True
        return bool(
            _CORRECT_SWAP_RE.search(text) or _CORRECT_SWAP_REV_RE.search(text)
        )

    def _handle_correction_rules(self, user_text):
        """规则纠错：“不是X，是Y”“是Y，不是X”→ 原地替换记忆里的旧词。

        返回 (fixed, excluded)：修正条数与成功被纠正的旧说法集合。
        extract_facts 用 excluded 过滤普通规则——防止旧说法以裸词形式
        被重新提取入库（删改完又被加回来）。未命中返回 (0, ())，由
        LLM 主路径兜底。
        """
        text = (user_text or "").strip()
        if not text:
            return 0, ()
        fixed = 0
        excluded = []
        # 正反向句式统一成 (old_term, new_term, min_len) 三元组；
        # 反向句式（“是Y，不是X”）对普通陈述句更易误触发，词长要求更严
        pairs = [
            (m.group(1).strip(" 的了"), m.group(2).strip(" 的了"), 2)
            for m in _CORRECT_SWAP_RE.finditer(text)
        ] + [
            (m.group(2).strip(" 的了"), m.group(1).strip(" 的了"), 3)
            for m in _CORRECT_SWAP_REV_RE.finditer(text)
        ]
        for old_term, new_term, min_len in pairs:
            if (not old_term or not new_term or len(old_term) < min_len
                    or len(new_term) < min_len or "错" in new_term
                    or new_term in ("对吗", "是不是")
                    or new_term.startswith(("这", "那"))):
                continue
            ids = self.agent.db.replace_fact_term(old_term, new_term)
            for item_id in ids:
                row = self.agent.db.memory_item(item_id)
                if row:
                    self._reembed(item_id, row["text"])
            if ids:
                excluded.append(old_term)
                fixed += len(ids)
        return fixed, tuple(excluded)

    def _reembed(self, item_id, text):
        """记忆文本更新后同步向量：异步队列优先，同步直嵌兜底；
        嵌入失败删旧向量行，留给 reindex 按新文本补嵌（防文本与向量不一致）。"""
        try:
            if not self.agent.db.vec_ready:
                return
            if self._enqueue_embed("memory", item_id, text):
                return
            vector = self.agent.embedder.embed_one(text)
            if vector:
                self.agent.db.add_embedding("memory", item_id, vector)
            else:
                self.agent.db.remove_embedding("memory", item_id)
        except Exception:
            pass

    def _apply_fix(self, old_text, new_text):
        """把旧记忆原地更正为新文本（更新而非新增）。

        定位顺序：精确 → 子串 LIKE → old_text 核心词 → 向量近邻+结构校验。
        任一命中即更新；全部失败返回 False（调用方回退纠错兜底）。
        """
        old_text = str(old_text or "").strip()
        new_text = str(new_text or "").strip()
        if not old_text or not new_text or old_text == new_text:
            return False
        item_id = self._locate_fact(old_text, new_text)
        if item_id is None:
            return False
        if not self.agent.db.update_memory_text(item_id, new_text):
            return False
        self._reembed(item_id, new_text)
        return True

    def _locate_fact(self, old_text, new_text=""):
        """定位要更正的旧记忆 id；None 表示未找到（调用方回退新增）。"""
        item_id = self.agent.db.find_fact_by_text(old_text)
        if item_id is not None:
            return item_id
        item_id = self.agent.db.find_fact_like(old_text)
        if item_id is not None:
            return item_id
        # LLM 引用的旧原文常有措辞差异（“买了”vs“持有”），按核心词定位
        for term in _fact_terms(old_text):
            item_id = self.agent.db.find_fact_like(term)
            if item_id is not None:
                return item_id
        # 最后兜底：new_text 与错误旧记忆通常只有专名不同，语义近重复
        return self._vec_locate_fact(new_text)

    def _vec_locate_fact(self, new_text):
        """向量近邻定位：与 new_text 语义近重复的旧事实（纠错兜底）。

        召回后必须过结构校验（_texts_same_event），防止把“关注长江电力”
        误覆盖成“持有长电科技”（动作不同+专名不同 → 拒绝更新）。
        """
        new_text = str(new_text or "").strip()
        if not new_text or not self.agent.db.vec_ready:
            return None
        try:
            if not self.agent.embedder.ready:
                return None
            vector = self.agent.embedder.embed_one(new_text)
            if not vector:
                return None
            hits = self.agent.db.search_embeddings(
                "memory", vector, k=3, roles=("fact",),
                min_distance=CORRECTION_VEC_DISTANCE,
            )
        except Exception:
            return None
        for hit in hits:
            row = self.agent.db.memory_item(hit["id"])
            if not row:
                continue
            if _texts_same_event(row["text"], new_text):
                return hit["id"]
        return None

    def _remember_fact_correction(self, new_text, category="misc"):
        """纠错场景的新事实入库兜底：与旧记忆语义近重复时更新而非新增。

        只在 _apply_fix 定位失败后使用：优先原地更新同一事件旧记忆，
        避免“纠正一条”变成“新增一条”；无法定位时才走普通新增。
        """
        new_text = str(new_text or "").strip()
        if not new_text or self._is_sensitive_fact(new_text):
            return False
        if self.agent.db.find_fact_by_text(new_text) is not None:
            return False  # 已在库中：幂等，无需新增
        item_id = self._vec_locate_fact(new_text)
        if item_id is not None:
            row = self.agent.db.memory_item(item_id)
            if row and row["text"] != new_text:
                if not self.agent.db.update_memory_text(item_id, new_text):
                    return False
                self._reembed(item_id, new_text)
                return True
            return False
        return self._remember_fact(new_text, category)

    def _remember_fact(self, fact, category="misc", importance=3):
        """新事实入库（敏感过滤 + 精确查重 + 统计），FACT 与 FIX 回退共用。"""
        fact = str(fact or "").strip()
        if not fact or self._is_sensitive_fact(fact):
            return False
        if self.agent.db.find_fact_by_text(fact) is not None:
            return False
        item_id = self.remember(
            "fact", fact, category=category, importance=importance, source="chat",
            expires_at=(
                self.parse_schedule_expiry(fact) if category == "schedule" else None
            ),
        )
        if item_id is None:
            return False
        if self.agent.stats:
            self.agent.stats.record_fact()
        return True

    def _handle_forget(self, user_text):
        """显式“忘掉/删掉 X”：删除匹配事实（含向量）。

        返回成功删除的旧词集合：extract_facts 用它过滤普通规则，
        防止“忘掉X”刚删完又被同句里的普通规则提取回库。
        """
        text = (user_text or "").strip()
        if not text:
            return ()
        excluded = []
        for match in _FORGET_RE.finditer(text):
            keyword = match.group(1).strip("的了 ")
            if keyword:
                try:
                    if self.agent.db.delete_memory_like(keyword):
                        excluded.append(keyword)
                except Exception:
                    pass
        return tuple(excluded)

    def embed_chat(self, item_id, text):
        if not self.agent.db.vec_ready:
            return
        if self._enqueue_embed("chat", item_id, text):
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
        显式“忘记”删除匹配记忆；纠错（“不是X，是Y”“是Y，不是X”）
        原地替换旧记忆而非新增；两者命中的旧说法会被普通规则跳过，
        防止删改完又被重新提取。返回本次新记住/修正的条数。
        """
        # 忘记/纠错与普通规则提取不再互斥：先执行删除/替换并收集
        # 被废弃的旧说法，普通规则提取时命中旧说法的条目直接丢弃
        # （防止“删了又加回”“纠正后旧词以裸词形式新增”）。
        excluded = list(self._handle_forget(user_text))
        fixed, corr_excluded = self._handle_correction_rules(user_text)
        excluded.extend(corr_excluded)
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
        seen = set()
        saved = fixed
        for pattern, template, category, importance in rules:
            match = re.search(pattern, user_text)
            if not match:
                continue
            if template == "{}":
                fact = match.group(0)
            else:
                fact = template.format(match.group(1))
            # 被纠正/被删除的旧说法不得以裸词重新入库
            if any(term and term in fact for term in excluded):
                continue
            # 精确查重走 SQL（O(log N)），不再全量加载；seen 防同轮重复规则命中
            if fact not in seen and self.agent.db.find_fact_by_text(fact) is None:
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
                    seen.add(fact)
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
        "[FIX:旧记忆原文] 修正后的新记忆（{owner}纠正之前的说法时使用：旧记忆原文从“现有记忆”里照抄原句，输出修正后的新记忆；找不到旧记忆时改用 FACT）\n"
        "[NONE]\n"
        "不要输出任何其他内容。"
    )

    @staticmethod
    def _is_sensitive_fact(text):
        low = text.lower()
        return any(m in low for m in _MEMORY_SECRET_MARKERS)

    def analyze_and_remember(self, user_text, reply=""):
        """让 LLM 自己判断这段对话有没有值得记住的事实，并入库。

        主人纠正之前的说法（“错了，是X”“不是A，是B”）时按 [FIX:旧] 新
        原地更正旧记忆而非新增。返回新记住/修正的条数。任何异常都静默降级。
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
            # 纠错场景才注入“现有记忆”清单（普通场景零额外 token），
            # 供 LLM 照抄旧记忆原文精确执行 FIX。
            if self._has_correction_cue(user_text):
                brief = self._facts_brief(limit=30)
                if brief:
                    content += (
                        "\n现有记忆：\n" + brief
                        + "\n主人在纠正之前的说法：如果纠正内容与上面某条记忆"
                        "描述的是同一件事（只是名称/措辞不同），必须输出"
                        " [FIX:照抄该记忆原文] 更正后的完整记忆 来更新它；"
                        "禁止输出 FACT 造成重复记忆。"
                    )
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
        seen = set()
        saved = 0
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("[FIX"):
                old_text, new_text = self._parse_fix_line(line)
                if not old_text or not new_text or old_text == new_text:
                    continue
                if new_text in seen or self._is_sensitive_fact(new_text):
                    continue
                if self._apply_fix(old_text, new_text):
                    saved += 1
                elif self._remember_fact_correction(new_text, "misc"):
                    saved += 1
                seen.add(new_text)
                continue
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
            if not fact or fact in seen:
                continue
            if category not in ("identity", "preference", "habit", "schedule", "finance", "misc"):
                category = "misc"
            if self._remember_fact(fact, category):
                saved += 1
            seen.add(fact)
        return saved

    @staticmethod
    def _parse_fix_line(line):
        """解析 [FIX:旧记忆原文] 新记忆 → (old_text, new_text)。"""
        rest = line[len("[FIX"):].strip()
        if not rest.startswith(":"):
            return "", ""
        end = rest.find("]")
        if end <= 1:
            return "", ""
        return rest[1:end].strip(), rest[end + 1:].strip()

    def _facts_brief(self, limit=30):
        """现有事实清单（纠错时注入给 LLM 定位旧记忆）。"""
        lines = []
        for item in self.agent.db.memory_items(roles=("fact",), limit=limit):
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    def should_analyze(self, user_text, rule_saved=0):
        """LLM 记忆分析门控：规则已命中、太短、无事实线索时省钱跳过；
        纠错类表述必定触发（需要 LLM 定位旧记忆执行 FIX）。"""
        text = (user_text or "").strip()
        if rule_saved or len(text) < 6:
            return False
        if any(c in text for c in _CORRECT_CUES) or _CORRECT_SWAP_RE.search(text):
            return True
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
            match = re.search(r"下(周|星期)([一二三四五六日天])", text)
            if match:
                weekday = "一二三四五六日天".index(match.group(2))
                day = day + timedelta(days=(weekday - day.weekday() + 7) % 7 or 7)
        elif re.search(r"(周|星期)([一二三四五六日天])", text):
            match = re.search(r"(周|星期)([一二三四五六日天])", text)
            if match:
                weekday = "一二三四五六日天".index(match.group(2))
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
