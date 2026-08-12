"""天气内容源：wttr.in 免费接口，无需 Key。"""

import json
import time
import urllib.parse

import core

META = {"name": "weather", "label": "天气", "default_enabled": True}

SETTINGS = [
    {"key": "city", "label": "城市（留空按 IP 定位）", "type": "text"},
]

# wttr.in 偶发 SSL 断连（UNEXPECTED_EOF_WHILE_READING），短重试兜底。
_MAX_TRIES = 3
_RETRY_DELAY = 1.5


def _fetch(url, timeout=12):
    last = None
    for attempt in range(_MAX_TRIES):
        try:
            return core.http_text(url, timeout=timeout)
        except Exception as exc:
            last = exc
            if attempt < _MAX_TRIES - 1:
                time.sleep(_RETRY_DELAY * (attempt + 1))
    assert last is not None  # _MAX_TRIES >= 1 时循环内必赋值
    raise last


def collect(settings):
    city = (settings or {}).get("city", "")
    query = urllib.parse.quote(city) if city else ""
    data = json.loads(_fetch("https://wttr.in/" + query + "?format=j1&lang=zh-cn"))
    cc = data["current_condition"][0]
    try:
        # JSON 接口的中文描述经常缺失，文本格式 %C 配 zh-cn 才稳定返回中文
        desc = _fetch(
            "https://wttr.in/" + query + "?format=%C&lang=zh-cn",
            timeout=8,
        ).strip()
    except Exception:
        desc = cc["weatherDesc"][0]["value"]
    return [{
        "title": "天气",
        "text": (
            f"{desc}，{cc['temp_C']}°C，体感{cc['FeelsLikeC']}°C，"
            f"湿度{cc['humidity']}%，风速{cc['windspeedKmph']}km/h"
        ),
        "data": {
            "temp": int(cc["temp_C"]),
            "feels": int(cc["FeelsLikeC"]),
            "desc": desc,
        },
    }]


def suggest(settings, entries, state):
    """规则模式：极端天气主动提醒。"""
    if not entries:
        return None
    data = entries[0].get("data") or {}
    temp = data.get("temp")
    desc = data.get("desc", "")
    if temp is not None:
        if temp <= 12:
            return f"外面才 {temp}°C，出门记得多穿点！"
        if temp >= 33:
            return f"外面 {temp}°C 好热，记得补水，别中暑啦。"
    if any(k in desc for k in ("雨", "雪", "雷", "霾")):
        return f"现在{desc}，出门记得带伞。"
    return None
