"""kernel.download：受控下载与技能包解压（内核安全边界扩展）。

背景：bash 工具把 curl/wget 硬禁（kernel.permission.HARD_BLOCK_COMMANDS，
网络逃逸防线），宠物此前没有任何联网下载通道。本模块提供进程内受控通道：
download_file（http/https 下载）+ extract_skill_zip（技能包安全解压）。

安全约束（架构评审 2026-08-12 确认）：
- 仅 http/https，重定向目标同样校验 scheme 与网段（自定义 HTTPRedirectHandler）；
- 阻断本机回环 / 链路本地 / 云元数据 / ULA / 组播网段（SSRF 基本防线）；
  RFC1918 局域网不阻断——桌面个人场景，下载需用户确认且弹窗可见完整 URL；
- 下载上限 DOWNLOAD_MAX_BYTES（200MB），解压膨胀上限 EXTRACT_MAX_BYTES（1GB，
  zip 炸弹防护，按声明的 file_size 累加，提取前拒绝）；
- zip 条目拒绝：绝对路径 / 盘符 / 目录穿越（..）/ 符号链接（S_ISLNK）；
- HTTPS 证书校验走默认上下文（ssl.create_default_context），绝不 unverified；
- 文件名 sanitize：仅取末段、去前导点、去控制字符、截断；
- 同目录 .tmp 临时文件 + os.replace 原子落盘（避免跨文件系统 rename）；
- 目标技能目录已存在时先备份为 <name>.old（仅保留最新一份）。

依赖方向：仅标准库；tools.py（brain 层）经 execute() 统一分级确认后调用本模块。
"""

import ipaddress
import os
import posixpath
import re
import shutil
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DOWNLOAD_TIMEOUT = 60  # 秒
DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024  # 下载大小上限
EXTRACT_MAX_BYTES = 1024 * 1024 * 1024  # 解压后总大小上限（防 zip 炸弹）
_CHUNK = 64 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 阻断目标网段：未指定 / 本机回环 / 链路本地（含云元数据 169.254.169.254）/
# 组播 / IPv6 ULA / 站点本地。RFC1918 局域网不阻断（见模块 docstring）。
_BLOCKED_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("ff00::/8"),
)


class DownloadError(Exception):
    """下载/解压失败（消息为中文，直接返回给 LLM 展示）。"""


# ---------- 目标校验 ----------


def _check_scheme(url):
    """仅允许 http/https。非法时抛 DownloadError。"""
    scheme = (urllib.parse.urlparse(url).scheme or "").lower()
    if scheme not in ("http", "https"):
        raise DownloadError(f"仅支持 http/https 下载：{scheme or '未知'} 协议")


def _check_target(url):
    """解析主机名并校验所有解析出的 IP 不在阻断网段。非法时抛 DownloadError。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise DownloadError("下载地址缺少主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise DownloadError(f"无法解析主机：{host}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if any(ip in net for net in _BLOCKED_NETS):
            raise DownloadError(f"目标地址在阻断网段（{addr}），已拒绝")


def validate_http_target(url):
    """公开目标校验：仅 http/https 且解析 IP 不在阻断网段。

    下载、重定向以及进化工具 ctx.http_text/http_json 共用同一道 SSRF 防线。
    """
    _check_scheme(url)
    _check_target(url)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时校验新目标（防 file:// 逃逸与重定向进内网）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_scheme(newurl)
        _check_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------- 下载 ----------


def _safe_filename(name, fallback="download.bin"):
    """从 URL 末段 / 用户文件名提取安全文件名：仅末段、去前导点、去控制字符、截断。"""
    name = posixpath.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    name = name.lstrip(".").strip()
    if not name:
        return fallback
    return name[:120] or fallback


def _silent_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def download_file(
    url,
    dest_dir,
    filename=None,
    timeout=DOWNLOAD_TIMEOUT,
    max_bytes=DOWNLOAD_MAX_BYTES,
):
    """下载 url 到 dest_dir（自动命名或指定 filename），原子落盘。

    返回 (绝对路径 Path, 字节数)。任何失败抛 DownloadError（中文消息）。
    """
    url = (url or "").strip()
    if not url:
        raise DownloadError("下载地址为空")
    validate_http_target(url)
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"无法创建下载目录：{exc}")
    if filename:
        name = _safe_filename(filename)
    else:
        url_name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        name = _safe_filename(url_name)
    target = dest / name
    tmp = dest / (".tmp-" + name)
    opener = urllib.request.build_opener(
        _SafeRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}
    )
    size = 0
    try:
        with opener.open(req, timeout=timeout) as resp:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise DownloadError(f"文件超过上限（{max_bytes} 字节），已中止")
                    fh.write(chunk)
        os.replace(tmp, target)
    except DownloadError:
        _silent_unlink(tmp)
        raise
    except urllib.error.HTTPError as exc:
        _silent_unlink(tmp)
        raise DownloadError(f"HTTP {exc.code}：{exc.reason}")
    except urllib.error.URLError as exc:
        _silent_unlink(tmp)
        raise DownloadError(f"网络错误：{exc.reason}")
    except OSError as exc:
        _silent_unlink(tmp)
        raise DownloadError(f"写入失败：{exc}")
    return target.resolve(), size


# ---------- 技能包解压 ----------


def _validate_members(infos):
    """校验 zip 成员清单：膨胀上限 / 绝对路径 / 盘符 / 穿越 / 符号链接。

    返回可安全提取的成员列表；发现危险条目抛 DownloadError。
    """
    total = 0
    clean = []
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue  # 目录条目无需提取
        total += info.file_size
        if total > EXTRACT_MAX_BYTES:
            raise DownloadError(
                f"压缩包解压后过大（超过 {EXTRACT_MAX_BYTES} 字节），已拒绝"
            )
        norm = name.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            raise DownloadError(f"压缩包含绝对路径条目：{name}")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise DownloadError(f"压缩包含目录穿越条目：{name}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:  # S_ISLNK：符号链接可指向解压目录外，等同穿越
            raise DownloadError(f"压缩包含符号链接条目：{name}")
        clean.append(info)
    return clean


def _collapse_root(infos):
    """若 zip 所有文件都在同一个顶层目录，返回该目录名（安装时剥掉一层）。

    官方 skill 包（如 zhihu-cli-skill）常带一个顶层目录；应用内技能扫描器
    按 <skills>/<包名>/SKILL.md 发现技能，剥掉这层才能“安装即生效”。
    """
    tops = set()
    for info in infos:
        norm = info.filename.replace("\\", "/")
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if len(parts) <= 1:
            return None  # 根目录有文件，不能剥
        tops.add(parts[0])
    return next(iter(tops)) if len(tops) == 1 else None


def extract_skill_zip(zip_path, dest_dir):
    """把技能包 zip 解压到 dest_dir/<zip 文件名去扩展名>/。

    目标目录已存在时先备份为 <name>.old（仅保留最新一份）。
    返回 (目标目录 Path, 已解压文件相对路径列表)。
    zip 非法 / 含危险条目 / 解压失败时抛 DownloadError。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise DownloadError(f"文件不存在：{zip_path}")
    try:
        with zip_path.open("rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        raise DownloadError(f"无法读取文件：{exc}")
    if magic != b"PK\x03\x04":
        raise DownloadError("不是有效的 zip 压缩包")
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DownloadError(f"zip 打开失败：{exc}")
    with zf:
        clean = _validate_members(zf.infolist())
        strip_root = _collapse_root(clean)
        prefix = (strip_root + "/") if strip_root else ""
        dest = Path(dest_dir)
        target = dest / zip_path.stem
        try:
            dest.mkdir(parents=True, exist_ok=True)
            if target.exists():
                old = dest / (target.name + ".old")
                if old.exists():
                    shutil.rmtree(old, ignore_errors=True)
                shutil.move(str(target), str(old))
            target.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise DownloadError(f"无法创建安装目录：{exc}")
        extracted = []
        try:
            for info in clean:
                rel = info.filename.replace("\\", "/")
                if prefix and rel.startswith(prefix):
                    rel = rel[len(prefix):]
                out = target / rel
                # 纵深防御：前面已过滤绝对/穿越条目，这里再按真实路径校验一次
                if not out.resolve().is_relative_to(target.resolve()):
                    raise DownloadError(f"压缩包含越界条目：{info.filename}")
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=_CHUNK)
                extracted.append(rel)
            if os.name == "nt":
                # Windows PowerShell 5.1 按 ANSI 读取无 BOM 的 .ps1，
                # UTF-8 中文注释会导致解析失败；安装时统一补 BOM。
                for rel in extracted:
                    if rel.lower().endswith(".ps1"):
                        p = target / rel
                        raw = p.read_bytes()
                        if not raw.startswith(b"\xef\xbb\xbf") and any(b >= 0x80 for b in raw):
                            p.write_bytes(b"\xef\xbb\xbf" + raw)
        except DownloadError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise DownloadError(f"解压失败：{exc}")
    return target.resolve(), extracted


def read_zip_text(zip_path, name, max_bytes=65536):
    """读取 zip 内文本文件（安装后展示 manifest/SKILL 摘要用）。不存在/过大返回 None。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        return None
    with zf:
        try:
            info = zf.getinfo(name)
        except KeyError:
            return None
        if info.file_size > max_bytes:
            return None
        try:
            return zf.read(name).decode("utf-8", errors="replace")
        except (OSError, zipfile.BadZipFile):
            return None
