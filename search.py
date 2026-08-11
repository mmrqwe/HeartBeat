"""搜索能力：网页、新闻、股票、天气。全部免费接口，无需 Key。"""

import base64
import html
import re
import time
import urllib.parse
import urllib.request

import core


# 行情缓存：代码 -> (quote_dict, 时间戳)，60 秒内重复查询直接返回
_QUOTE_CACHE = {}
_QUOTE_TTL = 60


def _http_text_enc(url, encoding="gbk", timeout=10):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": core.USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="replace")


def _strip_tags(text):
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _decode_ddg_url(href):
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and "uddg" in urllib.parse.parse_qs(parsed.query):
        return urllib.parse.parse_qs(parsed.query)["uddg"][0]
    return href


def _decode_bing_url(url):
    url = html.unescape(url)
    if "bing.com/ck/a" not in url:
        return url
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    encoded = params.get("u", [""])[0]
    if not encoded:
        return url
    try:
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        encoded += "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:
        return url


def _web_search_bing(query, limit=6):
    url = (
        "https://www.bing.com/search?q="
        + urllib.parse.quote(query)
        + "&setlang=zh-hans"
    )
    page = core.http_text(url, timeout=15)
    blocks = re.findall(r'<li class="b_algo".*?</li>', page, re.S)
    entries = []
    for block in blocks[:limit]:
        match = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.S,
        )
        if not match:
            continue
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        entries.append({
            "title": _strip_tags(match.group(2)),
            "url": _decode_bing_url(match.group(1)),
            "snippet": _strip_tags(snippet_match.group(1)) if snippet_match else "",
        })
    return entries


def _web_search_ddg(query, limit=6):
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    page = core.http_text(url, timeout=15)
    links = re.findall(
        r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>',
        page,
        re.S,
    )
    snippets = re.findall(
        r'<td class="result-snippet">(.*?)</td>',
        page,
        re.S,
    )
    entries = []
    for index, (href, title) in enumerate(links[:limit]):
        entries.append({
            "title": _strip_tags(title),
            "url": _decode_ddg_url(href),
            "snippet": _strip_tags(snippets[index]) if index < len(snippets) else "",
        })
    return entries


def _web_search_mojeek(query, limit=6):
    url = "https://www.mojeek.com/search?q=" + urllib.parse.quote(query)
    page = core.http_text(url, timeout=15)
    links = re.findall(
        r'class="title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        page,
        re.S,
    )
    snippets = re.findall(r'<p class="s">(.*?)</p>', page, re.S)
    entries = []
    for index, (href, title) in enumerate(links[:limit]):
        entries.append({
            "title": _strip_tags(title),
            "url": html.unescape(href),
            "snippet": _strip_tags(snippets[index]) if index < len(snippets) else "",
        })
    return entries


def web_search(query, limit=6):
    """网页搜索：依次尝试 Bing / DuckDuckGo / Mojeek。"""
    for provider in (_web_search_bing, _web_search_ddg, _web_search_mojeek):
        try:
            entries = provider(query, limit)
            if entries:
                return entries
        except Exception:
            continue
    return []


def news_search(query, limit=6):
    """新闻搜索：依次尝试 Bing 新闻 RSS / Google News RSS。"""
    url = (
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote(query)
        + "&format=rss"
    )
    urls = [
        url,
        (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        ),
    ]
    for candidate in urls:
        try:
            items = core.parse_rss_items(core.http_text(candidate, timeout=15))
        except Exception:
            continue
        if not items:
            continue
        return [
            {
                "title": item["title"],
                "url": item.get("link") or "",
                "snippet": _strip_tags(item.get("description") or ""),
            }
            for item in items[:limit]
        ]
    return []


def top_news(limit=8):
    """热点新闻：Google News 中文头条。"""
    url = "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    items = core.parse_rss_items(core.http_text(url, timeout=15))
    return [
        {
            "title": item["title"],
            "url": item.get("link") or "",
            "snippet": _strip_tags(item.get("description") or ""),
        }
        for item in items[:limit]
    ]


def wiki_search(query, limit=5):
    """维基百科（中文）搜索。"""
    url = (
        "https://zh.wikipedia.org/w/api.php?action=query&list=search"
        "&srsearch="
        + urllib.parse.quote(query)
        + "&format=json&srlimit="
        + str(limit)
    )
    data = core.http_json(url, timeout=15)
    entries = []
    for item in (data.get("query") or {}).get("search", []):
        title = item.get("title", "")
        entries.append({
            "title": title,
            "url": "https://zh.wikipedia.org/wiki/"
            + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": _strip_tags(item.get("snippet", "")),
        })
    return entries


def arxiv_search(query, limit=5):
    """arXiv 学术搜索。"""
    url = (
        "http://export.arxiv.org/api/query?search_query=all:"
        + urllib.parse.quote(query)
        + "&max_results="
        + str(limit)
    )
    items = core.parse_rss_items(core.http_text(url, timeout=20))
    return [
        {
            "title": item["title"],
            "url": item.get("link") or "",
            "snippet": _strip_tags(item.get("description") or ""),
        }
        for item in items[:limit]
    ]


def normalize_stock_code(code):
    code = code.strip().upper()
    if re.fullmatch(r"(SH|SZ|BJ|HK|US)\d{1,8}", code):
        return code.lower()
    if re.fullmatch(r"\d{6}", code):
        if code.startswith("6"):
            return "sh" + code
        if code.startswith(("4", "8")):
            return "bj" + code
        return "sz" + code
    if re.fullmatch(r"[A-Z]{1,6}", code):
        return "us" + code
    return code.lower()


def _smartbox_hits(text):
    """腾讯自选股智能搜索：按中文名/拼音/代码解析，返回候选行情代码列表。

    smartbox 返回 v_hint="市场~代码~名称~拼音~类型^..."，多条用 ^ 分隔；
    只保留股票（GP 开头）类型，美股代码去掉交易所后缀（aapl.oq -> aapl）。
    """
    url = (
        "https://smartbox.gtimg.cn/s3/?q="
        + urllib.parse.quote(text)
        + "&t=all"
    )
    raw = _http_text_enc(url, encoding="gbk", timeout=8)
    match = re.search(r'v_hint="(.*)"', raw)
    if not match or match.group(1) == "N":
        return []
    hits = []
    for item in match.group(1).split("^"):
        parts = item.split("~")
        if len(parts) < 5:
            continue
        market, code, _name, _pinyin, kind = parts[:5]
        if not kind.startswith("GP"):
            continue
        if market == "us":
            code = code.split(".")[0]
        if market in ("sh", "sz", "bj", "hk", "us") and code.isalnum():
            hits.append(f"{market}{code}")
    return hits


def resolve_stock_code(text):
    """把股票代码 / 中文名 / 拼音解析为腾讯行情代码（sh600584 / hk00700 / usaapl）。

    明确的代码直接规范化；名称/拼音走腾讯智能搜索（取第一个股票类结果，
    即最相关的 A 股或主要上市地）；纯英文单词兜底按美股代码处理。
    """
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"(SH|SZ|BJ|HK|US)\d{1,8}", text, re.I) or re.fullmatch(r"\d{6}", text):
        return normalize_stock_code(text)
    try:
        hits = _smartbox_hits(text)
    except Exception:
        hits = []
    if hits:
        return hits[0]
    if re.fullmatch(r"[A-Z]{1,6}", text, re.I):
        return "us" + text.lower()
    return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tencent_stock_quote(normalized):
    """腾讯行情（A 股/港股/京）：字段全（时间/涨跌/成交量额）。"""
    text = _http_text_enc(
        "https://qt.gtimg.cn/q=" + normalized, encoding="gbk", timeout=4
    )
    match = re.search(r'="(.*)"', text)
    if not match:
        return None
    fields = match.group(1).split("~")
    if len(fields) < 38 or not fields[1]:
        return None
    return {
        "name": fields[1],
        "code": fields[2],
        "price": _to_float(fields[3]),
        "prev_close": _to_float(fields[4]),
        "open": _to_float(fields[5]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "change": _to_float(fields[31]),
        "pct": _to_float(fields[32]),
        "volume": fields[36],
        "amount": fields[37],
        "time": fields[30],
        "url": f"https://gu.qq.com/{normalized}",
    }


def _sina_stock_quote(normalized):
    """新浪行情（hq.sinajs.cn）：A 股/港股/美股通用，作为腾讯慢/失败时的兜底。"""
    prefix = normalized[:2]
    symbol = normalized[2:]
    if prefix == "us":
        url = "https://hq.sinajs.cn/list=gb_" + symbol
    elif prefix == "hk":
        url = "https://hq.sinajs.cn/list=rt_hk" + symbol
    else:
        url = "https://hq.sinajs.cn/list=" + normalized
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": core.USER_AGENT,
            "Referer": "https://finance.sina.com.cn",
        },
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        text = resp.read().decode("gbk", errors="replace")
    match = re.search(r'="(.*)"', text)
    if not match or not match.group(1):
        return None
    fields = match.group(1).split(",")
    if prefix == "us":
        # 苹果,现价,涨跌幅%,时间,涨跌额,今开,最高,最低,52周高,52周低,成交量,...
        if len(fields) < 11 or not fields[0]:
            return None
        return {
            "name": fields[0],
            "code": symbol.upper(),
            "price": _to_float(fields[1]),
            "prev_close": None,
            "open": _to_float(fields[5]),
            "high": _to_float(fields[6]),
            "low": _to_float(fields[7]),
            "change": _to_float(fields[4]),
            "pct": _to_float(fields[2]),
            "volume": fields[10],
            "amount": "",
            "time": fields[3],
            "url": f"https://gu.qq.com/{normalized}",
        }
    if prefix == "hk":
        # 英文名,名称,今开,昨收,最高,最低,现价,涨跌额,涨跌幅%,买一,卖一,成交额,成交量,...
        if len(fields) < 13 or not fields[1]:
            return None
        price = _to_float(fields[6])
        prev_close = _to_float(fields[3])
        return {
            "name": fields[1],
            "code": fields[0] or symbol,
            "price": price,
            "prev_close": prev_close,
            "open": _to_float(fields[2]),
            "high": _to_float(fields[4]),
            "low": _to_float(fields[5]),
            "change": _to_float(fields[7]),
            "pct": _to_float(fields[8]),
            "volume": fields[12],
            "amount": fields[11],
            "time": (fields[18] if len(fields) > 18 else "") + " " + (fields[19] if len(fields) > 19 else ""),
            "url": f"https://gu.qq.com/{normalized}",
        }
    # A 股：名称,今开,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),...,日期,时间
    if len(fields) < 10 or not fields[0]:
        return None
    price = _to_float(fields[3])
    prev_close = _to_float(fields[2])
    change = (price - prev_close) if (price is not None and prev_close) else None
    pct = (change / prev_close * 100) if (change is not None and prev_close) else None
    return {
        "name": fields[0],
        "code": symbol,
        "price": price,
        "prev_close": prev_close,
        "open": _to_float(fields[1]),
        "high": _to_float(fields[4]),
        "low": _to_float(fields[5]),
        "change": change,
        "pct": pct,
        "volume": fields[8],
        "amount": fields[9],
        "time": (fields[30] if len(fields) > 30 else "") + " " + (fields[31] if len(fields) > 31 else ""),
        "url": f"https://gu.qq.com/{normalized}",
    }


def stock_quote(code):
    """实时行情：支持代码/名称/拼音（长电科技、changdian、600584、AAPL）。

    A 股/港股/京走腾讯（字段全），美股走新浪（腾讯不支持美股）；
    腾讯慢或失败时统一回退新浪；60 秒内重复查询命中缓存。
    """
    normalized = resolve_stock_code(code)
    if not normalized:
        return None
    cached = _QUOTE_CACHE.get(normalized)
    if cached and time.time() - cached[1] < _QUOTE_TTL:
        return cached[0]
    quote = None
    if normalized.startswith("us"):
        quote = _sina_stock_quote(normalized)
    else:
        try:
            quote = _tencent_stock_quote(normalized)
        except Exception:
            quote = None
        if not quote:
            try:
                quote = _sina_stock_quote(normalized)
            except Exception:
                quote = None
    if quote:
        _QUOTE_CACHE[normalized] = (quote, time.time())
    return quote


def weather_search(city):
    """按城市查天气：wttr.in 优先，失败时回退 Open-Meteo。"""
    query = urllib.parse.quote(city)
    try:
        text = core.http_text(
            "https://wttr.in/" + query + "?format=%l:+%C+%t+%h+%w&lang=zh-cn",
            timeout=15,
        )
        return text.strip()
    except Exception:
        pass
    geo = core.http_json(
        "https://geocoding-api.open-meteo.com/v1/search?name="
        + query
        + "&count=1&language=zh",
        timeout=15,
    )
    results = geo.get("results") or []
    if not results:
        return f"查不到 {city} 的天气。"
    place = results[0]
    forecast = core.http_json(
        "https://api.open-meteo.com/v1/forecast?latitude="
        + str(place["latitude"])
        + "&longitude="
        + str(place["longitude"])
        + "&current_weather=true&timezone=auto",
        timeout=15,
    )
    current = forecast.get("current_weather") or {}
    name = place.get("name", city)
    return (
        f"{name}：{current.get('temperature')}°C，"
        f"风速 {current.get('windspeed')} km/h，"
        f"风向 {current.get('winddirection')}°"
    )


def search_all(query, category="web", limit=6):
    """统一搜索入口，返回 [{title, url, snippet}]。"""
    category = (category or "web").lower()
    if category == "news":
        return news_search(query, limit)
    if category == "hot":
        return top_news(limit)
    if category == "wiki":
        return wiki_search(query, limit)
    if category == "arxiv":
        return arxiv_search(query, limit)
    if category == "stock":
        quote = stock_quote(query)
        if not quote:
            return [{
                "title": "未找到该股票",
                "url": "",
                "snippet": (
                    f"查不到 {query} 的行情。试试代码（600519 / hk00700 / usAAPL）"
                    "或名称/拼音（贵州茅台、gzmt）。"
                ),
            }]
        return [{
            "title": f"{quote['name']}（{quote['code']}）",
            "url": quote["url"],
            "snippet": (
                f"当前 {quote['price']}｜涨跌 {quote['change']}（{quote['pct']}%）｜"
                f"今开 {quote['open']}｜最高 {quote['high']}｜最低 {quote['low']}｜"
                f"成交量 {quote['volume']} 手｜成交额 {quote['amount']} 万｜{quote['time']}"
            ),
        }]
    if category == "weather":
        text = weather_search(query)
        return [{"title": f"{query} 天气", "url": "", "snippet": text}]
    return web_search(query, limit)


def format_results(entries, label):
    """把搜索结果格式化成给桌宠/聊天用的文本。"""
    if not entries:
        return f"{label}：没找到结果。"
    lines = [f"{label}结果："]
    for index, entry in enumerate(entries[:5], 1):
        lines.append(f"{index}. {entry.get('title', '')}")
        if entry.get("url"):
            lines.append(f"   {entry['url']}")
        snippet = entry.get("snippet") or ""
        if snippet:
            lines.append(f"   {snippet[:90]}")
    return "\n".join(lines)
