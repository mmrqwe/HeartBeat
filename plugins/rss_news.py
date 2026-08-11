"""RSS 新闻内容源：按顺序尝试多个订阅源，抓取头条。"""

import hashlib
import random

import core

META = {"name": "rss_news", "label": "RSS 新闻", "default_enabled": True}

DEFAULT_FEEDS = [
    # 时政 / 大事
    "https://www.people.com.cn/rss/politics.xml",
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
    "http://www.xinhuanet.com/politics/news_politics.xml",
    # 财经
    "https://www.people.com.cn/rss/finance.xml",
    "http://www.xinhuanet.com/fortune/news_fortune.xml",
    "https://www.chinanews.com.cn/rss/finance.xml",
    "https://www.ftchinese.com/rss/feed",
    # 科技
    "https://www.ithome.com/rss/",
    "http://www.xinhuanet.com/tech/news_tech.xml",
    "https://www.oschina.net/news/rss",
    # 国际
    "https://www.people.com.cn/rss/world.xml",
    "http://www.xinhuanet.com/world/news_world.xml",
    "https://www.chinanews.com.cn/rss/world.xml",
    "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "https://cn.nytimes.com/rss/",
    "https://www.rfi.fr/cn/rss",
    # 更多科技 / 财经
    "https://juejin.cn/rss",
    "https://www.cnbeta.com.tw/backend.php",
    "https://www.solidot.org/index.rss",
    "https://sspai.com/feed",
    "https://www.qbitai.com/feed",
    "https://www.tmtpost.com/rss.xml",
    "http://rss.sina.com.cn/news/marquee/ddt.xml",
]

SETTINGS = [
    {"key": "feeds", "label": "RSS 源", "type": "list", "default": DEFAULT_FEEDS},
    {"key": "max_news", "label": "每次最多几条", "type": "number", "default": 5},
]


def collect(settings):
    settings = settings or {}
    feeds = settings.get("feeds") or DEFAULT_FEEDS
    try:
        max_news = max(1, int(settings.get("max_news") or 5))
    except (TypeError, ValueError):
        max_news = 5
    # 按分类交错排序逐个拉取，每个源先取一条；凑够 max_news 就停，
    # 避免等完全部源才返回。可用源不够时再从已成功的源里补足。
    pools = []
    seen = set()
    results = []
    for feed in feeds[:12]:
        try:
            items = core.parse_rss_items(core.http_text(feed, timeout=6))
        except Exception:
            continue
        if items:
            pools.append((feed, items))
            for item in items:
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                key = title.lower()
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "title": "新闻",
                    "text": title,
                    "source": feed,
                    "link": item.get("link", ""),
                })
                break
            if len(results) >= max_news:
                return results
    idx = 1
    while len(results) < max_news and pools:
        added = False
        for feed, items in pools:
            if idx >= len(items):
                continue
            title = (items[idx].get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "title": "新闻",
                "text": title,
                "source": feed,
                "link": items[idx].get("link", ""),
            })
            added = True
            if len(results) >= max_news:
                return results
        idx += 1
        if not added:
            break
    return results


def suggest(settings, entries, state):
    """规则模式：有新头条就播报一次，同一条只说一遍。"""
    if not entries:
        return None
    title = entries[0]["text"]
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    if state.get("last_hash") == digest:
        return None
    state["last_hash"] = digest
    if random.random() < 0.7:
        short = title if len(title) <= 60 else title[:57] + "…"
        return f"刚看到一条新闻：{short}"
    return None
