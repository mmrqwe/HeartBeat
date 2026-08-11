"""HeartBeat 核心（brain 层）：信息采集、人设构建、LLM 封装（Brain）。

配置 / 数据目录 / 插件发现已迁入 kernel 包（kernel.boot / kernel.module），
本文件 re-export 保持旧引用兼容（agent / plugins / search / cli / 测试）。
不依赖 GUI，可单独测试。
"""

import hashlib
import http.client
import inspect
import json
import logging
import random
import re
import ssl
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from db import Stats  # noqa: F401  统计层（SQLite）

# 内核层 re-export（实现见 kernel/boot.py、kernel/module.py）
from kernel.boot import (  # noqa: F401
    DEFAULT_CONFIG,
    user_data_dir,
    migrate_legacy_data,
    load_config,
    save_config,
)
from kernel.module import (  # noqa: F401
    default_plugin_dirs,
    discover_plugins,
)

USER_AGENT = "HeartBeat/0.1 (desktop pet)"
# ---------- HTTP 工具（插件通用） ----------

def http_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_json(url, timeout=10):
    return json.loads(http_text(url, timeout))


# ---------- 技能包元数据（SKILL.md frontmatter） ----------

SKILL_NAME_MAX = 40    # 技能名截断
SKILL_DESC_MAX = 200   # 技能描述截断（注入 system prompt 的最小元数据面）


def parse_skill_frontmatter(text):
    """从 SKILL.md 提取 frontmatter 的 name/description（仅元数据）。

    安全设计（架构评审 2026-08-12）：技能包是外部不可信内容，注入宠物
    system prompt 前只保留这两个字段，丢弃其余全部内容；去控制字符、
    按长度截断，防止 description 内嵌指令文本进入上下文。
    支持 YAML 折叠多行（description: >- 后跟缩进行）。解析失败返回 {}。
    """
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    data = {}
    desc_parts = []
    in_desc = False
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t")):
            if in_desc:
                desc_parts.append(line.strip())
            continue
        in_desc = False
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "name":
            data["name"] = re.sub(r"[\x00-\x1f\x7f]", "", value.strip())[:SKILL_NAME_MAX]
        elif key == "description":
            in_desc = True
            if value.strip():
                desc_parts.append(value.strip())
    if desc_parts:
        desc = re.sub(r"[\x00-\x1f\x7f]", "", " ".join(desc_parts)).strip()
        if desc:
            data["description"] = desc[:SKILL_DESC_MAX]
    return data


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


def parse_usage(data):
    """从 LLM 响应里解析 token 用量，兼容 OpenAI / Anthropic 缓存字段。"""
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    if not cached:
        cached = int(usage.get("cache_read_input_tokens") or 0)
    if not cached:
        cached = int(usage.get("cache_creation_input_tokens") or 0)
    return prompt, completion, cached

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


logger = logging.getLogger("heartbeat.core")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class StreamInterrupted(Exception):
    """流式传输中途断连且已向 UI 推送过内容。

    此时整体重发会重复计费/重复显示（LLM 生成非确定，重发内容不同），
    由上层（agent._chat_llm_stream）接受已输出的部分内容直接收尾。
    """

    def __init__(self, partial):
        super().__init__(f"流式中断，已接收部分输出（{len(partial)} 字符）")
        self.partial = partial


def _is_retryable_error(exc):
    """判断异常是否值得自动重试。

    可重试：SSL 握手/传输被掐断（UNEXPECTED_EOF 等）、连接超时/重置、
    响应不完整（IncompleteRead/RemoteDisconnected）、5xx/429。
    不可重试：401/403/400 等参数鉴权错误、证书校验失败（重试无用）、
    以及非网络类错误。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False  # 证书问题重试无意义
    if isinstance(exc, ssl.SSLError):
        return True  # SSL EOF / 握手失败等（证书错误已在上方排除）
    if isinstance(exc, urllib.error.URLError):
        # reason 可能是嵌套异常（SSLError/TimeoutError/ConnectionError…）
        reason = exc.reason
        return _is_retryable_error(reason) if isinstance(reason, Exception) else True
    if isinstance(exc, (TimeoutError, http.client.HTTPException, OSError)):
        return True  # 超时 / IncompleteRead / RemoteDisconnected / ConnectionReset 等
    return False


def _retry_backoff(attempt, base=0.5, cap=8.0):
    """指数退避 + full jitter：第 attempt 次重试前等待 0~min(cap, base*2^attempt) 秒。"""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def _request_with_retry(fn, retries=2, base_delay=0.5, max_delay=8.0, on_retry=None):
    """指数退避重试：SSL 断连/超时/连接失败/5xx/429 自动重试，其余错误直接抛。

    retries=重试次数（总尝试 = retries+1）；每次重试前等待 full jitter 退避；
    on_retry(attempt, exc) 回调用于可观测（日志/UI 提示）。
    401/403/400 等参数/鉴权错误、证书校验失败不重试（重试也没用）。
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 统一走分类判断
            if not _is_retryable_error(exc) or attempt >= retries:
                raise
            delay = _retry_backoff(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(attempt, exc)
            logger.warning(
                "LLM 请求失败（第 %d/%d 次重试，%.1fs 后）: %s",
                attempt + 1, retries, delay, exc,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")


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
MOOD_STYLE = {
    "平静": "语气平和，回复节奏正常。",
    "开心": "语气轻快、有活力，可以更热情，偶尔卖个萌。",
    "有点蔫": "话少一点，句子更短，语气软软的，但不要消极抱怨。",
    "困了": "迷迷糊糊的，句子短，带着困意，但依然愿意陪主人。",
}

DEFAULT_EXAMPLE_LINES = (
    "主人：你是谁啊？\n"
    "你：我？住在你电脑里的小家伙，天天看你敲键盘也不理我，哼。\n"
    "主人：介绍一下你自己\n"
    "你：我是{pet_name}呀，{role}一只，饿了会自己充电的那种。\n"
    "主人：今天好累\n"
    "你：辛苦啦……要不要我陪你待会儿？我不吵你的。\n"
)


def build_persona(cfg, mood=None):
    """统一的角色人设 prompt：身份小传 + 说话方式 + 示例对话 + 情绪状态。

    关键设计：性格用“行为指令 + few-shot 示例”体现，而不是“性格：标签”陈述，
    否则 LLM 自我介绍时会逐条复述设定（“我的性格是……”）。

    speaking_style 留空时从 personality 推导；示例对话内置两个不同风格的
    自我介绍，避免自我介绍模板化。
    """
    name = str(cfg.get("pet_name") or "小跳")
    role = str(cfg.get("role") or "小宠物")
    # 人物类角色用"一个"，动物/精灵等用"一只"（如"男生/女生/小幽灵"→一个；"小橘猫/小兔"→一只）
    unit = "一个" if any(k in role for k in ("男生", "女生", "人", "幽灵")) else "一只"
    personality = str(cfg.get("personality") or "").strip()
    style = str(cfg.get("speaking_style") or "").strip()
    examples = str(cfg.get("example_lines") or "").strip()

    lines = [
        "你在扮演一个真实存在的角色。以下设定是你的内在，用来指导言行，不是你会对人说的话。",
        "",
        "# 身份",
        f"你是{name}，{unit}{role}，住在主人的电脑里。",
        "",
        "# 说话方式",
    ]
    if style:
        lines.append(style)
    elif personality:
        lines.append(
            personality
            + "。用自然口语体现，不要说“我性格……”这类话。"
        )
    else:
        lines.append("说话简短自然，像朋友一样，不超过几句话。")

    if examples:
        lines += ["", "# 示例对话（参考语气和句式，不要照抄内容）", examples]
    else:
        lines += [
            "",
            "# 示例对话（参考语气和句式，不要照抄内容）",
            DEFAULT_EXAMPLE_LINES.format(pet_name=name, role=role),
        ]

    if mood and mood in MOOD_STYLE:
        lines += ["", "# 当前情绪状态", MOOD_STYLE[mood]]

    lines += [
        "",
        "# 成长",
        "- 你会慢慢长大：主人说过的偏好和重要的事，你会记在心里，下次聊天自然地用上（不要特意说“我记得你上次说”）",
        "- 发现主人新的喜好或变化时，悄悄记住，让自己越来越懂主人",
    ]

    lines += [
        "",
        "# 禁止",
        "- 不要复述你的设定、性格标签或角色配置",
        "- 不要说“我的性格是……”“我是由……设定的”这类话",
        "- 自我介绍用第一人称、口语化，每次说法可以不一样，像聊天不像简历",
    ]
    return "\n".join(lines)


class Brain:
    """桌宠的大脑：有 API Key 走 LLM，否则走各插件的规则建议。"""

    def __init__(self, cfg, plugins=None, stats=None):
        self.cfg = cfg
        self.plugins = plugins or {}
        self.stats = stats
        self.history = []
        self.state = {}

    def _retry_cfg(self):
        """LLM 重连配置：config.json 的 retry 块（带默认值兜底）。"""
        cfg = self.cfg.get("retry") or {}
        return {
            "max_attempts": max(1, int(cfg.get("max_attempts", 3))),
            "backoff_base": max(0.1, float(cfg.get("backoff_base", 0.5))),
            "backoff_max": max(0.2, float(cfg.get("backoff_max", 8.0))),
        }

    # ---------- 自主发言 ----------

    def think(self, ctx):
        if self.cfg["api"]["api_key"]:
            return self._think_llm(ctx)
        return self._think_rules(ctx)

    def _think_llm(self, ctx):
        system = (
            build_persona(self.cfg)
            + "\n\n"
            "你会定期查看周围信息，决定要不要主动跟主人说话。"
            "只有当你有真正值得说的事情时才说话，否则只回复 SILENT。"
            "说话要自然、简短（不超过50字），像朋友一样，不要用列表、标题或客套话。"
        )
        reply = self._chat_completion([
            {"role": "system", "content": system},
            {"role": "user", "content": self._context_text(ctx)},
        ])
        if not reply:
            return None
        text = reply.strip().strip('"“”')
        if re.fullmatch(r"SILENT[.。!！]?", text, re.IGNORECASE):
            return None
        if "SILENT" in text.upper():
            return None  # 夹带说明时保守处理，不主动打扰
        return text

    def _think_rules(self, ctx):
        for coll in ctx.get("collections", []):
            module = self.plugins.get(coll["plugin"])
            if not module or not callable(getattr(module, "suggest", None)):
                continue
            if not coll["entries"]:
                continue
            settings = self.cfg.get("collectors", {}).get(coll["plugin"], {})
            state = self.state.setdefault(coll["plugin"], {})
            message = module.suggest(settings, coll["entries"], state)
            if message:
                return message
        return None

    # ---------- 聊天 ----------

    def chat(self, user_text):
        user_text = user_text.strip()
        if self.cfg["api"]["api_key"]:
            return self._chat_llm(user_text)
        return self._chat_rules(user_text)

    def complete(self, messages, max_tokens=None, timeout=60):
        """公开的模型调用入口，供 Agent 使用。timeout 秒（默认 60）。"""
        return self._chat_completion(messages, max_tokens, timeout=timeout)

    def complete_stream(self, messages, on_delta, max_tokens=None):
        """流式模型调用：SSE 逐块解析，每个增量文本回调 on_delta。"""
        api = self.cfg["api"]
        url = api["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        base_payload = {
            "model": api["model"],
            "messages": messages,
            "stream": True,
        }
        reasoning = self._reasoning_params(max_tokens or 300)
        if reasoning:
            base_payload.update(reasoning)
        else:
            base_payload.update({"temperature": 0.9, "max_tokens": max_tokens or 300})
        started = time.time()
        try:
            usage = self._stream_read(
                url,
                headers,
                {**base_payload, "stream_options": {"include_usage": True}},
                on_delta,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                if self.stats:
                    self.stats.record_llm(ok=False)
                raise
            # 部分兼容接口不认识 stream_options，去掉重试
            try:
                usage = self._stream_read(url, headers, base_payload, on_delta)
            except Exception:
                if self.stats:
                    self.stats.record_llm(ok=False)
                raise
        except Exception:
            if self.stats:
                self.stats.record_llm(ok=False)
            raise
        if self.stats:
            prompt, completion, cached = parse_usage({"usage": usage} if usage else {})
            latency_ms = int((time.time() - started) * 1000)
            self.stats.record_llm(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                latency_ms=latency_ms,
            )

    def _stream_read(self, url, headers, payload, on_delta):
        """SSE 流式读取，带自动重连。

        连接建立失败（SSL 被掐断/超时等）时整体重发请求（此时 UI 尚未收到
        任何内容，重发无副作用）；流传输中途断连且已推送过内容时抛
        StreamInterrupted 接受部分输出——整体重发会重复计费且 UI 无法撤回
        已显示内容（LLM 生成非确定，重发结果不同）。
        """
        attempts = self._retry_cfg()["max_attempts"]
        for attempt in range(attempts):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
            chunks = 0
            texts = []
            usage = None
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    buffer = b""
                    for chunk in resp:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.strip()
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                return usage
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            choices = obj.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    chunks += 1
                                    texts.append(content)
                                    on_delta(content)
                return usage
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                if chunks > 0:
                    # 已推送过内容：接受部分输出，由上层收尾
                    raise StreamInterrupted("".join(texts)) from exc
                if attempt >= attempts - 1:
                    raise
                rc = self._retry_cfg()
                logger.warning(
                    "LLM 流连接失败（第 %d/%d 次重连）: %s",
                    attempt + 1, attempts - 1, exc,
                )
                time.sleep(_retry_backoff(attempt, rc["backoff_base"], rc["backoff_max"]))
        raise RuntimeError("unreachable")

    def _chat_llm(self, user_text):
        system = (
            build_persona(self.cfg)
            + "\n\n"
            "回答简短自然，像朋友聊天，一般不超过80字，不要用列表和标题。"
        )
        self.history.append({"role": "user", "content": user_text})
        messages = [{"role": "system", "content": system}] + self.history[-8:]
        reply = self._chat_completion(messages)
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _chat_rules(self, user_text):
        text = user_text.lower()
        name = self.cfg["pet_name"]
        if any(k in text for k in ("天气", "温度", "冷不冷", "热不热")):
            module = self.plugins.get("weather")
            if module:
                try:
                    settings = self.cfg.get("collectors", {}).get("weather", {})
                    entries = module.collect(settings) or []
                    if entries:
                        return "刚查了下：" + entries[0]["text"]
                except Exception:
                    pass
            return "天气暂时没查到，网络好像不太给力。"
        if "新闻" in text:
            module = self.plugins.get("rss_news")
            if module:
                try:
                    settings = self.cfg.get("collectors", {}).get("rss_news", {})
                    entries = module.collect(settings) or []
                    if entries:
                        return f"刚看到一条：{entries[0]['text']}"
                except Exception:
                    pass
            return "新闻暂时没抓到，网络好像不太给力。"
        if any(k in text for k in ("你好", "hi", "hello", "在吗", "在不在")):
            return "在呀在呀，我一直都在～"
        if "名字" in text or "叫什么" in text:
            role = self.cfg.get("role") or "小宠物"
            return f"我叫{name}呀，你电脑里的{role}一只。"
        if any(k in text for k in ("可爱", "好看", "萌", "漂亮")):
            return "嘿嘿，被你夸得像素都亮了。"
        if any(k in text for k in ("干嘛", "做什么", "忙什么", "干什么")):
            return "刚才在巡视各个内容源，想找点新鲜事跟你说。"
        if any(k in text for k in ("谢谢", "感谢")):
            return "不客气～陪你我很开心。"
        if "无聊" in text:
            return "那我帮你念条新闻？或者你跟我说说今天发生了什么。"
        return random.choice([
            "嗯嗯，我在听。",
            "有意思，继续说！",
            "这个嘛……让我想想。",
            "哈哈，你说了算。",
        ])

    # ---------- 工具 ----------

    def _context_text(self, ctx):
        lines = [f"现在时间：{time.strftime('%Y-%m-%d %H:%M')}"]
        for coll in ctx.get("collections", []):
            if not coll["entries"]:
                continue
            for entry in coll["entries"]:
                lines.append(f"[{coll['label']}] {entry['text']}")
        if ctx.get("errors"):
            lines.append("采集异常：" + "；".join(ctx["errors"]))
        return "\n".join(lines)

    @staticmethod
    def _read_json(req, timeout=60):
        """一次请求并解析 JSON（供重试层包裹）。"""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_chat(self, payload, timeout=60):
        api = self.cfg["api"]
        url = api["base_url"].rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api['api_key']}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        started = time.time()
        try:
            rc = self._retry_cfg()
            data = _request_with_retry(
                lambda: self._read_json(req, timeout=timeout),
                retries=rc["max_attempts"] - 1,
                base_delay=rc["backoff_base"],
                max_delay=rc["backoff_max"],
            )
        except Exception:
            if self.stats:
                self.stats.record_llm(ok=False)
            raise
        if self.stats:
            prompt, completion, cached = parse_usage(data)
            latency_ms = int((time.time() - started) * 1000)
            self.stats.record_llm(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                latency_ms=latency_ms,
            )
        return data

    def _reasoning_params(self, max_tokens):
        """LLM 思考/推理模式：模型支持时附带 reasoning_effort，
        并适配推理模型参数（不支持 temperature，需用 max_completion_tokens）。
        非推理模型或开关关闭时返回 None，走原参数。"""
        if not self.cfg.get("thinking_enabled", True):
            return None
        model = str(self.cfg["api"].get("model", "")).lower()
        is_reasoning = model.startswith(("o1", "o3", "o4")) or any(
            k in model for k in ("reasoner", "reasoning", "thinking")
        )
        if not is_reasoning:
            return None
        effort = self.cfg.get("thinking_effort", "medium")
        if effort not in ("low", "medium", "high"):
            effort = "medium"
        return {
            "reasoning_effort": effort,
            "max_completion_tokens": max(max_tokens, 1000),
        }

    def _chat_completion(self, messages, max_tokens=None, timeout=60):
        reasoning = self._reasoning_params(max_tokens or 300)
        payload = {
            "model": self.cfg["api"]["model"],
            "messages": messages,
        }
        if reasoning:
            payload.update(reasoning)
        else:
            payload.update({"temperature": 0.9, "max_tokens": max_tokens or 300})
        data = self._post_chat(payload, timeout=timeout)
        return data["choices"][0]["message"]["content"].strip()

    def complete_tools(self, messages, tools):
        """带工具声明的一次模型调用，返回 (content, tool_calls)。"""
        reasoning = self._reasoning_params(500)
        payload = {
            "model": self.cfg["api"]["model"],
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if reasoning:
            payload.update(reasoning)
        else:
            payload.update({"temperature": 0.7, "max_tokens": 500})
        data = self._post_chat(payload)
        message = data["choices"][0]["message"]
        return message.get("content"), message.get("tool_calls") or []

    def complete_tools_stream(self, messages, tools, on_delta):
        """带工具声明的流式模型调用：content 逐块累积回调 on_delta（完整文本），
        tool_calls 增量按 index 拼接。返回 (content, tool_calls)。

        推理模型（o1 等）对流式工具兼容性差、以及接口不支持流式工具（400）时，
        回退非流式 complete_tools。
        """
        if self._reasoning_params(500) is not None:
            return self.complete_tools(messages, tools)
        api = self.cfg["api"]
        url = api["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = {
            "model": api["model"],
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 500,
        }
        started = time.time()
        try:
            content, tool_calls, usage = self._stream_read_tools(url, headers, payload, on_delta)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                if self.stats:
                    self.stats.record_llm(ok=False)
                raise
            # 部分兼容接口不支持流式工具：回退非流式（on_delta 已推的部分保留）
            return self.complete_tools(messages, tools)
        except Exception:
            if self.stats:
                self.stats.record_llm(ok=False)
            raise
        if self.stats:
            prompt, completion, cached = parse_usage({"usage": usage} if usage else {})
            latency_ms = int((time.time() - started) * 1000)
            self.stats.record_llm(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                latency_ms=latency_ms,
            )
        return content, tool_calls

    def _stream_read_tools(self, url, headers, payload, on_delta):
        """解析流式工具响应，带自动重连。

        连接失败重发；传输中断且已推送过内容时保留已输出的 content、
        丢弃半截 tool_calls（避免执行不完整参数）。
        """
        attempts = self._retry_cfg()["max_attempts"]
        for attempt in range(attempts):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
            parts = []
            tool_calls = []
            usage = None
            chunks = 0
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    buffer = b""
                    for chunk in resp:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.strip()
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                return "".join(parts), tool_calls, usage
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            choices = obj.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            content = delta.get("content")
                            if content:
                                chunks += 1
                                parts.append(content)
                                on_delta("".join(parts))
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                while len(tool_calls) <= idx:
                                    tool_calls.append({
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    })
                                slot = tool_calls[idx]
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += fn["arguments"]
                return "".join(parts), tool_calls, usage
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                if chunks > 0:
                    # 已推送过内容：保留部分内容、丢弃半截工具调用
                    logger.warning("工具流中断，保留已输出内容: %s", exc)
                    return "".join(parts), [], usage
                if attempt >= attempts - 1:
                    raise
                rc = self._retry_cfg()
                logger.warning(
                    "工具流连接失败（第 %d/%d 次重连）: %s",
                    attempt + 1, attempts - 1, exc,
                )
                time.sleep(_retry_backoff(attempt, rc["backoff_base"], rc["backoff_max"]))
        raise RuntimeError("unreachable")
