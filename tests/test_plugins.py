"""新内容源插件测试：hot_news / topic_watch / tech_watch / finance / rss 多源。"""

import inspect
from pathlib import Path
from tempfile import TemporaryDirectory

import core
import search

import plugins.finance as finance
import plugins.hot_news as hot_news
import plugins.rss_news as rss_news
import plugins.tech_watch as tech_watch
import plugins.topic_watch as topic_watch


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


def test_hot_news_google_fallback(monkeypatch):
    """主源失败时降级到国内 RSS 兜底链。"""
    monkeypatch.setattr(search, "top_news", lambda limit=8: (_ for _ in ()).throw(OSError("net down")))
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: "<rss><item><title>兜底新闻A</title></item></rss>")
    entries = hot_news.collect({"max_news": 2})
    assert entries and entries[0]["text"] == "兜底新闻A"


def test_hot_news_google_ok(monkeypatch):
    monkeypatch.setattr(
        search, "top_news",
        lambda limit=8: [{"title": "主源热点1"}, {"title": "主源热点2"}],
    )
    entries = hot_news.collect({"max_news": 2})
    assert [e["text"] for e in entries] == ["主源热点1", "主源热点2"]


def test_topic_watch_manual_topics_priority(monkeypatch):
    """设置页手动话题优先于 context。"""
    seen = {}

    def fake_news(topic, limit=6):
        seen["topic"] = topic
        return [{"title": f"{topic}相关新闻1"}]

    monkeypatch.setattr(search, "news_search", fake_news)
    settings = {"topics": ["摄影"]}
    entries = topic_watch.collect(settings, {"topics": ["手动覆盖"]})
    assert seen["topic"] == "摄影"
    assert entries[0]["text"] == "摄影相关新闻1"


def test_topic_watch_context_fallback(monkeypatch):
    """无手动话题时用 context.topics（agent 画像提取）。"""
    monkeypatch.setattr(
        search, "news_search",
        lambda topic, limit=6: [{"title": f"{topic}资讯"}],
    )
    entries = topic_watch.collect({}, {"topics": ["AI", "摄影"]})
    assert entries and "AI资讯" == entries[0]["text"]


def test_topic_watch_rotation(monkeypatch):
    """每 3 次巡视轮换话题（约 30 分钟）。"""
    topic_watch._calls = 0
    topic_watch._topic_index = 0
    seen = []
    monkeypatch.setattr(
        search, "news_search",
        lambda topic, limit=6: (seen.append(topic) or [{"title": topic}]),
    )
    for _ in range(6):
        topic_watch.collect({}, {"topics": ["A", "B"]})
    assert seen == ["A", "A", "A", "B", "B", "B"]


def test_tech_watch_arxiv_then_github(monkeypatch):
    """arXiv 失败时降级 GitHub API。"""
    monkeypatch.setattr(
        core, "parse_rss_items",
        lambda text: (_ for _ in ()).throw(OSError("arxiv down")),
    )
    monkeypatch.setattr(
        core, "http_json",
        lambda url, timeout=10: {"items": [
            {"full_name": "torvalds/linux", "description": "Linux kernel"},
            {"full_name": "user/repo", "description": None},
        ]},
    )
    entries = tech_watch.collect({"max_news": 2})
    assert entries[0]["text"] == "torvalds/linux：Linux kernel"
    assert entries[1]["text"] == "user/repo"


def test_finance_collect_format(monkeypatch):
    """行情格式化：名称 + 价格 + 涨跌幅。"""
    monkeypatch.setattr(
        search, "stock_quote",
        lambda code: {"name": "上证指数", "price": 3415.23, "pct": 0.32},
    )
    entries = finance.collect({"indices": ["sh000001"], "stocks": [], "max_news": 2})
    assert entries[0]["text"] == "上证指数 3415.23 +0.32%"


def test_rss_news_multi_feed_merge(monkeypatch):
    """多 feed 合并去重：两个 feed 各返回标题。"""
    feeds = {"https://a.com/rss": ["甲新闻", "乙新闻"], "https://b.com/rss": ["乙新闻", "丙新闻"]}
    monkeypatch.setattr(
        core, "http_text",
        lambda url, timeout=10: f"<rss><item><title>{feeds[url][0]}</title></item><item><title>{feeds[url][1]}</title></item></rss>",
    )
    entries = rss_news.collect({"feeds": list(feeds), "max_news": 5})
    texts = [e["text"] for e in entries]
    assert texts == ["甲新闻", "乙新闻", "丙新闻"]  # 乙新闻去重


def test_plugins_support_context_signature():
    """topic_watch 支持双参 collect（context 透传协议）。"""
    assert len(inspect.signature(topic_watch.collect).parameters) >= 2


def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if "monkeypatch" in params and "tmp_path" not in params:
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
