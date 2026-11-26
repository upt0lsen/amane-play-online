"""PR#1 复核 8 项拒绝项的回归测试 (修正单第 1 轮).

每条用例对应复核单里的一项拒绝项; 异常路径一律断言"零副作用调用"
(没有目录扫描 / 没有编码探测 / 没有子进程启动).
"""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from conftest import (
    PlaybackMediaFile,
    PlaybackQuery,
    PluginContext,
    SourceError,
)

import plugin as plugin_module
from plugin import (
    MediaCodecs,
    MountConfig,
    MountTarget,
    OnlineProvider,
    PluginSettings,
    StreamServer,
    Tools,
    _session_key,
    _video_encoder,
)

HEVC_AAC = MediaCodecs("hevc", 1080, "aac", 3600.0, fps=25.0)


# ---------------------------------------------------------------------------
# 公共脚手架
# ---------------------------------------------------------------------------


class _MediaHandler(BaseHTTPRequestHandler):
    """路由可编程的本地媒体服务: {path: {"status":, "content_type":}}."""

    routes: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        spec = type(self).routes.get(self.path)
        if spec is None:
            self.send_error(404)
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
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield H, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


class _StubProvider(OnlineProvider):
    """只打桩解析层/探测层; 预检走真实实现."""

    def __init__(self, context, settings, codecs_map, mount_urls, mount_headers=None):
        super().__init__(context, settings)
        self._stub_codecs = codecs_map
        self._stub_urls = list(mount_urls)
        self._stub_headers = dict(mount_headers or {})

    async def _resolve_tools(self):
        return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")

    async def _codecs(self, path, tools):
        return self._stub_codecs.get(str(path))

    async def _mount_target(self, path):
        if not self._stub_urls:
            return None
        # 一次以上时按顺序发放 (用来验证续签后拿到的是新直链).
        url = self._stub_urls.pop(0) if len(self._stub_urls) > 1 else self._stub_urls[0]
        return MountTarget(url=url, headers=dict(self._stub_headers))


class _RealMountProvider(OnlineProvider):
    """只打桩探测层; 挂载解析走真实实现 (真实 AList 现签 + 真实预检)."""

    def __init__(self, context, settings, codecs_map):
        super().__init__(context, settings)
        self._stub_codecs = codecs_map

    async def _resolve_tools(self):
        return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")

    async def _codecs(self, path, tools):
        return self._stub_codecs.get(str(path))


class _AlistSignHandler(BaseHTTPRequestHandler):
    """按顺序返回签名直链 (模拟 AList 现签, 每次点播现取)."""

    urls: list = []
    calls: int = 0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        index = type(self).calls
        type(self).calls += 1
        url = type(self).urls[index] if index < len(type(self).urls) else type(self).urls[-1]
        payload = json.dumps({"code": 200, "data": {"raw_url": url}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def _alist_server(urls: list) -> str:
    import socketserver

    class H(_AlistSignHandler):
        pass

    H.urls = list(urls)
    H.calls = 0
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def _provider(
    tmp_path: Path,
    *,
    mount_urls: list | None = None,
    mount_headers: dict | None = None,
    allow_direct: bool = True,
    prefer_url_transcode: bool = True,
    codecs_map: dict | None = None,
) -> _StubProvider:
    mount = MountConfig(
        local_prefix=str(tmp_path),
        backend="alist",
        address="http://alist:5244",
        remote_prefix="/115",
        token="t",
        allow_direct=allow_direct,
        prefer_url_transcode=prefer_url_transcode,
    )
    settings = PluginSettings(mounts=[mount])
    context = PluginContext(
        source_id="upt0lsen.play-online",
        http_client=None,
        web_client=None,
        data_dir=tmp_path / "plugin-data",
    )
    return _StubProvider(
        context, settings, codecs_map or {}, mount_urls or [], mount_headers
    )


def _file(path: Path, file_id: int = 1) -> PlaybackMediaFile:
    return PlaybackMediaFile(id=file_id, path=str(path), size=1024, library_id=1)


def _query(*files: PlaybackMediaFile) -> PlaybackQuery:
    return PlaybackQuery(metadata_id=1, number="ABC-123", files=tuple(files))


# ---------------------------------------------------------------------------
# 高: subtitle 绕过原始路径校验 / probe 先探测后拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subtitle_rejects_dot_segment_without_scanning(tmp_path, monkeypatch):
    """含 .. 的字幕请求必须在任何目录扫描之前被拒, 且读不到越界字幕."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.srt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    scanned = []

    def fake_scan(video):
        scanned.append(video)
        return ((outside / "a.srt", ".srt"),)

    monkeypatch.setattr(plugin_module, "_scan_subtitles", fake_scan)
    provider = _provider(tmp_path)
    item = _file(tmp_path / ".." / "outside" / "a.mkv")
    query = _query(item)
    assert await provider.subtitle(query, "s0") is None
    assert scanned == [], "拒绝前不得扫描任何目录"


@pytest.mark.asyncio
async def test_probe_rejects_dot_segment_without_codec_probe(tmp_path, monkeypatch):
    """probe 必须在 ffmpeg 探测与 stat 之前拒绝越界路径."""
    provider = _provider(tmp_path)
    probed = []

    async def counting_codecs(path, tools):
        probed.append(path)
        return HEVC_AAC

    monkeypatch.setattr(provider, "_codecs", counting_codecs)
    item = _file(tmp_path / "x" / ".." / "y.mp4")
    offers = await provider.probe(_query(item))
    assert len(offers) == 1
    assert offers[0].unavailable == "路径不合法"
    assert probed == [], "拒绝前不得探测越界文件"


# ---------------------------------------------------------------------------
# 中: 跨 SourceContext 复用会话
# ---------------------------------------------------------------------------


def test_session_key_separates_sources_and_metadata():
    """会话标识必须含稳定来源身份与元数据, 且同一来源身份算出同一个 key."""
    settings = PluginSettings(mounts=[])
    codecs = MediaCodecs("h264", 1080, "aac", 3600.0, fps=25.0)
    common = {
        "url": "http://cdn/a.mkv",
        "codecs": codecs,
        "settings": settings,
        "encoder": "libx264",
        "target_height": 1080,
        "codec": "hevc",
    }
    base = _session_key(
        **common, source_id="/mnt/a.mkv", headers={}, duration=10.0, source_height=1080, fps=25.0
    )
    # 稳定来源身份 -> key 必须稳定 (同一条流要能复用会话与缓存目录).
    same_source = _session_key(
        **common, source_id="/mnt/a.mkv", headers={}, duration=10.0, source_height=1080, fps=25.0
    )
    assert base == same_source
    other_source = _session_key(
        **common, source_id="/mnt/b.mkv", headers={}, duration=10.0, source_height=1080, fps=25.0
    )
    other_headers = _session_key(
        **common,
        source_id="/mnt/a.mkv",
        headers={"User-Agent": "x"},
        duration=10.0,
        source_height=1080,
        fps=25.0,
    )
    other_duration = _session_key(
        **common, source_id="/mnt/a.mkv", headers={}, duration=11.0, source_height=1080, fps=25.0
    )
    other_height = _session_key(
        **common, source_id="/mnt/a.mkv", headers={}, duration=10.0, source_height=720, fps=25.0
    )
    other_fps = _session_key(
        **common, source_id="/mnt/a.mkv", headers={}, duration=10.0, source_height=1080, fps=50.0
    )
    assert base != other_source
    assert base != other_headers
    assert base != other_duration
    assert base != other_height
    assert base != other_fps


class _FakeProcess:
    """假 ffmpeg 进程: 只提供插件用到的接口 (terminate/kill 由插件对象方法驱动, 不发信号)."""

    def __init__(self, *args, **kwargs) -> None:
        self.pid = 999999
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _stream_source(tmp_path: Path, token: str, url: str = "http://cdn/a.mkv"):
    return plugin_module.SourceContext(
        token=token,
        cache_root=tmp_path / "cache",
        url=url,
        headers={},
        local_path=tmp_path / "a.mkv",
        codecs=HEVC_AAC,
        duration=3600.0,
        seconds=4.0,
        settings=PluginSettings(mounts=[]),
        tools=Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264"),
        subtitles=(),
    )


@pytest.mark.asyncio
async def test_same_file_reuses_one_session_and_cache_dir(tmp_path, monkeypatch):
    """同一文件连续 resolve N 次: 只起转 1 次 ffmpeg, 只有 1 个会话与 1 个缓存目录.

    回归: 会话 key 一旦掺入每次 resolve 都变的随机 token, 就会出现同源重复转码,
    缓存目录名跟着变 (分片永不命中), 还会把 MAX_SESSIONS 槽位占满互杀.
    """
    server = StreamServer()
    started = []

    def fake_popen(command, *args, **kwargs):
        started.append(command)
        return _FakeProcess()

    monkeypatch.setattr(plugin_module.subprocess, "Popen", fake_popen)
    sessions = []
    for index in range(4):
        source = _stream_source(tmp_path, f"tok-{index}")
        sessions.append(await server.session_for(source, 1080, wait_first=False))
    assert len(started) == 1, f"同一文件只应起转 1 次, 实际 {len(started)}"
    assert len(server._sessions) == 1
    assert len({session.directory for session in sessions}) == 1
    cache_root = tmp_path / "cache"
    dirs = [item for item in cache_root.iterdir() if item.is_dir()]
    assert len(dirs) == 1, f"缓存目录应恒定 1 个, 实际 {len(dirs)}"
    await server.aclose()


@pytest.mark.asyncio
async def test_reused_session_command_uses_current_source(tmp_path, monkeypatch):
    """会话被后续 resolve 复用时, 命令必须用当前来源 (完整重绑, 不绑旧 SourceContext)."""
    server = StreamServer()
    monkeypatch.setattr(plugin_module.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    first = _stream_source(tmp_path, "tok-a")
    session = await server.session_for(first, 1080, wait_first=False)
    second = _stream_source(tmp_path, "tok-b")
    second.force_refresh = True
    second.refresher = lambda: ("http://cdn/a.mkv?new=1", {}, None)
    reused = await server.session_for(second, 1080, wait_first=False)
    assert reused is session, "同一文件必须复用同一个会话"
    command = reused.make_command(0)
    assert "http://cdn/a.mkv?new=1" in command, "命令必须用当前来源 (续签后的新直链)"
    await server.aclose()


# ---------------------------------------------------------------------------
# 中: 无 expiresIn 的签名 URL 也要能续签
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcode_input_registers_refresher_without_expires_in(tmp_path, media_server):
    """没有有效期元数据的签名直链也必须挂上续签回调, 且回调能换到新直链."""
    handler, base = media_server
    handler.routes = {
        "/signed/a.mkv": {"status": 206, "content_type": "video/mp4"},
        "/signed/b.mkv": {"status": 206, "content_type": "video/mp4"},
    }
    alist = _alist_server([f"{base}/signed/a.mkv", f"{base}/signed/b.mkv"])
    mount = MountConfig(
        local_prefix=str(tmp_path),
        backend="alist",
        address=alist,
        remote_prefix="/115",
        token="t",
        allow_direct=True,
        prefer_url_transcode=True,
    )
    settings = PluginSettings(mounts=[mount])
    context = PluginContext(
        source_id="upt0lsen.play-online",
        http_client=None,
        web_client=None,
        data_dir=tmp_path / "plugin-data",
    )
    video = tmp_path / "b.mkv"
    provider = _RealMountProvider(context, settings, {str(video): HEVC_AAC})
    source, headers, expires_in, refresher = await provider._transcode_input(video)
    assert source == f"{base}/signed/a.mkv"
    assert expires_in is None
    assert refresher is not None, "无有效期元数据也必须能强制续签"
    fresh = refresher()
    assert fresh is not None
    assert fresh[0] == f"{base}/signed/b.mkv", "续签必须拿到新直链"


def test_force_refresh_renews_url_without_expires_in(tmp_path):
    """强制续签 (401/403 后) 不依赖 expires_in."""
    calls = []

    def refresher():
        calls.append(1)
        return ("http://cdn/new.mkv", {}, None)

    source = plugin_module.SourceContext(
        token="tok",
        cache_root=tmp_path,
        url="http://cdn/old.mkv",
        headers={},
        local_path=tmp_path / "a.mkv",
        codecs=None,
        duration=10.0,
        seconds=4.0,
        settings=PluginSettings(mounts=[]),
        tools=Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264"),
        subtitles=(),
    )
    source.refresher = refresher
    source.force_refresh = True
    plugin_module._refresh_source_url(source)
    assert calls == [1]
    assert source.url == "http://cdn/new.mkv"
    assert source.force_refresh is False


# ---------------------------------------------------------------------------
# 中: AList 合法 JSON 非对象
# ---------------------------------------------------------------------------


class _JsonHandler(BaseHTTPRequestHandler):
    body: str = "{}"
    seen: dict = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = type(self).body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


@pytest.mark.parametrize("body", ["[]", "null", '"text"', "[1, 2]"])
def test_alist_non_object_payload_falls_back(body):
    """AList 返回合法 JSON 但不是对象时按解析失败处理 (不得抛 AttributeError)."""
    import socketserver

    class H(_JsonHandler):
        pass

    H.body = body
    H.seen = {}
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        mount = MountConfig(
            local_prefix="/mnt/115",
            backend="alist",
            address=base,
            remote_prefix="/115",
            token="t",
        )
        assert plugin_module.resolve_mount(mount, Path("/mnt/115/a.mp4")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 中: 来源 token 与签名 URL 的回收
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_source_token_reclaimed_after_grace(tmp_path):
    """没有活动会话的来源在宽限期后被回收, 签名 URL 与 token 一起清掉."""
    server = StreamServer()
    source = await server.register_source(
        cache_root=tmp_path,
        url="http://cdn/signed.mkv?token=secret",
        headers={},
        local_path=tmp_path / "a.mkv",
        codecs=None,
        duration=10.0,
        seconds=4.0,
        settings=PluginSettings(mounts=[]),
        tools=Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264"),
        subtitles=(),
        subtitle_sources={},
    )
    token = source.token
    assert token in server._tokens
    source.last_used = time.monotonic() - plugin_module.SOURCE_IDLE_SECONDS - 1
    await server._sweep_once()
    assert token not in server._tokens
    assert source.url == ""
    assert source.headers == {}
    assert source.refresher is None
    await server.aclose()


@pytest.mark.asyncio
async def test_active_source_not_reclaimed(tmp_path):
    """还有活动会话的来源不能被回收."""
    server = StreamServer()
    source = await server.register_source(
        cache_root=tmp_path,
        url="http://cdn/signed.mkv?token=secret",
        headers={},
        local_path=tmp_path / "a.mkv",
        codecs=None,
        duration=10.0,
        seconds=4.0,
        settings=PluginSettings(mounts=[]),
        tools=Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264"),
        subtitles=(),
        subtitle_sources={},
    )
    source.last_used = time.monotonic() - plugin_module.SOURCE_IDLE_SECONDS - 1
    source.sessions[(1080, "hevc")] = object()
    await server._sweep_once()
    assert source.token in server._tokens
    assert source.url != ""
    await server.aclose()


# ---------------------------------------------------------------------------
# 中: 只有 H.264 QSV 可用时不得选 hevc_qsv
# ---------------------------------------------------------------------------


def test_video_encoder_qsv_hevc_requires_real_capability():
    """没有 hevc_qsv 能力时 HEVC 档软编, 不得选一个跑不起来的编码器."""
    only_h264 = Tools(
        ffmpeg=Path("/usr/bin/ffmpeg"), encoder="h264_qsv", h264_qsv=True, hevc_qsv=False
    )
    assert _video_encoder(only_h264, "hevc") == "libx265"
    assert _video_encoder(only_h264, "h264") == "h264_qsv"
    both = Tools(
        ffmpeg=Path("/usr/bin/ffmpeg"), encoder="h264_qsv", h264_qsv=True, hevc_qsv=True
    )
    assert _video_encoder(both, "hevc") == "hevc_qsv"
    hevc = Tools(
        ffmpeg=Path("/usr/bin/ffmpeg"), encoder="hevc_qsv", h264_qsv=True, hevc_qsv=True
    )
    assert _video_encoder(hevc, "hevc") == "hevc_qsv"


# ---------------------------------------------------------------------------
# 低: gRPC Content-Type 精确白名单
# ---------------------------------------------------------------------------


class _GrpcHandler(BaseHTTPRequestHandler):
    content_type: str = "application/grpc-web+proto"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = b"\x08\x01"
        frame = b"\x00" + struct.pack(">I", len(payload)) + payload
        trailer_text = b"grpc-status: 0\r\n\r\n"
        trailer = b"\x80" + struct.pack(">I", len(trailer_text)) + trailer_text
        out = frame + trailer
        self.send_response(200)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:
        pass


def _grpc_call(content_type: str):
    import socketserver

    class H(_GrpcHandler):
        pass

    H.content_type = content_type
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        return plugin_module._cd2_grpc_unary(base, "/cd2.v1.CloudDrive2/GetDownloadUrlPath", b"", "tok")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_grpc_content_type_exact_whitelist():
    """近似伪造的 Content-Type 必须被拒, 精确白名单必须被接受."""
    assert plugin_module._GRPC_CONTENT_TYPES == frozenset(
        {"application/grpc-web", "application/grpc-web+proto"}
    )
    assert _grpc_call("application/grpc-web+proto") is not None
    assert _grpc_call("application/grpc-web") is not None
    assert _grpc_call("application/grpc-web-evil") is None
    assert _grpc_call("application/grpc-web+protox") is None


# ---------------------------------------------------------------------------
# 低: Popen OSError 转成受控失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_spawn_oserror_converted(tmp_path, monkeypatch):
    """首次启动 ffmpeg 失败 (OSError) 必须转成 SourceError, 并清掉半成品目录."""
    server = StreamServer()

    def boom(*args, **kwargs):
        raise OSError("ffmpeg 没了")

    monkeypatch.setattr(plugin_module.subprocess, "Popen", boom)
    with pytest.raises(SourceError):
        await server.open_session(
            cache_root=tmp_path,
            key="k",
            make_command=lambda index: ["ffmpeg"],
            duration=10.0,
            seconds=4.0,
            idle_seconds=10.0,
            wait_seconds=0.1,
            wait_first=False,
        )
    await server.aclose()


def test_restart_spawn_oserror_returns_none(tmp_path, monkeypatch):
    """拖动重起失败 (OSError) 不得把 OSError 抛给上层, 也不得留下 pid 文件."""

    class _FakeSession:
        def __init__(self, directory: Path) -> None:
            self.directory = directory
            self.log = directory / "ffmpeg.log"
            self.run_playlist = directory / "run.m3u8"
            self.make_command = lambda index: ["ffmpeg"]

    def boom(*args, **kwargs):
        raise OSError("ffmpeg 没了")

    monkeypatch.setattr(plugin_module.subprocess, "Popen", boom)
    session = _FakeSession(tmp_path)
    (tmp_path / plugin_module.PID_FILE).write_text("123\nffmpeg\n", encoding="utf-8")
    assert plugin_module.StreamServer._spawn_process(session, 0) is None
    assert not (tmp_path / plugin_module.PID_FILE).exists()


# ---------------------------------------------------------------------------
# 第2轮: probe 前置校验 / 反斜杠 dot 段 / gRPC 重复头 / 首次起转续签
# ---------------------------------------------------------------------------

BS = chr(92)


@pytest.mark.asyncio
async def test_probe_rejects_dot_segment_without_running_ffmpeg(tmp_path, monkeypatch):
    """probe() 必须先校验原始路径: 拒绝越界路径时一次子进程都不能起.

    刻意**不打桩** _resolve_tools: 它会真的跑 ffmpeg 探测编码器能力, 打桩了这条
    断言就成了假绿.
    """
    calls = []

    def fake_popen(command, *args, **kwargs):
        calls.append(command)
        raise OSError("test: ffmpeg 不该被起")

    monkeypatch.setattr(plugin_module.subprocess, "Popen", fake_popen)
    context = PluginContext(
        source_id="upt0lsen.play-online",
        http_client=None,
        web_client=None,
        data_dir=tmp_path / "plugin-data",
    )
    provider = plugin_module.OnlineProvider(context, PluginSettings())
    provider._tools_ready = False
    item = PlaybackMediaFile(id=1, path="/mnt/evil/../outside/movie.mkv")
    query = PlaybackQuery(metadata_id=1, number="1", files=(item,))
    offers = await provider.probe(query)
    assert len(offers) == 1
    assert offers[0].unavailable == "路径不合法"
    assert calls == [], f"拒绝越界路径前不得起任何子进程, 实际起了 {calls}"


@pytest.mark.parametrize(
    "raw",
    [
        "/mnt/evil/../outside/movie.mkv",
        BS + BS + "mnt" + BS + "evil" + BS + ".." + BS + "outside" + BS + "movie.mkv",
        "C:" + BS + "mnt" + BS + "evil" + BS + ".." + BS + "outside" + BS + "movie.mkv",
        "C:" + BS + "mnt" + BS + "evil" + BS + "." + BS + "movie.mkv",
    ],
)
def test_dot_segments_raw_rejects_backslash_forms(raw):
    """反斜杠形式的 . / .. 段同样必须判越界."""
    assert plugin_module._has_dot_segments_raw(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "/mnt/a/movie.mkv",
        "C:/mnt/a/movie.mkv",
        "C:" + BS + "mnt" + BS + "a" + BS + "movie.mkv",
        "/mnt/a.b/movie.mkv",
    ],
)
def test_dot_segments_raw_accepts_normal_paths(raw):
    """正常路径不能被误判 (盘符与普通文件名里的点都不是 . 段)."""
    assert plugin_module._has_dot_segments_raw(raw) is False


class _DuplicateCtHandler(BaseHTTPRequestHandler):
    """可发送多个 Content-Type 的 gRPC-web 桩 (重复头绕过复现)."""

    content_types: list = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = bytes([0x08, 0x01])
        frame = bytes([0x00]) + struct.pack(">I", len(payload)) + payload
        trailer_text = b"grpc-status: 0" + bytes([13, 10, 13, 10])
        trailer = bytes([0x80]) + struct.pack(">I", len(trailer_text)) + trailer_text
        out = frame + trailer
        self.send_response(200)
        for value in type(self).content_types:
            self.send_header("Content-Type", value)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args) -> None:
        pass


def _grpc_call_with_types(content_types: list):
    import socketserver

    class H(_DuplicateCtHandler):
        pass

    H.content_types = list(content_types)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    httpd.timeout = 5
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        return plugin_module._cd2_grpc_unary(
            base, "/cd2.v1.CloudDrive2/GetDownloadUrlPath", b"", "tok"
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_grpc_duplicate_content_type_rejected():
    """重复 Content-Type: 合法在前、伪造在后也必须整体拒绝; 缺头也拒绝."""
    assert _grpc_call_with_types(["application/grpc-web+proto"]) is not None
    assert _grpc_call_with_types(["application/grpc-web"]) is not None
    assert _grpc_call_with_types(["application/grpc-web-evil"]) is None
    assert (
        _grpc_call_with_types(["application/grpc-web+proto", "application/grpc-web-evil"]) is None
    )
    assert (
        _grpc_call_with_types(["application/grpc-web-evil", "application/grpc-web+proto"]) is None
    )
    assert (
        _grpc_call_with_types(["application/grpc-web", "application/grpc-web+proto"]) is not None
    )
    assert _grpc_call_with_types([]) is None


@pytest.mark.asyncio
async def test_first_spawn_failure_triggers_renewal(tmp_path, monkeypatch):
    """首次起转就失败 (模拟签名被拒 401 秒退): 必须置 force_refresh 并真的续签."""

    class _AliveProc:
        pid = 999999

        def poll(self):
            return None  # 进程活着但永远产不出分片

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout=None):
            return None

    monkeypatch.setattr(
        plugin_module.subprocess, "Popen", lambda command, *a, **k: _AliveProc()
    )
    refresh_calls = []

    def refresher():
        refresh_calls.append(1)
        return ("https://cdn.example.com/movie.mkv?sign=NEW", {}, None)

    server = StreamServer()
    source = await server.register_source(
        cache_root=tmp_path / "cache",
        url="https://cdn.example.com/movie.mkv?sign=OLD",
        headers={},
        local_path=tmp_path / "movie.mkv",
        codecs=HEVC_AAC,
        duration=120.0,
        seconds=1.0,
        settings=PluginSettings(mounts=[]),
        tools=Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264"),
        subtitles=(),
        subtitle_sources={},
        refresher=refresher,
    )
    with pytest.raises(SourceError):
        await server.open_session(
            cache_root=source.cache_root,
            key="probe-key",
            make_command=lambda index: ["ffmpeg", "-i", "x"],
            duration=120.0,
            seconds=1.0,
            idle_seconds=60.0,
            wait_seconds=0.2,
            wait_first=True,
        )
    assert refresh_calls, "首次起转失败必须触发续签 (refresher 至少调用一次)"
    assert source.force_refresh is True, "首次起转失败必须置位 force_refresh"
    await server.aclose()
