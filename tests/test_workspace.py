"""kernel.workspace 与 Agent 工作区集成测试。

覆盖：
- 工作区根创建 + 布局 + README；旧 sandbox/ 自动迁移
- 观察库落库/去重/统计/最近查询
- db_exec：查询、写入、ATTACH 阻断、readonly 限制
- 路径越界防护
- tools 门面（sandbox db action）与 Agent.live() 集成
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

import agent
import core
import tools
from kernel import workspace
from kernel.workspace import WorkspaceError, db_exec


def _cfg():
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["embedding_enabled"] = False
    return cfg


# ---------- 工作区根与迁移 ----------

def test_workspace_root_creates_layout(tmp_path):
    root = workspace.workspace_root(base=tmp_path)
    assert root == tmp_path / "workspace"
    assert (root / "data").is_dir()
    assert (root / "projects").is_dir()
    assert (root / "notes").is_dir()
    assert (root / "README.md").is_file()


def test_workspace_migrates_legacy_sandbox(tmp_path):
    legacy = tmp_path / "sandbox"
    legacy.mkdir()
    (legacy / "my-note.md").write_text("旧沙盒内容", encoding="utf-8")
    root = workspace.workspace_root(base=tmp_path)
    assert not legacy.exists()  # 已迁移改名
    assert (root / "my-note.md").read_text(encoding="utf-8") == "旧沙盒内容"


def test_workspace_path_blocks_escape(tmp_path):
    root = workspace.workspace_root(base=tmp_path)
    assert workspace.workspace_path("projects/a.html", base=tmp_path) == \
        (root / "projects" / "a.html").resolve()
    with pytest.raises(WorkspaceError):
        workspace.workspace_path("../../etc/passwd", base=tmp_path)
    with pytest.raises(WorkspaceError):
        workspace.workspace_path("/etc/passwd", base=tmp_path)


# ---------- 观察库 ----------

def _collections():
    return [
        {
            "plugin": "finance", "label": "财经行情",
            "entries": [
                {"title": "行情", "text": "贵州茅台 1500.00 +1.50%", "source": "600519",
                 "data": {"price": 1500.0, "pct": 1.5}},
                {"title": "行情", "text": "上证指数 3000.00 +0.80%"},
            ],
        },
        {
            "plugin": "rss_news", "label": "新闻",
            "entries": [{"title": "某新闻", "text": "正文……", "link": "https://example.com/1"}],
        },
        {"plugin": "empty", "label": "空插件", "entries": []},
    ]


def test_record_observations_insert_and_dedupe(tmp_path):
    summary = workspace.record_observations(_collections(), base=tmp_path)
    assert summary["added"] == 3
    assert summary["total"] == 3
    assert summary["by_plugin"] == {"finance": 2, "rss_news": 1}
    # 24h 窗口内重复落库被去重
    summary2 = workspace.record_observations(_collections(), base=tmp_path)
    assert summary2["added"] == 0
    assert summary2["total"] == 3
    # 不同内容可以继续入库
    cols = [{"plugin": "finance", "entries": [{"title": "行情", "text": "贵州茅台 1505.00 +1.83%"}]}]
    summary3 = workspace.record_observations(cols, base=tmp_path)
    assert summary3["added"] == 1


def test_observation_stats_and_recent(tmp_path):
    workspace.record_observations(_collections(), base=tmp_path)
    stats = workspace.observation_stats(base=tmp_path)
    assert stats["total"] == 3
    assert stats["by_plugin"]["finance"] == 2
    assert stats["newest"]
    recent = workspace.observations_recent(limit=2, base=tmp_path)
    assert len(recent) == 2
    assert recent[0][1] in ("finance", "rss_news")
    fin = workspace.observations_recent(limit=5, plugin="finance", base=tmp_path)
    assert len(fin) == 2
    assert all(r[1] == "finance" for r in fin)


# ---------- SQL 执行 ----------

def test_db_exec_query_and_write(tmp_path):
    workspace.record_observations(_collections(), base=tmp_path)
    text = db_exec("SELECT plugin, COUNT(*) AS n FROM observations GROUP BY plugin",
                   base=tmp_path)
    assert "finance" in text and "rss_news" in text
    db_exec("CREATE TABLE IF NOT EXISTS my_projects(name TEXT, score REAL)", base=tmp_path)
    db_exec("INSERT INTO my_projects VALUES('dashboard', 0.9)", base=tmp_path)
    text = db_exec("SELECT * FROM my_projects", base=tmp_path)
    assert "dashboard" in text
    assert "已执行" in db_exec("UPDATE my_projects SET score=1.0", base=tmp_path)


def test_db_exec_blocks_attach_and_empty(tmp_path):
    with pytest.raises(WorkspaceError):
        db_exec("ATTACH DATABASE '/tmp/x.db' AS other", base=tmp_path)
    with pytest.raises(WorkspaceError):
        db_exec("   ", base=tmp_path)


def test_db_exec_readonly_rejects_writes(tmp_path):
    workspace.record_observations(_collections(), base=tmp_path)
    text = db_exec("SELECT COUNT(*) AS n FROM observations", base=tmp_path, readonly=True)
    assert "SELECT" not in text and "n" in text
    with pytest.raises(WorkspaceError):
        db_exec("INSERT INTO observations(ts,plugin) VALUES('x','y')",
                base=tmp_path, readonly=True)
    with pytest.raises(WorkspaceError):
        db_exec("CREATE TABLE bad(x)", base=tmp_path, readonly=True)


def test_workspace_brief_contains_stats(tmp_path):
    workspace.record_observations(_collections(), base=tmp_path)
    (tmp_path / "workspace" / "projects" / "dashboard.html").write_text("<html>", encoding="utf-8")
    brief = workspace.workspace_brief(base=tmp_path)
    assert "观察库" in brief and "共 3 条" in brief
    assert "dashboard.html" in brief
    assert str(tmp_path / "workspace") in brief


# ---------- tools 门面 / 沙盒 db action ----------

def test_sandbox_db_action_via_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    workspace.record_observations(_collections(), base=tmp_path)
    result = tools.execute(
        "sandbox", {"action": "db", "sql": "SELECT COUNT(*) AS n FROM observations"},
        mode="confirm", source=tools.SOURCE_AUTO,
    )
    assert "n" in result
    # off 档拒绝
    denied = tools.execute(
        "sandbox", {"action": "db", "sql": "SELECT 1"},
        mode="off", source=tools.SOURCE_AUTO,
    )
    assert "已关闭" in denied
    # 越权 SQL 被拦截为失败文本
    blocked = tools.execute(
        "sandbox", {"action": "db", "sql": "ATTACH DATABASE '/tmp/x' AS o"},
        mode="confirm", source=tools.SOURCE_AUTO,
    )
    assert "不允许" in blocked


def test_workspace_facade_functions_exposed():
    assert callable(tools.workspace_record_observations)
    assert callable(tools.workspace_brief)


# ---------- Agent 集成 ----------

def test_agent_live_syncs_observations(tmp_path, monkeypatch):
    """live() 把巡视采集自动落进工作区观察库（安静时段也落库）。"""
    a = agent.Agent(
        _cfg(),
        data_dir=tmp_path,
        clock=lambda: datetime(2026, 8, 13, 23, 30),  # 安静时段
    )
    ctx = {"collections": _collections(), "errors": []}
    result = a.live(ctx)
    assert result is None  # 安静时段不发言
    stats = workspace.observation_stats(base=tmp_path)
    assert stats["total"] == 3
    # 关闭 workspace_enabled 时不再落库
    cfg2 = _cfg()
    cfg2["workspace_enabled"] = False
    sub = tmp_path / "sub"
    sub.mkdir()
    a2 = agent.Agent(cfg2, data_dir=sub,
                     clock=lambda: datetime(2026, 8, 13, 10, 0))
    a2.live({"collections": _collections(), "errors": []})
    assert workspace.observation_stats(base=sub)["total"] == 0


def test_agent_workspace_section_in_prompt(tmp_path):
    a = agent.Agent(_cfg(), data_dir=tmp_path)
    workspace.record_observations(_collections(), base=tmp_path)
    section = a._workspace_section({"added": 3, "total": 3})
    assert "工作区" not in section  # 快照文本不含标题，由提示词组装
    assert "观察库" in section and "共 3 条" in section
    assert "新入库 3 条" in section
    assert a._workspace_sync({"collections": []}) is not None


def test_agent_parse_life_reply_work_speak_priority(tmp_path):
    a = agent.Agent(_cfg(), data_dir=tmp_path)
    plan = a._parse_life_reply(
        "WORK 把股票数据存进观察库并更新仪表盘\nSPEAK 你的股票仪表盘更新啦"
    )
    assert plan == {"type": "speak", "text": "你的股票仪表盘更新啦"}
    plan2 = a._parse_life_reply("WORK 整理了一下观察数据")
    assert plan2 == {"type": "work", "text": "整理了一下观察数据"}
    plan3 = a._parse_life_reply("THINK 随便想想\nWORK 做了点事")
    assert plan3 == {"type": "work", "text": "做了点事"}
    assert a._parse_life_reply("SILENT") is None


def test_agent_live_work_plan_quiet(tmp_path):
    """WORK 计划：live() 安静记录工作，不打扰主人。"""
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    cfg["wake_greeting_enabled"] = False  # 跳过每日唤醒，直接进内心思考
    a = agent.Agent(cfg, data_dir=tmp_path,
                    clock=lambda: datetime(2026, 8, 13, 10, 0))
    a.brain.complete_tools = lambda msgs, decls, **kw: ("WORK 更新了观察数据", [])
    ctx = {"collections": [], "errors": []}
    result = a.live(ctx)
    assert result is None  # 做了实事，安静不发言
    desires = a.state.get("desires") or []
    assert any("工作：更新了观察数据" in d["text"] for d in desires)


def test_patrol_tool_budget_limits_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    a = agent.Agent(_cfg(), data_dir=tmp_path,
                    clock=lambda: datetime(2026, 8, 13, 10, 0))
    # 配置预算 3：第 4 个工具调用开始收到预算提示
    a.cfg["patrol_tool_budget"] = 3
    calls = []

    def fake_complete(msgs, decls, **kw):
        if len(calls) >= 1:
            return ("SILENT", [])
        calls.append(1)
        return (
            "",
            [
                {"id": f"c{i}", "function": {
                    "name": "sandbox",
                    "arguments": json.dumps({"action": "list", "path": "."}),
                }} for i in range(5)
            ],
        )

    a.brain.complete_tools = fake_complete
    plan = a._inner_thought({"collections": []}, datetime(2026, 8, 13, 10, 0))
    assert plan is None
    # 第二轮回复 SILENT，说明预算机制生效（首轮 5 个调用里 2 个被执行、3 个被拦）


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
