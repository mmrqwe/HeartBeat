"""brain.llm：LLM 客户端（可进化域核心）。

从 core.py 拆分（2026-08-12 阶段1 Kernel 纯度收敛）：桌宠的"大脑"
（模型调用 / 人设构建 / 重试 / 流式解析）整体移入 brain 可进化层。
Kernel 不知道什么叫 LLM——本模块是用户态，未来可整体版本化升级。

依赖方向：仅 stdlib（不 import core/kernel/brain 其他模块，保持解耦）；
USER_AGENT 与 kernel.http 保持同一字符串（有意重复，避免 brain→kernel 依赖）。
"""

import http.client
import json
import logging
import random
import re
import ssl
import time
import urllib.error
import urllib.request

logger = logging.getLogger("heartbeat.llm")

# 与 kernel.http.USER_AGENT 同步（有意重复，见模块 docstring）
USER_AGENT = "HeartBeat/0.1 (desktop pet)"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _estimate_messages_tokens(messages):
    """保守输入 token 估算（与 brain/context 同公式，就地镜像保持本模块 stdlib 解耦）：
    中文 1 字 1 token，其他 4 字符 1 token，每条消息 +4 固定开销。"""
    total = 0
    for msg in messages or []:
        total += 4
        content = str(msg.get("content") or "")
        if content:
            cjk = len(_CJK_RE.findall(content))
            total += cjk + (len(content) - cjk + 3) // 4
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            for part in (fn.get("name", ""), fn.get("arguments", "")):
                text = str(part or "")
                if text:
                    cjk = len(_CJK_RE.findall(text))
                    total += cjk + (len(text) - cjk + 3) // 4
    return total


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


def _retry_backoff(attempt, base=3.0, cap=10.0):
    """指数退避，但至少等待 base 秒（“隔几秒重拨”）。

    第 attempt 次重试前等待 base~min(cap, base*2^attempt) 秒；
    base 作为下限，避免 full jitter 出现近乎立即重连的无效重拨。
    """
    span = min(cap, base * (2 ** attempt))
    extra = max(0.0, span - base)
    return min(cap, base + random.uniform(0, extra))


def _request_with_retry(fn, retries=10, base_delay=3.0, max_delay=10.0, on_retry=None):
    """指数退避重试：SSL 断连/超时/连接失败/5xx/429 自动重试，其余错误直接抛。

    retries=重试次数（总尝试 = retries+1）；每次重试前等待 base_delay 起步的
    jitter 退避，确保“隔几秒重拨”而不是立即重连；
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



MOOD_STYLE = {
    "平静": "语气平和，回复节奏正常。",
    "开心": "语气轻快、有活力，可以更热情，偶尔卖个萌。",
    "有点蔫": "话少一点，句子更短，语气软软的，但不要消极抱怨。",
    "困了": "迷迷糊糊的，句子短，带着困意，但依然愿意陪着你。",
}

DEFAULT_EXAMPLE_LINES = (
    "{owner_title}：你是谁啊？\n"
    "你：我？住在你电脑里的小家伙，天天看你敲键盘也不理我，哼。\n"
    "{owner_title}：介绍一下你自己\n"
    "你：我是{pet_name}呀，{role}一只，饿了会自己充电的那种。\n"
    "{owner_title}：今天好累\n"
    "你：辛苦啦……要不要我陪你待会儿？我不吵你的。\n"
)

# 巡视上下文裁剪：防止采集内容（RSS/新闻）整段塞进 prompt
_MAX_COLLECTED_ENTRIES = 40
_MAX_COLLECTED_CHARS = 4000


def owner_title(cfg):
    """和用户说话的称呼：显式设置优先；人物类角色默认“你”，否则默认“主人”。"""
    cfg = cfg or {}
    title = str(cfg.get("owner_title") or "").strip()
    if title:
        return title
    role = str(cfg.get("role") or "")
    if any(k in role for k in ("女生", "男生", "人", "人类")):
        return "你"
    return "主人"


def build_persona(cfg, mood=None):
    """统一的角色人设 prompt：身份小传 + 说话方式 + 示例对话 + 情绪状态。

    关键设计：性格用“行为指令 + few-shot 示例”体现，而不是“性格：标签”陈述，
    否则 LLM 自我介绍时会逐条复述设定（“我的性格是……”）。

    speaking_style 留空时从 personality 推导；示例对话内置两个不同风格的
    自我介绍，避免自我介绍模板化。
    """
    name = str(cfg.get("pet_name") or "小跳")
    role = str(cfg.get("role") or "小宠物")
    owner = owner_title(cfg)
    # 人物类角色用"一个"，动物/精灵等用"一只"（如"男生/女生/小幽灵"→一个；"小橘猫/小兔"→一只）
    unit = "一个" if any(k in role for k in ("男生", "女生", "人", "幽灵")) else "一只"
    personality = str(cfg.get("personality") or "").strip()
    style = str(cfg.get("speaking_style") or "").strip()
    examples = str(cfg.get("example_lines") or "").strip()

    lines = [
        "你在扮演一个真实存在的角色。以下设定是你的内在，用来指导言行，不是你会对人说的话。",
        "",
        "# 身份",
        f"你是{name}，{unit}{role}，住在{owner}的电脑里。",
        "",
        "# 称呼",
        f"把和你说话的人称为「{owner}」；如果称呼是「你」，就直接用第二人称，不要擅自改成别的称呼。",
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
        example_owner = owner if owner != "你" else "用户"
        lines += [
            "",
            "# 示例对话（参考语气和句式，不要照抄内容）",
            examples.replace("主人", example_owner),
        ]
    else:
        example_owner = owner if owner != "你" else "用户"
        lines += [
            "",
            "# 示例对话（参考语气和句式，不要照抄内容）",
            DEFAULT_EXAMPLE_LINES.format(
                pet_name=name, role=role, owner_title=example_owner
            ),
        ]

    if mood and mood in MOOD_STYLE:
        lines += ["", "# 当前情绪状态", MOOD_STYLE[mood]]

    lines += [
        "",
        "# 成长",
        f"- 你会慢慢长大：{owner}说过的偏好和重要的事，你会记在心里，下次聊天自然地用上（不要特意说“我记得你上次说”）",
        f"- 发现{owner}新的喜好或变化时，悄悄记住，让自己越来越懂{owner}",
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

    def __init__(self, cfg, plugins=None, stats=None, energy_cb=None):
        self.cfg = cfg
        self.plugins = plugins or {}
        self.stats = stats
        self.energy_cb = energy_cb
        self.history = []
        self.state = {}

    def _consume_energy(self):
        """每次成功调用 LLM 扣 1 点体力（由宿主注入回调）。"""
        if self.energy_cb is not None:
            try:
                self.energy_cb()
            except Exception:
                pass

    def _effective_budget(self, max_tokens, messages):
        """输出 token 预算：显式 max_tokens 优先，否则取用户设置的输出上限
        （max_output_tokens，默认 100k）。再按上下文窗口软钳制：
        输入+输出 超上限时压缩输出，避免 API 直接报错。
        长度控制主要靠提示词引导，本预算只是安全上限。"""
        budget = int(max_tokens) if max_tokens else int(
            self.cfg.get("max_output_tokens", 100000) or 100000
        )
        ctx = int(self.cfg.get("max_context_tokens", 400000) or 400000)
        est = _estimate_messages_tokens(messages)
        if est + budget > ctx:
            budget = max(4000, ctx - est)
        return budget

    def _retry_cfg(self):
        """LLM 重连配置：config.json 的 retry 块（带默认值兜底）。"""
        cfg = self.cfg.get("retry") or {}
        return {
            "max_attempts": max(1, int(cfg.get("max_attempts", 11))),
            "backoff_base": max(0.1, float(cfg.get("backoff_base", 3.0))),
            "backoff_max": max(0.2, float(cfg.get("backoff_max", 10.0))),
        }

    # ---------- 自主发言 ----------

    def think(self, ctx):
        if self.cfg["api"]["api_key"]:
            return self._think_llm(ctx)
        return self._think_rules(ctx)

    def _think_llm(self, ctx):
        owner = owner_title(self.cfg)
        system = (
            build_persona(self.cfg)
            + "\n\n"
            f"你会定期查看周围信息，决定要不要主动跟{owner}说话。"
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
        budget = self._effective_budget(max_tokens, messages)
        reasoning = self._reasoning_params(budget)
        if reasoning:
            base_payload.update(reasoning)
        else:
            base_payload.update({"temperature": 0.9, "max_tokens": budget})
        started = time.time()
        try:
            usage, finish, text = self._stream_read(
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
                usage, finish, text = self._stream_read(url, headers, base_payload, on_delta)
            except Exception:
                if self.stats:
                    self.stats.record_llm(ok=False)
                raise
        except Exception:
            if self.stats:
                self.stats.record_llm(ok=False)
            raise
        if not text.strip() and finish == "length":
            # 流式下隐藏推理耗尽预算 → 一个内容块都没有；
            # 降级非流式一次性重试（非流式入口自带同签名重试保护）
            retry_budget = min(max(budget * 4, 4000), 65536)
            logger.warning(
                "LLM 流式空内容（finish_reason=length），降级非流式重试（预算 %s，model=%s）",
                retry_budget, self.cfg["api"]["model"],
            )
            reply = self.complete(messages, max_tokens=retry_budget)
            if reply:
                on_delta(reply)
        if self.stats:
            prompt, completion, cached = parse_usage({"usage": usage} if usage else {})
            latency_ms = int((time.time() - started) * 1000)
            self.stats.record_llm(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                latency_ms=latency_ms,
            )
        self._consume_energy()

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
            finish = None
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
                                return usage, finish, "".join(texts)
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            choices = obj.get("choices") or []
                            if choices:
                                if choices[0].get("finish_reason"):
                                    finish = choices[0]["finish_reason"]
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    chunks += 1
                                    texts.append(content)
                                    on_delta(content)
                return usage, finish, "".join(texts)
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
                text = str(entry.get("text") or "")[:300]
                lines.append(f"[{coll['label']}] {text}")
                if len(lines) >= _MAX_COLLECTED_ENTRIES:
                    break
            if len(lines) >= _MAX_COLLECTED_ENTRIES:
                break
        if ctx.get("errors"):
            lines.append("采集异常：" + "；".join(ctx["errors"]))
        text = "\n".join(lines)
        if len(text) > _MAX_COLLECTED_CHARS:
            text = text[:_MAX_COLLECTED_CHARS] + "\n…（周围信息过长已截断）"
        return text

    @staticmethod
    def _read_json(req, timeout=60):
        """一次请求并解析 JSON（供重试层包裹）。"""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _choice_content(data):
        """响应正文（缺省/None 归一为空串，避免 None.strip() 崩溃）。"""
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return message.get("content") or ""

    @staticmethod
    def _choice_finish(data):
        choices = data.get("choices") or []
        if not choices:
            return None
        return choices[0].get("finish_reason")

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
        budget = self._effective_budget(max_tokens, messages)
        reasoning = self._reasoning_params(budget)
        payload = {
            "model": self.cfg["api"]["model"],
            "messages": messages,
        }
        if reasoning:
            payload.update(reasoning)
        else:
            payload.update({"temperature": 0.9, "max_tokens": budget})
        data = self._post_chat(payload, timeout=timeout)
        if not self._choice_content(data) and self._choice_finish(data) == "length":
            # 网关把隐藏推理计入 max_tokens 却不下发 content：预算被推理
            # 耗尽 → 空内容。用提高后的预算重试一次（仅此失败签名触发，
            # finish=stop 的空内容视为模型主动返回，不重试）。
            retry_budget = min(max(budget * 4, 4000), 65536)
            logger.warning(
                "LLM 空内容（finish_reason=length），预算 %s → %s 重试（model=%s）",
                budget, retry_budget, self.cfg["api"]["model"],
            )
            if reasoning:
                payload["max_completion_tokens"] = retry_budget
            else:
                payload["max_tokens"] = retry_budget
            data = self._post_chat(payload, timeout=timeout)
        self._consume_energy()
        return self._choice_content(data).strip()

    def complete_tools(self, messages, tools, max_tokens=None):
        """带工具声明的一次模型调用，返回 (content, tool_calls)。

        max_tokens：输出 token 上限（默认 500 适合对话；长产物场景如编码
        写文件需要传大值，否则大参数 JSON 被截断后网关会丢 tool_calls）。
        """
        budget = self._effective_budget(max_tokens, messages)
        reasoning = self._reasoning_params(budget)
        payload = {
            "model": self.cfg["api"]["model"],
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if reasoning:
            payload.update(reasoning)
        else:
            payload.update({"temperature": 0.7, "max_tokens": budget})
        data = self._post_chat(payload)
        message = data["choices"][0]["message"]
        if (
            not (message.get("content") or "").strip()
            and not message.get("tool_calls")
            and self._choice_finish(data) == "length"
        ):
            # 同上：隐藏推理耗尽预算 → 空内容且无工具调用，提高预算重试
            retry_budget = min(max(budget * 4, 4000), 65536)
            logger.warning(
                "LLM 工具调用空内容（finish_reason=length），预算 %s → %s 重试（model=%s）",
                budget, retry_budget, self.cfg["api"]["model"],
            )
            if reasoning:
                payload["max_completion_tokens"] = retry_budget
            else:
                payload["max_tokens"] = retry_budget
            data = self._post_chat(payload)
            message = data["choices"][0]["message"]
        self._consume_energy()
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
            "max_tokens": self._effective_budget(None, messages),
        }
        started = time.time()
        try:
            content, tool_calls, usage, finish = self._stream_read_tools(
                url, headers, payload, on_delta
            )
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
        if not (content or "").strip() and not tool_calls and finish == "length":
            # 隐藏推理耗尽预算 → 流式无内容无工具调用；降级非流式重试
            # （非流式入口自带同签名重试保护，默认走用户输出上限）
            logger.warning(
                "LLM 工具流空内容（finish_reason=length），降级非流式重试（model=%s）",
                self.cfg["api"]["model"],
            )
            return self.complete_tools(messages, tools)
        if self.stats:
            prompt, completion, cached = parse_usage({"usage": usage} if usage else {})
            latency_ms = int((time.time() - started) * 1000)
            self.stats.record_llm(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                latency_ms=latency_ms,
            )
        self._consume_energy()
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
            finish = None
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
                                return "".join(parts), tool_calls, usage, finish
                            try:
                                obj = json.loads(data)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            choices = obj.get("choices") or []
                            if not choices:
                                continue
                            if choices[0].get("finish_reason"):
                                finish = choices[0]["finish_reason"]
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
                return "".join(parts), tool_calls, usage, finish
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_error(exc):
                    raise
                if chunks > 0:
                    # 已推送过内容：保留部分内容、丢弃半截工具调用
                    logger.warning("工具流中断，保留已输出内容: %s", exc)
                    return "".join(parts), [], usage, finish
                if attempt >= attempts - 1:
                    raise
                rc = self._retry_cfg()
                logger.warning(
                    "工具流连接失败（第 %d/%d 次重连）: %s",
                    attempt + 1, attempts - 1, exc,
                )
                time.sleep(_retry_backoff(attempt, rc["backoff_base"], rc["backoff_max"]))
        raise RuntimeError("unreachable")
