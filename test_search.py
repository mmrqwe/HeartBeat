"""搜索模块测试：网页/新闻/股票/天气解析与聊天意图。"""

import inspect
import json
import urllib.parse
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import agent
import core
import search


class _Patch:
    def __init__(self):
        self._saved = []

    def setattr(self, target, name, value):
        self._saved.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def restore(self):
        for target, name, old in reversed(self._saved):
            if old is None and hasattr(target, name):
                delattr(target, name)
            else:
                setattr(target, name, old)


def _cfg():
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["embedding_enabled"] = False  # 测试不下载嵌入模型
    return cfg


def _make_agent(tmp_path, cfg=None):
    return agent.Agent(
        cfg or _cfg(),
        data_dir=tmp_path,
        clock=lambda: datetime(2026, 8, 10, 10, 0),
    )


def test_web_search_parses_bing(monkeypatch):
    page = """
    <li class="b_algo">
      <h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8%3d&amp;ntb=1">Example <b>Title</b></a></h2>
      <div class="b_caption"><p>Some <b>snippet</b> here</p></div>
    </li>
    """
    monkeypatch.setattr(search, "_http_text_browser", lambda url, timeout=15: page)
    entries = search.web_search("example", 5)
    assert entries[0]["title"] == "Example Title"
    assert entries[0]["url"] == "https://example.com/"
    assert entries[0]["snippet"] == "Some snippet here"


def test_web_search_fallback_to_ddg(monkeypatch):
    page = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fddg.example%2F">'
        "DDG Title</a>"
        '<td class="result-snippet">DDG snip</td>'
    )

    def fake(url, timeout=15):
        if "bing.com" in url:
            raise RuntimeError("bing blocked")
        return page

    monkeypatch.setattr(search, "_http_text_browser", fake)
    entries = search.web_search("x", 5)
    assert entries and entries[0]["title"] == "DDG Title"
    assert entries[0]["url"] == "https://ddg.example/"
    assert entries[0]["snippet"] == "DDG snip"


def test_web_search_fallback_to_ddg_html(monkeypatch):
    """Bing 与 DDG lite 都不可用时，DDG html 端点兜底。"""
    page = (
        '<div class="result results_links_main">'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fhtml.example%2F">'
        "HTML Title</a>"
        '<a class="result__snippet">html snip</a>'
        "</div>"
    )

    def fake(url, timeout=15):
        if "bing.com" in url:
            raise RuntimeError("bing blocked")
        if "lite.duckduckgo.com" in url:
            return "<html>no results</html>"
        return page

    monkeypatch.setattr(search, "_http_text_browser", fake)
    entries = search.web_search("x", 5)
    assert entries and entries[0]["title"] == "HTML Title"
    assert entries[0]["url"] == "https://html.example/"
    assert entries[0]["snippet"] == "html snip"


def test_web_search_all_sources_fail_diag(monkeypatch):
    """全源失败：返回 [] 且 web_search_diag() 记录 3 个源的失败原因（不再静默）。"""

    def boom(url, timeout=15):
        raise RuntimeError("net down")

    monkeypatch.setattr(search, "_http_text_browser", boom)
    entries = search.web_search("x", 5)
    assert entries == []
    diag = search.web_search_diag()
    assert len(diag["errors"]) == 3
    assert all("net down" in e for e in diag["errors"])
    assert any("bing" in e for e in diag["errors"])
    assert any("ddg" in e for e in diag["errors"])


def test_web_search_success_clears_diag(monkeypatch):
    """失败后再成功：diag 被清空，调用方不会误判。"""
    state = {"failed": True}

    def fake(url, timeout=15):
        if "bing.com" in url:
            raise RuntimeError("bing down")
        if "lite.duckduckgo.com" in url:
            if state["failed"]:
                return "<html>empty</html>"
            return (
                '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fok.example%2F">'
                "OK</a>"
            )
        return "<html>empty</html>"

    monkeypatch.setattr(search, "_http_text_browser", fake)
    search.web_search("x", 5)  # bing 失败 + lite/html 空 → 全失败
    assert search.web_search_diag()["errors"]
    state["failed"] = False
    entries = search.web_search("y", 5)  # lite 成功
    assert entries
    assert search.web_search_diag()["errors"] == []


def test_news_search_parses_bing_rss(monkeypatch):
    rss = (
        "<rss><channel><item>"
        "<title>AI News</title>"
        "<link>https://example.com/news</link>"
        "<description>&lt;p&gt;desc text&lt;/p&gt;</description>"
        "</item></channel></rss>"
    )
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: rss)
    entries = search.news_search("AI", 5)
    assert entries[0]["title"] == "AI News"
    assert entries[0]["url"] == "https://example.com/news"
    assert entries[0]["snippet"] == "desc text"


def test_news_search_fallback_to_google(monkeypatch):
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>G News</title><link href="https://g.example"/>'
        "<summary>desc</summary></entry></feed>"
    )

    def fake(url, timeout=10):
        if "bing.com" in url:
            raise RuntimeError("blocked")
        return atom

    monkeypatch.setattr(core, "http_text", fake)
    entries = search.news_search("AI", 5)
    assert entries[0]["title"] == "G News"
    assert entries[0]["url"] == "https://g.example"


def test_wiki_search(monkeypatch):
    monkeypatch.setattr(
        core,
        "http_json",
        lambda url, timeout=10: {
            "query": {"search": [{"title": "人工智能", "snippet": "<b>AI</b> 领域"}]}
        },
    )
    entries = search.wiki_search("人工智能")
    assert entries[0]["title"] == "人工智能"
    assert "zh.wikipedia.org" in entries[0]["url"]
    assert entries[0]["snippet"] == "AI 领域"


def test_arxiv_search(monkeypatch):
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>Paper Title</title><link href="https://arxiv.org/abs/1"/>'
        "<summary>abstract text</summary></entry></feed>"
    )
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: atom)
    entries = search.arxiv_search("LLM")
    assert entries[0]["title"] == "Paper Title"
    assert entries[0]["url"] == "https://arxiv.org/abs/1"


def test_top_news(monkeypatch):
    rss = (
        "<rss><channel><item><title>Top1</title>"
        "<link>https://a.example</link></item></channel></rss>"
    )
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: rss)
    entries = search.top_news(5)
    assert entries[0]["title"] == "Top1"


def test_normalize_stock_code():
    assert search.normalize_stock_code("600519") == "sh600519"
    assert search.normalize_stock_code("000001") == "sz000001"
    assert search.normalize_stock_code("430047") == "bj430047"
    assert search.normalize_stock_code("sh600519") == "sh600519"
    assert search.normalize_stock_code("AAPL") == "usAAPL"
    assert search.normalize_stock_code("hk00700") == "hk00700"


def test_stock_quote_parses_tencent(monkeypatch):
    fields = [
        "1", "贵州茅台", "600519", "1710.00", "1690.00", "1700.00", "100000",
    ]
    fields += ["0"] * 23  # 索引 7..29，索引 30 是时间
    fields += ["20260810150000", "+20.00", "1.18", "1720.00", "1680.00", ""]
    fields += ["100000", "170000", "", ""]
    sample = 'v_sh600519="' + "~".join(fields) + '";'
    monkeypatch.setattr(
        search, "_http_text_enc", lambda url, encoding="gbk", timeout=10: sample
    )
    quote = search.stock_quote("600519")
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == 1710.0
    assert quote["pct"] == 1.18
    assert quote["url"] == "https://gu.qq.com/sh600519"


def test_weather_search(monkeypatch):
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: "上海: 多云 +30°C")
    assert search.weather_search("上海") == "上海: 多云 +30°C"


def test_agent_search_intent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: [
            {"title": "结果A", "url": "https://a.example", "snippet": "摘要A"}
        ],
    )
    a = _make_agent(tmp_path)
    reply = a.chat("搜索 人工智能")
    assert "结果A" in reply
    assert "https://a.example" in reply


def test_agent_stock_intent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: [
            {
                "title": "贵州茅台（600519）",
                "url": "https://gu.qq.com/sh600519",
                "snippet": "当前 1710.0",
            }
        ],
    )
    a = _make_agent(tmp_path)
    reply = a.chat("股票 600519")
    assert "茅台" in reply


def test_resolve_stock_code_via_smartbox(monkeypatch):
    """名称/拼音解析：smartbox 响应解析、多结果过滤（ZS 指数跳过）、美股去后缀。"""
    responses = {
        "长电科技": 'v_hint="sh~600584~长电科技~cdkj~GP-A";',
        "腾讯": (
            'v_hint="sh~000847~腾讯济安~txja~ZS^'
            'hk~00700~腾讯控股~txkg~GP^hk~80700~腾讯控股r~txkgr~GP";'
        ),
        "苹果": 'v_hint="us~aapl.oq~苹果~pg~GP^us~aply.am~苹果期权收益etf~qqsyetf~GP";',
        "茅台": 'v_hint="N";',
    }

    def fake_http(url, encoding="gbk", timeout=8):
        query = urllib.parse.unquote(url.split("q=")[1].split("&")[0])
        return responses.get(query, 'v_hint="N";')

    monkeypatch.setattr(search, "_http_text_enc", fake_http)
    assert search.resolve_stock_code("长电科技") == "sh600584"
    assert search.resolve_stock_code("腾讯") == "hk00700"  # 过滤指数，取第一个股票
    assert search.resolve_stock_code("苹果") == "usaapl"  # 美股去掉 .oq 后缀
    assert search.resolve_stock_code("600519") == "sh600519"  # 代码直通，不发请求
    assert search.resolve_stock_code("茅台") is None  # smartbox 无结果且非代码
    assert search.resolve_stock_code("AAPL") == "usaapl"  # 纯英文兜底美股


def test_stock_quote_fallback_to_sina(monkeypatch):
    """腾讯行情失败/超时时回退新浪（A 股）。"""
    monkeypatch.setattr(
        search,
        "_http_text_enc",
        lambda url, encoding="gbk", timeout=4: (_ for _ in ()).throw(RuntimeError("tencent down")),
    )
    sina_body = (
        'var hq_str_sh600584="长电科技,78.420,77.750,78.520,79.490,76.000,78.520,78.530,'
        '143099767,11127773471.000,102998,78.520,759100,78.510,88500,78.500,61600,'
        '78.490,56884,78.480,26100,78.530,55500,78.540,107600,78.550,48000,78.560,'
        '300,78.570,100,78.580,200,78.590,400,78.600,500,2026/08/10,15:00:00,00";'
    )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return sina_body.encode("gbk")

    monkeypatch.setattr(search.urllib.request, "urlopen", lambda req, timeout=4: _Resp())
    monkeypatch.setattr(search, "resolve_stock_code", lambda code: "sh600584")
    quote = search.stock_quote("600584")
    assert quote is not None
    assert quote["name"] == "长电科技"
    assert quote["price"] == 78.52
    expected_pct = (78.52 - 77.75) / 77.75 * 100
    assert quote["pct"] is not None and abs(quote["pct"] - expected_pct) < 0.01


def test_stock_quote_us_via_sina(monkeypatch):
    """美股行情走新浪（腾讯不支持美股）。"""
    body = (
        'var hq_str_gb_aapl="苹果,306.5662,-2.07,2026-08-11 01:15:29,-6.4938,'
        '306.8300,307.4900,304.6100,344.5700,222.9900,24418981,63792962,'
        '4474082304409,8.30,36.940000,0.00,0.86,0.00,0.00,14594179999,63,0.0000,";'
    )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body.encode("gbk")

    monkeypatch.setattr(search.urllib.request, "urlopen", lambda req, timeout=4: _Resp())
    monkeypatch.setattr(search, "resolve_stock_code", lambda code: "usaapl")
    quote = search.stock_quote("AAPL")
    assert quote is not None
    assert quote["name"] == "苹果"
    assert quote["price"] == 306.5662
    assert quote["pct"] == -2.07


def test_stock_quote_cache(monkeypatch):
    """60 秒内重复查询命中缓存，不重复发请求。"""
    calls = {"n": 0}

    def fake_tencent(normalized):
        calls["n"] += 1
        return {"name": "中国平安", "code": "601318", "price": 50.0, "pct": 1.0}

    monkeypatch.setattr(search, "resolve_stock_code", lambda code: "sh601318")
    monkeypatch.setattr(search, "_tencent_stock_quote", fake_tencent)
    search.stock_quote("601318")
    search.stock_quote("601318")
    assert calls["n"] == 1
    search._QUOTE_CACHE.pop("sh601318", None)


def test_agent_stock_intent_by_name(monkeypatch, tmp_path):
    """意图正则支持中文股票名：股票长电科技 -> search_all(长电科技, stock)。"""
    seen = {}

    def fake_search(query, category, limit=6):
        seen["query"] = query
        seen["category"] = category
        return [{"title": "长电科技（600584）", "url": "https://gu.qq.com/sh600584", "snippet": "当前 78.52"}]

    monkeypatch.setattr(search, "search_all", fake_search)
    a = _make_agent(tmp_path)
    reply = a.chat("股票长电科技")
    assert seen == {"query": "长电科技", "category": "stock"}
    assert "长电科技" in reply


def test_agent_search_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        search,
        "search_all",
        lambda query, category, limit=6: (_ for _ in ()).throw(RuntimeError("net down")),
    )
    a = _make_agent(tmp_path)
    reply = a.chat("搜索 人工智能")
    assert "搜索没成功" in reply


def test_selfcheck_search_retry_and_soft(monkeypatch):
    """selfcheck 的 web 搜索：重试恢复则 PASS；持续全源故障降级（不误报 FAIL）。"""
    import cli

    calls = {"n": 0}

    def fake_all(query, category, limit=6):
        calls["n"] += 1
        if calls["n"] >= 2:  # 首次失败，重试成功
            return [{"title": "T", "url": "https://t.example", "snippet": "S"}]
        return []

    def fake_diag():
        return {"errors": ["bing: HTTPError: 503"], "ts": 1}

    monkeypatch.setattr(cli.search, "search_all", fake_all)
    monkeypatch.setattr(cli.search, "web_search_diag", fake_diag)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    ok, detail = cli._web_search_selfcheck()
    assert ok and detail == ""
    assert calls["n"] == 2  # 确认重试了一次


def test_selfcheck_search_no_results_fails(monkeypatch):
    """真无结果（diag 无错误）→ 判定失败（不是降级）。"""
    import cli

    monkeypatch.setattr(cli.search, "search_all", lambda q, c, limit=6: [])
    monkeypatch.setattr(cli.search, "web_search_diag", lambda: {"errors": [], "ts": 1})
    ok, detail = cli._web_search_selfcheck()
    assert not ok and "no search results" in detail


def test_selfcheck_search_persistent_failure_detail(monkeypatch):
    """持续全源故障 → 失败且 detail 含各源原因（供 WARN 输出）。"""
    import cli

    monkeypatch.setattr(cli.search, "search_all", lambda q, c, limit=6: [])
    monkeypatch.setattr(
        cli.search,
        "web_search_diag",
        lambda: {"errors": ["bing: HTTPError: 503", "ddg-lite: TimeoutError"], "ts": 1},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    ok, detail = cli._web_search_selfcheck()
    assert not ok and "全源失败" in detail
    assert "bing" in detail and "ddg" in detail


def test_selfcheck_search_first_try_ok(monkeypatch):
    """首次即成功 → 不重试。"""
    import cli

    calls = {"n": 0}

    def fake_all(query, category, limit=6):
        calls["n"] += 1
        return [{"title": "T", "url": "https://t.example", "snippet": "S"}]

    monkeypatch.setattr(cli.search, "search_all", fake_all)
    ok, detail = cli._web_search_selfcheck()
    assert ok and calls["n"] == 1


def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if "tmp_path" in params:
                    with TemporaryDirectory() as d:
                        kwargs = {"tmp_path": Path(d)}
                        if "monkeypatch" in params:
                            kwargs["monkeypatch"] = patch
                        fn(**kwargs)
                elif "monkeypatch" in params:
                    fn(patch)
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
        patch.restore()
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    _run_plain()
