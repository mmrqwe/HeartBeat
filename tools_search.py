"""tools_search：联网搜索（web 工具，6 分类合一）。"""

import search

# ---------- 搜索工具（只读） ----------

# (name, description, 参数名, 参数说明, kind, limit, label)
SEARCH_SPECS = [
    ("web_search", "搜索网页，获取最新信息", "query", "搜索关键词", "web", 6, "搜索"),
    ("news_search", "搜索最新新闻", "query", "新闻主题", "news", 6, "新闻"),
    ("stock_quote", "查询股票实时行情（最新价/涨跌/成交量），支持代码或名称/拼音，"
     "如 600584、sh600584、长电科技、changdian、AAPL",
     "code", "股票代码或名称", "stock", 1, "股票"),
    ("weather", "查询城市天气", "city", "城市名", "weather", 1, "天气"),
    ("wiki_search", "搜索维基百科知识条目", "query", "词条", "wiki", 5, "百科"),
    ("arxiv_search", "搜索 arXiv 学术论文", "query", "研究主题", "arxiv", 5, "学术"),
]


def _make_search_handler(param, kind, limit, label):
    def handler(args):
        query = str(args.get(param, "")).strip()
        if not query:
            return "缺少查询参数"
        entries = search.search_all(query, kind, limit)
        if not entries and kind == "web" and search.web_search_diag().get("errors"):
            # 全源故障而非真无结果：明确告知，避免误导"没找到"
            return "搜索服务暂时不可用（" + "; ".join(search.web_search_diag()["errors"]) + "）"
        return search.format_results(entries, label)

    return handler


SEARCH_HANDLERS = {
    name: _make_search_handler(param, kind, limit, label)
    for name, _desc, param, _param_desc, kind, limit, label in SEARCH_SPECS
}

_WEB_KEY_TO_SEARCH = {
    "web": ("web_search", "query"),
    "news": ("news_search", "query"),
    "stock": ("stock_quote", "code"),
    "weather": ("weather", "city"),
    "wiki": ("wiki_search", "query"),
    "arxiv": ("arxiv_search", "query"),
}


def _exec_web(args, source, audit):
    """web：一个工具按 category 分发到 6 个搜索源。"""
    category = str(args.get("category", "web") or "web").strip().lower()
    query = str(args.get("query", "") or "").strip()
    entry = _WEB_KEY_TO_SEARCH.get(category)
    if entry is None:
        return "未知搜索分类：" + category
    key, param = entry
    if not query:
        return "缺少查询参数（query）"
    text = SEARCH_HANDLERS[key]({param: query})
    if audit:
        audit(source, "web", f"{category}:{query}", "readonly", True, True, text[:200])
    return text


