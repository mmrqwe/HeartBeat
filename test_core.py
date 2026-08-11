"""HeartBeat 核心逻辑测试：插件、采集、规则大脑。可直接运行，也可用 pytest。"""

import inspect
import json
import random
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

import core
import db as dbmod
import plugins.quote as quote
import plugins.rss_news as rss_news
import plugins.weather as weather


class _Patch:
    """兼容 pytest monkeypatch 的最小实现，供直接运行时使用。"""

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
    return json.loads(json.dumps(core.DEFAULT_CONFIG))


def _plugins():
    return {
        "weather": weather,
        "rss_news": rss_news,
        "quote": quote,
    }


# ---------- 配置 ----------

def test_config_merge(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"pet_name": "测试", "api": {"api_key": "abc"}}),
        encoding="utf-8",
    )
    cfg = core.load_config(str(cfg_path))
    assert cfg["pet_name"] == "测试"
    assert cfg["api"]["api_key"] == "abc"
    assert cfg["interval_minutes"] == 10
    assert cfg["api"]["model"] == "gpt-4o-mini"


def test_config_default_role():
    cfg = _cfg()
    assert cfg["role"] == "小橘猫"


def test_config_legacy_migration(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "weather_city": "beijing",
            "news_feeds": ["https://example.com/rss"],
        }),
        encoding="utf-8",
    )
    cfg = core.load_config(str(cfg_path))
    assert cfg["collectors"]["weather"]["city"] == "beijing"
    assert cfg["collectors"]["rss_news"]["feeds"] == ["https://example.com/rss"]


# ---------- 插件机制 ----------

def test_discover_plugins(tmp_path):
    (tmp_path / "hello.py").write_text(
        "META = {'name': 'hello', 'label': '问候'}\n"
        "SETTINGS = []\n"
        "def collect(settings):\n"
        "    return [{'title': '问候', 'text': '你好'}]\n",
        encoding="utf-8",
    )
    plugins = core.discover_plugins([tmp_path])
    assert "hello" in plugins
    assert plugins["hello"].collect({}) == [{"title": "问候", "text": "你好"}]


def test_discover_real_plugins():
    plugins = core.discover_plugins([Path(__file__).parent / "plugins"])
    assert {"weather", "rss_news", "quote"} <= set(plugins)


def test_collect_all_disabled(monkeypatch):
    cfg = _cfg()
    cfg["collectors"]["weather"]["enabled"] = False
    monkeypatch.setattr(weather, "collect", lambda settings: [{"title": "天气", "text": "晴"}])
    monkeypatch.setattr(rss_news, "collect", lambda settings: [{"title": "新闻", "text": "头条"}])
    monkeypatch.setattr(quote, "collect", lambda settings: [{"title": "一言", "text": "一句话"}])
    results = core.collect_all(_plugins(), cfg)
    names = [r["plugin"] for r in results]
    assert "weather" not in names
    assert "rss_news" in names
    assert "quote" in names


def test_collect_all_error_is_isolated(monkeypatch):
    monkeypatch.setattr(weather, "collect", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(rss_news, "collect", lambda settings: [{"title": "新闻", "text": "头条"}])
    monkeypatch.setattr(quote, "collect", lambda settings: [{"title": "一言", "text": "一句话"}])
    results = core.collect_all(_plugins(), _cfg())
    by_name = {r["plugin"]: r for r in results}
    assert by_name["weather"]["error"]
    assert by_name["rss_news"]["entries"]


def test_gather(monkeypatch):
    monkeypatch.setattr(weather, "collect", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(rss_news, "collect", lambda settings: [{"title": "新闻", "text": "头条"}])
    monkeypatch.setattr(quote, "collect", lambda settings: [{"title": "一言", "text": "一句话"}])
    ctx = core.gather(_plugins(), _cfg())
    assert any("天气" in e for e in ctx["errors"])
    assert len(ctx["collections"]) == 3


# ---------- 插件：天气 ----------

def test_weather_collect(monkeypatch):
    monkeypatch.setattr(
        core,
        "http_json",
        lambda url, timeout=10: {
            "current_condition": [{
                "temp_C": "18",
                "FeelsLikeC": "17",
                "weatherDesc": [{"value": "Cloudy"}],
                "humidity": "66",
                "windspeedKmph": "12",
            }]
        },
    )
    monkeypatch.setattr(core, "http_text", lambda url, timeout=10: "多云")
    entries = weather.collect({"city": "shanghai"})
    assert "多云" in entries[0]["text"]
    assert entries[0]["data"]["temp"] == 18


def test_weather_suggest():
    assert weather.suggest({}, [{"data": {"temp": 5, "desc": "晴"}}], {}) is not None
    assert weather.suggest({}, [{"data": {"temp": 20, "desc": "小雨"}}], {}) is not None
    assert weather.suggest({}, [{"data": {"temp": 20, "desc": "晴"}}], {}) is None


# ---------- 插件：RSS 新闻 ----------

def test_rss_news_collect(monkeypatch):
    monkeypatch.setattr(
        core,
        "http_text",
        lambda url, timeout=10: (
            "<rss><channel>"
            "<item><title>One</title></item>"
            "<item><title>Two</title></item>"
            "</channel></rss>"
        ),
    )
    entries = rss_news.collect({"feeds": ["https://example.com/rss"], "max_news": 1})
    assert [e["text"] for e in entries] == ["One"]


def test_rss_news_collect_merges_feeds(monkeypatch):
    def fake(url, timeout=10):
        if "a.example" in url:
            return (
                "<rss><channel>"
                "<item><title>A1</title></item>"
                "<item><title>A2</title></item>"
                "</channel></rss>"
            )
        return (
            "<rss><channel>"
            "<item><title>B1</title></item>"
            "<item><title>A1</title></item>"
            "</channel></rss>"
        )

    monkeypatch.setattr(core, "http_text", fake)
    entries = rss_news.collect({
        "feeds": ["https://a.example/rss", "https://b.example/rss"],
        "max_news": 2,
    })
    assert [e["text"] for e in entries] == ["A1", "B1"]


def test_rss_news_suggest_once(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.5)
    state = {}
    entries = [{"text": "大新闻标题"}]
    assert "大新闻标题" in rss_news.suggest({}, entries, state)
    assert rss_news.suggest({}, entries, state) is None


# ---------- 插件：每日一言 ----------

def test_quote_collect(monkeypatch):
    monkeypatch.setattr(
        core,
        "http_json",
        lambda url, timeout=10: {"hitokoto": "坚持就是胜利", "from": "佚名"},
    )
    entries = quote.collect({})
    assert "坚持就是胜利" in entries[0]["text"]
    assert "佚名" in entries[0]["text"]


def test_quote_suggest(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.1)
    state = {}
    entries = [{"text": "一句话"}]
    assert "一句话" in quote.suggest({"probability": 0.5}, entries, state)
    assert quote.suggest({"probability": 0.5}, entries, state) is None


# ---------- 大脑 ----------

def test_think_rules_uses_plugins():
    brain = core.Brain(_cfg(), _plugins())
    ctx = {
        "collections": [
            {"plugin": "weather", "label": "天气", "entries": [{"data": {"temp": 5, "desc": "晴"}}]}
        ]
    }
    assert "多穿" in brain.think(ctx)


def test_think_rules_quiet():
    brain = core.Brain(_cfg(), _plugins())
    ctx = {
        "collections": [
            {"plugin": "weather", "label": "天气", "entries": [{"data": {"temp": 20, "desc": "晴"}}]}
        ]
    }
    assert brain.think(ctx) is None


def test_think_llm_silent(monkeypatch):
    monkeypatch.setattr(core.Brain, "_chat_completion", lambda self, msgs: "SILENT")
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    brain = core.Brain(cfg, {})
    assert brain.think({"collections": []}) is None


def test_think_llm_message(monkeypatch):
    monkeypatch.setattr(
        core.Brain, "_chat_completion", lambda self, msgs: "今晚可能有流星雨！"
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    brain = core.Brain(cfg, {})
    assert brain.think({"collections": []}) == "今晚可能有流星雨！"


def test_chat_rules_weather(monkeypatch):
    monkeypatch.setattr(
        weather,
        "collect",
        lambda settings: [{"title": "天气", "text": "晴，21°C"}],
    )
    brain = core.Brain(_cfg(), _plugins())
    reply = brain.chat("今天天气怎么样")
    assert "21" in reply


def test_chat_rules_fallback():
    brain = core.Brain(_cfg(), _plugins())
    assert "在呀" in brain.chat("你好")
    assert "小跳" in brain.chat("你叫什么名字")


def test_chat_rules_uses_role():
    cfg = _cfg()
    cfg["role"] = "小女生"
    brain = core.Brain(cfg, _plugins())
    assert "小女生" in brain.chat("你叫什么名字")


# ---------- 工具 ----------

def test_parse_rss():
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>你好世界</title></entry></feed>"
    )
    rss = "<rss><channel><item><title>Hello</title></item></channel></rss>"
    assert core.parse_rss(atom) == ["你好世界"]
    assert core.parse_rss(rss) == ["Hello"]


def test_context_text():
    brain = core.Brain(_cfg(), {})
    ctx = {
        "collections": [
            {"plugin": "weather", "label": "天气", "entries": [{"text": "晴，20°C"}]},
            {"plugin": "rss_news", "label": "RSS 新闻", "entries": [{"text": "头条A"}]},
        ],
        "errors": [],
    }
    text = brain._context_text(ctx)
    assert "[天气]" in text
    assert "头条A" in text


# ---------- 用户数据目录与旧数据迁移 ----------

def test_user_data_dir_darwin():
    patch = _Patch()
    try:
        patch.setattr(core.sys, "platform", "darwin")
        patch.setattr(Path, "home", lambda: Path("/Users/test"))
        assert core.user_data_dir() == Path("/Users/test/Library/Application Support/HeartBeat")
    finally:
        patch.restore()


def test_user_data_dir_windows():
    # 真机（POSIX）上无法实例化 WindowsPath，用 stub Path 验证分支逻辑
    class _FakePath(str):
        def __truediv__(self, other):
            return _FakePath(str(self) + "/" + str(other))

    patch = _Patch()
    try:
        patch.setattr(core.sys, "platform", "win32")
        patch.setattr(core.os, "name", "nt")
        patch.setattr(core.os, "environ", {"APPDATA": r"C:\Users\test\AppData\Roaming"})
        patch.setattr(core, "Path", _FakePath)
        path = str(core.user_data_dir())
        assert "AppData" in path and path.endswith("HeartBeat")
    finally:
        patch.restore()


def test_user_data_dir_linux():
    patch = _Patch()
    try:
        patch.setattr(core.sys, "platform", "linux")
        patch.setattr(core.os, "name", "posix")
        patch.setattr(core.os, "environ", {"XDG_DATA_HOME": "/tmp/xdg"})
        assert core.user_data_dir() == Path("/tmp/xdg/HeartBeat")
        patch.setattr(core.os, "environ", {})
        assert core.user_data_dir() == Path.home() / ".local" / "share" / "HeartBeat"
    finally:
        patch.restore()


def test_migrate_legacy_data_copies(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"pet_name": "旧名"}', encoding="utf-8")
    (legacy / "heartbeat.db").write_bytes(b"old-db")
    data_dir = tmp_path / "data"
    result = core.migrate_legacy_data([str(legacy)], data_dir)
    assert result == data_dir
    assert (data_dir / "config.json").read_text(encoding="utf-8") == '{"pet_name": "旧名"}'
    assert (data_dir / "heartbeat.db").read_bytes() == b"old-db"
    assert (data_dir / ".migrated").exists()


def test_migrate_legacy_data_keeps_existing(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.json").write_text("newer", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".migrated").write_text("done", encoding="utf-8")
    (data_dir / "config.json").write_text("keep", encoding="utf-8")
    core.migrate_legacy_data([str(legacy)], data_dir)
    assert (data_dir / "config.json").read_text(encoding="utf-8") == "keep"


# ---------- 流式工具调用 ----------

class _FakeSSEResponse:
    """模拟 urlopen 返回的 SSE 流（逐 chunk 迭代 bytes）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        yield from self._chunks


def _stream_chunks():
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"好的"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"，我来帮你查"}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"run_bash","arguments":"{\\"command\\": "}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls -la\\"}"}}]}}]}\n\n',
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":8}}\n\n',
        "data: [DONE]\n\n",
    ]
    return [line.encode("utf-8") for line in lines]


def test_complete_tools_stream_parses_sse():
    """流式工具响应：content 累积回调完整文本，tool_calls 增量按 index 拼接。"""
    patch = _Patch()
    try:
        patch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=60: _FakeSSEResponse(_stream_chunks()),
        )
        cfg = _cfg()
        cfg["api"]["api_key"] = "test-key"
        brain = core.Brain(cfg, {})
        deltas = []
        content, tool_calls = brain.complete_tools_stream(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "run_bash", "parameters": {"type": "object", "properties": {}}}}],
            deltas.append,
        )
        assert content == "好的，我来帮你查"
        assert deltas == ["好的", "好的，我来帮你查"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "call_1"
        assert tool_calls[0]["function"]["name"] == "run_bash"
        assert tool_calls[0]["function"]["arguments"] == '{"command": "ls -la"}'
    finally:
        patch.restore()


def test_complete_tools_stream_fallback_400():
    """接口不支持流式工具（400）时回退非流式 complete_tools。"""
    patch = _Patch()
    try:
        def boom(req, timeout=60):
            raise urllib.error.HTTPError("url", 400, "bad request", None, None)

        patch.setattr(urllib.request, "urlopen", boom)
        patch.setattr(
            core.Brain,
            "_post_chat",
            lambda self, payload: {
                "choices": [{"message": {"content": "回退结果", "tool_calls": []}}]
            },
        )
        cfg = _cfg()
        cfg["api"]["api_key"] = "test-key"
        brain = core.Brain(cfg, {})
        deltas = []
        content, tool_calls = brain.complete_tools_stream(
            [{"role": "user", "content": "hi"}], [], deltas.append
        )
        assert content == "回退结果"
        assert tool_calls == []
    finally:
        patch.restore()


def test_complete_tools_stream_reasoning_model_skips_stream():
    """推理模型（o1 等）走非流式 complete_tools，不回调 on_delta。"""
    patch = _Patch()
    try:
        patch.setattr(
            core.Brain,
            "_post_chat",
            lambda self, payload: {
                "choices": [{"message": {"content": "推理结果", "tool_calls": []}}]
            },
        )
        cfg = _cfg()
        cfg["api"]["api_key"] = "test-key"
        cfg["api"]["model"] = "o3-mini"
        brain = core.Brain(cfg, {})
        deltas = []
        content, _ = brain.complete_tools_stream(
            [{"role": "user", "content": "hi"}], [], deltas.append
        )
        assert content == "推理结果"
        assert deltas == []
    finally:
        patch.restore()


# ---------- 人设 prompt ----------

def test_build_persona_no_label_recital():
    """人设不再用“性格：标签”直陈，改为行为指令 + 示例。"""
    cfg = _cfg()
    persona = core.build_persona(cfg)
    assert "性格：" not in persona  # 不再出现标签式直陈
    assert "住在主人的电脑里" in persona
    assert "介绍一下你自己" in persona  # 内置两个不同风格的自我介绍示例
    assert "你是谁啊" in persona
    assert "我的性格是" in persona  # 禁止条目存在
    assert "不要复述你的设定" in persona


def test_build_persona_style_priority():
    """speaking_style 优先于 personality 推导。"""
    cfg = _cfg()
    cfg["speaking_style"] = "冷幽默，短句"
    persona = core.build_persona(cfg)
    assert "冷幽默，短句" in persona
    assert cfg["personality"] not in persona  # style 存在时不再注入 personality 原文


def test_build_persona_mood():
    """情绪状态按 mood 注入语气指令。"""
    cfg = _cfg()
    assert "当前情绪状态" not in core.build_persona(cfg)
    happy = core.build_persona(cfg, mood="开心")
    assert "语气轻快" in happy
    low = core.build_persona(cfg, mood="有点蔫")
    assert "句子更短" in low


def test_build_persona_custom_examples():
    """自定义示例对话覆盖内置默认。"""
    cfg = _cfg()
    cfg["example_lines"] = "主人：在吗\n你：在呀，怎么啦？"
    persona = core.build_persona(cfg)
    assert "在吗" in persona
    assert "介绍一下你自己" not in persona


def test_collect_all_cache_hit_flag(tmp_path):
    """collect_all 暴露 cache_hit：内容未变时为 True（触发门控的新闻 diff 来源）。"""

    class FakePlugin:
        META = {"label": "fake", "default_enabled": True}

        def collect(self, settings):
            return [{"text": "hello world"}]

    stats = dbmod.Stats(dbmod.Database(tmp_path / "t.db"))
    r1 = core.collect_all({"fake": FakePlugin()}, {}, stats)
    r2 = core.collect_all({"fake": FakePlugin()}, {}, stats)
    assert r1[0]["cache_hit"] is False
    assert r2[0]["cache_hit"] is True
    # 无 stats 时无法判断，置 None（不触发）
    r3 = core.collect_all({"fake": FakePlugin()}, {})
    assert r3[0]["cache_hit"] is None


def test_merge_entries_dedup_and_priority():
    """跨源汇聚：同标题 2h 去重、topic_watch 优先、top_k 截断。"""
    colls = [
        {"plugin": "hot_news", "entries": [{"text": "快讯：AI 新突破"}], "cache_hit": False},
        {"plugin": "topic_watch", "entries": [{"text": "AI 新突破"}], "cache_hit": False},
        {"plugin": "topic_watch", "entries": [{"text": "摄影展 8 月开幕"}], "cache_hit": False},
        {"plugin": "rss_news", "entries": [{"text": "旧闻"}], "cache_hit": True},  # 非新内容不参与
        {"plugin": "weather", "entries": [{"text": "晴天"}], "cache_hit": False},  # 天气走 T0
    ]
    titles, seen = core.merge_entries(colls, None, top_k=2)
    # 去重：同标题归一化后只保留先到的（hot_news 先收录“快讯：AI 新突破”）
    # 排序：topic_watch 优先级最高 → 摄影展在前
    assert titles == ["摄影展 8 月开幕", "快讯：AI 新突破"]
    assert "旧闻" not in titles
    assert "晴天" not in titles
    # 同一标题 2h 内不重复报
    titles2, seen2 = core.merge_entries(
        [{"plugin": "hot_news", "entries": [{"text": "AI 新突破"}], "cache_hit": False}],
        seen,
    )
    assert titles2 == []


def test_merge_entries_prefix_normalize():
    """标题归一化：去“快讯/独家”前缀后跨源去重。"""
    colls = [
        {"plugin": "hot_news", "entries": [{"text": "独家：苹果发布新机"}], "cache_hit": False},
        {"plugin": "rss_news", "entries": [{"text": "苹果发布新机！"}], "cache_hit": False},
    ]
    titles, _ = core.merge_entries(colls, None, top_k=2)
    assert len(titles) == 1


def test_request_with_retry_retries_transient(monkeypatch):
    """可重试错误（超时）自动重试直到成功。"""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("connection timeout")
        return "ok"

    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    assert core._request_with_retry(flaky, retries=2) == "ok"
    assert len(calls) == 3


def test_request_with_retry_no_retry_on_401(monkeypatch):
    """401 等鉴权错误不重试（重试也没用）。"""
    calls = []

    def bad_key():
        calls.append(1)
        raise urllib.error.HTTPError("u", 401, "Unauthorized", None, None)

    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    try:
        core._request_with_retry(bad_key, retries=2)
        assert False, "应抛出 HTTPError"
    except urllib.error.HTTPError:
        pass
    assert len(calls) == 1


def test_collect_all_context_passthrough(tmp_path):
    """context 透传给双参 collect 的插件。"""

    class TwoArgPlugin:
        META = {"label": "two", "default_enabled": True}

        def collect(self, settings, context=None):
            return [{"title": "t", "text": context.get("topics", ["x"])[0]}]

    results = core.collect_all({"two": TwoArgPlugin()}, {}, context={"topics": ["摄影"]})
    assert results[0]["entries"][0]["text"] == "摄影"


def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if params and params[0] == "tmp_path":
                    with TemporaryDirectory() as d:
                        fn(Path(d))
                elif params:
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
