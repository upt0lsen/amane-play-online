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
    username="user",
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
        payload = json.dumps({"code": 200, "data": {"raw_url": "https://cdn.example.com/signed?sign=abc"}}).encode()
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
    assert target.url == "https://cdn.example.com/signed?sign=abc"
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
# 路径安全 (安全要求)
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
    """WebDAV 地址必须是绝对 http(s): file:// 等拒绝 (安全要求)."""
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
# 直链预检: 3xx 消化 + 同源凭据策略 (安全要求)
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
    """跨源 302: 目标站不得收到原 Authorization (安全要求)."""
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
# 预检放行严格化
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
    """可编程响应, 可显式省略 Content-Type (fail-closed 回归用)."""

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
    """无 Content-Type 的 200 + HTML 错误页: fail-closed 拒绝并回退本机."""

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


@pytest.mark.parametrize(
    "content_type",
    [
        # 空子类型
        "video/", "audio/", "video/; charset=binary", "VIDEO/", "  audio/  ",
        # 畸形 subtype 必须全部拒绝
        "video//",          # 额外斜杠
        "video/mp4/extra",  # 多余路径段
        "video/ mp4",       # subtype 内空白
        "video/mp4,text/html",  # 列表逗号
        "video/*",          # 通配符
        "audio/mp4/extra",  # 多余路径段 (audio)
    ],
)
def test_verify_rejects_malformed_media_subtype(content_type: str) -> None:
    """空 / 畸形 subtype (斜杠、空白、逗号、通配符): 全部拒绝 ."""

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
    [
        # 合法值必须继续放行
        "video/x-matroska",
        "video/mp4;codecs=avc1.640028",
        "video/mp4; codecs=avc1.42E01E,mp4a.40.2",
        "audio/mp4; charset=binary",
        "VIDEO/MP4",
        "video/quicktime",
    ],
)
def test_verify_accepts_valid_media_subtype(content_type: str) -> None:
    """合法 subtype (含参数、大小写): 继续放行 ."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": content_type}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is not None
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


def test_verify_octet_stream_split_by_consumer() -> None:
    """application/octet-stream 按消费者分流: 直连 (经主机反代) 拒绝回退本机, 转码拉流放行."""

    class H(_RedirectHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {
        "/a": {"status": 200, "content_type": "application/octet-stream"},
        "/b": {"status": 206, "content_type": "audio/mp4"},
    }
    try:
        # 直连 (direct=True 默认): 代理只转发 video/*/audio/*, octet-stream 预检放过也会 502
        assert _verify_target(MountTarget(url=f"{base}/a", headers={})) is None
        # 转码 (direct=False): ffmpeg 拉流不挑 Content-Type, 放行
        assert _verify_target(MountTarget(url=f"{base}/a", headers={}), direct=False) is not None
        # audio/* 两种消费都放行
        assert _verify_target(MountTarget(url=f"{base}/b", headers={})) is not None
        assert _verify_target(MountTarget(url=f"{base}/b", headers={}), direct=False) is not None
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# AList API 跨源凭据回归
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


# ---------------------------------------------------------------------------
# 凭据环境变量注入 (设计决策): config 只存非敏感项, 敏感凭据优先容器 env
# ---------------------------------------------------------------------------


def test_env_var_suffix() -> None:
    from plugin import _env_var_suffix

    assert _env_var_suffix("/media/mycloud") == "MEDIA_MYCLOUD"
    assert _env_var_suffix("/mnt/quark") == "MNT_QUARK"
    assert _env_var_suffix("/media/115-x") == "MEDIA_115_X"


def test_alist_env_token_overrides_config(alist_server: str, monkeypatch) -> None:
    monkeypatch.setenv("PLAYONLINE_TOKEN_MNT_115", "env-secret")
    mount = MountConfig(
        local_prefix="/mnt/115", backend="alist", address=alist_server,
        remote_prefix="/115", token="config-token",
    )
    target = resolve_mount(mount, Path("/mnt/115/film/a.mp4"))
    assert target is not None
    assert _AlistHandler.last_request["authorization"] == "env-secret"


def test_alist_config_token_fallback(alist_server: str, monkeypatch) -> None:
    monkeypatch.delenv("PLAYONLINE_TOKEN_MNT_115", raising=False)
    mount = MountConfig(
        local_prefix="/mnt/115", backend="alist", address=alist_server,
        remote_prefix="/115", token="config-token",
    )
    target = resolve_mount(mount, Path("/mnt/115/film/a.mp4"))
    assert target is not None
    assert _AlistHandler.last_request["authorization"] == "config-token"


def test_alist_empty_token_returns_none(monkeypatch) -> None:
    """env 与 config 都无令牌: fail-closed, 解析直接失败回退本机 (不空 Authorization 头)."""
    monkeypatch.delenv("PLAYONLINE_TOKEN_MNT_115", raising=False)
    mount = MountConfig(local_prefix="/mnt/115", backend="alist", address="http://127.0.0.1:1", remote_prefix="/115")
    assert resolve_mount(mount, Path("/mnt/115/a.mp4")) is None


def test_webdav_env_credentials_override(monkeypatch) -> None:
    import base64

    monkeypatch.setenv("PLAYONLINE_WEBDAV_USER_MNT_DAV", "envuser")
    monkeypatch.setenv("PLAYONLINE_WEBDAV_PASS_MNT_DAV", "envpass")
    mount = MountConfig(
        local_prefix="/mnt/dav", backend="webdav", address="https://dav.example.com/dav",
        remote_prefix="/media", username="user", token="dav-pass",
    )
    target = resolve_mount(mount, Path("/mnt/dav/x.mp4"))
    assert target is not None
    expected = "Basic " + base64.b64encode(b"envuser:envpass").decode("ascii")
    assert target.headers["Authorization"] == expected


# ---------------------------------------------------------------------------
# CloudDrive2 gRPC 直链后端
# ---------------------------------------------------------------------------

CD2_DIRECT_URL = "https://cdn.example.com/abc/sample.mp4?t=1&k=deadbeef"


class _Cd2Handler(BaseHTTPRequestHandler):
    """模拟 CD2 gRPC-web: 解析请求帧, 按类属性返回 DownloadUrlPathInfo."""

    seen: dict = {}
    response_fields: list = []
    grpc_status: int = 0

    def do_POST(self) -> None:  # noqa: N802
        import struct as _struct

        from plugin import _pb_decode_fields

        type(self).seen["calls"] = type(self).seen.get("calls", 0) + 1
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).seen["authorization"] = self.headers.get("Authorization", "")
        type(self).seen["content_type"] = self.headers.get("Content-Type", "")
        msg_len = _struct.unpack(">I", body[1:5])[0]
        msg = body[5 : 5 + msg_len]
        fields = _pb_decode_fields(msg)
        type(self).seen["path"] = fields.get(1, [(2, b"")])[0][1].decode()
        type(self).seen["get_direct_url"] = fields.get(4, [(0, 0)])[0][1]
        payload = b"".join(type(self).response_fields)
        frame = b"\x00" + _struct.pack(">I", len(payload)) + payload
        trailer_text = f"grpc-status: {type(self).grpc_status}\r\n\r\n".encode()
        trailer = b"\x80" + _struct.pack(">I", len(trailer_text)) + trailer_text
        out = frame + trailer
        self.send_response(200)
        self.send_header("Content-Type", "application/grpc-web+proto")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:
        pass


def _cd2_mount(base: str, token: str = "cd2-token") -> MountConfig:
    return MountConfig(
        local_prefix="/mnt/115", backend="clouddrive2", address=base,
        remote_prefix="/mycloud", token=token,
    )


def _cd2_server(response_fields: list, grpc_status: int = 0):
    from plugin import _pb_string  # noqa: F401 - 确保可用

    class H(_Cd2Handler):
        pass

    H.seen = {}
    H.response_fields = response_fields
    H.grpc_status = grpc_status
    return _start_server(H), H


def test_cd2_direct_url_resolved() -> None:
    from plugin import _pb_len_delim, _pb_string

    (httpd, base), H = _cd2_server([
        _pb_string(1, "/static/{SCHEME}/{HOST}/{PREVIEW}/x.mp4?token=abc"),
        _pb_string(3, CD2_DIRECT_URL),
        _pb_string(4, "CloudDrive2/1.0"),
        _pb_len_delim(5, _pb_string(1, "Referer") + _pb_string(2, "https://example.com/")),
    ])
    try:
        target = resolve_mount(_cd2_mount(base), Path("/mnt/115/云下载/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert target.url == CD2_DIRECT_URL
    assert target.headers["User-Agent"] == "CloudDrive2/1.0"
    assert target.headers["Referer"] == "https://example.com/"
    assert H.seen["authorization"] == "Bearer cd2-token"
    assert H.seen["path"] == "/mycloud/云下载/x.mp4"
    assert H.seen["get_direct_url"] == 1


def test_cd2_no_direct_url_returns_none() -> None:
    """只回 downloadUrlPath (无 directUrl): 不静默降级 CD2 代理, 回退本机."""
    from plugin import _pb_string

    (httpd, base), H = _cd2_server([
        _pb_string(1, "/static/{SCHEME}/{HOST}/{PREVIEW}/x.mp4?token=abc"),
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/云下载/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_grpc_error_returns_none() -> None:
    (httpd, base), H = _cd2_server([], grpc_status=16)  # UNAUTHENTICATED
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/云下载/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_unreachable_returns_none() -> None:
    mount = _cd2_mount("http://127.0.0.1:1")
    assert resolve_mount(mount, Path("/mnt/115/云下载/x.mp4")) is None


def test_cd2_no_token_fail_closed(monkeypatch) -> None:
    """env 与 config 都无令牌: 不发任何请求, 直接回退本机."""
    from plugin import _pb_string

    monkeypatch.delenv("PLAYONLINE_CD2_TOKEN_MNT_115", raising=False)
    (httpd, base), H = _cd2_server([_pb_string(3, CD2_DIRECT_URL)])
    try:
        assert resolve_mount(_cd2_mount(base, token=""), Path("/mnt/115/云下载/x.mp4")) is None
        assert H.seen.get("calls", 0) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_env_token_overrides_config(monkeypatch) -> None:
    from plugin import _pb_string

    monkeypatch.setenv("PLAYONLINE_CD2_TOKEN_MNT_115", "env-cd2-token")
    (httpd, base), H = _cd2_server([_pb_string(3, CD2_DIRECT_URL)])
    try:
        target = resolve_mount(_cd2_mount(base, token="config-token"), Path("/mnt/115/云下载/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert H.seen["authorization"] == "Bearer env-cd2-token"


def test_headers_args_splits_user_agent() -> None:
    """ffmpeg 输入参数: User-Agent 走 -user_agent 专用选项, 其余头留 -headers (115 强制校验 UA)."""
    from plugin import _headers_args

    assert _headers_args(None) == []
    assert _headers_args({}) == []
    args = _headers_args({"User-Agent": "CloudDrive2/1.2.0-beta (Linux-X64)", "Referer": "https://example.com/"})
    assert args[:2] == ["-user_agent", "CloudDrive2/1.2.0-beta (Linux-X64)"]
    assert args[2] == "-headers"
    assert "Referer: https://example.com/\r\n" in args[3]
    assert "User-Agent" not in args[3]
    # 仅 UA 时无 -headers
    assert _headers_args({"user-agent": "x"}) == ["-user_agent", "x"]


# ---------------------------------------------------------------------------
# gRPC 解析 fail-closed 对抗用例
# ---------------------------------------------------------------------------


class _Cd2RawHandler(BaseHTTPRequestHandler):
    """返回原始字节应答 (对抗用例: 缺 trailer / 截断帧 / 未知 flag / 错 Content-Type)."""

    raw: bytes = b""
    content_type: str = "application/grpc-web+proto"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(type(self).raw)))
        self.end_headers()
        self.wfile.write(type(self).raw)

    def log_message(self, *args) -> None:
        pass


def _cd2_raw_server(raw: bytes, content_type: str = "application/grpc-web+proto"):
    class H(_Cd2RawHandler):
        pass

    H.raw = raw
    H.content_type = content_type
    return _start_server(H)


def _frame(flag: int, payload: bytes) -> bytes:
    import struct

    return bytes([flag]) + struct.pack(">I", len(payload)) + payload


def _trailer(status: int | None = 0) -> bytes:
    text = "grpc-message: x\r\n\r\n" if status is None else f"grpc-status: {status}\r\n\r\n"
    return _frame(0x80, text.encode())


def _data_payload() -> bytes:
    from plugin import _pb_string

    return _pb_string(3, CD2_DIRECT_URL)


def test_cd2_missing_trailer_rejected() -> None:
    """只有 data 帧没有 trailer: grpc-status 缺失, 不得当成功."""
    (httpd, base) = _cd2_raw_server(_frame(0x00, _data_payload()))
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_trailer_without_status_rejected() -> None:
    """trailer 帧里没有 grpc-status 行: fail-closed."""
    (httpd, base) = _cd2_raw_server(_frame(0x00, _data_payload()) + _trailer(None))
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_truncated_frame_rejected() -> None:
    """帧声明长度超出实际正文: 截断帧必须拒绝 (Python 切片会静默截断)."""
    import struct

    payload = _data_payload()
    raw = b"\x00" + struct.pack(">I", len(payload) + 50) + payload
    (httpd, base) = _cd2_raw_server(raw + _trailer(0))
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_unknown_flag_rejected() -> None:
    """未知帧 flag (如压缩帧 0x01): 不支持即拒绝."""
    (httpd, base) = _cd2_raw_server(_frame(0x01, b"abcd") + _trailer(0))
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_trailing_garbage_rejected() -> None:
    """帧序列后有残余字节 (不足帧头): 拒绝."""
    (httpd, base) = _cd2_raw_server(_frame(0x00, _data_payload()) + _trailer(0) + b"\x00\x00\x00")
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_wrong_content_type_rejected() -> None:
    """应答 Content-Type 不是 grpc-web: 拒绝."""
    (httpd, base) = _cd2_raw_server(
        _frame(0x00, _data_payload()) + _trailer(0), content_type="text/plain",
    )
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_oversized_body_rejected(monkeypatch) -> None:
    """应答体超过上限: 拒绝 (防异常对端撑爆内存)."""
    import plugin

    monkeypatch.setattr(plugin, "_GRPC_MAX_BODY", 16)
    (httpd, base) = _cd2_raw_server(_frame(0x00, _data_payload()) + _trailer(0))
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_expires_in_and_untrusted_headers() -> None:
    """expiresIn (field 2 varint) 解析为有效期; additionalHeaders 置不可信标记."""
    from plugin import _pb_len_delim, _pb_string, _pb_varint

    expires = _pb_varint((2 << 3) | 0) + _pb_varint(3583)
    (httpd, base), _ = _cd2_server([
        expires,
        _pb_string(3, CD2_DIRECT_URL),
        _pb_string(4, "CloudDrive2/1.0"),
        _pb_len_delim(5, _pb_string(1, "Referer") + _pb_string(2, "https://example.com/")),
    ])
    try:
        target = resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert target.expires_in == 3583.0
    assert target.headers_untrusted is True  # 带 additionalHeaders

    (httpd, base), _ = _cd2_server([_pb_string(3, CD2_DIRECT_URL)])
    try:
        target = resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert target.expires_in is None
    assert target.headers_untrusted is False


# ---------------------------------------------------------------------------
# 直链续签 (expiresIn 消费)
# ---------------------------------------------------------------------------


def _source_ctx(**kw):
    from unittest.mock import Mock

    from plugin import SourceContext

    base = dict(
        token="t", cache_root=Path("/tmp/x"), url="https://cdn/x?t=1",
        headers={"User-Agent": "ua"}, local_path=Path("/media/mycloud/x.mkv"),
        codecs=None, duration=10.0, seconds=4.0, settings=Mock(), tools=Mock(),
        subtitles=(), subtitle_sources={},
    )
    base.update(kw)
    return SourceContext(**base)


def test_seek_after_expiry_renews_url() -> None:
    """过期后 seek (重起转): make_command 前置续签换新签名 URL."""
    import time

    from plugin import _refresh_source_url

    calls = []

    def refresher():
        calls.append(1)
        return ("https://cdn/new?t=2", {"User-Agent": "ua2"}, 3600.0)

    ctx = _source_ctx(refresher=refresher, url_expires_at=time.monotonic() + 10)  # 临期
    _refresh_source_url(ctx)
    assert calls == [1]
    assert ctx.url == "https://cdn/new?t=2"
    assert ctx.headers == {"User-Agent": "ua2"}
    assert ctx.url_expires_at > time.monotonic() + 3000


def test_refresh_skipped_when_fresh() -> None:
    """剩余有效期高于提前量: 不续签."""
    import time

    from plugin import _refresh_source_url

    def refresher():  # pragma: no cover - 不应被调用
        raise AssertionError("不应续签")

    ctx = _source_ctx(refresher=refresher, url_expires_at=time.monotonic() + 3600)
    _refresh_source_url(ctx)
    assert ctx.url == "https://cdn/x?t=1"


def test_refresh_forced_on_cdn_reject() -> None:
    """起转秒退 (疑似 401/403): force_refresh 无视剩余有效期强制续签."""
    import time

    from plugin import _refresh_source_url

    ctx = _source_ctx(
        refresher=lambda: ("https://cdn/new?t=3", {}, 3600.0),
        url_expires_at=time.monotonic() + 3600,
        force_refresh=True,
    )
    _refresh_source_url(ctx)
    assert ctx.url == "https://cdn/new?t=3"
    assert ctx.force_refresh is False


def test_refresh_failure_falls_back_local() -> None:
    """续签失败 (返 None): 回退本机路径, 且不再重试."""
    import time

    from plugin import _refresh_source_url

    ctx = _source_ctx(refresher=lambda: None, url_expires_at=time.monotonic() + 10)
    _refresh_source_url(ctx)
    assert ctx.url == str(ctx.local_path)
    assert ctx.headers == {}
    assert ctx.refresher is None
    assert ctx.url_expires_at is None


def test_refresh_exception_falls_back_local() -> None:
    """续签抛异常: 同样回退本机, 不炸播放."""
    import time

    from plugin import _refresh_source_url

    def boom():
        raise RuntimeError("cd2 down")

    ctx = _source_ctx(refresher=boom, url_expires_at=time.monotonic() + 10)
    _refresh_source_url(ctx)
    assert ctx.url == str(ctx.local_path)
    assert ctx.refresher is None


# ---------------------------------------------------------------------------
# P0 第 2 轮: 元数据保留 / protobuf 严格校验 / 媒体 token 严格校验 (跨函数回归)
# ---------------------------------------------------------------------------


def test_verify_target_preserves_expires_in_and_untrusted_flag() -> None:
    """真实预检之后必须保留 expires_in 与 headers_untrusted (P0 回归)."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": "video/mp4"}}
    try:
        target = MountTarget(
            url=f"{base}/x",
            headers={"User-Agent": "CloudDrive2/1.0"},
            expires_in=120.0,
            headers_untrusted=True,
        )
        verified = _verify_target(target, direct=False)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert verified is not None
    assert verified.expires_in == 120.0
    assert verified.headers_untrusted is True
    assert verified.headers["User-Agent"] == "CloudDrive2/1.0"


def test_verify_target_preserves_metadata_defaults() -> None:
    """无 expires_in / 可信头时预检后仍为默认 (不凭空造值)."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": "video/mp4"}}
    try:
        verified = _verify_target(MountTarget(url=f"{base}/x", headers={}))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert verified is not None
    assert verified.expires_in is None
    assert verified.headers_untrusted is False


def test_cd2_rejects_field_number_zero() -> None:
    """非法 field number 0: _pb_decode_fields 必须拒绝."""
    from plugin import _pb_string, _pb_varint

    (httpd, base), H = _cd2_server([
        _pb_varint((0 << 3) | 2) + _pb_varint(1) + b"x",  # field_no=0 wire=2
        _pb_string(3, CD2_DIRECT_URL),
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_rejects_expires_in_wrong_wire() -> None:
    """expiresIn 以 len-delim 出现 (错 wire): fail-closed, 不当成 None."""
    from plugin import _pb_len_delim, _pb_string

    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_len_delim(2, b"120"),  # 应为 varint
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_rejects_corrupt_additional_headers() -> None:
    """损坏的嵌套 additionalHeaders: 整体拒绝, 不静默忽略."""
    from plugin import _pb_len_delim, _pb_string

    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_len_delim(5, b"\xff\xff\xff"),  # 无法解码的嵌套
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_rejects_non_utf8_header() -> None:
    """additionalHeaders 的 value 非合法 UTF-8: fail-closed."""
    from plugin import _pb_len_delim, _pb_string

    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_len_delim(5, _pb_string(1, "X-Test") + _pb_len_delim(2, b"\xff\xfe")),
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_valid_expires_in_preserved() -> None:
    """合法 expiresIn (varint 正整数) 保留到 MountTarget.expires_in."""
    from plugin import _pb_string, _pb_varint

    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_varint((2 << 3) | 0) + _pb_varint(3583),
    ])
    try:
        target = resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert target.expires_in == 3583.0


@pytest.mark.parametrize(
    "content_type",
    [
        "video/mp4?bad",
        "video/(mp4)",
        "video/mp4@bad",
        "video/mp4;bad",     # 畸形参数 (无 =)
        "video/mp4;=x",      # 空参数名
        "audio/ogg?x=1",
    ],
)
def test_verify_rejects_malformed_media_token_or_param(content_type: str) -> None:
    """畸形 subtype token (? ( ) @) 与畸形参数: 全部拒绝."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": content_type}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_origin_of_rejects_userinfo() -> None:
    """_origin_of 拒绝 user:pass@host 内嵌凭据形态."""
    from plugin import _origin_of

    assert _origin_of("http://user:pass@example.com/x.mp4") is None
    assert _origin_of("http://user@example.com/x.mp4") is None
    assert _origin_of("https://example.com/x.mp4") == "https://example.com:443"


def test_resolve_mount_rejects_raw_dot_slash() -> None:
    """resolve_mount 归一化后无法检出原始 ./; 但 Path 归一化会把 ./a 变 a ——
    这里验证 _has_dot_segments_raw 能检出原始字符串形态."""
    from plugin import _has_dot_segments_raw

    assert _has_dot_segments_raw("/media/x/./y.mp4")
    assert _has_dot_segments_raw("./y.mp4")
    assert _has_dot_segments_raw("/media/x/../y.mp4")
    assert not _has_dot_segments_raw("/media/x/y.mp4")


def test_cd2_rejects_varint_over_ten_bytes() -> None:
    """expiresIn 编码超过 10 字节 (如 1<<1024): fail-closed, 不得抛 OverflowError."""
    from plugin import _pb_string, _pb_varint

    huge = b"\xff" * 147 + b"\x01"  # 148 字节 varint, 远超 protobuf 10 字节上限
    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_varint((2 << 3) | 0) + huge,
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_rejects_varint_tenth_byte_overflow() -> None:
    """第 10 字节值位超出 bit63 (64-bit 溢出): fail-closed."""
    from plugin import _pb_string, _pb_varint

    ten_bytes = b"\xff" * 9 + b"\x02"  # 第 10 字节值位 > 1
    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_varint((2 << 3) | 0) + ten_bytes,
    ])
    try:
        assert resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cd2_max_uint64_expires_in_ok() -> None:
    """合法 10 字节 varint (2^64-1): 正常解析且不抛异常."""
    from plugin import _pb_string, _pb_varint

    (httpd, base), H = _cd2_server([
        _pb_string(3, CD2_DIRECT_URL),
        _pb_varint((2 << 3) | 0) + b"\xff" * 9 + b"\x01",
    ])
    try:
        target = resolve_mount(_cd2_mount(base), Path("/mnt/115/x.mp4"))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert target is not None
    assert target.expires_in == float(2**64 - 1)


@pytest.mark.parametrize(
    "content_type",
    [
        'video/mp4; x="a"b"',   # 引号内裸双引号
        'video/mp4; x="a\rb"',  # 引号内 CR 控制字符
        'video/mp4; x="a\\"',   # 不完整转义 (反斜杠收尾)
        'video/mp4; x="abc',     # 未闭合引号
    ],
)
def test_verify_rejects_malformed_quoted_string(content_type: str) -> None:
    """quoted-string 严格校验: 裸引号/控制字符/残缺转义/未闭合一律拒绝."""

    class H(_NoContentTypeHandler):
        pass

    httpd, base = _start_server(H)
    H.routes = {"/x": {"status": 206, "content_type": content_type}}
    try:
        assert _verify_target(MountTarget(url=f"{base}/x", headers={})) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_verify_accepts_valid_quoted_string() -> None:
    """合法 quoted-string 参数 (含转义与逗号) 仍放行."""
    from plugin import _is_valid_media_param

    assert _is_valid_media_param('codecs="avc1.42E01E,mp4a.40.2"')
    assert _is_valid_media_param('x="a\\"b"')
    assert _is_valid_media_param('x=""')
