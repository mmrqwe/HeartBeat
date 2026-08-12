"""上下文管理：token 估算、稳定前缀、动态尾部、超限压缩。

设计原则：
- 服务端 prompt cache 优先：稳定前缀尽量字节级不变，动态内容全部放尾部；
- 本地不缓存任何时间敏感数据（天气/行情/新闻/工具结果）；
- 超过 max_context_tokens 才压缩，当前用户消息永远不丢。
"""

import math
import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ROLE_OVERHEAD = 4  # 每条消息 role 等固定开销的 token 估算


def estimate_tokens(text):
    """保守 token 估算：中文按 1 字 1 token，其他按 4 字符 1 token。"""
    text = str(text or "")
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def estimate_messages_tokens(messages):
    total = 0
    for msg in messages or []:
        total += _ROLE_OVERHEAD
        content = msg.get("content")
        if content:
            total += estimate_tokens(content)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += estimate_tokens(fn.get("name", ""))
            total += estimate_tokens(fn.get("arguments", ""))
    return total


def truncate_messages(messages, max_tokens, keep_recent=20, max_content_chars=2000):
    """超限时先丢最旧的非 system 消息，再逐条截断；最后只保留最近 keep_recent 条。"""
    if estimate_messages_tokens(messages) <= max_tokens:
        return messages, 0
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    dropped = 0

    if len(rest) > keep_recent:
        dropped = len(rest) - keep_recent
        rest = rest[-keep_recent:]
    new_messages = system + rest
    if estimate_messages_tokens(new_messages) <= max_tokens:
        return new_messages, dropped

    for msg in new_messages:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > max_content_chars:
            msg["content"] = content[:max_content_chars] + "\n…（内容过长已截断）"
    if estimate_messages_tokens(new_messages) <= max_tokens:
        return new_messages, dropped

    rest = [m for m in new_messages if m.get("role") != "system"][-10:]
    dropped += len([m for m in new_messages if m.get("role") != "system"]) - len(rest)
    return system + rest, dropped
