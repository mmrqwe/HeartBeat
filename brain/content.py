"""brain.content：内容采集与跨源汇聚（可进化域，纯逻辑）。

从 core.py 拆分（阶段1）：RSS/Atom 解析、插件采集编排、跨源去重汇聚。
不直接做 HTTP（插件自己经 core shim / kernel.http 抓取），保持零依赖。
"""

import hashlib
import inspect
import logging
import re
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger("heartbeat.content")


def parse_rss(text):
    """同时支持 RSS 2.0 和 Atom。"""
    return [item["title"] for item in parse_rss_items(text)]


def parse_rss_items(text):
    """解析 RSS/Atom，返回 [{title, link, description}]。"""
    root = ET.fromstring(text)
    items = []
    if root.tag.lower().endswith("feed"):
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(ns + "entry"):
            title = entry.findtext(ns + "title")
            if title:
                link_el = entry.find(ns + "link")
                link = link_el.get("href") if link_el is not None else ""
                summary = entry.findtext(ns + "summary")
                items.append({
                    "title": title.strip(),
                    "link": link or "",
                    "description": (summary or "").strip(),
                })
    else:
        for item in root.findall(".//item"):
            title = item.findtext("title")
            if title:
                items.append({
                    "title": title.strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "description": (item.findtext("description") or "").strip(),
                })
    return items


def collect_all(plugins, config, stats=None, context=None):
    """运行所有启用的插件，单项失败不影响其他项。"""
    results = []
    for name, module in plugins.items():
        settings = config.get("collectors", {}).get(name, {})
        default_enabled = module.META.get("default_enabled", True) if hasattr(module, "META") else True
        if not settings.get("enabled", default_enabled):
            continue
        label = module.META.get("label", name) if hasattr(module, "META") else name
        cache_hit = None
        try:
            if context is not None:
                try:
                    sig = inspect.signature(module.collect)
                    if len(sig.parameters) >= 2:
                        entries = module.collect(settings, context)
                    else:
                        entries = module.collect(settings)
                except (TypeError, ValueError):
                    entries = module.collect(settings)
            else:
                entries = module.collect(settings) or []
            if stats:
                text = "\n".join(str(e.get("text", "")) for e in entries)
                digest = hashlib.md5(text.encode("utf-8")).hexdigest()
                cache_hit = stats.check_content_hash(name, digest)
                chars = sum(len(str(e.get("text", ""))) for e in entries)
                stats.record_collect(name, True, len(entries), chars, cache_hit)
            results.append({
                "plugin": name,
                "label": label,
                "entries": entries,
                "error": None,
                "cache_hit": cache_hit,  # True=内容与上次巡视相同；False=有新内容；None=无法判断
            })
        except Exception as exc:
            if stats:
                stats.record_collect(name, False, 0, 0, False)
            results.append({
                "plugin": name,
                "label": label,
                "entries": [],
                "error": str(exc),
            })
    return results


def gather(plugins, config, stats=None, context=None):
    """一次自主巡视：运行所有内容源插件。context 透传给支持双参 collect 的插件。"""
    collections = collect_all(plugins, config, stats, context=context)
    errors = [
        f"{c['label']}: {c['error']}"
        for c in collections
        if c["error"]
    ]
    return {
        "collections": collections,
        "fetched_at": time.time(),
        "errors": errors,
    }


# ---------- 跨源汇聚（merge_select） ----------

MERGE_PRIORITY = {
    "topic_watch": 1.5,  # 主人兴趣相关资讯最优先
    "hot_news": 1.2,
    "rss_news": 1.0,
    "tech_watch": 1.0,
    "finance": 0.8,
    "quote": 0.6,
}
MERGE_TTL = 7200  # 跨源去重窗口（2 小时内同标题不重复报）


def _normalize_title(title):
    """标题归一化：去常见前缀 + 标点空白，用于跨源去重。"""
    text = str(title or "").strip().lower()
    for prefix in ("快讯", "独家", "突发", "最新", "今日", "推荐", "滚动"):
        while text.startswith(prefix):
            text = text[len(prefix):]
    return re.sub(r"[\s，。！？、：:；;\"'“”‘’（）()【】\[\]-]+", "", text)


def merge_entries(collections, seen=None, top_k=2):
    """跨源汇聚：只收本轮新内容（cache_hit is False），标题级去重（TTL），
    按源优先级排序后取 top_k 条。返回 (titles, updated_seen)。

    天气走 T0 突变通道，不在此汇聚。"""
    seen = dict(seen or {})
    now = time.time()
    for key in [k for k, t in seen.items() if now - t > MERGE_TTL]:
        del seen[key]
    pool = []
    for coll in collections:
        if coll.get("cache_hit") is not False or not coll.get("entries"):
            continue
        if coll["plugin"] == "weather":
            continue
        base = MERGE_PRIORITY.get(coll["plugin"], 1.0)
        for entry in coll["entries"][:3]:
            title = entry.get("text") or ""
            key = _normalize_title(title)
            if not key or key in seen:
                continue
            seen[key] = now
            pool.append((base, len(title), title))
    pool.sort(key=lambda x: (-x[0], -x[1]))
    return [t for _, _, t in pool[:top_k]], seen


# ---------- 大脑 ----------

# 情绪 -> 语气指令：只调语气强度，不改变性格内核
