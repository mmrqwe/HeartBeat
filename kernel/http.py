"""kernel.http：HTTP 基础设施（内核级，仅 stdlib）。

从 core.py 拆分（2026-08-12 阶段1 Kernel 纯度收敛）：HTTP 是内核自身
（download / updater 在线检查）与用户态（插件采集 / 搜索）共用的传输层，
不属于任何可进化模块。不依赖 core/brain；调用方可直接 import 或经 core shim。
"""

import json
import urllib.request

USER_AGENT = "HeartBeat/0.1 (desktop pet)"


def http_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_json(url, timeout=10):
    return json.loads(http_text(url, timeout))
