"""统计模块测试。可直接运行，也可用 pytest。"""

import inspect
import gc
import json
import urllib.error
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import agent
import core
import plugins.rss_news as rss_news


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


def _stats(tmp_path):
    return core.Stats(tmp_path / "stats.json")


# ---------- 基础统计 ----------

def test_stats_record_and_persist(tmp_path):
    stats = _stats(tmp_path)
    stats.record_llm(prompt_tokens=100, completion_tokens=20, cached_tokens=30)
    stats.record_llm(ok=False)
    stats.record_chat(2)
    stats.record_proactive()
    stats.record_thought()
    stats.record_fact()
    stats.record_tick()
    stats2 = _stats(tmp_path)
    day = stats2.today()
    assert day["llm_calls"] == 2
    assert day["llm_errors"] == 1
    assert day["prompt_tokens"] == 100
    assert day["completion_tokens"] == 20
    assert day["cached_tokens"] == 30
    assert day["chat_messages"] == 2
    assert day["proactive_messages"] == 1
    assert day["thoughts"] == 1
    assert day["facts"] == 1
    assert day["ticks"] == 1


def test_stats_totals(tmp_path):
    stats = _stats(tmp_path)
    stats.record_llm(prompt_tokens=10)
    stats.record_llm(prompt_tokens=20)
    total = stats.totals()
    assert total["llm_calls"] == 2
    assert total["prompt_tokens"] == 30


def test_stats_record_tool(tmp_path):
    stats = _stats(tmp_path)
    stats.record_tool()
    stats.record_tool()
    assert stats.today()["tool_calls"] == 2


def test_stats_clear(tmp_path):
    stats = _stats(tmp_path)
    stats.record_llm(prompt_tokens=10)
    stats.clear()
    assert stats.today()["llm_calls"] == 0
    assert _stats(tmp_path).today()["llm_calls"] == 0


def test_content_hash_cache_hit(tmp_path):
    stats = _stats(tmp_path)
    digest = "abc"
    assert stats.check_content_hash("rss_news", digest) is False
    assert stats.check_content_hash("rss_news", digest) is True
    assert stats.check_content_hash("rss_news", "xyz") is False


# ---------- LLM 用量解析 ----------

def test_parse_usage_openai():
    data = {
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
    }
    assert core.parse_usage(data) == (120, 30, 80)


def test_parse_usage_anthropic():
    data = {
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "cache_read_input_tokens": 150,
        }
    }
    assert core.parse_usage(data) == (200, 50, 150)


def test_parse_usage_empty():
    assert core.parse_usage({}) == (0, 0, 0)


def test_brain_records_llm_usage(monkeypatch, tmp_path):
    class FakeResponse:
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "\u4f60\u597d"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            }).encode("utf-8")

    class FakeContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        core.urllib.request, "urlopen", lambda req, timeout=60: FakeContext()
    )
    stats = _stats(tmp_path)
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    brain = core.Brain(cfg, {}, stats)
    assert brain.complete([{"role": "user", "content": "hi"}]) == "\u4f60\u597d"
    day = stats.today()
    assert day["llm_calls"] == 1
    assert day["prompt_tokens"] == 10
    assert day["completion_tokens"] == 5
    assert day["cached_tokens"] == 4
    stats.close()


def test_complete_stream_deltas_and_usage(monkeypatch, tmp_path):
    chunks = [
        'data: {"choices":[{"delta":{"content":"\\u4f60"}}]}\n\n'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":"\\u597d"}}]}\n\n'.encode("utf-8"),
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,'
        b'"prompt_tokens_details":{"cached_tokens":4}}}\n\n',
        b"data: [DONE]\n\n",
    ]

    class FakeResponse:
        def __iter__(self):
            return iter(chunks)

    class FakeContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        core.urllib.request, "urlopen", lambda req, timeout=60: FakeContext()
    )
    stats = _stats(tmp_path)
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    brain = core.Brain(cfg, {}, stats)
    deltas = []
    brain.complete_stream([{"role": "user", "content": "hi"}], deltas.append)
    assert deltas == ["\u4f60", "\u597d"]
    day = stats.today()
    assert day["llm_calls"] == 1
    assert day["prompt_tokens"] == 10
    assert day["completion_tokens"] == 5
    assert day["cached_tokens"] == 4
    stats.close()


def test_complete_stream_retries_without_stream_options(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=60):
        calls["n"] += 1
        payload = json.loads(req.data.decode("utf-8"))
        if calls["n"] == 1:
            assert payload.get("stream_options")
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, None
            )
        assert "stream_options" not in payload

        class FakeResponse:
            def __iter__(self):
                return iter([
                    'data: {"choices":[{"delta":{"content":"\\u597d"}}]}\n\n'.encode("utf-8"),
                    b"data: [DONE]\n\n",
                ])

        class FakeContext:
            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        return FakeContext()

    monkeypatch.setattr(
        core.urllib.request, "urlopen", fake_urlopen
    )
    cfg = _cfg()
    cfg["api"]["api_key"] = "test-key"
    brain = core.Brain(cfg)
    deltas = []
    brain.complete_stream([{"role": "user", "content": "hi"}], deltas.append)
    assert deltas == ["好"]
    assert calls["n"] == 2


# ---------- 采集统计 ----------

def test_collect_all_records_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(
        core,
        "http_text",
        lambda url, timeout=10: (
            "<rss><channel><item><title>Same</title></item></channel></rss>"
        ),
    )
    stats = _stats(tmp_path)
    plugins = {"rss_news": rss_news}
    cfg = _cfg()
    core.collect_all(plugins, cfg, stats)
    core.collect_all(plugins, cfg, stats)
    coll = stats.today()["collectors"]["rss_news"]
    assert coll["fetches"] == 2
    assert coll["cache_hits"] == 1
    assert coll["entries"] == 2
    assert coll["chars"] > 0


def test_collect_all_records_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        rss_news,
        "collect",
        lambda settings: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    stats = _stats(tmp_path)
    cfg = _cfg()
    core.collect_all({"rss_news": rss_news}, cfg, stats)
    coll = stats.today()["collectors"]["rss_news"]
    assert coll["fails"] == 1
    assert coll["fetches"] == 0


# ---------- Agent 统计钩子 ----------

def test_agent_chat_records_stats(tmp_path):
    stats = _stats(tmp_path)
    a = agent.Agent(_cfg(), data_dir=tmp_path, stats=stats)
    a.chat("你好")
    assert stats.today()["chat_messages"] == 2


def test_agent_think_records_stats(tmp_path):
    stats = _stats(tmp_path)
    a = agent.Agent(
        _cfg(),
        data_dir=tmp_path,
        stats=stats,
        clock=lambda: datetime(2026, 8, 10, 10, 0),
    )
    a.think({"collections": []})
    assert stats.today()["ticks"] == 1


def test_agent_parse_records_fact_and_thought(monkeypatch, tmp_path):
    stats = _stats(tmp_path)
    a = agent.Agent(_cfg(), data_dir=tmp_path, stats=stats)
    a._parse_agent_reply("好的\n[FACT] 主人喜欢喝茶\n[THINK] 下次可以聊茶")
    day = stats.today()
    assert day["facts"] == 1
    assert day["thoughts"] == 1


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
                        gc.collect()
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
