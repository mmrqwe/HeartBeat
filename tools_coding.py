"""tools_coding：编码文件/后台/待办/项目约定/备份工具。"""

import json
import fnmatch
import glob
import os
import re
import shutil
import threading
import time
from pathlib import Path

from kernel import pathguard, processpool
from kernel.permission import (
    CONFIRM,
    REJECT,
    SHELL_MODE_CONFIRM,
    SHELL_MODE_FULL,
    SHELL_MODE_OFF,
    SHELL_MODE_READONLY,
    SOURCE_AUTO,
    SOURCE_USER,
    SENSITIVE_PATH_MARKERS,
    classify,
)
from tools_common import _confirm, _deny, user_data_dir


def _user_data_dir():
    """运行时经 tools 门面取数据目录，保证测试/宿主替换 tools.user_data_dir 生效。"""
    import tools
    return tools.user_data_dir()


# ---------- Coding 文件/后台工具（project_dir 基座，锁定层校验在 kernel.pathguard） ----------

CODING_TOOLS = frozenset({
    "bash", "read", "write", "edit", "glob", "grep",
    "todo", "bg", "skill", "backup", "note",
})
_LEGACY_TOOL_NAMES = frozenset({
    "read_file", "write_file", "edit_file", "search_files", "glob_match",
    "list_files", "bg_exec", "bg_check", "bg_cancel",
    "list_backups", "restore_backup", "backup_preview",
})

READ_MAX_BYTES = 256 * 1024
EDIT_MAX_BYTES = 512 * 1024
WRITE_MAX_BYTES = 2 * 1024 * 1024
READ_MAX_LINES = 2000
LIST_MAX_ENTRIES = 500
GLOB_MAX_ENTRIES = 300
SEARCH_MAX_MATCHES = 50
SEARCH_FILE_MAX_BYTES = 512 * 1024
CONFIRM_PREVIEW_CHARS = 60000


def _backups_root():
    """Coding 写操作备份目录：<用户数据目录>/backups。"""
    return Path(_user_data_dir()) / "backups"


def _coding_project(project_dir):
    """校验并返回项目根目录 Path；失败抛 PathGuardError（调用方转 deny 文本）。"""
    return pathguard.project_root(project_dir)


def _read_text_snippet(path, max_bytes, max_lines=READ_MAX_LINES):
    """读文本文件：二进制检测 → 解码 → 行号格式化 + 截断提示。

    返回 (text, None) 或 (None, error)。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"读取失败：{exc}"
    if b"\x00" in raw[:8192]:
        return None, "二进制文件，跳过读取"
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    numbered = "\n".join(f"{i + 1:>4}|{line}" for i, line in enumerate(lines))
    if truncated:
        numbered += "\n…（内容超出上限，已截断）"
    return numbered, None


def _guard_error(audit, source, tool, detail, mode, exc):
    """pathguard 校验失败统一转拒绝文本 + 审计。"""
    return _deny(audit, source, tool, detail, mode, str(exc))


# ---------- 只读文件工具 ----------


def _exec_read_file(args, mode, source, project_dir, audit):
    rel = str(args.get("path", "") or "").strip()
    if not rel:
        return "缺少路径（path）"
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "read_file", rel, mode, exc)
    if not target.is_file():
        return f"文件不存在：{rel}"
    text, err = _read_text_snippet(target, READ_MAX_BYTES)
    if audit:
        audit(source, "read_file", rel, mode, True, err is None,
              (err or text)[:200])
    return err or text


def _walk_tree(base, depth, cap):
    """目录树文本（目录后加 /，缩进表示层级；跳过忽略目录与隐藏目录）。"""
    out = []

    def visit(d, level):
        if len(out) >= cap:
            return
        try:
            children = sorted(
                d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return
        for child in children:
            if len(out) >= cap:
                return
            if child.is_dir():
                name = child.name
                if name in pathguard.IGNORED_DIR_NAMES or name.startswith("."):
                    continue
                out.append(f"{'  ' * level}{name}/")
                if level + 1 < depth:
                    visit(child, level + 1)
            else:
                out.append(f"{'  ' * level}{child.name}")

    visit(base, 0)
    return out


def _exec_list_files(args, mode, source, project_dir, audit):
    rel = str(args.get("path", "") or "").strip() or "."
    try:
        base = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "list_files", rel, mode, exc)
    if not base.is_dir():
        return f"目录不存在：{rel}"
    try:
        depth = max(1, min(int(args.get("depth", 2) or 2), 4))
    except (TypeError, ValueError):
        depth = 2
    entries = _walk_tree(base, depth, LIST_MAX_ENTRIES)
    text = "\n".join(entries) if entries else "（空目录）"
    if len(entries) >= LIST_MAX_ENTRIES:
        text += "\n…（条目过多，已截断）"
    if audit:
        audit(source, "list_files", rel, mode, True, True, text[:200])
    return text


def _exec_search_files(args, mode, source, project_dir, audit):
    pattern = str(args.get("pattern", "") or "").strip()
    if not pattern:
        return "缺少匹配模式（pattern）"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"正则无效：{exc}"
    rel = str(args.get("path", "") or "").strip() or "."
    try:
        base = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "search_files", rel, mode, exc)
    if not base.is_dir():
        return f"目录不存在：{rel}"
    file_glob = str(args.get("file_glob", "") or "").strip() or "*"
    out = []
    scanned = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(
            d for d in dirs
            if d not in pathguard.IGNORED_DIR_NAMES and not d.startswith(".")
        )
        for name in sorted(files):
            if len(out) >= SEARCH_MAX_MATCHES:
                break
            if not fnmatch.fnmatch(name, file_glob):
                continue
            scanned += 1
            path = Path(root) / name
            try:
                if path.stat().st_size > SEARCH_FILE_MAX_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    out.append(
                        f"{path.relative_to(base).as_posix()}:{i}: {line.strip()[:160]}"
                    )
                    if len(out) >= SEARCH_MAX_MATCHES:
                        break
        if len(out) >= SEARCH_MAX_MATCHES:
            break
    if not out:
        return f"没有匹配内容（扫描 {scanned} 个文件）"
    text = "\n".join(out[:SEARCH_MAX_MATCHES])
    if len(out) >= SEARCH_MAX_MATCHES:
        text += "\n…（已达匹配上限）"
    if audit:
        audit(source, "search_files", pattern, mode, True, True, text[:200])
    return text


def _exec_glob_match(args, mode, source, project_dir, audit):
    pattern = str(args.get("pattern", "") or "").strip()
    if not pattern:
        return "缺少匹配模式（pattern）"
    if pattern.startswith("/") or pattern.startswith("~") or ".." in pattern.split("/"):
        return _deny(audit, source, "glob_match", pattern, mode,
                     "匹配模式必须限制在项目目录内")
    try:
        base = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "glob_match", pattern, mode, exc)
    matches = []
    for hit in glob.glob(str(base / pattern), recursive=True):
        p = Path(hit)
        rel = p.relative_to(base).as_posix()
        low = rel.lower()
        if any(marker in low for marker in SENSITIVE_PATH_MARKERS):
            continue
        if any(part in pathguard.IGNORED_DIR_NAMES for part in p.parts):
            continue
        matches.append(rel + ("/" if p.is_dir() else ""))
        if len(matches) >= GLOB_MAX_ENTRIES:
            break
    if not matches:
        return "没有匹配的文件"
    text = "\n".join(sorted(matches))
    if len(matches) >= GLOB_MAX_ENTRIES:
        text += "\n…（已达匹配上限）"
    if audit:
        audit(source, "glob_match", pattern, mode, True, True, text[:200])
    return text


# ---------- 写文件工具（confirm 档确认 + 写前备份 + 原子写） ----------


def _diff_payload(action, rel, before, after):
    return {
        "kind": "diff",
        "action": action,
        "path": rel,
        "before": before[:CONFIRM_PREVIEW_CHARS],
        "after": after[:CONFIRM_PREVIEW_CHARS],
    }


def _exec_write_file(args, source, confirm_cb, audit, mode, project_dir):
    rel = str(args.get("path", "") or "").strip()
    content = str(args.get("content", "") or "")
    if not rel:
        return "缺少路径（path）"
    if not content:
        return "缺少内容（content）"
    if len(content.encode("utf-8")) > WRITE_MAX_BYTES:
        return f"内容超过上限（{WRITE_MAX_BYTES // 1024}KB），请分批写入"
    if source != SOURCE_USER:
        return _deny(audit, source, "write_file", rel, mode,
                     "自主触发不允许写文件（需要主人在场）")
    if mode == SHELL_MODE_READONLY:
        return _deny(audit, source, "write_file", rel, mode,
                     f"当前工具档位（{mode}）不允许写文件")
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "write_file", rel, mode, exc)
    if target.exists() and target.is_dir():
        return _deny(audit, source, "write_file", rel, mode, "目标是目录，拒绝写入")
    try:
        backup = pathguard.backup_before_write(project_dir, rel, _backups_root())
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "write_file", rel, mode, exc)
    before = ""
    if target.exists():
        try:
            before = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
    # confirm 档弹窗；full 档新建文件自动放行，但覆盖已有文件必须确认
    if mode == SHELL_MODE_CONFIRM or (
        mode == SHELL_MODE_FULL and target.exists()
    ):
        approved, denied = _confirm(
            _diff_payload("write_file", rel, before, content),
            confirm_cb, audit, source, "write_file", rel, mode,
        )
        if not approved:
            return denied
    try:
        pathguard.atomic_write_text(target, content)
    except pathguard.PathGuardError as exc:
        if audit:
            audit(SOURCE_USER, "write_file", rel, mode, True, False, str(exc)[:200])
        return f"写入失败：{exc}"
    text = f"已写入 {rel}（{len(content)} 字符）"
    if backup is not None:
        text += f"，旧内容已备份到 {backup}"
    if audit:
        audit(SOURCE_USER, "write_file", rel, mode, True, True, text[:200])
    return text


def _exec_edit_file(args, source, confirm_cb, audit, mode, project_dir):
    rel = str(args.get("path", "") or "").strip()
    search_text = str(args.get("search", "") or "")
    replace_text = str(args.get("replace", "") or "")
    if not rel:
        return "缺少路径（path）"
    if not search_text:
        return "缺少锚点（search）：必须提供要替换的唯一原文片段"
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "edit_file", rel, mode, exc)
    if not target.is_file():
        return f"文件不存在：{rel}"
    if source != SOURCE_USER:
        return _deny(audit, source, "edit_file", rel, mode,
                     "自主触发不允许编辑文件（需要主人在场）")
    if mode == SHELL_MODE_READONLY:
        return _deny(audit, source, "edit_file", rel, mode,
                     f"当前工具档位（{mode}）不允许编辑文件")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return f"读取失败：{exc}"
    if b"\x00" in raw[:8192]:
        return "二进制文件，拒绝编辑"
    if len(raw) > EDIT_MAX_BYTES:
        return f"文件超过编辑上限（{EDIT_MAX_BYTES // 1024}KB）"
    text = raw.decode("utf-8", errors="replace")
    count = text.count(search_text)
    if count == 0:
        return "锚点未找到：search 内容在文件中不存在"
    expected = args.get("expected_occurrences")
    replace_count = 1
    if expected is not None:
        try:
            replace_count = int(expected)
        except (TypeError, ValueError):
            return "expected_occurrences 必须是整数"
        if replace_count < 1:
            return "expected_occurrences 必须 ≥ 1"
        if count != replace_count:
            return f"锚点匹配数不符：预期 {replace_count} 处，实际 {count} 处"
    elif count != 1:
        return (f"锚点不唯一（{count} 处匹配）。请提供更长的唯一锚点，"
                "或用 expected_occurrences 明确指定替换数量")
    new_text = text.replace(search_text, replace_text, replace_count)
    if new_text == text:
        return "替换后内容无变化"
    try:
        backup = pathguard.backup_before_write(project_dir, rel, _backups_root())
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "edit_file", rel, mode, exc)
    # 编辑已有文件本身就是破坏性操作：任何档位都必须确认
    if mode in (SHELL_MODE_CONFIRM, SHELL_MODE_FULL):
        approved, denied = _confirm(
            _diff_payload("edit_file", rel, text, new_text),
            confirm_cb, audit, source, "edit_file", rel, mode,
        )
        if not approved:
            return denied
    try:
        pathguard.atomic_write_text(target, new_text)
    except pathguard.PathGuardError as exc:
        if audit:
            audit(SOURCE_USER, "edit_file", rel, mode, True, False, str(exc)[:200])
        return f"写入失败：{exc}"
    text_out = f"已编辑 {rel}（替换 {replace_count} 处）"
    if backup is not None:
        text_out += f"，旧内容已备份到 {backup}"
    if audit:
        audit(SOURCE_USER, "edit_file", rel, mode, True, True, text_out[:200])
    return text_out


# ---------- 后台进程工具（kernel.processpool：并发/超时/输出上限） ----------

_BG_POOL = None


def _bg_pool():
    global _BG_POOL
    if _BG_POOL is None:
        _BG_POOL = processpool.BgPool()
    return _BG_POOL


def cancel_all_background():
    """取消所有后台进程（用户主动停止编码任务时调用，幂等）。"""
    return _bg_pool().cancel_all()


def _exec_bg_exec(args, source, confirm_cb, audit, mode, project_dir):
    command = str(args.get("command", "") or "").strip()
    if not command:
        return "缺少命令（command）"
    if source != SOURCE_USER:
        return _deny(audit, source, "bg_exec", command, mode,
                     "自主触发不允许执行后台命令（需要主人在场）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "bg_exec", command, mode,
                     f"当前工具档位（{mode}）不允许执行命令")
    # 硬禁/敏感路径判定复用内核规则（curl/wget/sudo/密钥路径等）
    decision, reason = classify(command, mode, source)
    if decision == REJECT:
        if audit:
            audit(source, "bg_exec", command, mode, False, False, reason)
        return f"已拒绝执行：{reason}"
    try:
        root = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "bg_exec", command, mode, exc)
    try:
        timeout = int(args.get("timeout", processpool.DEFAULT_TIMEOUT)
                      or processpool.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = processpool.DEFAULT_TIMEOUT
    approved = True
    if decision == CONFIRM:
        approved = bool(confirm_cb(command)) if confirm_cb else False
        if not approved:
            if audit:
                audit(source, "bg_exec", command, mode, False, False, "用户未确认")
            return "用户未确认，已取消执行。"
    try:
        pid = _bg_pool().start(command, str(root), timeout=timeout)
    except processpool.PoolError as exc:
        if audit:
            audit(source, "bg_exec", command, mode, True, False, str(exc)[:200])
        return f"启动失败：{exc}"
    text = f"后台任务已启动：{pid}（超时 {timeout}s，工作目录 {root}）。请用 bg_check 轮询结果。"
    if audit:
        audit(source, "bg_exec", command, mode, approved, True, text[:200])
    return text


def _exec_bg_check(args, source, audit, mode):
    pid = str(args.get("task_id", "") or "").strip()
    if not pid:
        return "缺少任务 ID（task_id）"
    info = _bg_pool().poll(pid)
    if info is None:
        return f"任务不存在：{pid}（可能已被清理）"
    text = f"状态：{info['status']}；已运行 {info['elapsed']}s"
    if info["exit_code"] is not None:
        text += f"；exit={info['exit_code']}"
    text += "\n最近输出：\n" + (info["output_tail"] or "（暂无输出）")
    if audit:
        audit(source, "bg_check", pid, mode, True, True, text[:200])
    return text


def _exec_bg_cancel(args, source, audit, mode):
    pid = str(args.get("task_id", "") or "").strip()
    if not pid:
        return "缺少任务 ID（task_id）"
    text = _bg_pool().cancel(pid)
    if audit:
        audit(source, "bg_cancel", pid, mode, True, True, text[:200])
    return text


def _exec_bg(args, source, confirm_cb, audit, mode, project_dir):
    """bg：action = exec | check | cancel。"""
    action = str(args.get("action", "exec") or "exec").strip().lower()
    if action == "exec":
        return _exec_bg_exec(args, source, confirm_cb, audit, mode, project_dir)
    if action == "check":
        return _exec_bg_check(args, source, audit, mode)
    if action == "cancel":
        return _exec_bg_cancel(args, source, audit, mode)
    return "未知 bg action：" + action


# ---------- 待办清单（按项目持久化到用户数据目录，不污染项目仓库） ----------

_TODO_LOCK = threading.Lock()


def _todo_store_path():
    return Path(_user_data_dir()) / "coding_todos.json"


def _todo_load():
    path = _todo_store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _todo_save(data):
    path = _todo_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _exec_todo(args, source, audit, project_dir):
    """todo：list / add / done / clear，按项目根目录隔离。"""
    try:
        root = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "todo", str(args), "readonly", exc)
    action = str(args.get("action", "list") or "list").strip().lower()
    key = str(root)
    with _TODO_LOCK:
        data = _todo_load()
        todos = data.get(key, [])
        if action == "add":
            item = str(args.get("item", "") or "").strip()
            if not item:
                return "缺少待办内容（item）"
            next_id = max((t.get("id", 0) for t in todos), default=0) + 1
            todos.append({"id": next_id, "text": item, "done": False})
            data[key] = todos
            _todo_save(data)
            text = f"已添加待办 #{next_id}：{item}"
        elif action == "done":
            try:
                tid = int(args.get("id", 0) or 0)
            except (TypeError, ValueError):
                return "done 需要数字 id"
            for t in todos:
                if t.get("id") == tid:
                    t["done"] = True
                    data[key] = todos
                    _todo_save(data)
                    text = f"已完成待办 #{tid}：{t['text']}"
                    break
            else:
                return f"待办不存在：{tid}"
        elif action == "clear":
            data[key] = []
            _todo_save(data)
            text = "待办清单已清空"
        else:
            if not todos:
                return "待办清单是空的"
            lines = [
                ("✅" if t.get("done") else "⬜") + f" #{t['id']} {t['text']}"
                for t in todos
            ]
            text = "待办清单：\n" + "\n".join(lines)
    if audit:
        audit(source, "todo", str(args), "readonly", True, True, text[:200])
    return text


# ---------- 项目约定记忆（按项目持久化，供后续编码任务复用） ----------

_NOTE_LOCK = threading.Lock()


def _note_store_path():
    return Path(_user_data_dir()) / "coding_notes.json"


def _note_load():
    path = _note_store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _note_save(data):
    path = _note_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _exec_note(args, source, audit, project_dir):
    """note：按项目记录/查看约定，action = list | add | clear。"""
    try:
        root = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "note", str(args), "readonly", exc)
    action = str(args.get("action", "list") or "list").strip().lower()
    key = str(root)
    with _NOTE_LOCK:
        data = _note_load()
        notes = data.get(key, [])
        if action == "add":
            text = str(args.get("text", "") or "").strip()
            if not text:
                return "缺少约定内容（text）"
            notes.append(text)
            data[key] = notes[-50:]  # 每个项目最多记 50 条
            _note_save(data)
            result = f"已记住项目约定：{text}"
        elif action == "clear":
            data[key] = []
            _note_save(data)
            result = "项目约定已清空"
        else:
            if not notes:
                return "这个项目还没有记录约定"
            result = "项目约定：\n" + "\n".join(f"- {n}" for n in notes)
    if audit:
        audit(source, "note", str(args), "readonly", True, True, result[:200])
    return result


# ---------- 备份浏览与恢复 ----------


def _backup_entries(project_dir, rel=""):
    """列出当前项目最近的备份（按时间片倒序，最多 20 条）。"""
    try:
        root = _coding_project(project_dir)
    except pathguard.PathGuardError:
        return []
    backups_root = Path(_backups_root())
    if not backups_root.is_dir():
        return []
    entries = []
    for ts_dir in sorted(backups_root.iterdir(), reverse=True):
        if not ts_dir.is_dir():
            continue
        marker = ts_dir / ".project"
        try:
            if marker.is_file() and marker.read_text(
                encoding="utf-8", errors="replace"
            ).strip() != str(root):
                continue
        except OSError:
            continue
        rel_path = rel.strip("/") if rel else ""
        if rel_path:
            candidate = ts_dir / rel_path
            if candidate.is_file():
                entries.append({
                    "id": ts_dir.name,
                    "path": rel,
                    "size": candidate.stat().st_size,
                })
        else:
            for f in sorted(ts_dir.rglob("*")):
                if not f.is_file() or f.name == ".project":
                    continue
                entries.append({
                    "id": ts_dir.name,
                    "path": f.relative_to(ts_dir).as_posix(),
                    "size": f.stat().st_size,
                })
                if len(entries) >= 20:
                    break
        if len(entries) >= 20:
            break
    return entries


def _exec_list_backups(args, source, audit, project_dir):
    rel = str(args.get("path", "") or "").strip()
    entries = _backup_entries(project_dir, rel)
    if not entries:
        return "没有找到该项目的备份"
    lines = [
        f"[{e['id']}] {e['path']}（{e['size']} 字节）"
        for e in entries
    ]
    text = "最近备份：\n" + "\n".join(lines)
    if audit:
        audit(source, "list_backups", rel, "readonly", True, True, text[:200])
    return text


def _exec_restore_backup(args, source, confirm_cb, audit, mode, project_dir):
    """从备份恢复文件：先校验归属项目，恢复前再备份当前状态。"""
    backup_id = str(args.get("backup_id", "") or "").strip()
    rel = str(args.get("path", "") or "").strip()
    if not re.fullmatch(r"\d{20}-\d{6}", backup_id):
        return "备份 ID 格式不正确（用 list_backups 获取）"
    if not rel:
        return "缺少路径（path）"
    if source != SOURCE_USER:
        return _deny(audit, source, "restore_backup", rel, mode,
                     "自主触发不允许恢复文件（需要主人在场）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "restore_backup", rel, mode,
                     f"当前工具档位（{mode}）不允许写文件")
    try:
        root = _coding_project(project_dir)
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "restore_backup", rel, mode, exc)
    ts_dir = Path(_backups_root()) / backup_id
    marker = ts_dir / ".project"
    try:
        if marker.is_file() and marker.read_text(
            encoding="utf-8", errors="replace"
        ).strip() != str(root):
            return "该备份不属于当前项目，已拒绝恢复"
    except OSError:
        return "备份元数据不可读，已拒绝恢复"
    src = ts_dir / rel.strip("/")
    if not src.is_file():
        return f"备份文件不存在：{rel}"
    approved, denied = _confirm(
        (
            f"从备份恢复文件：{rel}\n"
            f"备份时间片：{backup_id}\n"
            "恢复前会把当前文件再备份一次，确认覆盖吗？"
        ),
        confirm_cb, audit, source, "restore_backup", rel, mode,
    )
    if not approved:
        return denied
    try:
        pathguard.backup_before_write(project_dir, rel, _backups_root())
        shutil.copy2(src, target)
    except OSError as exc:
        if audit:
            audit(source, "restore_backup", rel, mode, True, False, str(exc)[:200])
        return f"恢复失败：{exc}"
    text = f"已从备份 {backup_id} 恢复 {rel}"
    if audit:
        audit(source, "restore_backup", rel, mode, True, True, text[:200])
    return text


def _exec_backup_preview(args, source, audit, mode, project_dir):
    """backup preview：对比当前文件与备份内容，先看再决定是否恢复。"""
    backup_id = str(args.get("backup_id", "") or "").strip()
    rel = str(args.get("path", "") or "").strip()
    if not re.fullmatch(r"\d{20}-\d{6}", backup_id):
        return "备份 ID 格式不正确（用 backup list 获取）"
    if not rel:
        return "缺少路径（path）"
    try:
        root = _coding_project(project_dir)
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "backup_preview", rel, mode, exc)
    ts_dir = Path(_backups_root()) / backup_id
    marker = ts_dir / ".project"
    try:
        if marker.is_file() and marker.read_text(
            encoding="utf-8", errors="replace"
        ).strip() != str(root):
            return "该备份不属于当前项目，已拒绝预览"
    except OSError:
        return "备份元数据不可读"
    src = ts_dir / rel.strip("/")
    if not src.is_file():
        return f"备份文件不存在：{rel}"
    try:
        before = src.read_text(encoding="utf-8", errors="replace")
        after = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    except OSError as exc:
        return f"读取失败：{exc}"
    text = f"备份 {backup_id} 的 {rel}（当前 → 备份）：\n"
    text += "当前内容：\n" + (after[:2000] or "（不存在）") + "\n\n"
    text += "备份内容：\n" + before[:2000]
    if audit:
        audit(source, "backup_preview", rel, "readonly", True, True, text[:200])
    return text


def _exec_backup(args, source, confirm_cb, audit, mode, project_dir):
    """backup：action = list | preview | restore。"""
    action = str(args.get("action", "list") or "list").strip().lower()
    if action == "list":
        return _exec_list_backups(args, source, audit, project_dir)
    if action == "preview":
        return _exec_backup_preview(args, source, audit, mode, project_dir)
    if action == "restore":
        return _exec_restore_backup(args, source, confirm_cb, audit, mode, project_dir)
    return "未知 backup action：" + action


