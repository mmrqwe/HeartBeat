"""SQLite 存储层测试：记忆、聊天、状态、统计、向量检索。"""

import inspect
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from db import Database, Memory, Stats


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


def _db(tmp_path, name="test.db"):
    return Database(tmp_path / name)


def test_memory_crud(tmp_path):
    d = _db(tmp_path)
    d.add_memory("fact", "主人喜欢喝咖啡")
    d.add_memory("thought", "今天天气不错")
    items = d.memory_items()
    assert [i["role"] for i in items] == ["fact", "thought"]
    assert items[0]["text"] == "主人喜欢喝咖啡"
    assert items[0]["time"]
    facts = d.memory_items(roles=("fact",))
    assert len(facts) == 1
    d.clear_memory()
    assert d.memory_items() == []


def test_memory_limit(tmp_path):
    d = _db(tmp_path)
    for i in range(5):
        d.add_memory("fact", f"fact{i}")
    items = d.memory_items(limit=3)
    assert [i["text"] for i in items] == ["fact2", "fact3", "fact4"]


def test_chat_crud_and_clear(tmp_path):
    d = _db(tmp_path)
    d.add_chat("user", "你好")
    d.add_chat("assistant", "在呀")
    items = d.chat_items()
    assert [i["role"] for i in items] == ["user", "assistant"]
    d.clear_chat()
    assert d.chat_items() == []


def test_state_roundtrip(tmp_path):
    d = _db(tmp_path)
    assert d.get_state("mood", "平静") == "平静"
    d.set_state("mood", "开心")
    d.set_state("last_proactive_ts", 123.45)
    d2 = Database(tmp_path / "test.db")
    assert d2.get_state("mood") == "开心"
    assert d2.get_state("last_proactive_ts") == 123.45


def test_find_fact_by_text_exact(tmp_path):
    """P0：SQL 精确查重（索引等值匹配），未命中返回 None。"""
    d = _db(tmp_path)
    mid = d.add_memory("fact", "主人喜欢喝咖啡", category="preference")
    assert d.find_fact_by_text("主人喜欢喝咖啡") == mid
    assert d.find_fact_by_text(" 主人喜欢喝咖啡 ") == mid  # strip 后命中
    assert d.find_fact_by_text("主人爱喝咖啡") is None


def test_find_fact_by_text_role_filtered(tmp_path):
    """只匹配 fact 角色：thought 同文本不命中。"""
    d = _db(tmp_path)
    d.add_memory("thought", "主人喜欢喝咖啡")
    assert d.find_fact_by_text("主人喜欢喝咖啡") is None


def test_find_fact_by_text_index_created(tmp_path):
    """(role, text) 索引随建表创建。"""
    d = _db(tmp_path)
    idx = d._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memory_role_text'"
    ).fetchone()
    assert idx is not None, "缺 idx_memory_role_text 索引"


def test_event_log_write_and_query(tmp_path):
    """P1 Event Store：写入 + 时间线查询（按类型/会话过滤）。"""
    d = _db(tmp_path)
    assert d.log_event("chat.started", "main.chat", {"text_len": 3}, "chat_abc")
    assert d.log_event("chat.finished", "main.chat", {"elapsed_ms": 12}, "chat_abc")
    assert d.log_event("tool.called", "brain.tool", {"tool": "web_search"})
    items = d.event_items(limit=10)
    assert len(items) == 3
    assert items[0]["type"] == "tool.called"  # 倒序
    chat_events = d.event_items(type_="chat.started", limit=10)
    assert len(chat_events) == 1
    assert chat_events[0]["trace_id"] == "chat_abc"
    traced = d.event_items(trace_id="chat_abc", limit=10)
    assert len(traced) == 2
    payload = traced[0]["payload"]
    assert "elapsed_ms" in payload  # JSON 文本可解析


def test_event_log_silent_on_failure(tmp_path):
    """埋点失败静默返回 False，不抛异常（不阻断主链路）。"""
    d = _db(tmp_path)
    # 不可序列化 payload → 静默 False
    assert d.log_event("x", "src", object()) is False
    assert d.event_items(limit=10) == []


def test_event_indexes_created(tmp_path):
    """events 时间线索引（ts/type/trace_id）随建表创建。"""
    d = _db(tmp_path)
    names = {
        r["name"]
        for r in d._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_events_%'"
        ).fetchall()
    }
    assert {"idx_events_ts", "idx_events_type", "idx_events_trace"} <= names


def test_stats_record_today_totals_days(tmp_path):
    d = _db(tmp_path)
    d.stats_record_llm(prompt_tokens=100, completion_tokens=20, cached_tokens=30)
    d.stats_record_llm(ok=False)
    d.stats_add("chat_messages", 2)
    d.stats_add("proactive_messages")
    d.stats_add("ticks")
    d.stats_add("uptime_seconds", 60)
    today = d.stats_get()
    assert today["llm_calls"] == 2
    assert today["llm_errors"] == 1
    assert today["prompt_tokens"] == 100
    assert today["chat_messages"] == 2
    total = d.stats_totals()
    assert total["llm_calls"] == 2
    days = d.stats_days(7)
    assert len(days) == 1
    assert days[0]["date"] == today["date"]


def test_stats_collectors(tmp_path):
    d = _db(tmp_path)
    d.stats_record_collect("rss_news", True, 3, 120, False)
    d.stats_record_collect("rss_news", True, 3, 120, True)
    d.stats_record_collect("weather", False)
    today = d.stats_get()
    news = today["collectors"]["rss_news"]
    assert news["fetches"] == 2
    assert news["cache_hits"] == 1
    assert news["entries"] == 6
    assert today["collectors"]["weather"]["fails"] == 1
    d.stats_clear()
    assert d.stats_get()["collectors"] == {}


def test_tool_calls_migration(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute(
        "CREATE TABLE stats_daily(date TEXT PRIMARY KEY, "
        "llm_calls INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    d = Database(tmp_path / "old.db")
    d.stats_add("tool_calls")
    assert d.stats_get()["tool_calls"] == 1
    d.close()


def test_content_hash(tmp_path):
    d = _db(tmp_path)
    assert d.content_hash("rss_news") is None
    d.set_content_hash("rss_news", "abc")
    assert d.content_hash("rss_news") == "abc"


def test_vec_roundtrip(tmp_path):
    d = _db(tmp_path)
    if not d.vec_ready:
        return
    item_id = d.add_memory("fact", "主人喜欢喝咖啡")
    vector = [0.01] * 512
    assert d.add_embedding("memory", item_id, vector)
    results = d.search_embeddings("memory", vector, 5)
    assert results and results[0]["text"] == "主人喜欢喝咖啡"
    assert results[0]["distance"] < 0.001


def test_vec_search_ranking(tmp_path):
    d = _db(tmp_path)
    if not d.vec_ready:
        return
    a = d.add_memory("fact", "主人喜欢咖啡")
    b = d.add_memory("fact", "主人明天要开会")
    coffee_vec = [0.9] * 512
    meeting_vec = [0.1] * 512
    d.add_embedding("memory", a, coffee_vec)
    d.add_embedding("memory", b, meeting_vec)
    results = d.search_embeddings("memory", coffee_vec, 5)
    assert results[0]["text"] == "主人喜欢咖啡"


def test_reindex(tmp_path):
    d = _db(tmp_path)
    if not d.vec_ready:
        return
    item_id = d.add_memory("fact", "需要补索引")

    class FakeEmbedder:
        ready = True

        def embed_one(self, text):
            return [0.5] * 512

    assert d.ids_without_embedding("memory") == [item_id]
    assert d.reindex(FakeEmbedder(), "memory") == 1
    assert d.ids_without_embedding("memory") == []


def test_memory_structured_fields(tmp_path):
    """记忆结构化字段：类别/重要性/来源/到期时间。"""
    d = _db(tmp_path)
    d.add_memory("fact", "主人喜欢打羽毛球", category="preference", importance=4, source="chat")
    d.add_memory("fact", "明天开会", category="schedule", expires_at="2026-08-12 09:00")
    items = d.memory_items()
    by_text = {i["text"]: i for i in items}
    assert by_text["主人喜欢打羽毛球"]["category"] == "preference"
    assert by_text["主人喜欢打羽毛球"]["importance"] == 4
    assert by_text["明天开会"]["expires_at"] == "2026-08-12 09:00"
    assert "category" in items[0]


def test_memory_profile_groups(tmp_path):
    """画像按类别分组、importance 排序、limit_per 截断。"""
    d = _db(tmp_path)
    d.add_memory("fact", "低重要", category="habit", importance=1)
    d.add_memory("fact", "高重要", category="habit", importance=5)
    d.add_memory("fact", "主人喜欢咖啡", category="preference")
    profile = d.memory_profile(limit_per=1)
    by_cat = {g["category"]: g["items"] for g in profile}
    assert by_cat["habit"][0]["text"] == "高重要"
    assert by_cat["preference"][0]["text"] == "主人喜欢咖啡"


def test_memory_schedule_due(tmp_path):
    """日程到期查询：只返回时间窗内的未过期日程。"""
    d = _db(tmp_path)
    d.add_memory("fact", "明天开会", category="schedule", expires_at="2026-08-12 09:00")
    d.add_memory("fact", "下周面试", category="schedule", expires_at="2026-08-18 09:00")
    d.add_memory("fact", "过期日程", category="schedule", expires_at="2026-08-01 09:00")
    due = d.memory_schedule_due(within_hours=24, now="2026-08-11 10:00")
    texts = [i["text"] for i in due]
    assert "明天开会" in texts
    assert "下周面试" not in texts
    assert "过期日程" not in texts


def test_memory_mark_used(tmp_path):
    d = _db(tmp_path)
    mid = d.add_memory("fact", "x")
    d.mark_memory_used(mid)
    assert d.memory_items()[0]["last_used_at"]


def test_memory_wrapper_compat(tmp_path):
    mem = Memory(tmp_path / "memory.db", limit=3)
    mem.add("fact", "x1")
    mem.add("fact", "x2")
    assert [i["text"] for i in mem.items] == ["x1", "x2"]
    assert mem.facts()[-1]["text"] == "x2"
    mem.clear()
    assert mem.items == []


def test_stats_wrapper_compat(tmp_path):
    stats = Stats(tmp_path / "stats.db")
    stats.record_llm(prompt_tokens=10, completion_tokens=5, cached_tokens=2)
    stats.record_collect("quote", True, 1, 20, False)
    stats.record_chat(2)
    stats.record_tick()
    today = stats.today()
    assert today["llm_calls"] == 1
    assert today["cached_tokens"] == 2
    assert today["collectors"]["quote"]["fetches"] == 1
    assert stats.days()[0]["date"] == today["date"]
    stats.clear()
    assert stats.today()["llm_calls"] == 0


# ---------- 工具审计 ----------

def test_tool_log_roundtrip(tmp_path):
    d = _db(tmp_path)
    d.log_tool("user", "run_bash", '{"command":"ls"}', "confirm", True, True, "exit=0")
    d.log_tool("auto", "run_bash", '{"command":"rm x"}', "confirm", False, False, "用户未确认")
    d.log_tool("user", "web_search", '{"query":"AI"}', "readonly", True, True, "搜索结果")
    items = d.tool_log_items()
    assert len(items) == 3
    assert items[0]["tool"] == "run_bash" and items[0]["approved"] == 1
    assert items[1]["approved"] == 0 and items[1]["ok"] == 0
    assert items[2]["source"] == "user" and items[2]["mode"] == "readonly"


def test_tool_log_limit(tmp_path):
    d = _db(tmp_path)
    for i in range(10):
        d.log_tool("user", "run_bash", f"cmd{i}", "confirm", True, True, "ok")
    items = d.tool_log_items(limit=5)
    assert len(items) == 5


def test_chat_after_watermark(tmp_path):
    """水位线查询：只返回指定 id 之后的用户消息（记忆补采用）。"""
    d = _db(tmp_path)
    id1 = d.add_chat("user", "你好")
    d.add_chat("assistant", "在呀")
    id3 = d.add_chat("user", "我喜欢喝咖啡")
    rows = d.chat_after(id1)
    assert [r["text"] for r in rows] == ["我喜欢喝咖啡"]
    assert d.chat_after(id3) == []


def test_delete_memory(tmp_path):
    """删除单条记忆（设置页记忆管理）。"""
    d = _db(tmp_path)
    mid = d.add_memory("fact", "主人喜欢喝茶")
    d.add_memory("fact", "主人喜欢咖啡")
    d.delete_memory(mid)
    assert [i["text"] for i in d.memory_items()] == ["主人喜欢咖啡"]


def test_streak_days(tmp_path):
    """连续陪伴天数：今天没聊则从昨天起算，中断重置。"""
    d = _db(tmp_path)
    now = datetime(2026, 8, 11, 10, 0)
    assert d.streak_days(now) == 0
    for dt in ("2026-08-08", "2026-08-09", "2026-08-10"):
        d._conn.execute("INSERT OR IGNORE INTO stats_daily(date) VALUES (?)", (dt,))
        d._conn.execute("UPDATE stats_daily SET chat_messages=3 WHERE date=?", (dt,))
    d._conn.commit()
    assert d.streak_days(now) == 3
    # 今天（8/11）已插入但没聊天：从昨天起算仍 3 天
    d._conn.execute("INSERT OR IGNORE INTO stats_daily(date) VALUES ('2026-08-11')")
    d._conn.commit()
    assert d.streak_days(now) == 3
    # 中间断一天：重置为 1
    d._conn.execute("DELETE FROM stats_daily WHERE date='2026-08-09'")
    d._conn.commit()
    assert d.streak_days(now) == 1


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


def test_vec_search_filters_expired(tmp_path):
    d = _db(tmp_path)
    if not d.vec_ready:
        return
    past_id = d.add_memory(
        "fact", "旧日程", category="schedule", expires_at="2000-01-01 00:00"
    )
    live_id = d.add_memory("fact", "主人喜欢咖啡")
    d.add_embedding("memory", past_id, [0.1] * 512)
    d.add_embedding("memory", live_id, [0.1] * 512)
    results = d.search_embeddings("memory", [0.1] * 512, k=5)
    texts = [r["text"] for r in results]
    assert "旧日程" not in texts
    assert "主人喜欢咖啡" in texts


def test_cleanup_memory_expired_and_cap(tmp_path):
    d = _db(tmp_path)
    d.add_memory("fact", "过期日程", category="schedule", expires_at="2000-01-01 00:00")
    for i in range(3):
        d.add_memory("fact", f"事实{i}")
    expired, capped = d.cleanup_memory(now="2099-01-01 00:00", cap=2)
    assert (expired, capped) == (1, 1)
    assert len(d.memory_items(limit=None)) == 2


def test_keyword_search_fallback(tmp_path):
    d = _db(tmp_path)
    d.add_memory("fact", "主人喜欢喝咖啡", category="preference")
    d.add_memory("fact", "明天开会", category="schedule", expires_at="2000-01-01 00:00")
    results = d.search_memory_keywords("咖啡", k=5, now="2099-01-01 00:00")
    assert any("咖啡" in r["text"] for r in results)
    assert not any("开会" in r["text"] for r in results)


def test_retire_and_delete_memory_like(tmp_path):
    d = _db(tmp_path)
    d.add_memory("fact", "主人喜欢咖啡", category="preference")
    assert d.retire_memory_like("咖啡", category="preference") == 1
    items = d.memory_items(limit=None)
    assert items[0]["expires_at"] is not None
    d.add_memory("fact", "主人喜欢摄影", category="preference")
    assert d.delete_memory_like("摄影") == 1
    assert not any("摄影" in i["text"] for i in d.memory_items(limit=None))


def test_clear_embeddings(tmp_path):
    d = _db(tmp_path)
    if not d.vec_ready:
        return
    mid = d.add_memory("fact", "需要重建")
    d.add_embedding("memory", mid, [0.5] * 512)
    assert d.ids_without_embedding("memory") == []
    assert d.clear_embeddings("memory")
    assert mid in d.ids_without_embedding("memory")


if __name__ == "__main__":
    _run_plain()
