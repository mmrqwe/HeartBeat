"""tools_common：工具层共享小件（脱敏/确认/参数声明/权限符号）。"""

import json
import os
import re
from pathlib import Path

from kernel import download as kdownload  # noqa: F401
from kernel import pathguard  # noqa: F401
from kernel import processpool  # noqa: F401
from kernel.boot import user_data_dir
from kernel.permission import (
    AUTO,
    BASH_TIMEOUT,
    CONFIRM,
    REJECT,
    SHELL_MODES,
    SHELL_MODE_CONFIRM,
    SHELL_MODE_FULL,
    SHELL_MODE_OFF,
    SHELL_MODE_READONLY,
    SOURCE_AUTO,
    SOURCE_USER,
    SENSITIVE_PATH_MARKERS,
    classify,
    human_brief,
    run_bash,
    resolve_workdir,
    shell_name,
    shell_hint,
    _filter_env,
)

_SECRET_KEY_RE = re.compile(
    r"(?i)((?:sk-|key|api[_-]?key|token|password|passwd|secret|authorization)"
    r"\s*[:=]\s*)([A-Za-z0-9_\-\.]{6,})"
)
_SECRET_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.]{6,})")
_SECRET_SK_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})")


def redact_secrets(text):
    """把日志/状态行/气泡里的疑似密钥打码（key=xxx、Bearer xxx、sk-...）。"""
    if not text:
        return text
    out = _SECRET_KEY_RE.sub(r"\1***", str(text))
    out = _SECRET_BEARER_RE.sub(r"\1***", out)
    out = _SECRET_SK_RE.sub("sk-***", out)
    return out


def _params_decl(param, param_desc):
    return {
        "type": "object",
        "properties": {param: {"type": "string", "description": param_desc}},
        "required": [param],
    }


def _deny(audit, source, tool, detail, mode, reason):
    """拒绝并审计（approved=False），返回给 LLM 的文本。"""
    if audit:
        audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
              tool, detail, mode, False, False, reason)
    return reason


def _confirm(desc, confirm_cb, audit, source, tool, detail, mode):
    """下载/安装/写操作确认：无回调视为拒绝。返回 (approved, 拒绝文本或 None)。"""
    approved = bool(confirm_cb(desc)) if confirm_cb else False
    if not approved:
        reason = "用户未确认，已取消。"
        if audit:
            audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                  tool, detail, mode, False, False, reason)
        return False, reason
    return True, None


