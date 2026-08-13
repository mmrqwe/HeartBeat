"""会话分栏专项测试：db 会话 CRUD + 目录绑定 + Agent 消息归属隔离。"""

import sys
from pathlib import Path

import pytest

import agent
import db as dbmod

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_db(tmp_path):
    return dbmod.Database(str(tmp_path / "heartbeat.db"))


def _make_agent(tmp_path, cfg=None):
    import core

    base = core.load_config(str(tmp_path / "config.json"))
    return agent.create_agent(base, data_dir=str(tmp_path))


# ---------- db 会话 CRUD ----------


def test_default_session_created_on_init(tmp_path):
    d = _make_db(tmp_path)
    sessions = d.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == "default"
    assert sessions[0]["name"] == "默认对话"
    assert sessions[0]["project_dir"] is None


def test_create_and_find_session_by_project_dir(tmp_path):
    d = _make_db(tmp_path)
    sid = d.create_session("我的项目", project_dir="/tmp/proj-a")
    assert sid != "default"
    # 目录↔会话一对一：同目录再建返回已有会话
    assert d.create_session("另一个名字", project_dir="/tmp/proj-a") == sid
    info = d.find_session_by_project_dir("/tmp/proj-a")
    assert info["id"] == sid and info["name"] == "我的项目"
    assert d.find_session_by_project_dir("/tmp/nonexistent") is None
    # 无目录 → 返回默认会话
    assert d.find_session_by_project_dir("")["id"] == "default"


def test_rename_session(tmp_path):
    d = _make_db(tmp_path)
    sid = d.create_session("旧名")
    d.rename_session(sid, "新名字")
    assert d.session(sid)["name"] == "新名字"
    d.rename_session("default", "默认也支持改名")
    assert d.session("default")["name"] == "默认也支持改名"


def test_delete_session_cascades_messages(tmp_path):
    d = _make_db(tmp_path)
    sid = d.create_session("临时对话")
    d.add_chat("user", "hello", session_id=sid)
    d.add_chat("assistant", "hi", session_id=sid)
    d.add_chat("user", "留在默认", session_id="default")
    assert len(d.chat_items(session_id=sid, limit=10)) == 2
    assert d.delete_session(sid) is True
    assert d.chat_items(session_id=sid, limit=10) == []
    assert len(d.chat_items(session_id="default", limit=10)) == 1
    # 默认会话不可删
    assert d.delete_session("default") is False


def test_chat_items_default_returns_all(tmp_path):
    d = _make_db(tmp_path)
    sid = d.create_session("s1")
    d.add_chat("user", "a", session_id="default")
    d.add_chat("user", "b", session_id=sid)
    all_items = d.chat_items(limit=10)
    assert [m["text"] for m in all_items] == ["a", "b"]
    assert [m["text"] for m in d.chat_items(limit=10, session_id=sid)] == ["b"]


# ---------- Agent 会话隔离 ----------


def test_agent_chat_session_isolation(tmp_path, monkeypatch):
    import core

    a = _make_agent(tmp_path)
    # 规则模式（无 api_key）走 _chat_rules，回复可预测
    a.append_chat("user", "会话A的消息", session_id="aaa")
    a.append_chat("assistant", "A的回复", session_id="aaa")
    a.append_chat("user", "会话B的消息", session_id="bbb")
    assert [m["text"] for m in a.chat_history(session_id="aaa")] == [
        "会话A的消息",
        "A的回复",
    ]
    assert [m["text"] for m in a.chat_history(session_id="bbb")] == ["会话B的消息"]
    # 全量（None）仍兼容旧语义
    assert len(a.chat_history()) == 3
    # 清当前会话不影响其他会话
    a.clear_chat_history(session_id="aaa")
    assert a.chat_history(session_id="aaa") == []
    assert len(a.chat_history(session_id="bbb")) == 1


def test_agent_chat_writes_to_session(tmp_path):
    a = _make_agent(tmp_path)
    a.append_chat("user", "你好")
    # chat() 在会话 bbb 里跑一轮（规则模式）
    a.chat("你好", session_id="bbb")
    bbb = a.chat_history(session_id="bbb")
    assert bbb[0]["role"] == "user"
    assert bbb[-1]["role"] == "assistant"
    # 默认会话只有手工 append 的一条
    assert len(a.chat_history(session_id="default")) == 1


def test_clear_chat_removes_summary_state(tmp_path):
    a = _make_agent(tmp_path)
    a.state["conversation_summary:aaa"] = "旧摘要"
    a._save_state()
    a.clear_chat_history(session_id="aaa")
    assert "conversation_summary:aaa" not in a.state
    assert a.db.get_state("conversation_summary:aaa") is None

    a.state["conversation_summary"] = "旧摘要"
    a._save_state()
    a.clear_chat_history()
    assert a.db.get_state("conversation_summary") is None


def test_clear_chat_session_keeps_other_session_vectors(tmp_path):
    d = _make_db(tmp_path)
    if not d.vec_ready:
        pytest.skip("sqlite_vec 不可用")
    sid1 = d.create_session("s1")
    sid2 = d.create_session("s2")
    id1 = d.add_chat("user", "会话1", session_id=sid1)
    id2 = d.add_chat("user", "会话2", session_id=sid2)
    d.add_embedding("chat", id1, [0.1] * 512)
    d.add_embedding("chat", id2, [0.2] * 512)
    d.clear_chat(session_id=sid1)
    remaining = d._conn.execute("SELECT COUNT(*) AS n FROM chat_vec").fetchone()["n"]
    assert remaining == 1


def test_delete_session_removes_chat_vectors(tmp_path):
    d = _make_db(tmp_path)
    if not d.vec_ready:
        pytest.skip("sqlite_vec 不可用")
    sid = d.create_session("s")
    mid = d.add_chat("user", "x", session_id=sid)
    d.add_embedding("chat", mid, [0.3] * 512)
    assert d.delete_session(sid) is True
    assert d._conn.execute("SELECT COUNT(*) AS n FROM chat_vec").fetchone()["n"] == 0
