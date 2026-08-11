"""科技前沿内容源：arXiv 最新论文 + GitHub 热榜（API，无需 token）。"""

import random
import urllib.parse

import core
import search

META = {"name": "tech_watch", "label": "科技前沿", "default_enabled": False}

SETTINGS = [
    {"key": "max_news", "label": "每次最多几条", "type": "number", "default": 3},
]

_ARXIV_QUERY = "cat:cs.AI OR cat:cs.LG OR cat:cs.CL"


def _arxiv_entries(limit):
    """arXiv 最新 AI/机器学习论文（https 直连，国内部分网络可用）。"""
    url = (
        "https://export.arxiv.org/api/query?search_query="
        + urllib.parse.quote(_ARXIV_QUERY)
        + "&sortBy=submittedDate&sortOrder=descending&max_results="
        + str(limit)
    )
    items = core.parse_rss_items(core.http_text(url, timeout=15))
    return [
        {"title": "论文", "text": item["title"], "source": "arXiv",
         "url": item.get("link") or ""}
        for item in items[:limit]
    ]


def _github_entries(limit):
    """GitHub 一周热门新仓库（API，公开无需 token）。"""
    url = (
        "https://api.github.com/search/repositories"
        "?q=created:%3E7d&sort=stars&order=desc&per_page=" + str(limit)
    )
    data = core.http_json(url, timeout=10)
    entries = []
    for repo in (data.get("items") or [])[:limit]:
        full = repo.get("full_name", "")
        desc = (repo.get("description") or "")[:40]
        text = f"{full}：{desc}" if desc else full
        entries.append({"title": "GitHub", "text": text, "source": "GitHub",
                        "url": repo.get("html_url") or ""})
    return entries


def collect(settings):
    settings = settings or {}
    try:
        max_news = max(1, int(settings.get("max_news") or 3))
    except (TypeError, ValueError):
        max_news = 3
    # 主源：arXiv 论文
    try:
        entries = _arxiv_entries(max_news)
        if entries:
            return entries
    except Exception:
        pass
    # 兜底：GitHub 热榜
    try:
        return _github_entries(max_news)
    except Exception:
        return []


def suggest(settings, entries, state):
    """规则模式：新科技动态播报。"""
    if not entries:
        return None
    title = entries[0]["text"]
    digest = __import__("hashlib").md5(title.encode("utf-8")).hexdigest()
    if state.get("last_hash") == digest:
        return None
    state["last_hash"] = digest
    if random.random() < 0.6:
        short = title if len(title) <= 55 else title[:52] + "…"
        return f"科技圈新动向：{short}"
    return None
