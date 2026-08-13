"""kernel.workspace：Agent 自己的默认文件夹（工作区）。

Agent 可以在工作区里自由做自己想做的事：写文件、跑命令、用 sqlite 建数据、
生成网页/仪表盘等产物。这是"主动思考"从"查询回一句话"升级为"持续做事"
的地基（2026-08-13，架构师裁决 P0）。

布局（<用户数据目录>/workspace/）：
  README.md             —— Agent 自己维护的工作区说明/索引
  data/observations.db  —— 插件采集自动落库的观察库（sqlite）
  projects/             —— Agent 的产物（网站/仪表盘/脚本/报告）
  notes/                —— Agent 的笔记与想法

职责：
- 根目录解析与旧 sandbox/ 目录迁移（workspace 是 sandbox 的正式名）
- 观察库读写（plugins 采集数据持久化，跨 tick 累积时间序列）
- 工作区快照（供主动思考提示词注入，保证跨 tick 连续性）
- SQL 执行原语（Agent 用 sqlite 自己建模分析）

安全边界：
- 所有路径解析限制在工作区根内（越界抛 ValueError）
- SQL 仅限工作区 observations.db，阻断 ATTACH/DETACH（文件逃逸面）
- 只依赖标准库 + kernel.boot.user_data_dir；不 import 任何业务模块
"""

import json
import os
import sqlite3
import time
from pathlib import Path

from kernel.boot import user_data_dir

# 工作区根下的固定子目录（首次访问自动创建）
LAYOUT_DIRS = ("data", "projects", "notes")

README_DEFAULT = (
    "这是我的工作区（Agent 自己的默认文件夹）。\n"
    "\n"
    "我可以在这里自由做自己想做的事：\n"
    "- data/observations.db：插件采集自动落库的观察库（行情/新闻/天气等历史数据）\n"
    "- projects/：我生成的产物（网站、仪表盘、脚本、报告）\n"
    "- notes/：我的笔记与想法\n"
    "\n"
    "（这个文件是我自己维护的索引，可以随时更新。）\n"
)

# 观察库 schema：所有插件采集按统一结构落库
OBSERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  plugin TEXT NOT NULL,
  title TEXT DEFAULT '',
  text TEXT DEFAULT '',
  extra TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_obs_plugin_ts ON observations(plugin, ts);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);
"""

DEDUP_WINDOW_HOURS = 24  # 相同 (plugin,title,text) 在该窗口内只保留一条


class WorkspaceError(Exception):
    """工作区操作失败（路径越界 / SQL 被拒等）。"""


def workspace_root(base=None):
    """Agent 自己的默认文件夹。旧 sandbox/ 目录自动迁移为 workspace/。

    base：可选数据目录（测试隔离/宿主注入）；默认 kernel.boot.user_data_dir()。
    """
    data = Path(base) if base else Path(user_data_dir())
    root = data / "workspace"
    legacy = data / "sandbox"
    try:
        if not root.exists() and legacy.exists():
            os.replace(str(legacy), str(root))  # 同名迁移，旧沙盒内容不丢
        root.mkdir(parents=True, exist_ok=True)
        for sub in LAYOUT_DIRS:
            (root / sub).mkdir(exist_ok=True)
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(README_DEFAULT, encoding="utf-8")
    except OSError:
        pass  # 权限/磁盘异常不阻断调用方，返回 root
    return root


def workspace_path(rel=".", base=None):
    """把相对/绝对路径解析到工作区内；越界抛 WorkspaceError。"""
    root = workspace_root(base=base).resolve()
    p = Path(rel)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if not p.is_relative_to(root):
        raise WorkspaceError(f"路径越出工作区：{rel}")
    return p


# ---------- 观察库（插件采集自动落库） ----------


def _connect(base=None):
    root = workspace_root(base=base)
    conn = sqlite3.connect(str(root / "data" / "observations.db"))
    try:
        conn.executescript(OBSERVATIONS_SCHEMA)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def record_observations(collections, base=None):
    """把一次巡视的插件采集结果落库（去重：24h 窗口内相同条目只留一条）。

    collections：brain.content.collect_all 的产物（plugin/label/entries）。
    返回摘要 dict：{added, total, by_plugin}，供提示词注入/日志。
    """
    added = 0
    by_plugin = {}
    rows = []
    for coll in collections or []:
        plugin = str(coll.get("plugin") or coll.get("label") or "unknown")
        for entry in coll.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "")[:200]
            text = str(entry.get("text") or "")[:2000]
            if not text and not title:
                continue
            extra = {}
            data = entry.get("data")
            if isinstance(data, dict):
                extra["data"] = data
            if entry.get("link"):
                extra["link"] = str(entry["link"])[:500]
            if entry.get("url"):
                extra["url"] = str(entry["url"])[:500]
            if entry.get("source"):
                extra["source"] = str(entry["source"])[:200]
            rows.append((plugin, title, text,
                         json.dumps(extra, ensure_ascii=False) if extra else ""))
    if not rows:
        return {"added": 0, "total": _observation_total(base=base), "by_plugin": {}}
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - DEDUP_WINDOW_HOURS * 3600)
    )
    try:
        conn = _connect(base=base)
    except sqlite3.Error:
        return {"added": 0, "total": _observation_total(base=base), "by_plugin": {}}
    try:
        for plugin, title, text, extra in rows:
            cur = conn.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE plugin=? AND title=? AND text=? AND ts>=?",
                (plugin, title, text, cutoff),
            )
            if cur.fetchone()[0] > 0:
                continue
            conn.execute(
                "INSERT INTO observations(ts, plugin, title, text, extra) "
                "VALUES(?,?,?,?,?)",
                (ts, plugin, title, text, extra),
            )
            added += 1
            by_plugin[plugin] = by_plugin.get(plugin, 0) + 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        total = _observation_total(base=base)
    finally:
        conn.close()
    return {"added": added, "total": total, "by_plugin": by_plugin}


def _observation_total(base=None):
    try:
        conn = _connect(base=base)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def observation_stats(base=None):
    """观察库统计：总量 + 各插件条数 + 最新时间。"""
    try:
        conn = _connect(base=base)
    except sqlite3.Error:
        return {"total": 0, "by_plugin": {}, "newest": ""}
    try:
        total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        newest = conn.execute("SELECT MAX(ts) FROM observations").fetchone()[0] or ""
        by_plugin = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT plugin, COUNT(*) FROM observations GROUP BY plugin "
                "ORDER BY COUNT(*) DESC LIMIT 10"
            ).fetchall()
        }
    except sqlite3.Error:
        total, newest, by_plugin = 0, "", {}
    finally:
        conn.close()
    return {"total": total, "by_plugin": by_plugin, "newest": newest}


def observations_recent(limit=5, plugin=None, base=None):
    """最近 N 条观察（可选按插件过滤），返回 [(ts, plugin, title, text)]。"""
    try:
        conn = _connect(base=base)
    except sqlite3.Error:
        return []
    sql = ("SELECT ts, plugin, title, text FROM observations")
    params = ()
    if plugin:
        sql += " WHERE plugin=?"
        params = (plugin,)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (max(1, min(int(limit), 20)),)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return rows


# ---------- SQL 执行（Agent 自建数据分析用） ----------

_BLOCKED_SQL_PREFIXES = ("attach", "detach")


def db_exec(sql, base=None, max_rows=50, max_chars=4000, readonly=False):
    """在观察库上执行 SQL。返回给 LLM 的文本结果。

    阻断 ATTACH/DETACH（文件逃逸面）；输出行数与字符数封顶。
    readonly=True 时拒绝非 SELECT/PRAGMA 语句。
    """
    statement = str(sql or "").strip()
    if not statement:
        raise WorkspaceError("SQL 为空")
    first_word = statement.split(None, 1)[0].lower() if statement else ""
    if first_word in _BLOCKED_SQL_PREFIXES:
        raise WorkspaceError(f"不允许执行 {first_word.upper()}（文件逃逸面）")
    if readonly and first_word not in ("select", "pragma", "with", "explain"):
        raise WorkspaceError("只读模式只允许 SELECT/PRAGMA 查询")
    try:
        conn = _connect(base=base)
    except sqlite3.Error as exc:
        raise WorkspaceError(f"打开观察库失败：{exc}")
    try:
        cur = conn.execute(statement)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows)
            lines = [" | ".join(cols)]
            lines.append("-" * min(len(lines[0]), 60))
            for row in rows:
                lines.append(" | ".join(str(v) for v in row)[:200])
            if cur.fetchone() is not None:
                lines.append(f"...（结果超过 {max_rows} 行，已截断）")
            conn.commit()
            text = "\n".join(lines)[:max_chars]
            return f"{text}（共返回 {len(rows)} 行）"
        conn.commit()
        return f"已执行（影响 {cur.rowcount if cur.rowcount >= 0 else 0} 行）"
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise WorkspaceError(f"SQL 执行失败：{exc}")
    finally:
        conn.close()


# ---------- 工作区快照（注入主动思考提示词） ----------


def _recent_files(root, dirs, limit=8):
    """按修改时间列出工作区内的最近文件（产物/笔记等）。"""
    items = []
    for d in dirs:
        p = root / d
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if f.is_file() and f.name not in ("observations.db", "README.md"):
                try:
                    items.append((f, f.stat().st_mtime))
                except OSError:
                    continue
    items.sort(key=lambda x: x[1], reverse=True)
    out = []
    for f, mtime in items[:limit]:
        rel = f.relative_to(root)
        stamp = time.strftime("%m-%d %H:%M", time.localtime(mtime))
        out.append(f"{rel}（{stamp}）")
    return out


def workspace_brief(base=None, recent_limit=8):
    """工作区快照文本：供主动思考提示词注入，保证跨 tick 连续性。"""
    root = workspace_root(base=base)
    stats = observation_stats(base=base)
    lines = [f"路径：{root}"]
    plugin_text = "、".join(
        f"{k} {v}" for k, v in list(stats["by_plugin"].items())[:6]
    ) or "暂无"
    lines.append(f"观察库：共 {stats['total']} 条（{plugin_text}），最新 {stats['newest'] or '暂无'}")
    recent = _recent_files(root, ("projects", "notes"), limit=recent_limit)
    lines.append("最近产物/笔记：" + ("、".join(recent) if recent else "暂无"))
    return "\n".join(lines)
