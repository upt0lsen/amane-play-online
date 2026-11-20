"""挂载解析器测试: 前缀匹配 / alist 现签 / WebDAV 拼地址 / 回退语义."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from plugin import (
    MountConfig,
    PluginSettings,
    _match_mount,
    _remote_path,
    _webdav_target,
    resolve_mount,
)


def _settings(*mounts: MountConfig) -> PluginSettings:
    return PluginSettings(mounts=list(mounts))


ALIST_115 = MountConfig(
    local_prefix="/mnt/115",
    backend="alist",
    address="http://alist:5244",
    remote_prefix="/115",
    token="alist-token",
)
ALIST_QUARK = MountConfig(
    local_prefix="/mnt/quark",
    backend="alist",
    address="http://alist:5244",
    remote_prefix="/quark",
    token="alist-token",
)
DAV = MountConfig(
    local_prefix="/mnt/dav",
    backend="webdav",
    address="https://dav.example.com/dav",
    remote_prefix="/media",
    username="uk",
    token="dav-pass",
)


def test_match_mount_longest_prefix() -> None:
    settings = _settings(ALIST_115, MountConfig(local_prefix="/mnt/115/sub", backend="alist", address="http://a"))
    hit = _match_mount(settings, Path("/mnt/115/sub/film/a.mp4"))
    assert hit is not None and hit.local_prefix == "/mnt/115/sub"
    hit = _match_mount(settings, Path("/mnt/115/film/b.mp4"))
    assert hit is not None and hit.local_prefix == "/mnt/115"
    assert _match_mount(settings, Path("/mnt/other/c.mp4")) is None
    # 空 mounts 不配
    assert _match_mount(PluginSettings(), Path("/mnt/115/a.mp4")) is None


def test_remote_path_join() -> None:
    assert _remote_path(ALIST_115, Path("/mnt/115/film/a.mp4")) == "/115/film/a.mp4"
    assert _remote_path(ALIST_QUARK, Path("/mnt/quark/a.mkv")) == "/quark/a.mkv"
    assert _remote_path(DAV, Path("/mnt/dav/x y/a.mp4")) == "/media/x y/a.mp4"


def test_webdav_target_quoting_and_auth() -> None:
    target = _webdav_target(DAV, "/media/x y/a.mp4")
    assert target is not None
    assert target.url == "https://dav.example.com/dav/media/x%20y/a.mp4"
    assert target.headers["Authorization"].startswith("Basic ")
    # 无凭据不带认证头
    plain = MountConfig(local_prefix="/m", backend="webdav", address="https://d/dav")
    target = _webdav_target(plain, "/a.mp4")
    assert target is not None and target.headers == {}


class _AlistHandler(BaseHTTPRequestHandler):
    """模拟 AList /api/fs/get: 记录请求, 返回固定 raw_url."""

    last_request: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - http.server 命名
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).last_request = {
            "path": body.get("path"),
            "authorization": self.headers.get("Authorization"),
        }
        payload = json.dumps({"code": 200, "data": {"raw_url": "https://cdn.115.com/signed?sign=abc"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def alist_server():
    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _AlistHandler)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_alist_dlink_live_sign(alist_server: str) -> None:
    mount = MountConfig(
        local_prefix="/mnt/115", backend="alist", address=alist_server,
        remote_prefix="/115", token="secret-token",
    )
    target = resolve_mount(mount, Path("/mnt/115/film/a.mp4"))
    assert target is not None
    assert target.url == "https://cdn.115.com/signed?sign=abc"
    assert target.headers == {}
    # 请求路径与鉴权头正确
    assert _AlistHandler.last_request["path"] == "/115/film/a.mp4"
    assert _AlistHandler.last_request["authorization"] == "secret-token"


def test_alist_failure_falls_back(alist_server: str) -> None:
    """AList 返回非 200 -> None (调用方回退本机路径)."""

    class _BadHandler(_AlistHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload = json.dumps({"code": 500, "message": "fail"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _BadHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        mount = MountConfig(local_prefix="/m", backend="alist", address=f"http://127.0.0.1:{httpd.server_address[1]}")
        assert resolve_mount(mount, Path("/m/a.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_alist_unreachable_returns_none() -> None:
    mount = MountConfig(local_prefix="/m", backend="alist", address="http://127.0.0.1:1")
    assert resolve_mount(mount, Path("/m/a.mp4")) is None


def test_backend_local_or_unknown_returns_none() -> None:
    # 未配置挂载: 无匹配即 None
    assert _match_mount(PluginSettings(), Path("/anywhere/a.mp4")) is None
