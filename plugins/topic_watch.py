"""兴趣资讯内容源：从主人画像提取话题（LLM 或规则），定期搜索相关新闻。

话题来源优先级：设置页手动 topics > agent 自动提取（context.topics）。
话题轮换：每 3 次巡视（约 30 分钟）换一个话题，避免同一话题刷屏。
"""

import hashlib
import random

import search

META = {"name": "topic_watch", "label": "兴趣资讯", "default_enabled": True}

SETTINGS = [
    {
        "key": "topics",
        "label": "关注话题（留空自动从画像提取）",
        "type": "list",
        "default": [],
    },
    {"key": "max_news", "label": "每个话题几条", "type": "number", "default": 2},
]

_calls = 0


def collect(settings, context=None):
    """collect(settings, context)：context.topics 由 agent 从画像提取。"""
    global _calls
    settings = settings or {}
    topics = [t.strip() for t in (settings.get("topics") or []) if t and t.strip()]
    if not topics:
        topics = [t.strip() for t in ((context or {}).get("topics") or []) if t and t.strip()]
    if not topics:
        return []
    try:
        max_news = max(1, int(settings.get("max_news") or 2))
    except (TypeError, ValueError):
        max_news = 2
    # 每 3 次巡视换一个话题（约 30 分钟），防同一话题刷屏
    _calls += 1
    topic = topics[((_calls - 1) // 3) % len(topics)]
    try:
        entries = search.news_search(topic, limit=max_news + 1)
    except Exception:
        return []
    return [
        {"title": f"话题·{topic}", "text": e["title"], "source": topic,
         "url": e.get("url") or ""}
        for e in entries[:max_news]
    ]


def suggest(settings, entries, state):
    """规则模式：话题相关新资讯播报。"""
    if not entries:
        return None
    title = entries[0]["text"]
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    if state.get("last_hash") == digest:
        return None
    state["last_hash"] = digest
    if random.random() < 0.75:
        topic = entries[0].get("source") or "你关注的事"
        short = title if len(title) <= 50 else title[:47] + "…"
        return f"关于{topic}，看到一条：{short}"
    return None
