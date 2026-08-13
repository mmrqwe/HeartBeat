"""tools_skill：下载/安装/技能生命周期/沙盒。"""

import json
import locale
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import core

from kernel import download as kdownload
from kernel.permission import (
    REJECT,
    SHELL_MODE_OFF,
    SHELL_MODE_FULL,
    SHELL_MODE_READONLY,
    SOURCE_AUTO,
    SOURCE_USER,
    classify,
    run_bash,
    run_process,
)
from tools_common import _confirm, _deny, user_data_dir


def _user_data_dir():
    """运行时经 tools 门面取数据目录，保证测试/宿主替换 tools.user_data_dir 生效。"""
    import tools
    return tools.user_data_dir()


# ---------- 下载 / 安装（受控通道，目标目录固定） ----------


def _downloads_dir():
    """下载目录：<用户数据目录>/downloads。工具不接受任意写入路径。"""
    return Path(_user_data_dir()) / "downloads"


def _skills_dir():
    """技能目录：<用户数据目录>/skills。"""
    return Path(_user_data_dir()) / "skills"


def _exec_download(args, source, confirm_cb, audit, mode):
    url = str(args.get("url", "") or "").strip()
    filename = str(args.get("filename", "") or "").strip() or None
    if not url:
        return "缺少下载地址（url）"
    if source != SOURCE_USER:
        return _deny(audit, source, "download_file", url, mode,
                     "自主触发不允许下载（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "download_file", url, mode,
                     f"当前工具档位（{mode}）不允许下载")
    desc = f"下载文件：{url} → 保存到 {_downloads_dir()}"
    if filename:
        desc += f"/{filename}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "download_file", url, mode)
    if not approved:
        return denied
    try:
        path, size = kdownload.download_file(url, _downloads_dir(), filename=filename)
    except kdownload.DownloadError as exc:
        if audit:
            audit(SOURCE_USER, "download_file", url, mode, True, False, str(exc)[:200])
        return f"下载失败：{exc}"
    except Exception as exc:
        if audit:
            audit(SOURCE_USER, "download_file", url, mode, True, False, str(exc)[:200])
        return f"下载失败：{exc}"
    text = f"下载完成：{path}（{size} 字节）"
    if audit:
        audit(SOURCE_USER, "download_file", url, mode, True, True, text[:200])
    return text


def _exec_install(args, source, confirm_cb, audit, mode):
    zip_path = str(args.get("zip_path", "") or "").strip()
    if not zip_path:
        return "缺少 zip 文件路径（zip_path）"
    if source != SOURCE_USER:
        return _deny(audit, source, "install_skill", zip_path, mode,
                     "自主触发不允许安装（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "install_skill", zip_path, mode,
                     f"当前工具档位（{mode}）不允许安装")
    try:
        p = Path(zip_path).resolve()
    except OSError:
        p = Path(zip_path)
    # 架构评审约束：只能安装 download_file 下载到下载目录里的 zip
    if p.parent != _downloads_dir().resolve():
        return _deny(audit, source, "install_skill", zip_path, mode,
                     "只能安装 download_file 下载到下载目录里的技能包")
    desc = f"安装技能包：{p} → 解压到 {_skills_dir()}/{p.stem}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "install_skill", zip_path, mode)
    if not approved:
        return denied
    try:
        target, files = kdownload.extract_skill_zip(p, _skills_dir())
    except kdownload.DownloadError as exc:
        if audit:
            audit(SOURCE_USER, "install_skill", zip_path, mode, True, False, str(exc)[:200])
        return f"安装失败：{exc}"
    except Exception as exc:
        if audit:
            audit(SOURCE_USER, "install_skill", zip_path, mode, True, False, str(exc)[:200])
        return f"安装失败：{exc}"
    version = ""
    mf = next((f for f in files if f == "manifest.json" or f.endswith("/manifest.json")), None)
    if mf:
        try:
            data = json.loads(kdownload.read_zip_text(p, mf) or "{}")
            version = str(data.get("version", ""))
        except Exception:
            pass
    skill_md = next((f for f in files if f == "SKILL.md" or f.endswith("/SKILL.md")), None)
    text = f"安装完成：{target}（{len(files)} 个文件）"
    if version:
        text += f"，版本 {version}"
    if skill_md:
        text += f"\n技能说明文档：{target / skill_md}"
    if audit:
        audit(SOURCE_USER, "install_skill", zip_path, mode, True, True, text[:200])
    return text


# ---------- 技能生命周期（zhihu-cli 等：状态 / 初始化 / 认证） ----------


def find_skill_dir(name):
    """按目录名或 SKILL.md frontmatter name 定位已安装技能目录。"""
    root = _skills_dir()
    direct = root / name
    if direct.is_dir():
        return direct
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            md = folder / "SKILL.md"
            if md.is_file():
                try:
                    meta = core.parse_skill_frontmatter(
                        md.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    continue
                if meta.get("name") == name:
                    return folder
    return None


def run_skill_script(name, script_name, args=(), timeout=180):
    """运行已安装技能的 scripts/<script_name>，返回 (returncode, text)。

    Windows 下自动给 UTF-8 无 BOM 的 .ps1 补 BOM：Windows PowerShell 5.1
    按 ANSI 读取无 BOM 脚本，中文注释会导致解析失败。
    """
    skill_dir = find_skill_dir(name)
    if skill_dir is None:
        return 1, f"技能不存在：{name}"
    script = skill_dir / "scripts" / script_name
    if not script.is_file():
        return 1, f"技能缺少脚本：{script}"
    if os.name == "nt":
        if script.suffix.lower() == ".ps1":
            raw = script.read_bytes()
            if not raw.startswith(b"\xef\xbb\xbf") and any(b >= 0x80 for b in raw):
                script.write_bytes(b"\xef\xbb\xbf" + raw)
        argv = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args,
        ]
        enc = locale.getpreferredencoding(False)
    else:
        argv = ["bash", str(script), *args]
        enc = "utf-8"
    try:
        proc = run_process(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "技能脚本超时（180 秒）"
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(enc, errors="replace")
    return proc.returncode, text


def _default_cli_binary():
    if os.name == "nt":
        cli_home = os.environ.get("ZHIHU_CLI_HOME") or (
            Path(os.environ.get("LOCALAPPDATA", "")) / "ZhihuCLI"
        )
        return str(Path(cli_home) / "current" / "zhihu-cli.exe")
    return str(Path.home() / ".local" / "share" / "zhihu-cli" / "current" / "zhihu-cli")


SKILL_CLI_READONLY = frozenset({
    "hot", "search", "answer", "me", "capabilities", "version", "help",
})


def _resolve_skill_binary(name):
    """优先从技能 status JSON 的 cli.binary_path 取，找不到回退默认路径。"""
    if find_skill_dir(name) is None:
        return None
    script = "run.ps1" if os.name == "nt" else "run.sh"
    rc, text = run_skill_script(name, script, ["status"], timeout=30)
    if rc == 0:
        try:
            data = json.loads(text)
            path = str(data.get("cli", {}).get("binary_path", "") or "")
            if path and Path(path).is_file():
                return Path(path)
        except Exception:
            pass
    fallback = _default_cli_binary()
    return Path(fallback) if Path(fallback).is_file() else None


def run_skill_cli(name, args, timeout=90, max_output=8192):
    """运行已安装技能的 CLI，返回 (returncode, text)。

    args 必须是字符串列表，直接作为 argv 传入，不经过 shell。
    是否自动执行由调用方（_exec_skill_exec）按只读白名单 + 确认门控决定。
    """
    args = [str(a) for a in (args or [])]
    if not args:
        return 1, "CLI 参数为空"
    binary = _resolve_skill_binary(name)
    if binary is None:
        return 1, f"找不到技能 {name} 的 CLI（先运行 skill_setup）"
    try:
        proc = run_process([str(binary)] + args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "技能 CLI 超时"
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) if os.name == "nt" else "utf-8"
        text = raw.decode(enc, errors="replace")
    text = text[:max_output]
    return proc.returncode, text


def configure_skill_auth(name, secret, binary=None, timeout=90):
    """配置技能 CLI 认证：auth set（stdin 传 Secret）→ verify → 最小本人读取。

    返回 (returncode, 输出文本)；任一步失败即停。Secret 不回显。
    """
    bin_path = Path(binary or _default_cli_binary())
    if not bin_path.is_file():
        return 1, f"CLI 不存在：{bin_path}（先运行 skill_setup）"
    steps = [
        (["auth", "set", "--secret-stdin"], (secret + "\n").encode("utf-8")),
        (["auth", "status", "--verify"], None),
        (["me", "contents", "--type", "all", "--limit", "1"], None),
    ]
    parts = []
    for cmd, data in steps:
        try:
            proc = run_process(
                [str(bin_path)] + cmd, input=data, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 1, "认证命令超时：" + " ".join(cmd)
        raw = proc.stdout + proc.stderr
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            enc = locale.getpreferredencoding(False) if os.name == "nt" else "utf-8"
            text = raw.decode(enc, errors="replace").strip()
        parts.append(f"$ {bin_path} {' '.join(cmd)}\n{text or '(无输出)'}")
        if proc.returncode != 0:
            return proc.returncode, "\n".join(parts)
    return 0, "\n".join(parts) + "\n初始化完成：认证验证通过，本人内容读取成功。"


def _exec_skill_status(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "缺少技能名（name）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "skill_status", name, mode, "shell 工具已关闭")
    rc, text = run_skill_script(
        name, "run.ps1" if os.name == "nt" else "run.sh", ["status"]
    )
    if audit:
        audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
              "skill_status", name, mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能状态检查失败（exit={rc}）：\n{text}"
    return text.strip() or "(无输出)"


def _exec_skill_setup(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "缺少技能名（name）"
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_setup", name, mode,
                     "自主触发不允许初始化技能（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "skill_setup", name, mode,
                     f"当前工具档位（{mode}）不允许初始化技能")
    desc = f"初始化技能：{name} → 运行 scripts/setup.*（下载并安装官方 CLI 到用户目录）"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "skill_setup", name, mode)
    if not approved:
        return denied
    rc, text = run_skill_script(
        name, "setup.ps1" if os.name == "nt" else "setup.sh", []
    )
    if audit:
        audit(SOURCE_USER, "skill_setup", name, mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能初始化失败（exit={rc}）：\n{text}"
    return text.strip() or "(初始化完成，无输出)"


def _exec_skill_auth(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    secret = str(args.get("secret", "") or "").strip()
    binary = str(args.get("binary", "") or "").strip() or None
    if not name:
        return "缺少技能名（name）"
    if not secret:
        return "缺少 Access Secret（secret）"
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_auth", name, mode,
                     "自主触发不允许配置认证（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "skill_auth", name, mode,
                     f"当前工具档位（{mode}）不允许配置认证")
    desc = f"配置技能 {name} 的 Access Secret（写入本机 CLI 凭证，不回显内容）"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "skill_auth", name, mode)
    if not approved:
        return denied
    rc, text = configure_skill_auth(name, secret, binary)
    if audit:
        audit(SOURCE_USER, "skill_auth", name, mode, True, rc == 0, text[:200])
    return text


def _exec_skill_exec(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    cmd_args = args.get("args") or []
    if not name:
        return "缺少技能名（name）"
    if not isinstance(cmd_args, list) or not cmd_args:
        return "缺少 CLI 参数（args，字符串列表）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "skill_exec", name, mode, "shell 工具已关闭")
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_exec", name, mode,
                     "自主触发不允许调用技能 CLI（需要主人在场）")
    cmd_args = [str(a) for a in cmd_args]
    auto = bool(cmd_args) and cmd_args[0] in SKILL_CLI_READONLY
    if not auto:
        if mode == SHELL_MODE_READONLY:
            return _deny(audit, source, "skill_exec", name + " " + " ".join(cmd_args),
                         mode, "只读档不允许调用技能 CLI 写命令")
        desc = f"调用技能 {name} CLI：{' '.join(cmd_args)}"
        approved, denied = _confirm(desc, confirm_cb, audit, source,
                                    "skill_exec", name + " " + " ".join(cmd_args), mode)
        if not approved:
            return denied
    rc, text = run_skill_cli(name, cmd_args)
    if audit:
        audit(SOURCE_USER, "skill_exec",
              name + " " + " ".join(cmd_args),
              mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能 CLI 执行失败（exit={rc}）：\n{text}"
    return text.strip() or "(无输出)"


def _exec_skill_list(args, source, audit):
    skills_root = Path(_user_data_dir()) / "skills"
    found = []
    if skills_root.is_dir():
        for folder in sorted(skills_root.iterdir()):
            md = folder / "SKILL.md"
            if md.is_file():
                found.append(folder.name)
    text = "已安装技能：\n" + "\n".join(f"- {n}" for n in found) if found else "还没有安装技能"
    if audit:
        audit(source, "skill_list", "", "readonly", True, True, text[:200])
    return text


def _exec_skill(args, source, confirm_cb, audit, mode):
    """skill：action = list | download | install | status | setup | auth | exec。"""
    action = str(args.get("action", "list") or "list").strip().lower()
    if action == "list":
        return _exec_skill_list(args, source, audit)
    if action == "download":
        return _exec_download(args, source, confirm_cb, audit, mode)
    if action == "install":
        return _exec_install(args, source, confirm_cb, audit, mode)
    if action == "status":
        return _exec_skill_status(args, source, confirm_cb, audit, mode)
    if action == "setup":
        return _exec_skill_setup(args, source, confirm_cb, audit, mode)
    if action == "auth":
        return _exec_skill_auth(args, source, confirm_cb, audit, mode)
    if action == "exec":
        return _exec_skill_exec(args, source, confirm_cb, audit, mode)
    return "未知 skill action：" + action


# ---------- 沙盒（Agent 可读可写可执行的工作区，即 kernel.workspace） ----------


def _sandbox_root():
    import kernel.workspace
    return kernel.workspace.workspace_root(base=_user_data_dir())


def _resolve_sandbox_path(rel):
    """把相对/绝对路径解析到沙盒内；越界抛 ValueError。"""
    root = _sandbox_root().resolve()
    p = Path(rel)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"路径越出沙盒：{rel}")
    return p


def sandbox_read(rel, max_bytes=65536):
    p = _resolve_sandbox_path(rel)
    if not p.is_file():
        return f"文件不存在：{p}"
    data = p.read_bytes()[:max_bytes]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def sandbox_write(rel, content):
    p = _resolve_sandbox_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = str(content)
    p.write_text(text, encoding="utf-8")
    return f"已写入：{p}（{len(text)} 字符）"


def sandbox_list(rel="."):
    p = _resolve_sandbox_path(rel)
    if not p.is_dir():
        return f"目录不存在：{p}"
    lines = []
    for child in sorted(p.iterdir()):
        lines.append(("DIR  " if child.is_dir() else "FILE ") + child.name)
    return "\n".join(lines) or "(空目录)"


def sandbox_run(command, timeout=60, max_output=8192):
    """在沙盒根目录执行一条完整 shell 命令（允许管道/重定向/脚本语法）。"""
    # 唯一执行引擎：复用 run_bash（完整 shell + 隐藏窗口 + 环境过滤）
    return run_bash(str(command), cwd=str(_sandbox_root()), timeout=timeout, max_output=max_output)


def _exec_sandbox_read(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or "").strip()
    if not path:
        return "缺少路径（path）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "sandbox_read", path, mode, "shell 工具已关闭")
    try:
        text = sandbox_read(path)
    except ValueError as exc:
        return f"读取失败：{exc}"
    if audit:
        audit(source, "sandbox_read", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_list(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or ".").strip() or "."
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "sandbox_list", path, mode, "shell 工具已关闭")
    try:
        text = sandbox_list(path)
    except ValueError as exc:
        return f"列出失败：{exc}"
    if audit:
        audit(source, "sandbox_list", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_write(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or "").strip()
    content = args.get("content", "")
    if not path:
        return "缺少路径（path）"
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "sandbox_write", path, mode,
                     f"当前工具档位（{mode}）不允许写沙盒")
    try:
        text = sandbox_write(path, content)
    except ValueError as exc:
        return f"写入失败：{exc}"
    if audit:
        audit(source, "sandbox_write", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_run(args, source, confirm_cb, audit, mode):
    command = str(args.get("command", "") or "").strip()
    if not command:
        return "缺少命令（command）"
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "sandbox_run", command, mode,
                     f"当前工具档位（{mode}）不允许执行沙盒命令")
    # 沙盒是它自己的空间：不再弹确认，但保留硬禁命令/敏感路径判定
    decision, reason = classify(command, SHELL_MODE_FULL, SOURCE_USER)
    if decision == REJECT:
        return _deny(audit, source, "sandbox_run", command, mode, reason)
    try:
        timeout = max(5, min(int(args.get("timeout", 60) or 60), 300))
    except (TypeError, ValueError):
        timeout = 60
    text = sandbox_run(command, timeout=timeout)
    ok = text.startswith("exit=0")
    if audit:
        audit(source, "sandbox_run", command, mode, True, ok, text[:200])
    return text


def _exec_sandbox_db(args, source, confirm_cb, audit, mode):
    sql = str(args.get("sql", "") or "").strip()
    if not sql:
        return "缺少 SQL（sql）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "sandbox_db", sql, mode, "shell 工具已关闭")
    readonly = mode == SHELL_MODE_READONLY
    import kernel.workspace as workspace_mod
    try:
        text = workspace_mod.db_exec(sql, base=_user_data_dir(), readonly=readonly)
    except Exception as exc:
        text = f"数据库操作失败：{exc}"
    if audit:
        audit(source, "sandbox_db", sql, mode, True, "失败" not in text, text[:200])
    return text


def _exec_sandbox(args, source, confirm_cb, audit, mode):
    """统一 sandbox 工具：action = list | read | write | run | db。"""
    action = str(args.get("action", "list") or "list").strip().lower()
    if action == "list":
        return _exec_sandbox_list(args, source, confirm_cb, audit, mode)
    if action == "read":
        return _exec_sandbox_read(args, source, confirm_cb, audit, mode)
    if action == "write":
        return _exec_sandbox_write(args, source, confirm_cb, audit, mode)
    if action == "run":
        return _exec_sandbox_run(args, source, confirm_cb, audit, mode)
    if action == "db":
        return _exec_sandbox_db(args, source, confirm_cb, audit, mode)
    return f"未知 sandbox action：{action}"


# ---------- 工作区门面（供 brain 主动思考使用：brain 不 import kernel） ----------


def workspace_record_observations(collections, base=None):
    """把巡视采集结果落进工作区观察库（去重）。返回摘要 dict。"""
    import kernel.workspace as workspace_mod
    return workspace_mod.record_observations(collections, base=base)


def workspace_brief(base=None):
    """工作区快照文本（注入主动思考提示词）。"""
    import kernel.workspace as workspace_mod
    return workspace_mod.workspace_brief(base=base)


