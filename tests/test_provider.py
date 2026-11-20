"""OnlineProvider 双通路判定与取流测试.

C1: 打桩只到 _mount_target (解析层), _verified_target 真实预检跑通 (本地 HTTP 服务).
A2/C2: 带凭据直链不交给 ffmpeg 的决策与真实 ffmpeg 回归.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from conftest import (
    FailureReason,
    FilePlaybackTarget,
    HlsPlaybackTarget,
    PlaybackMediaFile,
    PlaybackQuery,
    PluginContext,
    SourceError,
    UpstreamPlaybackTarget,
)

import plugin as plugin_module
from plugin import (
    MediaCodecs,
    MountConfig,
    MountTarget,
    OnlineProvider,
    PluginSettings,
    Tools,
)

H264_AAC = MediaCodecs("h264", 1080, "aac", 3600.0, fps=25.0)
HEVC_AAC = MediaCodecs("hevc", 1080, "aac", 3600.0, fps=25.0)

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# 本地媒体 HTTP 服务 (真实预检目标)
# ---------------------------------------------------------------------------


class _MediaHandler(BaseHTTPRequestHandler):
    """路由可编程: {path: {"status":, "content_type":, "redirect":}}; 记录收到的请求头."""

    routes: dict = {}
    seen: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen[self.path] = dict(self.headers)
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
        content_type = spec.get("content_type", "video/mp4")
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def media_server():
    import socketserver

    class H(_MediaHandler):
        pass

    H.routes = {}
    H.seen = {}
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield H, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


class _StubProvider(OnlineProvider):
    """只打桩解析层与探测层; 预检 (_verified_target) 走真实实现 (C1)."""

    def __init__(self, context, settings, codecs_map, mount_url, mount_headers=None):
        super().__init__(context, settings)
        self._stub_codecs = codecs_map
        self._stub_url = mount_url
        self._stub_headers = dict(mount_headers or {})

    async def _resolve_tools(self):
        if self._tools_ready:
            return self._tools
        return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")

    async def _codecs(self, path, tools):
        return self._stub_codecs.get(str(path))

    async def _mount_target(self, path):
        if self._stub_url is None:
            return None
        return MountTarget(url=self._stub_url, headers=dict(self._stub_headers))


def _make_provider(
    tmp_path: Path,
    *,
    mount_url: str | None,
    mount_headers: dict | None = None,
    allow_direct: bool = True,
    direct_play: bool = True,
    codecs_map: dict | None = None,
) -> _StubProvider:
    mount = MountConfig(
        local_prefix=str(tmp_path),
        backend="alist",
        address="http://alist:5244",
        remote_prefix="/115",
        token="t",
        allow_direct=allow_direct,
    )
    settings = PluginSettings(mounts=[mount], direct_play=direct_play)
    context = PluginContext(
        source_id="upt0lsen.play-online",
        http_client=None,
        web_client=None,
        data_dir=tmp_path / "plugin-data",
    )
    return _StubProvider(context, settings, codecs_map or {}, mount_url, mount_headers)


def _file(path: Path, file_id: int = 1) -> PlaybackMediaFile:
    path.write_bytes(b"fake-video")
    return PlaybackMediaFile(id=file_id, path=str(path), size=path.stat().st_size, library_id=1)


def _query(*files: PlaybackMediaFile, selected_key=None) -> PlaybackQuery:
    return PlaybackQuery(metadata_id=1, number="ABC-123", files=tuple(files), selected_key=selected_key)


@pytest.mark.asyncio
async def test_probe_direct_for_browser_safe(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): H264_AAC})
    offers = await provider.probe(_query(_file(video)))
    assert len(offers) == 1
    assert offers[0].key == "f1"
    assert offers[0].unavailable is None
    assert offers[0].name == "a.mp4"


@pytest.mark.asyncio
async def test_probe_unavailable_for_non_media(tmp_path) -> None:
    iso = tmp_path / "a.iso"
    provider = _make_provider(tmp_path, mount_url=None)
    offers = await provider.probe(_query(_file(iso)))
    assert offers[0].unavailable is not None


@pytest.mark.asyncio
async def test_resolve_direct_uses_verified_signed_url_no_cache(tmp_path, media_server) -> None:
    """真实预检: 302 -> 206, 拿到最终 URL; sign 时效不声明 cache_ttl."""
    handler, base = media_server
    handler.routes = {
        "/dav/a.mp4": {"redirect": f"{base}/signed/a.mp4"},
        "/signed/a.mp4": {"status": 206, "content_type": "video/mp4"},
    }
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=f"{base}/dav/a.mp4", codecs_map={str(video): H264_AAC})
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, UpstreamPlaybackTarget)
    assert target.url == f"{base}/signed/a.mp4"
    assert target.content_type == "video/mp4"
    assert target.cache_ttl is None


@pytest.mark.asyncio
async def test_resolve_direct_falls_back_to_local_file(tmp_path) -> None:
    """预检不过 (不可达) -> FilePlaybackTarget 本机路径."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url="http://127.0.0.1:1/dead.mp4", codecs_map={str(video): H264_AAC})
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)
    assert target.path == video


@pytest.mark.asyncio
async def test_resolve_direct_rejects_non_media_content_type(tmp_path, media_server) -> None:
    """预检终点是 image/png: 判不可用回退本机 (萱儿复审 B2)."""
    handler, base = media_server
    handler.routes = {"/img.mp4": {"status": 206, "content_type": "image/png"}}
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=f"{base}/img.mp4", codecs_map={str(video): H264_AAC})
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)


@pytest.mark.asyncio
async def test_resolve_direct_falls_back_when_no_content_type(tmp_path, media_server) -> None:
    """预检 200 但无 Content-Type (fail-closed): 回退 FilePlaybackTarget (萱儿三审 B2)."""
    handler, base = media_server
    handler.routes = {"/notype.mp4": {"status": 200, "content_type": None}}
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=f"{base}/notype.mp4", codecs_map={str(video): H264_AAC})
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)
    assert target.path == video


@pytest.mark.asyncio
async def test_allow_direct_disabled_uses_local_file(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(
        tmp_path, mount_url="http://127.0.0.1:1/x", allow_direct=False, codecs_map={str(video): H264_AAC}
    )
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)


@pytest.mark.asyncio
async def test_transcode_needs_ffmpeg(tmp_path) -> None:
    video = tmp_path / "b.mkv"
    provider = _make_provider(tmp_path, mount_url="http://127.0.0.1:1/x", codecs_map={str(video): HEVC_AAC})
    provider._tools = None
    provider._tools_ready = True
    assert await provider.resolve(_query(_file(video))) is None
    with pytest.raises(SourceError) as excinfo:
        await provider.resolve(_query(_file(video), selected_key="f1"))
    assert excinfo.value.reason == FailureReason.NO_USABLE_METADATA


@pytest.mark.asyncio
async def test_direct_play_disabled_forces_hls(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(
        tmp_path, mount_url="http://127.0.0.1:1/x", direct_play=False, codecs_map={str(video): H264_AAC}
    )
    provider._tools = None
    provider._tools_ready = True
    assert await provider.resolve(_query(_file(video))) is None


@pytest.mark.asyncio
async def test_no_ffmpeg_browser_safe_still_direct(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): H264_AAC})
    provider._tools = None
    provider._tools_ready = True
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)


@pytest.mark.asyncio
async def test_illegal_selected_key_rejected(tmp_path) -> None:
    """SDK 契约: 认不出的 key 必须拒绝, 不能改播别的文件 (萱儿复核项 2)."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): H264_AAC})
    f = _file(video)
    with pytest.raises(SourceError) as excinfo:
        await provider.resolve(_query(f, selected_key="f999"))
    assert excinfo.value.reason == FailureReason.NO_USABLE_METADATA
    assert await provider.subtitle(_query(f, selected_key="f999"), "s0") is None


@pytest.mark.asyncio
async def test_transcode_input_skips_credentialed_url(tmp_path, media_server) -> None:
    """A2: 带凭据直链不交给 ffmpeg, 回退本机路径 (预检本身仍通过)."""
    handler, base = media_server
    handler.routes = {"/dav/a.mkv": {"status": 206, "content_type": "video/mp4"}}
    video = tmp_path / "b.mkv"
    provider = _make_provider(
        tmp_path,
        mount_url=f"{base}/dav/a.mkv",
        mount_headers={"Authorization": "Basic secret"},
        codecs_map={str(video): HEVC_AAC},
    )
    source, headers = await provider._transcode_input(video)
    assert source == str(video)  # 回退本机
    assert headers == {}


@pytest.mark.asyncio
async def test_transcode_input_uses_url_without_credentials(tmp_path, media_server) -> None:
    """无凭据直链 (alist 签名链): 预检通过后正常作 ffmpeg 输入."""
    handler, base = media_server
    handler.routes = {"/signed/a.mkv": {"status": 206, "content_type": "video/mp4"}}
    video = tmp_path / "b.mkv"
    provider = _make_provider(tmp_path, mount_url=f"{base}/signed/a.mkv", codecs_map={str(video): HEVC_AAC})
    source, headers = await provider._transcode_input(video)
    assert source == f"{base}/signed/a.mkv"
    assert headers == {}


@pytest.mark.asyncio
async def test_transcode_input_falls_back_on_probe_failure(tmp_path) -> None:
    video = tmp_path / "b.mkv"
    provider = _make_provider(tmp_path, mount_url="http://127.0.0.1:1/dead", codecs_map={str(video): HEVC_AAC})
    source, headers = await provider._transcode_input(video)
    assert source == str(video)
    assert headers == {}


@pytest.mark.asyncio
@pytest.mark.skipif(FFMPEG is None, reason="需要本机安装 ffmpeg")
async def test_transcode_credentialed_mount_uses_local_input(tmp_path, media_server) -> None:
    """C2 真实 ffmpeg 回归: 预检成功(206)但带凭据 -> 实际转录用本机输入,

    ffmpeg 从不接触上游, 重定向目标站零请求, 凭据无泄漏链路.
    """
    handler, base = media_server
    handler.routes = {"/dav/movie.mkv": {"status": 206, "content_type": "video/mp4"}}
    source = tmp_path / "movie.mkv"
    subprocess_run = shutil.which("ffmpeg")
    import subprocess

    subprocess.run(
        [
            subprocess_run, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=25",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-f", "matroska", str(source),
        ],
        check=True,
    )
    provider = _make_provider(
        tmp_path,
        mount_url=f"{base}/dav/movie.mkv",
        mount_headers={"Authorization": "Basic secret"},
        codecs_map={str(source): MediaCodecs("h264", 240, None, 4.0, fps=25.0)},
    )
    # 注入真实 ffmpeg 工具链, 绕过桩的占位路径
    provider._tools = Tools(ffmpeg=Path(FFMPEG), encoder="libx264")
    provider._tools_ready = True
    # 注意: 这里不能走 _file() —— 它会把真实片源覆盖成占位字节
    entry = PlaybackMediaFile(id=1, path=str(source), size=source.stat().st_size, library_id=1)
    target = await provider.resolve(_query(entry))
    assert isinstance(target, HlsPlaybackTarget)
    # 回环服务登记的输入必须是本机路径, 不是 URL
    registered = list(plugin_module._SERVER._tokens.values())[-1]
    assert registered.url == str(source)
    assert registered.headers == {}
    # 上游只收到预检请求 (Range bytes=0-0), 凭据头在预检同源内, 且无重定向发生
    assert "/dav/movie.mkv" in handler.seen
    assert handler.seen["/dav/movie.mkv"].get("Range") == "bytes=0-0"
    # resolve 内已预创建会话并转出首段: 证明 ffmpeg 用的是本机输入
    session = registered.sessions[(0, "hevc")]
    assert session.fragment(0) is not None


@pytest.mark.asyncio
async def test_subtitle_scans_local_dir(tmp_path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"fake")
    sub = tmp_path / "movie.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): HEVC_AAC})
    offers = await provider.probe(_query(_file(video)))
    assert len(offers[0].subtitles) == 1
    assert offers[0].subtitles[0].id == "s0"
    vtt = await provider.subtitle(_query(_file(video)), "s0")
    assert vtt is not None and vtt.startswith("WEBVTT")
    assert await provider.subtitle(_query(_file(video)), "s9") is None
