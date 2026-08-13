"""LLM 重连/重试机制测试：SSL 断连自动重发、错误分类、流式中断接受部分。

覆盖：
- _is_retryable_error 错误分类矩阵（SSL EOF/超时/5xx 可重试，401/400/证书不可）
- 非流式请求重试成功 / 耗尽抛原异常 / 401 不重试
- 流式连接阶段失败 → 整体重发（UI 无重复）
- 流式传输中断且已推送内容 → StreamInterrupted(partial) 不重发
- 工具流中断 → 保留 content、丢弃半截 tool_calls
- max_attempts 配置生效
"""

import inspect
import json
import ssl
import urllib.error
import urllib.request
from tempfile import TemporaryDirectory
from pathlib import Path

import core


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
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["api"]["api_key"] = "test-key"
    return cfg


def _brain():
    return core.Brain(_cfg(), {})


class _FakeSSEResponse:
    """可配置的 SSE 响应：按顺序 yield chunks，yield 指定数量后抛异常。"""

    def __init__(self, chunks, raise_after=None, exc=None):
        self._chunks = chunks
        self._raise_after = raise_after  # 已 yield 的 chunk 数达到该值后抛
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            yield chunk
            if self._raise_after is not None and i + 1 >= self._raise_after:
                raise self._exc()


def _sse_line(text):
    return f"data: {text}\n\n".encode("utf-8")


# ---------- 错误分类 ----------

def test_is_retryable_error_matrix():
    # 可重试：SSL EOF（直接抛 / URLError 包裹）、超时、连接重置、IncompleteRead、5xx、429
    assert core._is_retryable_error(
        ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    )
    assert core._is_retryable_error(urllib.error.URLError(ssl.SSLError("EOF")))
    assert core._is_retryable_error(urllib.error.URLError(TimeoutError("timed out")))
    assert core._is_retryable_error(
        urllib.error.URLError(ConnectionResetError("connection reset by peer"))
    )
    assert core._is_retryable_error(
        __import__("http.client").client.RemoteDisconnected(
            "Remote end closed connection without response"
        )
    )
    assert core._is_retryable_error(__import__("http.client").client.IncompleteRead(b"x"))
    assert core._is_retryable_error(urllib.error.HTTPError("u", 503, "unavailable", None, None))
    assert core._is_retryable_error(urllib.error.HTTPError("u", 429, "rate limited", None, None))
    # 不可重试：鉴权/参数/资源不存在、证书错误、非网络错误
    assert not core._is_retryable_error(urllib.error.HTTPError("u", 401, "unauthorized", None, None))
    assert not core._is_retryable_error(urllib.error.HTTPError("u", 400, "bad request", None, None))
    assert not core._is_retryable_error(urllib.error.HTTPError("u", 404, "not found", None, None))
    assert not core._is_retryable_error(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    assert not core._is_retryable_error(ValueError("bad payload"))


# ---------- 非流式请求重试 ----------

def test_request_retry_ssl_then_success():
    """SSL EOF 连续失败 2 次后成功：重试层透明恢复，结果正确。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")
            return "ok"

        assert core._request_with_retry(fn, retries=2, base_delay=0.01) == "ok"
        assert len(calls) == 3
    finally:
        patch.restore()


def test_request_retry_exhausted_raises():
    """持续失败：重试耗尽后抛原异常，不吞错。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        calls = []

        def fn():
            calls.append(1)
            raise ssl.SSLError("EOF")

        try:
            core._request_with_retry(fn, retries=2, base_delay=0.01)
            raised = False
        except ssl.SSLError:
            raised = True
        assert raised
        assert len(calls) == 3
    finally:
        patch.restore()


def test_request_retry_401_no_retry():
    """401 鉴权错误：不重试（重试也没用）。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        calls = []

        def fn():
            calls.append(1)
            raise urllib.error.HTTPError("u", 401, "unauthorized", None, None)

        try:
            core._request_with_retry(fn, retries=2, base_delay=0.01)
            raised = False
        except urllib.error.HTTPError:
            raised = True
        assert raised
        assert len(calls) == 1
    finally:
        patch.restore()


# ---------- 流式：连接阶段失败 → 整体重发 ----------

def test_stream_read_reconnects_on_connect_failure():
    """连接阶段 SSL EOF：整体重发请求，UI 收到完整内容且无重复。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        ok = _FakeSSEResponse(
            [
                _sse_line('{"choices":[{"delta":{"content":"你好"}}]}'),
                _sse_line('{"choices":[{"delta":{"content":"，我"}}]}'),
                _sse_line('{"choices":[{"delta":{"content":"在"}}]}'),
                _sse_line("[DONE]"),
            ]
        )
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            if len(calls) == 1:
                raise ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")
            return ok

        patch.setattr(urllib.request, "urlopen", urlopen)
        brain = _brain()
        deltas = []
        usage, finish, text = brain._stream_read("http://x/v1/chat/completions", {"A": "B"}, {"model": "m"}, deltas.append)
        assert len(calls) == 2
        # 文本流 on_delta 是增量语义（agent 层累积成全量后推 UI）
        assert deltas == ["你好", "，我", "在"]
        assert usage is None
        assert finish is None
        assert text == "你好，我在"
    finally:
        patch.restore()


def test_stream_read_connect_failure_exhausted_raises():
    """连接阶段持续失败：重试耗尽后抛原异常（默认 11 次 → 尝试 11 次）。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            raise ssl.SSLError("EOF")

        patch.setattr(urllib.request, "urlopen", urlopen)
        brain = _brain()
        try:
            brain._stream_read("http://x", {}, {"model": "m"}, lambda t: None)
            raised = False
        except ssl.SSLError:
            raised = True
        assert raised
        assert len(calls) == 11
    finally:
        patch.restore()


def test_retry_backoff_has_minimum_delay():
    """每次重拨至少等 backoff_base 秒，避免近乎立即重连。"""
    patch = _Patch()
    patch.setattr(core.random, "uniform", lambda a, b: 0.0)
    try:
        assert core._retry_backoff(0, base=3.0, cap=10.0) == 3.0
        patch.setattr(core.random, "uniform", lambda a, b: 7.0)
        assert core._retry_backoff(9, base=3.0, cap=10.0) == 10.0
    finally:
        patch.restore()


def test_retry_cfg_defaults_allow_ten_redials():
    """retry 配置缺失时默认至少 10 次重拨，间隔下限为 3 秒。"""
    cfg = _cfg()
    cfg.pop("retry", None)
    rc = core.Brain(cfg, {})._retry_cfg()
    assert rc["max_attempts"] == 11
    assert rc["backoff_base"] == 3.0
    assert rc["backoff_max"] == 10.0


def test_stream_read_retry_config_honored():
    """max_attempts 配置生效：设为 2 → 只尝试 2 次。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        cfg = _cfg()
        cfg["retry"] = {"max_attempts": 2, "backoff_base": 0.1, "backoff_max": 1.0}
        brain = core.Brain(cfg, {})
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            raise ssl.SSLError("EOF")

        patch.setattr(urllib.request, "urlopen", urlopen)
        try:
            brain._stream_read("http://x", {}, {"model": "m"}, lambda t: None)
        except ssl.SSLError:
            pass
        assert len(calls) == 2
    finally:
        patch.restore()


# ---------- 流式：传输中途断连 → 接受部分 ----------

def test_stream_read_interrupted_keeps_partial():
    """流中途断连且已推送内容：抛 StreamInterrupted(partial)，不再重发。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        resp = _FakeSSEResponse(
            [
                _sse_line('{"choices":[{"delta":{"content":"这是一段"}}]}'),
                _sse_line('{"choices":[{"delta":{"content":"很长的话"}}]}'),
            ],
            raise_after=2,
            exc=lambda: __import__("http.client").client.IncompleteRead(b"xx"),
        )
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            return resp

        patch.setattr(urllib.request, "urlopen", urlopen)
        brain = _brain()
        deltas = []
        try:
            brain._stream_read("http://x", {}, {"model": "m"}, deltas.append)
            raised = False
        except core.StreamInterrupted as exc:
            raised = True
            assert exc.partial == "这是一段很长的话"
        assert raised
        assert len(calls) == 1  # 已输出内容 → 不重发
    finally:
        patch.restore()


def test_stream_read_tools_interrupted_keeps_content():
    """工具流中断且已推送内容：保留 content、丢弃半截 tool_calls。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        resp = _FakeSSEResponse(
            [
                _sse_line('{"choices":[{"delta":{"content":"好的"}}]}'),
                _sse_line('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":\\"ls"}}]}}]}'),
            ],
            raise_after=2,
            exc=lambda: __import__("http.client").client.RemoteDisconnected("closed"),
        )
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            return resp

        patch.setattr(urllib.request, "urlopen", urlopen)
        brain = _brain()
        deltas = []
        content, tool_calls, usage, finish = brain._stream_read_tools(
            "http://x", {}, {"model": "m"}, deltas.append
        )
        assert content == "好的"
        assert tool_calls == []  # 半截工具调用被丢弃，避免执行不完整参数
        assert len(calls) == 1
    finally:
        patch.restore()


def test_stream_read_tools_reconnects_on_connect_failure():
    """工具流连接阶段失败：整体重发后正常解析（含 tool_calls）。"""
    patch = _Patch()
    patch.setattr(core.time, "sleep", lambda s: None)
    try:
        ok = _FakeSSEResponse(
            [
                _sse_line('{"choices":[{"delta":{"content":"查到了"}}]}'),
                _sse_line('{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"run_bash","arguments":"{\\"cmd\\":\\"ls\\"}"}}]}}]}'),
                _sse_line("[DONE]"),
            ]
        )
        calls = []

        def urlopen(req, timeout=60):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return ok

        patch.setattr(urllib.request, "urlopen", urlopen)
        brain = _brain()
        deltas = []
        content, tool_calls, usage, finish = brain._stream_read_tools(
            "http://x", {}, {"model": "m"}, deltas.append
        )
        assert len(calls) == 2
        assert content == "查到了"
        assert tool_calls[0]["function"]["name"] == "run_bash"
    finally:
        patch.restore()


# ---------- runner ----------

def _run_plain():
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if params and params[0] == "tmp_path":
                    with TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


# ---------- 空内容（finish_reason=length）→ 提高预算重试 ----------


class _FakePostChat:
    """脚本化 _post_chat：依次返回响应，并记录每次 payload。"""

    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    def __call__(self, payload, timeout=60):
        self.payloads.append(dict(payload))
        return self.script.pop(0)


def _resp(content, finish):
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


def test_complete_retries_on_empty_length():
    """隐藏推理耗尽预算 → 空内容 + finish=length：提高预算重试一次。"""
    fake = _FakePostChat([_resp("", "length"), _resp("计划：1 2 3", "stop")])
    patch = _Patch()
    patch.setattr(core.Brain, "_post_chat", fake)
    try:
        brain = _brain()
        reply = brain.complete([{"role": "user", "content": "hi"}], max_tokens=400)
        assert reply == "计划：1 2 3"
        assert len(fake.payloads) == 2
        assert fake.payloads[0]["max_tokens"] == 400
        assert fake.payloads[1]["max_tokens"] == 4000
    finally:
        patch.restore()


def test_complete_no_retry_on_stop_empty():
    """finish=stop 的空内容视为模型主动返回，不重试。"""
    fake = _FakePostChat([_resp("", "stop")])
    patch = _Patch()
    patch.setattr(core.Brain, "_post_chat", fake)
    try:
        brain = _brain()
        reply = brain.complete([{"role": "user", "content": "hi"}])
        assert reply == ""
        assert len(fake.payloads) == 1
    finally:
        patch.restore()


def test_complete_tools_retries_on_empty_length():
    """工具调用空内容无 tool_calls + finish=length：提高预算重试。"""
    fake = _FakePostChat([
        {"choices": [{"finish_reason": "length", "message": {}}]},
        {"choices": [{"finish_reason": "stop",
                      "message": {"content": "好的", "tool_calls": []}}]},
    ])
    patch = _Patch()
    patch.setattr(core.Brain, "_post_chat", fake)
    try:
        brain = _brain()
        content, calls = brain.complete_tools(
            [{"role": "user", "content": "hi"}], [{}]
        )
        assert content == "好的"
        assert calls == []
        assert len(fake.payloads) == 2
        # 未显式传 max_tokens → 用户输出上限（默认 100k）；重试受 65536 封顶
        assert fake.payloads[0]["max_tokens"] == 100000
        assert fake.payloads[1]["max_tokens"] == 65536
    finally:
        patch.restore()


def test_complete_stream_falls_back_on_empty_length():
    """流式空内容 + finish=length：降级非流式 complete 一次性重试。"""
    calls = {"n": 0}

    def fake_stream_read(self, url, headers, payload, on_delta):
        calls["n"] += 1
        return None, "length", ""

    patch = _Patch()
    patch.setattr(core.Brain, "_stream_read", fake_stream_read)
    patch.setattr(
        core.Brain,
        "complete",
        lambda self, messages, max_tokens=None, timeout=60: "非流式重试成功",
    )
    try:
        brain = _brain()
        deltas = []
        brain.complete_stream([{"role": "user", "content": "hi"}], deltas.append)
        assert calls["n"] == 1
        assert deltas == ["非流式重试成功"]
    finally:
        patch.restore()


def test_complete_tools_stream_falls_back_on_empty_length():
    """工具流空内容无工具调用 + finish=length：降级非流式重试。"""
    patch = _Patch()
    patch.setattr(
        core.Brain,
        "_stream_read_tools",
        lambda self, url, headers, payload, on_delta: ("", [], None, "length"),
    )
    patch.setattr(
        core.Brain,
        "complete_tools",
        lambda self, messages, tools, max_tokens=None: ("非流式", []),
    )
    try:
        brain = _brain()
        deltas = []
        content, calls = brain.complete_tools_stream(
            [{"role": "user", "content": "hi"}], [{}], deltas.append
        )
        assert content == "非流式"
        assert calls == []
    finally:
        patch.restore()


if __name__ == "__main__":
    _run_plain()
