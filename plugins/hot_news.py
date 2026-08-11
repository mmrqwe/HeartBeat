"""热点新闻内容源：全网中文热点，多源 fallback 链（国内可达性优先）。"""

import random

import core
import search

META = {"name": "hot_news", "label": "热点新闻", "default_enabled": True}

SETTINGS = [
    {"key": "max_news", "label": "每次最多几条", "type": "number", "default": 3},
]

# fallback 链：Google News 中文头条 → 36kr → 少数派 → V2EX
_FALLBACK_FEEDS = [
    "https://36kr.com/feed",
    "https://sspai.com/feed",
    "https://www.v2ex.com/index.xml",
]


def collect(settings):
    settings = settings or {}
    try:
        max_news = max(1, int(settings.get("max_news") or 3))
    except (TypeError, ValueError):
        max_news = 3
    # 主源：Google News 中文头条（search.top_news 已封装）
    try:
        entries = search.top_news(limit=max_news + 2)
        if entries:
            return [
                {"title": "热点", "text": e["title"], "source": "google_news",
                 "url": e.get("url") or ""}
                for e in entries[:max_news]
            ]
    except Exception:
        pass
    # 兜底链：国内可达的 RSS 源
    for feed in _FALLBACK_FEEDS:
        try:
            items = core.parse_rss(core.http_text(feed, timeout=8))
        except Exception:
            continue
        if items:
            return [
                {"title": "热点", "text": title, "source": feed}
                for title in items[:max_news]
            ]
    return []


def suggest(settings, entries, state):
    """规则模式：有新热点就播报一次，同一条只说一遍。"""
    if not entries:
        return None
    title = entries[0]["text"]
    digest = __import__("hashlib").md5(title.encode("utf-8")).hexdigest()
    if state.get("last_hash") == digest:
        return None
    state["last_hash"] = digest
    if random.random() < 0.7:
        short = title if len(title) <= 60 else title[:57] + "…"
        return f"刷到个热点：{short}"
    return None
