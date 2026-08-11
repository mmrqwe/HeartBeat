"""每日一言内容源：免费的一言 API，偶尔主动分享一句。"""

import random

import core

META = {"name": "quote", "label": "每日一言", "default_enabled": True}

SETTINGS = [
    {"key": "probability", "label": "主动分享概率（0-1）", "type": "number", "default": 0.3},
]


def collect(settings):
    data = core.http_json("https://v1.hitokoto.cn/?encode=json")
    text = data.get("hitokoto", "").strip()
    source = data.get("from", "").strip()
    if not text:
        return []
    full = f"{text} —— {source}" if source else text
    return [{"title": "一言", "text": full, "data": {"text": text}}]


def suggest(settings, entries, state):
    """规则模式：有一定概率把新读到的一句话分享给主人。"""
    if not entries:
        return None
    text = entries[0]["text"]
    if state.get("last_quote") == text:
        return None
    try:
        probability = float((settings or {}).get("probability") or 0.3)
    except (TypeError, ValueError):
        probability = 0.3
    if random.random() < probability:
        state["last_quote"] = text
        return f"刚读到一句话：{text}"
    return None
