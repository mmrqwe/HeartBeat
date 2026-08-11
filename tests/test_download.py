"""kernel.download 安全下载/解压测试：目标校验、文件名清洗、zip 防护、正常路径。"""

import socket
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import kernel.download as dl


class _Patch:
    """兼容 pytest monkeypatch 的最小实现，供直接运行时使用。"""

    def __init__(self):
        self._saved = []

    def setattr(self, target, name, value):
        self._saved.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def restore(self):
        for target, name, old in reversed(self._saved):
            setattr(target, name, old)


class _Handler(BaseHTTPRequestHandler):
    payload = b""
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):
        pass


def _serve(payload=b"data", path="/file.bin", status=200):
    """本地 loopback HTTP server（测试用；真实调用会被 _check_target 阻断）。"""
    handler = type("H", (_Handler,), {"payload": payload, "status": status})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _fake_resolve(ip):
    """伪造 socket.getaddrinfo：把任意主机解析到指定 IP。"""
    if ":" in ip:
        sockaddr, family = (ip, 443, 0, 0), socket.AF_INET6
    else:
        sockaddr, family = (ip, 80), socket.AF_INET
    return lambda host, port, proto=0: [
        (family, socket.SOCK_STREAM, 6, "", sockaddr)
    ]


# ---------- 文件名清洗 ----------


def test_safe_filename_basic():
    assert dl._safe_filename("a.zip") == "a.zip"
    assert dl._safe_filename("../evil.zip") == "evil.zip"
    assert dl._safe_filename("a/b/c.bin") == "c.bin"
    assert dl._safe_filename("C:\\Users\\x\\f.exe") == "f.exe"
    assert dl._safe_filename(".hidden") == "hidden"
    assert dl._safe_filename("..") == "download.bin"
    assert dl._safe_filename("") == "download.bin"
    assert dl._safe_filename("a\x00b\x1fc.txt") == "abc.txt"
    assert len(dl._safe_filename("x" * 500 + ".zip")) <= 120


# ---------- scheme 与目标校验 ----------


def test_check_scheme():
    for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x", "javascript:alert(1)"):
        try:
            dl._check_scheme(url)
            assert False, f"{url} 应被拒绝"
        except dl.DownloadError:
            pass
    dl._check_scheme("https://example.com/a.zip")
    dl._check_scheme("http://example.com/a.zip")


def test_check_target_blocks_private():
    patch = _Patch()
    try:
        for ip in ("169.254.169.254", "127.0.0.1", "0.0.0.0", "::1",
                   "fe80::1", "fc00::1", "ff02::1"):
            patch.setattr(dl.socket, "getaddrinfo", _fake_resolve(ip))
            try:
                dl._check_target("https://host.invalid/a.zip")
                assert False, f"{ip} 应被阻断"
            except dl.DownloadError as exc:
                assert "阻断网段" in str(exc)
    finally:
        patch.restore()


def test_check_target_allows_public_and_lan():
    patch = _Patch()
    try:
        for ip in ("8.8.8.8", "93.184.216.34", "192.168.1.5", "10.0.0.1",
                   "172.16.0.1", "2606:4700:4700::1111"):
            patch.setattr(dl.socket, "getaddrinfo", _fake_resolve(ip))
            dl._check_target("https://host.invalid/a.zip")
    finally:
        patch.restore()


def test_check_target_missing_host():
    try:
        dl._check_target("http:///a.zip")
        assert False
    except dl.DownloadError:
        pass


def test_redirect_handler_blocks_file_scheme():
    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request("http://example.com/a")
    try:
        handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
        assert False, "重定向到 file:// 应被拒绝"
    except dl.DownloadError:
        pass


def test_redirect_handler_blocks_private_target():
    patch = _Patch()
    try:
        patch.setattr(dl.socket, "getaddrinfo", _fake_resolve("169.254.169.254"))
        handler = dl._SafeRedirectHandler()
        req = urllib.request.Request("http://example.com/a")
        try:
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://metadata.local/a")
            assert False
        except dl.DownloadError:
            pass
    finally:
        patch.restore()


# ---------- 下载 ----------


def test_download_success():
    srv, url = _serve(b"hello-download", path="/pkg/skill.zip")
    try:
        patch = _Patch()
        try:
            patch.setattr(dl, "_check_target", lambda u: None)
            with TemporaryDirectory() as d:
                path, size = dl.download_file(url, d)
                assert size == 14
                assert Path(path).read_bytes() == b"hello-download"
                assert Path(path).name == "skill.zip"
                # 自定义文件名
                path2, size2 = dl.download_file(url, d, filename="../evil name.bin")
                assert Path(path2).name == "evil name.bin"
                assert size2 == 14
                # 无残留临时文件
                assert not [p for p in Path(d).iterdir() if p.name.startswith(".tmp-")]
        finally:
            patch.restore()
    finally:
        srv.shutdown()


def test_download_scheme_blocked():
    patch = _Patch()
    try:
        # 即使绕过目标校验，scheme 校验也在 download_file 内独立存在（纵深防御）
        patch.setattr(dl, "_check_target", lambda u: None)
        with TemporaryDirectory() as d:
            try:
                dl.download_file("file:///etc/passwd", d)
                assert False
            except dl.DownloadError as exc:
                assert "http/https" in str(exc)
            assert list(Path(d).iterdir()) == []
    finally:
        patch.restore()


def test_download_size_cap():
    srv, url = _serve(b"x" * 10240, path="/big.bin")
    try:
        patch = _Patch()
        try:
            patch.setattr(dl, "_check_target", lambda u: None)
            with TemporaryDirectory() as d:
                try:
                    dl.download_file(url, d, max_bytes=1024)
                    assert False
                except dl.DownloadError as exc:
                    assert "超过上限" in str(exc)
                # 超限后不留文件与临时文件
                assert list(Path(d).iterdir()) == []
        finally:
            patch.restore()
    finally:
        srv.shutdown()


def test_download_http_error():
    srv, url = _serve(b"not found", path="/404.bin", status=404)
    try:
        patch = _Patch()
        try:
            patch.setattr(dl, "_check_target", lambda u: None)
            with TemporaryDirectory() as d:
                try:
                    dl.download_file(url, d)
                    assert False
                except dl.DownloadError as exc:
                    assert "HTTP 404" in str(exc)
                assert list(Path(d).iterdir()) == []
        finally:
            patch.restore()
    finally:
        srv.shutdown()


# ---------- zip 校验与解压 ----------


def _zip_bytes(entries):
    """entries: {name: bytes} 或 [(ZipInfo, bytes)]。返回 zip 文件字节。"""
    buf = __import__("io").BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_zip(path, entries):
    path.write_bytes(_zip_bytes(entries))
    return path


def test_extract_success():
    with TemporaryDirectory() as d:
        z = _write_zip(Path(d) / "skill.zip", {
            "zhihu/SKILL.md": b"# skill",
            "zhihu/manifest.json": b'{"version":"0.2.1"}',
            "zhihu/references/cli.md": b"cli doc",
            "readme.txt": b"top-level",
        })
        target, files = dl.extract_skill_zip(z, Path(d) / "skills")
        assert target == (Path(d) / "skills" / "skill").resolve()
        assert sorted(files) == [
            "readme.txt", "zhihu/SKILL.md", "zhihu/manifest.json", "zhihu/references/cli.md",
        ]
        assert (target / "zhihu" / "SKILL.md").read_text() == "# skill"
        assert (target / "zhihu" / "references" / "cli.md").read_text() == "cli doc"


def test_extract_not_zip():
    with TemporaryDirectory() as d:
        z = Path(d) / "fake.zip"
        z.write_bytes(b"this is not a zip file at all")
        try:
            dl.extract_skill_zip(z, Path(d) / "skills")
            assert False
        except dl.DownloadError as exc:
            assert "不是有效的 zip" in str(exc)


def test_extract_missing_file():
    with TemporaryDirectory() as d:
        try:
            dl.extract_skill_zip(Path(d) / "nope.zip", Path(d) / "skills")
            assert False
        except dl.DownloadError as exc:
            assert "不存在" in str(exc)


def test_extract_traversal_rejected():
    with TemporaryDirectory() as d:
        z = _write_zip(Path(d) / "evil.zip", {"../evil.txt": b"x"})
        try:
            dl.extract_skill_zip(z, Path(d) / "skills")
            assert False
        except dl.DownloadError as exc:
            assert "目录穿越" in str(exc)
        assert not (Path(d).parent / "evil.txt").exists()


def test_extract_absolute_rejected():
    with TemporaryDirectory() as d:
        z = _write_zip(Path(d) / "evil.zip", {"/etc/evil.txt": b"x"})
        try:
            dl.extract_skill_zip(z, Path(d) / "skills")
            assert False
        except dl.DownloadError as exc:
            assert "绝对路径" in str(exc)


def test_extract_symlink_rejected():
    with TemporaryDirectory() as d:
        buf = __import__("io").BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zi = zipfile.ZipInfo("link", (2020, 1, 1, 0, 0, 0))
            zi.external_attr = 0o120777 << 16  # S_ISLNK
            zf.writestr(zi, "/etc/passwd")
        z = Path(d) / "evil.zip"
        z.write_bytes(buf.getvalue())
        try:
            dl.extract_skill_zip(z, Path(d) / "skills")
            assert False
        except dl.DownloadError as exc:
            assert "符号链接" in str(exc)


def test_extract_bomb_rejected():
    # 声明 2GB 单文件 > 1GB 膨胀上限：_validate_members 在提取前拒绝
    zi = zipfile.ZipInfo("big.bin", (2020, 1, 1, 0, 0, 0))
    zi.file_size = 2 * 1024 * 1024 * 1024
    try:
        dl._validate_members([zi])
        assert False
    except dl.DownloadError as exc:
        assert "过大" in str(exc)
    assert dl._validate_members([]) == []


def test_extract_reinstall_backup_old():
    with TemporaryDirectory() as d:
        skills = Path(d) / "skills"
        z = _write_zip(Path(d) / "skill.zip", {"a.txt": b"v1"})
        target, _ = dl.extract_skill_zip(z, skills)
        assert (target / "a.txt").read_bytes() == b"v1"
        # 重装：旧版备份为 .old，目标目录换新版
        z = _write_zip(Path(d) / "skill.zip", {"a.txt": b"v2"})
        target2, _ = dl.extract_skill_zip(z, skills)
        assert target2 == target
        assert (target / "a.txt").read_bytes() == b"v2"
        old = Path(d) / "skills" / "skill.old"
        assert old.is_dir() and (old / "a.txt").read_bytes() == b"v1"


def test_read_zip_text():
    with TemporaryDirectory() as d:
        z = _write_zip(Path(d) / "skill.zip", {
            "zhihu/manifest.json": b'{"version":"1"}',
            "big.txt": b"y" * 100000,
        })
        assert '"version"' in (dl.read_zip_text(z, "zhihu/manifest.json") or "")
        assert dl.read_zip_text(z, "missing.json") is None
        assert dl.read_zip_text(z, "big.txt", max_bytes=1024) is None
        assert dl.read_zip_text(Path(d) / "nope.zip", "x") is None


def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
        patch.restore()
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    _run_plain()
