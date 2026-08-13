"""kernel.pathguard：Coding Agent 文件操作的安全基座（锁定层，不可进化）。

为什么放 kernel 且不可进化（架构师裁决 2026-08-13）：
文件读写是 Coding Agent 的主要破坏面。路径穿越判定、敏感路径拒绝、
写前备份这三条是安全基线——如果可被进化改写，LLM 生成的候选就能
绕过项目目录限制读任意文件、覆盖任意文件且不留备份。

职责：
- resolve_project_path：把工具传入的相对路径解析到项目目录内，
  拒绝绝对路径 / 穿越（..）/ 符号链接逃逸 / 敏感路径；
- backup_before_write：覆盖写前把旧文件备份到 <backups_root>/<ts>/<rel>，
  带数量与体积上限的 LRU 清理（防备份无限膨胀）；
- atomic_write_text：临时文件 + os.replace 原子写；
- IGNORED_DIR_NAMES：目录遍历时默认跳过的目录（VCS/依赖/构建产物）。

依赖方向：只依赖 stdlib。不 import kernel 内其他模块（permission_judge
的 SENSITIVE_PATH_MARKERS 由调用方 tools.py 传入校验，保持零耦合）。
"""

import os
import shutil
import time
from pathlib import Path

# 目录遍历默认跳过的目录名（VCS / 依赖 / 构建产物 / 缓存）
IGNORED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env",
    ".idea", ".vscode",
    "dist", "build", "target", ".gradle", ".next", ".nuxt", ".cache",
})

# 写前备份：最多保留的备份时间片数量 / 总字节上限（LRU 清理）
BACKUP_MAX_COUNT = 50
BACKUP_MAX_BYTES = 500 * 1024 * 1024  # 500MB

_backup_seq = 0  # 进程内序号：Windows 时钟粒度下同一 tick 多次备份也能保证唯一


class PathGuardError(Exception):
    """路径校验/备份失败（工具层捕获后转成给 LLM 的文本，不抛到循环外）。"""


def project_root(project_dir):
    """项目根目录：非空校验 + 展开 + 解析。返回 Path；未配置抛 PathGuardError。"""
    raw = str(project_dir or "").strip()
    if not raw:
        raise PathGuardError("未配置项目目录（project_dir）")
    expanded = os.path.expanduser(raw)
    if not os.path.isdir(expanded):
        raise PathGuardError(f"项目目录不存在：{raw}")
    return Path(expanded).resolve()


def resolve_project_path(project_dir, rel, sensitive_markers=()):
    """把相对路径解析到项目目录内，返回 Path。

    拒绝：绝对路径、空 project_dir、路径穿越（.. / 符号链接逃逸）、
    敏感路径标记（凭据/密钥文件）。解析走 resolve()（跟随符号链接），
    符号链接指向项目外会被识别并拒绝。
    """
    root = project_root(project_dir)
    rel = str(rel or "").strip() or "."
    if rel.startswith("~"):
        raise PathGuardError("路径必须以项目目录内的相对路径给出（不支持 ~）")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise PathGuardError("路径必须是项目目录内的相对路径（不支持绝对路径）")
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise PathGuardError(f"路径越界，禁止访问项目目录之外：{rel}")
    low = rel.lower()
    if sensitive_markers and any(marker in low for marker in sensitive_markers):
        raise PathGuardError(f"涉及敏感文件（密钥/凭据），已拒绝：{rel}")
    return target


def backup_before_write(project_dir, rel, backups_root):
    """覆盖写前的旧文件备份：<backups_root>/<ts>/<rel>。

    返回备份路径；目标不存在返回 None（新建无需备份）。
    备份失败抛 PathGuardError（宁可阻断写入也不无备份覆盖）。
    """
    target = resolve_project_path(project_dir, rel)
    if not target.exists():
        return None
    if not target.is_file():
        raise PathGuardError(f"目标是目录，拒绝覆盖写：{rel}")
    # 时间片 = 纳秒时间戳 + 进程内序号：Windows 时钟粒度粗，单纯微秒会碰撞；
    # 固定宽度保证按名字排序 == 按时间排序。
    global _backup_seq
    _backup_seq += 1
    ts_dir = Path(backups_root) / f"{time.time_ns():020d}-{_backup_seq:06d}"
    backup_path = ts_dir / Path(rel).as_posix().strip("/")
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # 记录项目根目录，供备份浏览/恢复工具确认归属，避免跨项目误恢复
        try:
            (ts_dir / ".project").write_text(
                str(Path(project_dir).resolve()), encoding="utf-8"
            )
        except OSError:
            pass
        shutil.copy2(target, backup_path)
        _prune_backups(backups_root)
    except OSError as exc:
        raise PathGuardError(f"备份失败，已取消写入：{exc}") from exc
    return backup_path


def _prune_backups(backups_root):
    """备份 LRU 清理：按时间片目录名排序，超数量/超体积删最旧（尽力而为）。"""
    root = Path(backups_root)
    try:
        entries = sorted(
            (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name
        )
    except OSError:
        return
    if not entries:
        return
    # 按最旧→最新累积体积，找出需要保留的最早时间片
    sizes = []
    for entry in entries:
        total = 0
        try:
            for base, _dirs, files in os.walk(entry):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(base, name))
                    except OSError:
                        pass
        except OSError:
            pass
        sizes.append(total)
    keep_from = 0
    cumulative = 0
    for idx in range(len(entries) - 1, -1, -1):
        cumulative += sizes[idx]
        if cumulative >= BACKUP_MAX_BYTES:
            keep_from = idx
            break
    keep_from = max(keep_from, len(entries) - BACKUP_MAX_COUNT)
    for entry in entries[:keep_from]:
        try:
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def atomic_write_text(path, content):
    """临时文件 + os.replace 原子写文本（utf-8）。失败抛 PathGuardError。"""
    target = Path(path)
    tmp = target.with_name(target.name + ".hb.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise PathGuardError(f"写入失败：{exc}") from exc
