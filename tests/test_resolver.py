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


# ---------------------------------------------------------------------------
# 路径安全 (萱儿复核项 4)
# ---------------------------------------------------------------------------


def test_dot_segments_rejected() -> None:
    """含 .. 的路径段一律不解析, 防越出挂载点.

    pathlib 构造时会归一化掉 ``.`` 段 (不构成越界), ``..`` 会保留在 parts 里,
    因此实际可越界的只有 ``..`` —— 它必须被拒绝.
    """
    settings = _settings(ALIST_115)
    assert _match_mount(settings, Path("/mnt/115/../outside/a.mp4")) is None
    assert _match_mount(settings, Path("/mnt/115/film/../../outside/a.mp4")) is None
    assert resolve_mount(ALIST_115, Path("/mnt/115/../outside/a.mp4")) is None
    # 正常路径不受影响
    assert _match_mount(settings, Path("/mnt/115/film/a.mp4")) is ALIST_115
    # pathlib 归一化后无 dot-segment 的路径照常解析
    assert _match_mount(settings, Path("/mnt/115/film/sub/a.mp4")) is ALIST_115


def test_sibling_prefix_not_matched() -> None:
    """/mnt/115x 不得匹配 /mnt/115 挂载."""
    settings = _settings(ALIST_115)
    assert _match_mount(settings, Path("/mnt/115x/a.mp4")) is None


def test_webdav_address_must_be_http() -> None:
    """WebDAV 地址必须是绝对 http(s): file:// 等拒绝 (萱儿复核项 4)."""
    bad = MountConfig(local_prefix="/m", backend="webdav", address="file:///tmp/dav")
    assert _webdav_target(bad, "/a.mp4") is None


def test_alist_non_http_url_rejected() -> None:
    """AList 返回的 URL 限绝对 http(s): file:/// concat: 等拒绝."""

    class _EvilHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload = json.dumps(
                {"code": 200, "data": {"raw_url": "file:///etc/passwd"}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _EvilHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        mount = MountConfig(
            local_prefix="/m", backend="alist",
            address=f"http://127.0.0.1:{httpd.server_address[1]}",
        )
        assert resolve_mount(mount, Path("/m/a.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 直链预检: 3xx 消化 + 同源凭据策略 (萱儿复核项 1 / 3)
# ---------------------------------------------------------------------------

from plugin import MountTarget, _verify_target  # noqa: E402


class _RedirectHandler(BaseHTTPRequestHandler):
    """可编程重定向/记录请求头的小型 HTTP 服务."""

    routes: dict = {}
    seen_headers: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_headers[self.path] = dict(self.headers)
        spec = type(self).routes.get(self.path)
        if spec is None:
            self.send_error(404)
            return
        if "redirect" in spec:
            self.send_response(302)
            self.send_header("Location", spec["redirect"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"x"
        self.send_response(spec.get("status", 206))
        self.send_header("Content-Type", spec.get("content_type", "video/mp4"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _start_server(handler_cls) -> str:
    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_verify_follows_same_origin_redirect_keeps_auth() -> None:
    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {
        "/dav/a.mp4": {"redirect": f"{base}/signed/a.mp4"},
        "/signed/a.mp4": {"status": 206, "content_type": "video/mp4"},
    }
    try:
        target = MountTarget(url=f"{base}/dav/a.mp4", headers={"Authorization": "Basic abc"})
        verified = _verify_target(target)
        assert verified is not None
        assert verified.url == f"{base}/signed/a.mp4"
        # 同源: 凭据保留
        assert verified.headers == {"Authorization": "Basic abc"}
        assert "Authorization" in H.seen_headers.get("/signed/a.mp4", {})
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_cross_origin_strips_credentials() -> None:
    """跨源 302: 目标站不得收到原 Authorization (萱儿复核项 3)."""
    class H(_RedirectHandler):
        pass

    httpd_b, base_b = _start_server(H)
    H.routes = {"/final/a.mp4": {"status": 206, "content_type": "video/mp4"}}

    class G(_RedirectHandler):
        pass

    httpd_a, base_a = _start_server(G)
    G.routes = {"/dav/a.mp4": {"redirect": f"{base_b}/final/a.mp4"}}
    try:
        target = MountTarget(url=f"{base_a}/dav/a.mp4", headers={"Authorization": "Basic secret"})
        verified = _verify_target(target)
        assert verified is not None
        assert verified.url == f"{base_b}/final/a.mp4"
        # 跨源: 凭据被剥离, 目标站没收到 Authorization
        assert verified.headers == {}
        assert "Authorization" not in H.seen_headers.get("/final/a.mp4", {})
    finally:
        httpd_a.shutdown()
        httpd_a.server_close()
        httpd_b.shutdown()
        httpd_b.server_close()


def test_verify_non_2xx_returns_none() -> None:
    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/missing.mp4": {"status": 404}}
    try:
        target = MountTarget(url=f"{base}/missing.mp4", headers={})
        assert _verify_target(target) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_error_page_content_type_rejected() -> None:
    """终态是 text/html 错误页: 判不可用 (调用方回退本机)."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/err.mp4": {"status": 200, "content_type": "text/html"}}
    try:
        target = MountTarget(url=f"{base}/err.mp4", headers={})
        assert _verify_target(target) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_hop_limit() -> None:
    """重定向超过上限: 判不可用, 不死循环."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    # 6 跳循环, 上限 5
    for i in range(7):
        H.routes[f"/r{i}"] = {"redirect": f"{base}/r{i + 1}"}
    try:
        target = MountTarget(url=f"{base}/r0", headers={})
        assert _verify_target(target) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 预检放行严格化 (萱儿复审 B1/B2)
# ---------------------------------------------------------------------------


def test_verify_rejects_204() -> None:
    """204 No Content 不是可播媒体: 拒绝 (B1)."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/empty.mp4": {"status": 204, "content_type": "video/mp4"}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/empty.mp4", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


class _NoContentTypeHandler(BaseHTTPRequestHandler):
    """可编程响应, 可显式省略 Content-Type (萱儿三审 B2 fail-closed 回归用)."""

    routes: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        spec = type(self).routes.get(self.path)
        if spec is None:
            self.send_error(404)
            return
        self.send_response(spec.get("status", 200))
        if "content_range" in spec:
            self.send_header("Content-Range", spec["content_range"])
        if spec.get("content_type") is not None:
            self.send_header("Content-Type", spec["content_type"])
        body = spec.get("body", b"ok")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def test_verify_rejects_missing_content_type_error_page() -> None:
    """无 Content-Type 的 200 + HTML 错误页: fail-closed 拒绝并回退本机 (萱儿三审 B2)."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 200, "body": b"<!doctype html><title>Error</title><h1>boom</h1>"}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_rejects_partial_without_content_type() -> None:
    """206 + Content-Range 但无 Content-Type: Range 预检路径同样 fail-closed 拒绝."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_range": "bytes 0-0/1048576", "body": b"x"}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("content_type", ["video/", "audio/", "video/; charset=binary", "VIDEO/", "  audio/  "])
def test_verify_rejects_empty_media_subtype(content_type: str) -> None:
    """video/、audio/ 空子类型 (含参数、大小写、空白变体): 拒绝 (萱儿三审 B2)."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 200, "content_type": content_type}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    "content_type",
    ["image/png", "application/problem+json", "text/html", "application/json"],
)
def test_verify_rejects_non_media_content_type(content_type: str) -> None:
    """非媒体 Content-Type (image/*、+json、text/*) 一律拒绝 (B2 白名单)."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": content_type}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_accepts_octet_stream_and_audio() -> None:
    """白名单内: video/* / audio/* / application/octet-stream 放行."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {
        "/a": {"status": 200, "content_type": "application/octet-stream"},
        "/b": {"status": 206, "content_type": "audio/mp4"},
    }
    try:
        assert _verify_target(MountTarget(url=f"{base}/a", headers={})) is not None
        assert _verify_target(MountTarget(url=f"{base}/b", headers={})) is not None
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# AList API 跨源凭据回归 (萱儿复审 A1)
# ---------------------------------------------------------------------------


def test_alist_api_cross_origin_strips_authorization() -> None:
    """POST /api/fs/get 跨源 302: 目标站不得收到 AList 令牌 (A1)."""

    class _Recorder(BaseHTTPRequestHandler):
        seen_headers: dict = {}

        def do_GET(self) -> None:  # noqa: N802
            type(self).seen_headers = dict(self.headers)
            payload = json.dumps(
                {"code": 200, "data": {"raw_url": "https://cdn.example.com/v.mp4"}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    import socketserver

    recorder = socketserver.TCPServer(("127.0.0.1", 0), _Recorder)
    recorder.timeout = 5
    threading.Thread(target=recorder.serve_forever, daemon=True).start()
    recorder_base = f"http://127.0.0.1:{recorder.server_address[1]}"

    class _AlistRedirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"{recorder_base}/fs/get")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    alist = socketserver.TCPServer(("127.0.0.1", 0), _AlistRedirect)
    alist.timeout = 5
    threading.Thread(target=alist.serve_forever, daemon=True).start()
    try:
        mount = MountConfig(
            local_prefix="/m",
            backend="alist",
            address=f"http://127.0.0.1:{alist.server_address[1]}",
            token="DUMMY_ALIST_SECRET",
        )
        url = resolve_mount(mount, Path("/m/a.mp4"))
        # 跨源 302 后仍拿到 dlink (凭据已剥离, 目标站正常返回 JSON)
        assert url is not None
        assert url.url == "https://cdn.example.com/v.mp4"
        assert "Authorization" not in _Recorder.seen_headers
    finally:
        alist.shutdown()
        alist.server_close()
        recorder.shutdown()
        recorder.server_close()
