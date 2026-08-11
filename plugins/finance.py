"""财经行情内容源：沪深指数 + 自选股（腾讯/新浪双源，带缓存）。"""

import random

import search

META = {"name": "finance", "label": "财经行情", "default_enabled": False}

SETTINGS = [
    {
        "key": "indices",
        "label": "指数代码（如 sh000001 上证 / sz399001 深成 / sz399006 创业板）",
        "type": "list",
        "default": ["sh000001", "sz399001", "sz399006"],
    },
    {
        "key": "stocks",
        "label": "自选股（名称或代码，如 贵州茅台 / 600519 / AAPL）",
        "type": "list",
        "default": [],
    },
    {"key": "max_news", "label": "每次最多几条", "type": "number", "default": 3},
]


def _format_quote(quote):
    if not quote:
        return None
    pct = quote.get("pct")
    pct_text = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else ""
    price = quote.get("price")
    price_text = f"{price:.2f}" if isinstance(price, (int, float)) else str(price)
    return f"{quote.get('name', '')} {price_text} {pct_text}".strip()


def collect(settings):
    settings = settings or {}
    try:
        max_news = max(1, int(settings.get("max_news") or 3))
    except (TypeError, ValueError):
        max_news = 3
    codes = list(settings.get("indices") or []) + list(settings.get("stocks") or [])
    if not codes:
        return []
    entries = []
    for code in codes[:6]:  # 最多 6 个代码，防止长时间卡住巡视
        try:
            quote = search.stock_quote(code)
        except Exception:
            continue
        text = _format_quote(quote)
        if text:
            entries.append({"title": "行情", "text": text, "source": code})
    return entries[:max_news]


def suggest(settings, entries, state):
    """规则模式：盘中大幅波动时播报。"""
    if not entries:
        return None
    pick = None
    for entry in entries:
        text = entry["text"]
        import re

        match = re.search(r"([+-]\d+\.\d+)%", text)
        if match and abs(float(match.group(1))) >= 1.5:
            pick = text
            break
    if not pick:
        return None
    digest = __import__("hashlib").md5(pick.encode("utf-8")).hexdigest()
    if state.get("last_hash") == digest:
        return None
    state["last_hash"] = digest
    if random.random() < 0.6:
        return f"盘面有动静：{pick}"
    return None
