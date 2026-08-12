"""Agent：桌宠的记忆、想法、自主行为。记忆/聊天/状态全部存 SQLite。

阶段2（2026-08-12）拆分：本文件保留 Agent 主类（构造/状态/入口/
委托壳/回复解析），聊天链路在 agent_chat.py（ChatMixin），自主思考在
agent_think.py（ThinkMixin）。

2026-08-13 移除自进化：brain 层全部静态内置（直接 import 内置
memory/planner），不再支持版本目录/动态加载/自我改写。
"""

import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import core
import rag
import tools
from db import Database, Memory

from .agent_chat import ChatMixin
from .agent_think import ThinkMixin
# memory/planner 内置实现（自进化已移除：静态绑定内置实现）。
from brain.memory import MemoryModule
from brain.planner import Planner


class Agent(ChatMixin, ThinkMixin):
    """桌宠的自主层：SQLite 记忆 + 向量检索 + 想法 + 主动行为。"""

    def __init__(self, cfg, plugins=None, data_dir=None, clock=None, stats=None, db=None, embed_queue=None):
        self.cfg = cfg
        self.plugins = plugins or {}
        self.data_dir = Path(data_dir) if data_dir else Path(".")
        self.db = db or Database(self.data_dir / "heartbeat.db")
        self.stats = stats
        self.embedder = rag.default_embedder(cfg, self.data_dir)
        self._embed_sig = (cfg.get("embedding_enabled"), cfg.get("embedding_model"))
        self._reindex_pending = False
        self.tool_confirm_cb: Optional[Callable[[str], bool]] = None  # GUI 注入：confirm 档写命令的用户确认回调
        self.eventbus = None  # GUI 注入：kernel.eventbus（工具执行旁路通知）
        # 向量索引异步队列（kernel.embedqueue，GUI 注入）：embedding 不再同步
        # 阻塞聊天/记忆写入；None（测试/CLI 直连）时保持同步旧行为
        self.embed_queue = embed_queue
        self.memory = Memory(self.db)
        self.chat_history = self.db.chat_items(100)
        self.state: dict = self._load_state()
        self._sync_embed_model()
        self.clock = clock or datetime.now
        self.brain = core.Brain(
            cfg, self.plugins, stats, energy_cb=self._consume_energy
        )
        # 领域模块（组合）：记忆 / 规划 —— 静态内置实现（自进化已移除）。
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
            "embed_model": "",          # 当前向量模型（切换时重建向量表）
            "energy_date": "",          # 体力日（按醒来时间划分）
            "energy_used": 0,           # 当天已消耗的 LLM 调用次数
            "sleep_mode": False,        # 是否处于睡眠模式
            "current_desire_id": None,  # 当前欲望/计划 id
            "last_wake_date": "",       # 当天是否已执行过唤醒
            "activity_log": [],         # 最近活动日志（cap 50）
            "desires": [],              # 欲望/计划列表（状态 JSON）
            "conversation_summary": "", # 旧对话滚动摘要（超限压缩用）
        }
        return {
            key: self.db.get_state(key, default)
            for key, default in defaults.items()
        }

    def _save_state(self):
        for key, value in self.state.items():
            self.db.set_state(key, value)

    def _sync_embed_model(self):
        """模型变化时清空旧向量，避免新旧语义混用/维度不匹配。"""
        model = self.cfg.get("embedding_model", "BAAI/bge-small-zh-v1.5")
        previous = self.state.get("embed_model", "")
        if previous and previous != model:
            # 队列里待处理的旧模型任务一并作废（向量表已清空，reindex 会全表补齐）
            if self.embed_queue is not None:
                try:
                    self.embed_queue.clear()
                except Exception:
                    pass
            try:
                self.db.clear_embeddings("memory")
                self.db.clear_embeddings("chat")
            except Exception:
                pass
        self.state["embed_model"] = model
        self._save_state()

    # ---------- 体力模型（LLM 调用次数 = 每日体力） ----------

    def _energy_day(self, now=None):
        """体力日按醒来时间划分：quiet_end 之前算前一天。"""
        now = now or self.clock()
        end = int(self.cfg.get("quiet_end", 7) or 7)
        day = now.date()
        if now.hour < end:
            day = day - timedelta(days=1)
        return day.isoformat()

    def _consume_energy(self, amount=1):
        """每次成功 LLM 调用扣 1 点体力；无 API Key 时不计。"""
        if not (self.cfg.get("api") or {}).get("api_key"):
            return
        day = self._energy_day()
        if self.state.get("energy_date") != day:
            self.state["energy_date"] = day
            self.state["energy_used"] = 0
        self.state["energy_used"] = int(self.state.get("energy_used", 0)) + amount
        self._save_state()

    def _energy_used(self, now=None):
        day = self._energy_day(now)
        if self.state.get("energy_date") != day:
            return 0
        return int(self.state.get("energy_used", 0))

    def _energy_remaining(self, now=None):
        budget = int(self.cfg.get("daily_energy_budget", 1000) or 1000)
        return max(0, budget - self._energy_used(now))

    def _proactive_energy_ok(self, now=None):
        """主动思考/行动还有没有余力：同时受总预算和主动预算约束。"""
        used = self._energy_used(now)
        budget = int(self.cfg.get("daily_energy_budget", 1000) or 1000)
        cap = int(self.cfg.get("proactive_energy_daily_cap", 150) or 150)
        return used < budget and used < cap

    def _log_activity(self, kind, summary, energy=0):
        """活动日志：它主动想了/做了什么，便于观察（cap 50 条）。"""
        log = list(self.state.get("activity_log") or [])
        log.append({
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "kind": kind,
            "summary": str(summary or "")[:200],
            "energy": int(energy or 0),
        })
        self.state["activity_log"] = log[-50:]
        self._save_state()

    def reload(self, cfg, plugins=None):
        self.cfg = cfg
        if plugins is not None:
            self.plugins = plugins
        self.brain = core.Brain(
            cfg, self.plugins, self.stats, energy_cb=self._consume_energy
        )
        # embedder 仅在模型配置变化时重建，避免每次保存都重新加载/下载模型
        sig = (cfg.get("embedding_enabled"), cfg.get("embedding_model"))
        if sig != self._embed_sig:
            self._embed_sig = sig
            self.embedder = rag.default_embedder(cfg, self.data_dir)
            self._sync_embed_model()
        # 补索引挪到后台线程执行（reindex_async），避免保存设置卡 UI
        self._reindex_pending = True

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

    # ---------- 聊天入口 ----------

    def chat(self, user_text, on_delta=None):
        user_text = user_text.strip()
        entry = self.append_chat("user", user_text)
        # 规则提取作为零成本兜底；有 LLM 时主路径是 analyze_and_remember
        rule_saved = self._extract_facts_rule(user_text)
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
        if (self.cfg.get("api") or {}).get("api_key"):
            # 有 LLM 时由 Agent 自己分析该记住什么（不依赖写死规则）
            if self.memory_module.should_analyze(user_text, rule_saved):
                try:
                    self._analyze_async(user_text, reply)
                except Exception:
                    pass
        if self.stats:
            self.stats.record_chat(2)
        return reply

    # ---------- 委托壳（领域模块组合：经 self.memory_module / self.planner） ----------

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
        """聊天向量索引（委托 brain.memory）：有异步队列时入队，否则同步。"""
        if self.embed_queue is not None:
            try:
                if self.embed_queue.enqueue("chat", item_id, text):
                    return
            except Exception:
                pass
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
                if fact:
                    item_id = self._remember("fact", fact, importance=4)
                    if item_id is not None and self.stats:
                        self.stats.record_fact()
            elif text.startswith("[FACT:"):
                # [FACT:category] 结构化事实（LLM 可自行分类，如 [FACT:schedule]）
                category = text[6:text.find("]")].strip()
                fact = text[text.find("]") + 1:].strip()
                if category and fact:
                    item_id = self._remember(
                        "fact",
                        fact,
                        category=category,
                        importance=4,
                        expires_at=self._parse_schedule_expiry(fact),
                    )
                    if item_id is not None and self.stats:
                        self.stats.record_fact()
            elif text.startswith("[OBSERVE]"):
                # 巡视时的观察记录（低重要性，不显示给主人）
                observe = text[9:].strip()
                if observe:
                    item_id = self._remember(
                        "thought", observe, importance=2, source="observation"
                    )
                    if item_id is not None and self.stats:
                        self.stats.record_thought()
            elif text.startswith("[THINK]"):
                thought = text[7:].strip()
                if thought:
                    item_id = self._remember("thought", thought)
                    if item_id is not None and self.stats:
                        self.stats.record_thought()
            else:
                body.append(line)
        return "\n".join(body).strip()

    def _extract_facts_watermark(self, limit=100):
        """巡视补采水位线之后的主人对白（委托 brain.memory）。"""
        self.memory_module.extract_facts_watermark(limit)

    def _extract_facts_rule(self, user_text):
        """规则事实提取（委托 brain.memory）。"""
        return self.memory_module.extract_facts(user_text)

    # ---------- Coding 协作（P0：同步循环） ----------

    def coding_task(self, user_text, on_status=None, on_delta=None, max_rounds=None):
        """Coding 模式入口：在 project_dir 项目内完成编程任务。

        安全基座在 kernel（pathguard/processpool/权限判定），控制循环在
        brain.coding_agent（策略层）。工具以 SOURCE_USER 执行——写操作
        走 confirm 档用户确认，与聊天路径的 confirm_cb 共用同一弹窗。
        """
        from brain.coding_agent import run_coding_task

        def run(name, arguments):
            return self._run_tool(name, arguments, source=tools.SOURCE_USER)

        return run_coding_task(
            self.brain, self.cfg, user_text, run,
            on_status=on_status, on_delta=on_delta, max_rounds=max_rounds,
        )
