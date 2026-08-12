"""天气内容源：wttr.in + Open-Meteo 双源冗余。

策略（架构师评审）：
- 主备顺序切换：按 SOURCES 优先级依次尝试，首个成功即返回；
- 断路器：源连续失败 FAIL_THRESHOLD 次进入冷却 COOL_DURATION 秒（tick 间隔 20min，
  冷却 1h ≈ 3 个周期），冷却内直接跳过不发请求，过期后试探一次；
- 源内请求带 1 次快速重试（应对偶发 SSL 断连）；
- 无城市配置时 Open-Meteo 不可用（无 IP 定位），自动降级 wttr.in；
- 全部源失败时返回最近一次成功结果（TTL 3h 内），否则抛异常让采集层记录失败。
"""

import json
import time
import urllib.parse

import core

META = {"name": "weather", "label": "天气", "default_enabled": True}

SETTINGS = [
    {"key": "city", "label": "城市（留空按 IP 定位）", "type": "text"},
]

# ---------- 断路器 ----------
FAIL_THRESHOLD = 2       # 连续失败次数阈值
COOL_DURATION = 3600     # 冷却时长（秒）≈ 3 个 tick 周期（interval 20min）
_RETRY_DELAY = 1.0       # 源内重试间隔
_FETCH_TIMEOUT = 8       # 单请求超时（秒）

_health = {}             # source_name -> {"fails": int, "cool_until": float}


def _reset_health():
    """测试/调试：清空源健康状态。"""
    _health.clear()


def _source_cooling(name, now=None):
    now = now if now is not None else time.time()
    h = _health.get(name)
    return bool(h and h["fails"] >= FAIL_THRESHOLD and now < h["cool_until"])


def _record_success(name):
    _health.pop(name, None)


def _record_failure(name, now=None):
    now = now if now is not None else time.time()
    h = _health.setdefault(name, {"fails": 0, "cool_until": 0.0})
    h["fails"] += 1
    if h["fails"] >= FAIL_THRESHOLD:
        h["cool_until"] = now + COOL_DURATION


def _fetch_text(url, timeout=_FETCH_TIMEOUT, tries=2):
    """单请求带 1 次快速重试（应对 wttr.in 偶发 SSL 断连）。"""
    last = None
    for attempt in range(tries):
        try:
            return core.http_text(url, timeout=timeout)
        except Exception as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(_RETRY_DELAY)
    assert last is not None  # tries >= 1 时循环内必赋值
    raise last


# ---------- 源 1：wttr.in（主） ----------

def _fetch_wttr(settings):
    """wttr.in：j1 JSON + %C 中文描述。无城市时按 IP 定位。返回归一化 dict。"""
    city = (settings or {}).get("city", "")
    query = urllib.parse.quote(city) if city else ""
    data = json.loads(_fetch_text("https://wttr.in/" + query + "?format=j1&lang=zh-cn"))
    cc = data["current_condition"][0]
    try:
        # JSON 接口的中文描述经常缺失，文本格式 %C 配 zh-cn 才稳定返回中文
        desc = _fetch_text(
            "https://wttr.in/" + query + "?format=%C&lang=zh-cn",
        ).strip()
    except Exception:
        desc = cc["weatherDesc"][0]["value"]
    return {
        "temp": int(float(cc["temp_C"])),
        "feels": int(float(cc["FeelsLikeC"])),
        "desc": desc,
        "humidity": str(cc.get("humidity", "")),
        "wind": str(cc.get("windspeedKmph", "")),
        "source": "wttr.in",
    }


# ---------- 源 2：Open-Meteo（备） ----------

WMO_CODE_ZH = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "霰",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

_geocode_cache = {}      # 城市名 -> (lat, lon)，进程内有效


def _geocode(city):
    if city in _geocode_cache:
        return _geocode_cache[city]
    q = urllib.parse.quote(city)
    data = core.http_json(
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={q}&count=1&language=zh&format=json",
        timeout=_FETCH_TIMEOUT,
    )
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"geocode 无结果: {city}")
    lat = float(results[0]["latitude"])
    lon = float(results[0]["longitude"])
    _geocode_cache[city] = (lat, lon)
    return lat, lon


def _fetch_open_meteo(settings):
    """Open-Meteo 备源：geocode + forecast current。无城市配置时返回 None（降级主源）。"""
    city = (settings or {}).get("city", "")
    if not city:
        return None
    lat, lon = _geocode(city)
    data = core.http_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current="
        "temperature_2m,apparent_temperature,relative_humidity_2m,"
        "weather_code,wind_speed_10m",
        timeout=_FETCH_TIMEOUT,
    )
    cur = data.get("current") or {}
    code = int(cur.get("weather_code", 0))
    temp = cur.get("temperature_2m", 0)
    return {
        "temp": int(round(temp)),
        "feels": int(round(cur.get("apparent_temperature", temp))),
        "desc": WMO_CODE_ZH.get(code, "未知"),
        "humidity": str(cur.get("relative_humidity_2m", "")),
        "wind": str(cur.get("wind_speed_10m", "")),
        "source": "open-meteo",
    }


# ---------- 编排 ----------

SOURCES = [
    ("wttr", _fetch_wttr),
    ("open-meteo", _fetch_open_meteo),
]

_LAST_GOOD = None        # 最近一次成功结果
_LAST_GOOD_TS = 0.0
_LAST_GOOD_TTL = 3 * 3600


def _remember_good(result):
    global _LAST_GOOD, _LAST_GOOD_TS
    _LAST_GOOD = result
    _LAST_GOOD_TS = time.time()


def _make_entries(w):
    return [{
        "title": "天气",
        "text": (
            f"{w['desc']}，{w['temp']}°C，体感{w['feels']}°C，"
            f"湿度{w['humidity']}%，风速{w['wind']}km/h"
        ),
        "data": {
            "temp": w["temp"],
            "feels": w["feels"],
            "desc": w["desc"],
            "source": w.get("source", ""),
        },
    }]


def collect(settings):
    last_err = None
    for name, fetcher in SOURCES:
        if _source_cooling(name):
            continue
        try:
            result = fetcher(settings)
        except Exception as exc:
            last_err = exc
            _record_failure(name)
            continue
        if result is None:  # 源不适用（如无城市配置），不算失败
            continue
        _record_success(name)
        _remember_good(result)
        return _make_entries(result)
    # 全部源失败/冷却：返回最近一次成功结果（TTL 内），否则抛异常让采集层记录失败
    if _LAST_GOOD is not None and time.time() - _LAST_GOOD_TS <= _LAST_GOOD_TTL:
        return _make_entries(_LAST_GOOD)
    if last_err is not None:
        raise last_err
    raise RuntimeError("所有天气源均在冷却中")


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
