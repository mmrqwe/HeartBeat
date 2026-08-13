"""tools_execute：统一执行分发（新名 + 旧名别名）。"""

import json

from tools_common import (
    BASH_TIMEOUT,
    CONFIRM,
    REJECT,
    SHELL_MODE_OFF,
    SOURCE_AUTO,
    SOURCE_USER,
    _deny,
    classify,
    run_bash,
)
from tools_search import SEARCH_HANDLERS, _exec_web
from tools_skill import (
    _exec_download,
    _exec_install,
    _exec_skill,
    _exec_skill_auth,
    _exec_skill_exec,
    _exec_skill_setup,
    _exec_skill_status,
    _exec_sandbox_list,
    _exec_sandbox_read,
    _exec_sandbox_run,
    _exec_sandbox_write,
)
from tools_coding import (
    CODING_TOOLS,
    _LEGACY_TOOL_NAMES,
    _exec_backup,
    _exec_backup_preview,
    _exec_bg,
    _exec_bg_cancel,
    _exec_bg_check,
    _exec_bg_exec,
    _exec_edit_file,
    _exec_glob_match,
    _exec_list_backups,
    _exec_list_files,
    _exec_note,
    _exec_read_file,
    _exec_restore_backup,
    _exec_search_files,
    _exec_todo,
    _exec_write_file,
)

def _exec_bash(args, source, confirm_cb, audit, mode, cwd):
    """run_bash 统一执行（内置工具与进化工具 ctx 原语共用同一权限路径）。"""
    cmdline = str(args.get("command", "")).strip()
    if not cmdline:
        return "命令为空"
    decision, reason = classify(cmdline, mode, source)
    if decision == REJECT:
        if audit:
            audit(source, "run_bash", cmdline, mode, False, False, reason)
        return f"已拒绝执行：{reason}"
    approved = True
    if decision == CONFIRM:
        approved = bool(confirm_cb(cmdline)) if confirm_cb else False
        if not approved:
            if audit:
                audit(source, "run_bash", cmdline, mode, False, False, "用户未确认")
            return "用户未确认，已取消执行。"
    try:
        timeout = min(int(args.get("timeout", BASH_TIMEOUT) or BASH_TIMEOUT), 60)
    except (TypeError, ValueError):
        timeout = BASH_TIMEOUT
    text = run_bash(cmdline, cwd=cwd, timeout=timeout)
    ok = not text.startswith(("命令超时", "命令不存在", "没有执行权限", "执行失败"))
    if audit:
        audit(source, "run_bash", cmdline, mode, approved, ok, text[:200])
    return text


# ---------- 统一执行入口 ----------

def execute(name, arguments, *, mode, source, confirm_cb=None, cwd=None, audit=None,
           project_dir=None):
    """执行工具调用：分类 → 确认 → 执行 → 审计。返回给 LLM 的文本结果。

    confirm_cb: 需要用户确认时调用 confirm_cb(cmdline) -> bool（超时/未提供视为拒绝）。
    audit: 审计回调 audit(source, tool, detail, mode, approved, ok, summary)。
    project_dir: Coding 文件工具的项目根目录（kernel.pathguard 校验边界）。
    安全判定与 bash 执行见 kernel.permission。
    """
    args = {}
    if arguments:
        # 网关兼容：arguments 可能是已解析的 dict（部分 OpenAI 兼容网关行为）
        if isinstance(arguments, dict):
            args = arguments
        else:
            try:
                args = json.loads(arguments)
            except ValueError:
                args = {}
    if name in SEARCH_HANDLERS:
        try:
            text = SEARCH_HANDLERS[name](args)
        except Exception as exc:
            text = f"搜索没成功：{exc}"
        if audit:
            audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                  name, arguments, "readonly", True, True, text[:200])
        return text
    if name == "web":
        return _exec_web(args, source, audit)
    if name in ("bash", "run_bash"):
        return _exec_bash(args, source, confirm_cb, audit, mode, cwd)
    if name == "skill":
        return _exec_skill(args, source, confirm_cb, audit, mode)
    if name == "download_file":
        return _exec_download(args, source, confirm_cb, audit, mode)
    if name == "install_skill":
        return _exec_install(args, source, confirm_cb, audit, mode)
    if name == "skill_status":
        return _exec_skill_status(args, source, confirm_cb, audit, mode)
    if name == "skill_setup":
        return _exec_skill_setup(args, source, confirm_cb, audit, mode)
    if name == "skill_auth":
        return _exec_skill_auth(args, source, confirm_cb, audit, mode)
    if name == "skill_exec":
        return _exec_skill_exec(args, source, confirm_cb, audit, mode)
    if name == "sandbox_read":
        return _exec_sandbox_read(args, source, confirm_cb, audit, mode)
    if name == "sandbox_write":
        return _exec_sandbox_write(args, source, confirm_cb, audit, mode)
    if name == "sandbox_list":
        return _exec_sandbox_list(args, source, confirm_cb, audit, mode)
    if name == "sandbox_run":
        return _exec_sandbox_run(args, source, confirm_cb, audit, mode)
    # Coding 文件/后台工具（project_dir 基座；路径校验/备份在 kernel.pathguard，
    # 后台进程资源边界在 kernel.processpool）
    if name in CODING_TOOLS or name in _LEGACY_TOOL_NAMES:
        if mode == SHELL_MODE_OFF:
            return _deny(audit, source, name, arguments, mode, "shell 工具已关闭")
        if name in ("read", "read_file"):
            return _exec_read_file(args, mode, source, project_dir, audit)
        if name in ("list", "list_files"):
            return _exec_list_files(args, mode, source, project_dir, audit)
        if name in ("grep", "search_files"):
            return _exec_search_files(args, mode, source, project_dir, audit)
        if name in ("glob", "glob_match"):
            return _exec_glob_match(args, mode, source, project_dir, audit)
        if name in ("write", "write_file"):
            return _exec_write_file(args, source, confirm_cb, audit, mode, project_dir)
        if name in ("edit", "edit_file"):
            return _exec_edit_file(args, source, confirm_cb, audit, mode, project_dir)
        if name == "bg":
            return _exec_bg(args, source, confirm_cb, audit, mode, project_dir)
        if name == "bg_exec":
            return _exec_bg_exec(args, source, confirm_cb, audit, mode, project_dir)
        if name == "bg_check":
            return _exec_bg_check(args, source, audit, mode)
        if name == "bg_cancel":
            return _exec_bg_cancel(args, source, audit, mode)
        if name == "todo":
            return _exec_todo(args, source, audit, project_dir)
        if name == "backup":
            return _exec_backup(args, source, confirm_cb, audit, mode, project_dir)
        if name == "list_backups":
            return _exec_list_backups(args, source, audit, project_dir)
        if name == "backup_preview":
            return _exec_backup_preview(args, source, audit, mode, project_dir)
        if name == "restore_backup":
            return _exec_restore_backup(
                args, source, confirm_cb, audit, mode, project_dir
            )
        if name == "note":
            return _exec_note(args, source, audit, project_dir)
    return f"未知工具：{name}"
