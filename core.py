"""core：兼容 shim（历史根模块 re-export）。

2026-08-12 阶段1 Kernel 纯度收敛拆分：
- HTTP 基础设施 → kernel/http.py
- LLM 客户端 / 人设 / 重试 / 流式 → brain/llm.py
- 技能包元数据 → brain/skills.py
- 内容采集 / RSS 解析 / 跨源汇聚 → brain/content.py

本文件只 re-export，保持旧引用兼容（agent / plugins / search / cli / gui / 测试）。
新代码建议直接 import 各归属模块；本 shim 随引用清理后可删除。
"""

import logging as _logging

# 模块对象 re-export：测试与旧代码 mock core.time / core.urllib.request 等。
# 模块对象全局共享，mock 对 brain.llm / kernel.http 内的同名引用同样生效。
import http.client  # noqa: F401
import json  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import ssl  # noqa: F401
import time  # noqa: F401
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
import xml.etree.ElementTree  # noqa: F401
import hashlib  # noqa: F401
import inspect  # noqa: F401

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
from kernel.http import USER_AGENT, http_text, http_json  # noqa: F401

# 可进化层 re-export（实现见 brain/llm.py、brain/skills.py、brain/content.py）
from brain.llm import (  # noqa: F401
    Brain,
    StreamInterrupted,
    RETRYABLE_STATUS,
    _is_retryable_error,
    _retry_backoff,
    _request_with_retry,
    parse_usage,
    MOOD_STYLE,
    DEFAULT_EXAMPLE_LINES,
    build_persona,
    owner_title,
)
from brain.skills import (  # noqa: F401
    SKILL_NAME_MAX,
    SKILL_DESC_MAX,
    parse_skill_frontmatter,
)
from brain.content import (  # noqa: F401
    parse_rss,
    parse_rss_items,
    collect_all,
    gather,
    MERGE_PRIORITY,
    MERGE_TTL,
    _normalize_title,
    merge_entries,
)

logger = _logging.getLogger("heartbeat.core")
