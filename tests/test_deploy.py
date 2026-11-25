"""ffmpeg 自动部署安全门禁测试 (安全要求)."""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from plugin import PluginSettings, _deploy_ffmpeg, _fetch_bundle


def test_download_disabled_by_default(tmp_path) -> None:
    """默认关闭自动下载: 即便配了下载地址也直接返回 None, 不发网络请求."""
    settings = PluginSettings(ffmpeg_download_url="http://127.0.0.1:1/ffmpeg.tar.xz")
    assert settings.allow_ffmpeg_download is False
    assert _deploy_ffmpeg(settings, tmp_path) is None


def test_download_requires_sha256(tmp_path) -> None:
    settings = PluginSettings(
        allow_ffmpeg_download=True,
        ffmpeg_download_url="http://127.0.0.1:1/ffmpeg.tar.xz",
        ffmpeg_download_sha256="",
    )
    assert _deploy_ffmpeg(settings, tmp_path) is None


def test_download_rejects_malformed_sha256(tmp_path) -> None:
    settings = PluginSettings(
        allow_ffmpeg_download=True,
        ffmpeg_download_url="http://127.0.0.1:1/ffmpeg.tar.xz",
        ffmpeg_download_sha256="not-a-hash",
    )
    assert _deploy_ffmpeg(settings, tmp_path) is None
    settings.ffmpeg_download_sha256 = "abc123"  # 不足 64 位
    assert _deploy_ffmpeg(settings, tmp_path) is None


def test_download_unreachable_fails_closed(tmp_path) -> None:
    """opt-in + 合法哈希但下载失败: 静默返回 None, 不留半成品."""
    settings = PluginSettings(
        allow_ffmpeg_download=True,
        ffmpeg_download_url="http://127.0.0.1:1/ffmpeg.tar.xz",
        ffmpeg_download_sha256="0" * 64,
    )
    assert _deploy_ffmpeg(settings, tmp_path) is None
    leftovers = list((tmp_path / "downloads").glob("*.tmp")) if (tmp_path / "downloads").exists() else []
    assert leftovers == []


# ---------------------------------------------------------------------------
# C3: 哈希正反用例 —— 错哈希删除, 正确哈希才落盘
# ---------------------------------------------------------------------------

_PAYLOAD = b"fake-ffmpeg-bundle-bytes"
_GOOD_SHA = hashlib.sha256(_PAYLOAD).hexdigest()


class _StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def bundle_server():
    import socketserver

    httpd = socketserver.TCPServer(("127.0.0.1", 0), _StaticHandler)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/ffmpeg.tar.xz"
    httpd.shutdown()
    httpd.server_close()


def test_fetch_bundle_rejects_wrong_hash(bundle_server, tmp_path) -> None:
    """哈希不匹配: 返回 None 且不留文件 (C3 反例)."""
    bad_sha = "f" * 64
    assert _fetch_bundle(bundle_server, tmp_path, bad_sha) is None
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_fetch_bundle_accepts_correct_hash(bundle_server, tmp_path) -> None:
    """哈希匹配: 返回落盘路径, 内容与源一致 (C3 正例)."""
    local = _fetch_bundle(bundle_server, tmp_path, _GOOD_SHA)
    assert local is not None
    assert local.read_bytes() == _PAYLOAD
    local.unlink()
