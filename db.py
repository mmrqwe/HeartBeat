"""SQLite 存储层：记忆、聊天、状态、统计、向量检索（sqlite-vec）。"""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

VEC_DIM = 512  # BAAI/bge-small-zh-v1.5 的向量维度
DEFAULT_MEMORY_CAP = 500  # 记忆条数上限（可被配置 memory_cap 覆盖）


def _merge_replace(text, old, new):
    """替换 text 中所有 old，并修复与相邻字的重复：

    old 前一字 == new 首字 → 把前一字并入替换范围（“喝咖啡”→“喝茶”，
    不产生“喝喝茶”）；old 后一字 == new 尾字 → 后一字并入（“咖啡豆”→
    “咖啡”，不产生“咖啡咖啡”）。返回替换后的文本。
    """
    out = []
    i = 0
    n = len(old)
    while True:
        j = text.find(old, i)
        if j < 0:
            out.append(text[i:])
            break
        start, end = j, j + n
        if start > 0 and new and text[start - 1] == new[0]:
            start -= 1
        if end < len(text) and new and text[end] == new[-1]:
            end += 1
        out.append(text[i:start])
        out.append(new)
        i = end
    return "".join(out)


class EventType:
    """事件时间线类型常量（P1 Event Store）：集中管理防拼写错误。

    与 tool_logs / stats / updates.log 并存：events 只做统一时间线，
    统计与审计继续用各自机制（不替代）。
    """

    CHAT_STARTED = "chat.started"
    CHAT_FINISHED = "chat.finished"
    TICK_STARTED = "tick.started"
    TICK_FINISHED = "tick.finished"
    TOOL_CALLED = "tool.called"
    MEMORY_CREATED = "memory.created"
    BRAIN_UPDATED = "brain.updated"


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.vec_ready = self._init_vec()

    def _init_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'misc',
                    importance INTEGER NOT NULL DEFAULT 3,
                    source TEXT NOT NULL DEFAULT 'chat',
                    expires_at TEXT,
                    last_used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_role_text
                    ON memory(role, text);
                CREATE TABLE IF NOT EXISTS chat_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    project_dir TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stats_daily(
                    date TEXT PRIMARY KEY,
                    llm_calls INTEGER NOT NULL DEFAULT 0,
                    llm_errors INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    llm_latency_ms INTEGER NOT NULL DEFAULT 0,
                    chat_messages INTEGER NOT NULL DEFAULT 0,
                    proactive_messages INTEGER NOT NULL DEFAULT 0,
                    thoughts INTEGER NOT NULL DEFAULT 0,
                    facts INTEGER NOT NULL DEFAULT 0,
                    ticks INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    uptime_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stats_collectors(
                    date TEXT NOT NULL,
                    plugin TEXT NOT NULL,
                    fetches INTEGER NOT NULL DEFAULT 0,
                    fails INTEGER NOT NULL DEFAULT 0,
                    entries INTEGER NOT NULL DEFAULT 0,
                    chars INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(date, plugin)
                );
                CREATE TABLE IF NOT EXISTS content_hashes(
                    plugin TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0,
                    ok INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
                CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(stats_daily)")
            }
            if "tool_calls" not in columns:
                self._conn.execute(
                    "ALTER TABLE stats_daily "
                    "ADD COLUMN tool_calls INTEGER NOT NULL DEFAULT 0"
                )
            mem_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(memory)")
            }
            for col, ddl in (
                ("category", "TEXT NOT NULL DEFAULT 'misc'"),
                ("importance", "INTEGER NOT NULL DEFAULT 3"),
                ("source", "TEXT NOT NULL DEFAULT 'chat'"),
                ("expires_at", "TEXT"),
                ("last_used_at", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                if col not in mem_columns:
                    self._conn.execute(f"ALTER TABLE memory ADD COLUMN {col} {ddl}")
            # 会话分栏迁移：旧库 chat_messages 无 session_id → 补齐并归入默认会话
            chat_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(chat_messages)")
            }
            if "session_id" not in chat_columns:
                self._conn.execute(
                    "ALTER TABLE chat_messages "
                    "ADD COLUMN session_id TEXT NOT NULL DEFAULT 'default'"
                )
            # 索引必须在列迁移之后建（旧库 executescript 时列还不存在）
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_session "
                "ON chat_messages(session_id, id)"
            )
            self._ensure_default_session()
            self._conn.commit()

    def _ensure_default_session(self):
        """默认会话（无项目目录的闲聊）必须存在；旧数据都归它。"""
        now = time.strftime("%Y-%m-%d %H:%M")
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions(id, name, project_dir, created_at, updated_at) "
            "VALUES ('default', '默认对话', NULL, ?, ?)",
            (now, now),
        )

    # ---------- 会话 ----------

    def list_sessions(self):
        """会话列表（默认会话置顶），含消息数，按最近活跃排序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.name, s.project_dir, s.created_at, s.updated_at, "
                "       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count "
                "FROM sessions s "
                "ORDER BY CASE s.id WHEN 'default' THEN 0 ELSE 1 END, s.updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def session(self, session_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, project_dir, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_session_by_project_dir(self, project_dir):
        """按项目目录找会话（目录为主键绑定）；无匹配返回 None。"""
        if not str(project_dir or "").strip():
            return self.session("default")
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, project_dir, created_at, updated_at "
                "FROM sessions WHERE project_dir = ?",
                (str(project_dir),),
            ).fetchone()
        return dict(row) if row else None

    def create_session(self, name, project_dir=None):
        """新建会话；project_dir 已存在会话时直接返回已有会话（目录↔会话一对一）。"""
        import uuid

        project_dir = str(project_dir or "").strip() or None
        existing = self.find_session_by_project_dir(project_dir) if project_dir else None
        if existing is not None:
            return existing["id"]
        now = time.strftime("%Y-%m-%d %H:%M")
        sid = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, name, project_dir, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, str(name or "会话")[:40], project_dir, now, now),
            )
            self._conn.commit()
        return sid

    def rename_session(self, session_id, name):
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET name = ? WHERE id = ?",
                (str(name or "会话")[:40], session_id),
            )
            self._conn.commit()

    def delete_session(self, session_id):
        """删除会话并级联删除其消息。默认会话不可删（返回 False）。"""
        if session_id == "default":
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            if self.vec_ready:
                # 会话消息已删，顺手清掉对应 chat_vec 孤儿向量
                self._conn.execute(
                    "DELETE FROM chat_vec WHERE rowid NOT IN "
                    "(SELECT id FROM chat_messages)"
                )
            self._conn.commit()
            return cur.rowcount > 0

    def _touch_session(self, session_id):
        """活跃时间更新（消息写入时调用；会话不存在时静默跳过）。"""
        if session_id is None:
            return
        now = time.strftime("%Y-%m-%d %H:%M")
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )

    def _init_vec(self):
        try:
            import sqlite_vec

            with self._lock:
                self._conn.enable_load_extension(True)
                sqlite_vec.load(self._conn)
                self._conn.executescript(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec
                    USING vec0(embedding float[{VEC_DIM}]);
                    CREATE VIRTUAL TABLE IF NOT EXISTS chat_vec
                    USING vec0(embedding float[{VEC_DIM}]);
                    """
                )
                self._conn.commit()
            return True
        except Exception:
            return False

    # ---------- 记忆 ----------

    def add_memory(self, role, text, category="misc", importance=3, source="chat",
                   expires_at=None):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory(role, text, created_at, category, importance, "
                "source, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    role,
                    text.strip(),
                    time.strftime("%Y-%m-%d %H:%M"),
                    category,
                    importance,
                    source,
                    expires_at,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def memory_items(self, roles=None, limit=100):
        query = (
            "SELECT id, role, text, created_at AS time, category, importance, "
            "source, expires_at, last_used_at FROM memory"
        )
        params = []
        if roles:
            placeholders = ",".join("?" for _ in roles)
            query += f" WHERE role IN ({placeholders})"
            params = list(roles)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def find_fact_by_text(self, text):
        """事实精确查重：SQL 等值匹配（走 idx_memory_role_text 索引）。

        替代“全量加载 + Python 循环”的去重抽象（O(N) → O(log N)）；
        语义去重（向量近重复）仍由调用方在未命中时兜底。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM memory WHERE role='fact' AND text=? LIMIT 1",
                (str(text or "").strip(),),
            ).fetchone()
        return row["id"] if row else None

    def find_fact_like(self, keyword):
        """模糊定位第一条包含关键词的事实（纠错路径找旧记忆）。

        LLM 引用的旧记忆原文可能与库内文本有细微差异（空格/标点），
        精确匹配失败后用 LIKE 兜底；% 与 _ 从关键词中剔除防通配注入。
        """
        escaped = str(keyword or "").replace("%", "").replace("_", "").strip()
        if not escaped:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM memory WHERE role='fact' AND text LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                (f"%{escaped}%",),
            ).fetchone()
        return row["id"] if row else None

    def memory_item(self, item_id):
        """按 id 取单条记忆（纠错后重嵌向量用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, role, text, created_at AS time, category, importance, "
                "source, expires_at, last_used_at, updated_at FROM memory "
                "WHERE id = ?",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_memory_text(self, item_id, text):
        """原地更正记忆文本：保留 id/created_at/category，记录 updated_at。

        返回受影响行数（0/1）。向量由调用方重嵌（update 后旧向量与新文本
        不一致，检索层按 rowid 回查 base 表取新文本，仅排序语义略旧）。
        """
        text = str(text or "").strip()
        if not text:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory SET text = ?, updated_at = ? WHERE id = ?",
                (text, time.strftime("%Y-%m-%d %H:%M"), item_id),
            )
            self._conn.commit()
            return cur.rowcount

    def replace_fact_term(self, old_term, new_term, min_len=2, max_rows=2):
        """把事实文本中的旧词原地替换为新词（“不是X，是Y”纠错）。

        护栏：① 词长下限防短词误伤；② 已含新词的行跳过（防“长电”→“长电科技”
        导致“长电科技科技”式增长）；③ 受影响行数上限——超过视为旧词太泛，
        返回空列表（调用方降级 LLM 精确处理）；④ 相邻字重叠修复——旧词前一字
        与新词首字相同（“喝咖啡”→“喝茶”）时把前一字并入替换范围，防“喝喝茶”
        式残片；旧词后一字与新词尾字相同同理。
        返回受影响的事实 id 列表（供调用方重嵌向量）。
        """
        old_term = str(old_term or "").strip()
        new_term = str(new_term or "").strip()
        if len(old_term) < min_len or not new_term or old_term == new_term:
            return []
        old_esc = old_term.replace("%", "").replace("_", "")
        new_esc = new_term.replace("%", "").replace("_", "")
        if not old_esc or not new_esc:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text FROM memory WHERE role='fact' AND text LIKE ? "
                "AND text NOT LIKE ?",
                (f"%{old_esc}%", f"%{new_esc}%"),
            ).fetchall()
            if not rows or len(rows) > max_rows:
                return []
            ids = []
            for row in rows:
                new_text = _merge_replace(row["text"], old_term, new_term)
                if not new_text or new_text == row["text"]:
                    continue
                self._conn.execute(
                    "UPDATE memory SET text = ?, updated_at = ? WHERE id = ?",
                    (new_text, time.strftime("%Y-%m-%d %H:%M"), row["id"]),
                )
                ids.append(row["id"])
            self._conn.commit()
            return ids

    def memory_profile(self, limit_per=3, roles=None, now=None):
        """按类别分组的记忆画像：每类取 importance 最高的 limit_per 条。

        返回 [{category, items: [...]}]，未过期优先、按 importance 排序。
        """
        now = now or time.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, text, created_at AS time, category, importance, "
                "source, expires_at, last_used_at FROM memory "
                "WHERE (expires_at IS NULL OR expires_at >= ?) "
                "ORDER BY importance DESC, id DESC",
                (now,),
            ).fetchall()
        groups = {}
        for row in rows:
            item = dict(row)
            role = item["role"]
            if roles and role not in roles:
                continue
            category = item.get("category") or "misc"
            if category not in groups:
                groups[category] = []
            if len(groups[category]) < limit_per:
                groups[category].append(item)
        return [
            {"category": category, "items": items}
            for category, items in groups.items()
        ]

    def mark_memory_used(self, memory_id):
        with self._lock:
            self._conn.execute(
                "UPDATE memory SET last_used_at=? WHERE id=?",
                (time.strftime("%Y-%m-%d %H:%M"), memory_id),
            )
            self._conn.commit()

    def memory_schedule_due(self, within_hours=12, now=None):
        """即将到期的日程类记忆（用于主动提醒）：expires_at 在 [now, now+window]。"""
        now = now or time.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, created_at AS time, expires_at FROM memory "
                "WHERE role='fact' AND category='schedule' AND expires_at IS NOT NULL "
                "AND expires_at >= ? AND expires_at <= datetime(?, ?)",
                (now, now, f"+{within_hours} hours"),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_memory(self):
        with self._lock:
            self._conn.execute("DELETE FROM memory")
            if self.vec_ready:
                self._conn.execute("DELETE FROM memory_vec")
            self._conn.commit()

    # ---------- 聊天 ----------

    def add_chat(self, role, text, session_id="default"):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO chat_messages(role, text, created_at, session_id) "
                "VALUES (?, ?, ?, ?)",
                (role, text.strip(), time.strftime("%Y-%m-%d %H:%M"), session_id or "default"),
            )
            self._touch_session(session_id)
            self._conn.commit()
            return cur.lastrowid

    def chat_items(self, limit=100, session_id=None):
        """session_id=None 返回全量（兼容旧调用/记忆水位线），否则只取该会话。"""
        with self._lock:
            if session_id is None:
                rows = self._conn.execute(
                    "SELECT id, role, text, created_at AS time, session_id FROM chat_messages "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, role, text, created_at AS time, session_id FROM chat_messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_chat(self, session_id=None):
        """session_id=None 全清（旧语义）；显式传只清该会话。"""
        with self._lock:
            if session_id is None:
                self._conn.execute("DELETE FROM chat_messages")
                if self.vec_ready:
                    self._conn.execute("DELETE FROM chat_vec")
            else:
                self._conn.execute(
                    "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
                )
                if self.vec_ready:
                    # 只清该会话消息对应的向量，其他会话的 chat_vec 保留
                    self._conn.execute(
                        "DELETE FROM chat_vec WHERE rowid NOT IN "
                        "(SELECT id FROM chat_messages)"
                    )
            self._conn.commit()

    def chat_after(self, message_id, limit=100):
        """水位线之后的主人对白（用于巡视时补采记忆，幂等）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text FROM chat_messages "
                "WHERE role='user' AND id > ? ORDER BY id LIMIT ?",
                (int(message_id), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id):
        """删除单条记忆（设置页记忆管理）。"""
        with self._lock:
            self._conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
            if self.vec_ready:
                self._conn.execute(
                    "DELETE FROM memory_vec WHERE rowid = ?", (memory_id,)
                )
            self._conn.commit()

    def streak_days(self, now=None):
        """连续陪伴天数：从今天（今天没聊则从昨天）往前数连续有聊天的天数。"""
        from datetime import datetime, timedelta

        today = (now or datetime.now()).strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                "SELECT date FROM stats_daily WHERE chat_messages > 0 "
                "ORDER BY date DESC"
            ).fetchall()
        days = {r["date"] for r in rows}
        streak = 0
        day = today
        if day not in days:
            day = (
                datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)
            ).strftime("%Y-%m-%d")
        while day in days:
            streak += 1
            day = (
                datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
            ).strftime("%Y-%m-%d")
        return streak

    # ---------- Agent 状态 ----------

    def get_state(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM agent_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    def set_state(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()

    def delete_state(self, key):
        with self._lock:
            self._conn.execute("DELETE FROM agent_state WHERE key = ?", (key,))
            self._conn.commit()

    # ---------- 统计 ----------

    @staticmethod
    def _today_key():
        return time.strftime("%Y-%m-%d")

    def stats_get(self, date=None):
        date = date or self._today_key()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM stats_daily WHERE date = ?", (date,)
            ).fetchone()
            colls = self._conn.execute(
                "SELECT * FROM stats_collectors WHERE date = ?", (date,)
            ).fetchall()
        day = dict(row) if row else {
            "date": date,
            "llm_calls": 0,
            "llm_errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "llm_latency_ms": 0,
            "chat_messages": 0,
            "proactive_messages": 0,
            "thoughts": 0,
            "facts": 0,
            "ticks": 0,
            "tool_calls": 0,
            "uptime_seconds": 0.0,
        }
        day["collectors"] = {
            c["plugin"]: {
                "fetches": c["fetches"],
                "fails": c["fails"],
                "entries": c["entries"],
                "chars": c["chars"],
                "cache_hits": c["cache_hits"],
            }
            for c in colls
        }
        return day

    def stats_record_llm(self, prompt_tokens=0, completion_tokens=0, cached_tokens=0,
                         ok=True, latency_ms=0):
        date = self._today_key()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO stats_daily(date, llm_calls, llm_errors, prompt_tokens,
                    completion_tokens, cached_tokens, llm_latency_ms)
                VALUES (?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    llm_calls = llm_calls + 1,
                    llm_errors = llm_errors + excluded.llm_errors,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    llm_latency_ms = llm_latency_ms + excluded.llm_latency_ms
                """,
                (date, 0 if ok else 1, prompt_tokens, completion_tokens,
                 cached_tokens, latency_ms),
            )
            self._conn.commit()

    def stats_record_collect(self, plugin, ok, entries=0, chars=0, cache_hit=False):
        date = self._today_key()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO stats_collectors(date, plugin, fetches, fails, entries, chars, cache_hits)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, plugin) DO UPDATE SET
                    fetches = fetches + excluded.fetches,
                    fails = fails + excluded.fails,
                    entries = entries + excluded.entries,
                    chars = chars + excluded.chars,
                    cache_hits = cache_hits + excluded.cache_hits
                """,
                (date, plugin, 1 if ok else 0, 0 if ok else 1,
                 entries, chars, 1 if cache_hit else 0),
            )
            self._conn.commit()

    def stats_add(self, field, delta=1):
        if field not in (
            "chat_messages", "proactive_messages", "thoughts", "facts", "ticks",
            "tool_calls",
            "uptime_seconds",
        ):
            raise ValueError(f"unknown stats field: {field}")
        date = self._today_key()
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO stats_daily(date, {field}) VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET {field} = {field} + excluded.{field}
                """,
                (date, delta),
            )
            self._conn.commit()

    def stats_totals(self):
        fields = (
            "llm_calls", "llm_errors", "prompt_tokens", "completion_tokens",
            "cached_tokens", "chat_messages", "proactive_messages", "thoughts",
            "facts", "ticks", "tool_calls", "uptime_seconds",
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT " + ", ".join(f"SUM({f}) AS {f}" for f in fields)
                + " FROM stats_daily"
            ).fetchone()
            coll_rows = self._conn.execute(
                """
                SELECT plugin, SUM(fetches) AS fetches, SUM(fails) AS fails,
                    SUM(entries) AS entries, SUM(chars) AS chars,
                    SUM(cache_hits) AS cache_hits
                FROM stats_collectors GROUP BY plugin
                """
            ).fetchall()
        total = {f: (row[f] or 0) for f in fields}
        total["collectors"] = {
            c["plugin"]: {
                "fetches": c["fetches"] or 0,
                "fails": c["fails"] or 0,
                "entries": c["entries"] or 0,
                "chars": c["chars"] or 0,
                "cache_hits": c["cache_hits"] or 0,
            }
            for c in coll_rows
        }
        return total

    def stats_days(self, limit=7):
        with self._lock:
            rows = self._conn.execute(
                "SELECT date FROM stats_daily ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.stats_get(row["date"]) for row in reversed(rows)]

    def stats_clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM stats_daily")
            self._conn.execute("DELETE FROM stats_collectors")
            self._conn.execute("DELETE FROM content_hashes")
            self._conn.commit()

    # ---------- 工具审计 ----------

    def log_tool(self, source, tool, detail, mode, approved, ok, summary=""):
        """记录一次工具调用审计（含搜索 / bash / 拒绝的执行）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_logs(ts, source, tool, detail, mode, approved, ok, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%d %H:%M"), source, tool,
                 (detail or "")[:2000], mode, 1 if approved else 0, 1 if ok else 0,
                 (summary or "")[:500]),
            )
            self._conn.commit()

    def log_event(self, type_, source="", payload=None, trace_id=""):
        """事件时间线（P1）：统一调试/监控时间线，与 tool_logs/stats 并存。

        - 同步单行 INSERT（WAL 亚毫秒），不做异步队列——埋点必须在主链路
          立即可见，且单行插入开销可忽略；
        - 任何异常静默返回 False：埋点绝不阻断主链路；
        - payload 为 JSON 可序列化对象；trace_id 关联一次会话（chat_xxx/tick_xxx）。
        """
        try:
            blob = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events(ts, type, source, payload, trace_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        time.strftime("%Y-%m-%dT%H:%M:%S"),
                        str(type_),
                        str(source or ""),
                        blob,
                        str(trace_id or ""),
                    ),
                )
                self._conn.commit()
            return True
        except Exception:
            return False

    def event_items(self, type_=None, limit=100, trace_id=None):
        """查询事件时间线（调试用）：按时间倒序，可按类型/会话过滤。"""
        query = "SELECT id, ts, type, source, payload, trace_id FROM events"
        params = []
        conditions = []
        if type_:
            conditions.append("type = ?")
            params.append(str(type_))
        if trace_id:
            conditions.append("trace_id = ?")
            params.append(str(trace_id))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def tool_log_items(self, limit=100):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, source, tool, detail, mode, approved, ok, summary "
                "FROM tool_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # ---------- 内容缓存标记 ----------

    def content_hash(self, plugin):
        with self._lock:
            row = self._conn.execute(
                "SELECT digest FROM content_hashes WHERE plugin = ?", (plugin,)
            ).fetchone()
        return row["digest"] if row else None

    def set_content_hash(self, plugin, digest):
        with self._lock:
            self._conn.execute(
                "INSERT INTO content_hashes(plugin, digest, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(plugin) DO UPDATE SET digest = excluded.digest, "
                "updated_at = excluded.updated_at",
                (plugin, digest, time.strftime("%Y-%m-%d %H:%M")),
            )
            self._conn.commit()

    # ---------- 向量 ----------

    def add_embedding(self, table, row_id, vector):
        if not self.vec_ready:
            return False
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        blob = json.dumps([float(x) for x in vector])
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {table}_vec(rowid, embedding) VALUES (?, ?)",
                (row_id, blob),
            )
            self._conn.commit()
        return True

    def remove_embedding(self, table, row_id):
        """删除单条向量行（纠错重嵌失败时保底，让 reindex 按新文本补嵌）。"""
        if not self.vec_ready:
            return False
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        with self._lock:
            self._conn.execute(
                f"DELETE FROM {table}_vec WHERE rowid = ?", (row_id,)
            )
            self._conn.commit()
        return True

    def search_embeddings(self, table, vector, k=5, now=None, min_distance=None,
                          roles=None):
        """向量召回：过滤过期记忆，可选距离阈值与角色过滤。"""
        if not self.vec_ready:
            return []
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        base = "memory" if table == "memory" else "chat_messages"
        blob = json.dumps([float(x) for x in vector])
        now = now or time.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT rowid, distance FROM {table}_vec "
                "WHERE embedding MATCH ? AND k = ?",
                (blob, k),
            ).fetchall()
            results = []
            for rowid, distance in rows:
                if min_distance is not None and distance > min_distance:
                    continue
                row = self._conn.execute(
                    f"SELECT id, role, text, created_at AS time FROM {base} WHERE id = ?",
                    (rowid,),
                ).fetchone()
                if not row:
                    continue
                if roles and row["role"] not in roles:
                    continue
                item = dict(row)
                item["distance"] = distance
                if table == "memory":
                    meta = self._conn.execute(
                        "SELECT expires_at, importance, last_used_at, category "
                        "FROM memory WHERE id = ?",
                        (rowid,),
                    ).fetchone()
                    if not meta:
                        continue
                    if meta["expires_at"] and meta["expires_at"] < now:
                        continue
                    item["importance"] = meta["importance"]
                    item["last_used_at"] = meta["last_used_at"]
                    item["category"] = meta["category"]
                results.append(item)
        return results

    def search_memory_keywords(self, query, k=5, roles=("fact", "thought"), now=None):
        """关键词兜底：向量不可用/零结果时用 LIKE（含中文二元组）检索。"""
        now = now or time.strftime("%Y-%m-%d %H:%M")
        text = (query or "").strip()
        if not text:
            return []
        terms = set()
        for tok in re.findall(r"[A-Za-z0-9]+", text):
            if len(tok) >= 2:
                terms.add(tok.lower())
        for i in range(len(text) - 1):
            bigram = text[i:i + 2]
            if re.search(r"[\u4e00-\u9fff]", bigram):
                terms.add(bigram)
        terms = list(terms)[:8]
        if not terms:
            return []
        role_sql = ""
        params = []
        if roles:
            role_sql = " AND role IN ({})".format(",".join("?" for _ in roles))
            params.extend(roles)
        like_sql = " OR ".join("text LIKE ?" for _ in terms)
        params.extend(f"%{t}%" for t in terms)
        full_term = text[:30]
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, text, created_at AS time, category, importance, "
                "source, expires_at, last_used_at FROM memory "
                f"WHERE (expires_at IS NULL OR expires_at >= ?){role_sql} "
                f"AND ({like_sql}) "
                "ORDER BY CASE WHEN text LIKE ? THEN 0 ELSE 1 END, "
                "importance DESC, id DESC LIMIT ?",
                [now] + params + [f"%{full_term}%", k],
            ).fetchall()
        return [dict(r) for r in rows]

    def fact_texts(self):
        """全部事实文本（去重范围不再限于最近 20 条）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT text FROM memory WHERE role='fact'"
            ).fetchall()
        return {r["text"] for r in rows}

    def delete_memory_like(self, keyword, category=None):
        """显式“忘记/删除”时删除相关事实（含向量行）。"""
        params = [f"%{keyword}%"]
        cat_sql = ""
        if category:
            cat_sql = " AND category = ?"
            params.append(category)
        with self._lock:
            ids = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM memory WHERE role='fact' AND text LIKE ?" + cat_sql,
                    params,
                ).fetchall()
            ]
            if ids:
                marks = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"DELETE FROM memory WHERE id IN ({marks})", ids
                )
                if self.vec_ready:
                    self._conn.execute(
                        f"DELETE FROM memory_vec WHERE rowid IN ({marks})", ids
                    )
                self._conn.commit()
        return len(ids)

    def retire_memory_like(self, keyword, category=None, now=None):
        """矛盾事实更新：把“主人喜欢 X”类旧事实标记为过期，并同步清理
        其向量行（此前只改 expires_at，旧向量残留靠检索层过滤是隐式耦合）。"""
        now = now or time.strftime("%Y-%m-%d %H:%M")
        params = [f"%{keyword}%"]
        cat_sql = ""
        if category:
            cat_sql = " AND category = ?"
            params.append(category)
        with self._lock:
            ids = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM memory WHERE role='fact' AND text LIKE ?" + cat_sql,
                    params,
                ).fetchall()
            ]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            cur = self._conn.execute(
                f"UPDATE memory SET expires_at = ? WHERE id IN ({marks})",
                [now] + ids,
            )
            if self.vec_ready:
                self._conn.execute(
                    f"DELETE FROM memory_vec WHERE rowid IN ({marks})", ids
                )
            self._conn.commit()
            return cur.rowcount

    def cleanup_memory(self, now=None, cap=DEFAULT_MEMORY_CAP):
        """生命周期清理：删除过期项；超上限时按 importance/最近使用/新旧淘汰。
        返回 (expired_deleted, cap_deleted)。"""
        now = now or time.strftime("%Y-%m-%d %H:%M")
        expired = []
        excess = []
        with self._lock:
            expired = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                ).fetchall()
            ]
            if expired:
                marks = ",".join("?" for _ in expired)
                self._conn.execute(f"DELETE FROM memory WHERE id IN ({marks})", expired)
                if self.vec_ready:
                    self._conn.execute(
                        f"DELETE FROM memory_vec WHERE rowid IN ({marks})", expired
                    )
            rows = self._conn.execute(
                "SELECT id, importance, last_used_at, created_at FROM memory "
                "ORDER BY importance DESC, "
                "(last_used_at IS NULL) ASC, last_used_at DESC, id DESC"
            ).fetchall()
            cap = max(0, int(cap or DEFAULT_MEMORY_CAP))
            excess = [r["id"] for r in rows[cap:]]
            if excess:
                marks = ",".join("?" for _ in excess)
                self._conn.execute(f"DELETE FROM memory WHERE id IN ({marks})", excess)
                if self.vec_ready:
                    self._conn.execute(
                        f"DELETE FROM memory_vec WHERE rowid IN ({marks})", excess
                    )
            self._conn.commit()
        return len(expired), len(excess)

    def clear_embeddings(self, table):
        """清空单个向量表（模型切换后重建用）。"""
        if not self.vec_ready:
            return False
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        with self._lock:
            self._conn.execute(f"DELETE FROM {table}_vec")
            self._conn.commit()
        return True

    def ids_without_embedding(self, table):
        if not self.vec_ready:
            return []
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        base = "memory" if table == "memory" else "chat_messages"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM {base} WHERE id NOT IN (SELECT rowid FROM {table}_vec)"
            ).fetchall()
        return [row["id"] for row in rows]

    def reindex(self, embedder, table):
        """给还没有向量的记录补嵌入。"""
        if not self.vec_ready or not embedder.ready:
            return 0
        if table not in ("memory", "chat"):
            raise ValueError("table must be memory or chat")
        base = "memory" if table == "memory" else "chat_messages"
        ids = self.ids_without_embedding(table)
        count = 0
        for item_id in ids:
            with self._lock:
                row = self._conn.execute(
                    f"SELECT text FROM {base} WHERE id = ?", (item_id,)
                ).fetchone()
            if row and self.add_embedding(table, item_id, embedder.embed_one(row["text"])):
                count += 1
        return count

    def close(self):
        with self._lock:
            self._conn.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Memory:
    """记忆访问层：兼容旧接口，底层是 SQLite。"""

    def __init__(self, source, limit=80):
        self.db = source if isinstance(source, Database) else Database(source)
        self.limit = limit

    @property
    def items(self):
        return self.db.memory_items(limit=self.limit)

    def add(self, role, text, category="misc", importance=3, source="chat",
            expires_at=None):
        return self.db.add_memory(
            role, text, category=category, importance=importance,
            source=source, expires_at=expires_at,
        )

    def recent(self, n=10, roles=None):
        return self.db.memory_items(roles=roles, limit=n)

    def facts(self, n=20):
        return self.db.memory_items(roles=("fact",), limit=n)

    def all_facts(self):
        return self.db.memory_items(roles=("fact",), limit=None)

    def thoughts(self, n=20):
        return self.db.memory_items(roles=("thought",), limit=n)

    def clear(self):
        self.db.clear_memory()


class Stats:
    """统计访问层：兼容旧接口，底层是 SQLite。"""

    def __init__(self, source):
        self.db = source if isinstance(source, Database) else Database(source)
        self._last_uptime_ts = time.time()

    def check_content_hash(self, plugin, digest):
        previous = self.db.content_hash(plugin)
        self.db.set_content_hash(plugin, digest)
        return previous == digest

    def record_llm(self, prompt_tokens=0, completion_tokens=0, cached_tokens=0,
                   ok=True, latency_ms=0):
        self.db.stats_record_llm(prompt_tokens, completion_tokens, cached_tokens, ok, latency_ms)

    def record_collect(self, plugin, ok, entries=0, chars=0, cache_hit=False):
        self.db.stats_record_collect(plugin, ok, entries, chars, cache_hit)

    def record_chat(self, count=1):
        self.db.stats_add("chat_messages", count)

    def record_proactive(self):
        self.db.stats_add("proactive_messages")

    def record_thought(self):
        self.db.stats_add("thoughts")

    def record_fact(self):
        self.db.stats_add("facts")

    def record_tick(self):
        self.db.stats_add("ticks")
        now = time.time()
        self.db.stats_add("uptime_seconds", now - self._last_uptime_ts)
        self._last_uptime_ts = now

    def record_tool(self):
        self.db.stats_add("tool_calls")

    def today(self):
        return self.db.stats_get()

    def totals(self):
        return self.db.stats_totals()

    def days(self, limit=7):
        return self.db.stats_days(limit)

    def clear(self):
        self.db.stats_clear()

    def close(self):
        self.db.close()
