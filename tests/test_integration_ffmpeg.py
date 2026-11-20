"""端到端集成测试: 真实 ffmpeg 从 HTTP URL 拉流转 HLS.

本机起两个服务:
- 带 Range 支持的静态文件 HTTP 服务 (模拟在线媒体上游);
- 插件的 StreamServer (回环, 供 amane 反代).

验证: URL 探测 -> 起转 -> VOD 清单 -> 首段产出 -> 拖动到末段触发 -ss 重起转.
"""

from __future__ import annotations

import asyncio
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

from plugin import (
    PluginSettings,
    StreamServer,
    Tools,
    _probe_codecs,
    segment_seconds,
)

FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="需要本机安装 ffmpeg")


class _RangeFile:
    """只读到指定剩余字节的文件包装, 配合 Range 响应."""

    def __init__(self, handle, remaining: int) -> None:
        self._handle = handle
        self._remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        data = self._handle.read(size)
        self._remaining -= len(data)
        return data

    def close(self) -> None:
        self._handle.close()


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """支持单段 bytes= 区间的静态文件服务."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    def send_head(self):  # noqa: N802 - 父类命名如此
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if match:
                path = self.translate_path(self.path)
                if os.path.isfile(path):
                    start = int(match.group(1))
                    size = os.path.getsize(path)
                    end = int(match.group(2)) if match.group(2) else size - 1
                    end = min(end, size - 1)
                    if start <= end:
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", str(end - start + 1))
                        self.send_header("Content-Type", "video/mp4")
                        self.end_headers()
                        handle = open(path, "rb")
                        handle.seek(start)
                        return _RangeFile(handle, end - start + 1)
        return super().send_head()


@pytest.fixture()
def media_url(tmp_path) -> str:
    """生成 8 秒测试片 (h264+aac) 并在本机起带 Range 的 HTTP 服务."""
    source = tmp_path / "src.mp4"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=8:size=640x360:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
    )
    handler = lambda *args, **kwargs: RangeHandler(*args, directory=str(tmp_path), **kwargs)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/src.mp4"
    httpd.shutdown()
    httpd.server_close()


def test_probe_codecs_from_url(media_url: str) -> None:
    codecs = _probe_codecs(Path(FFMPEG), media_url)
    assert codecs is not None
    assert codecs.video == "h264"
    assert codecs.audio == "aac"
    assert codecs.duration is not None and 7.0 < codecs.duration < 9.5
    assert codecs.height == 360


def test_transcode_pipeline_from_url(media_url: str, tmp_path) -> None:
    """完整链路: URL 探测 -> 起转 -> 首段 -> 清单 -> 拖动末段重起转."""

    async def main() -> None:
        tools = Tools(ffmpeg=Path(FFMPEG), encoder="libx264")
        codecs = await asyncio.to_thread(_probe_codecs, tools.ffmpeg, media_url)
        assert codecs is not None and codecs.duration
        settings = PluginSettings(encoder="cpu", preset="ultrafast")
        seconds = segment_seconds(codecs)
        server = StreamServer()
        try:
            source = await server.register_source(
                cache_root=tmp_path / "cache",
                url=media_url,
                headers={},
                codecs=codecs,
                duration=codecs.duration,
                seconds=seconds,
                settings=settings,
                tools=tools,
                subtitles=(),
                subtitle_sources={},
            )
            session = await server.session_for(source, 0)
            # 首段已经产出 (open_session wait_first).
            first = session.fragment(0)
            assert first is not None and first.stat().st_size > 0
            # 初始化段存在.
            init = session.directory / "init.mp4"
            assert init.is_file() and init.stat().st_size > 0

            # 清单: VOD + ENDLIST + 合理段数.
            text = session.playlist_text()
            assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
            assert "#EXT-X-ENDLIST" in text
            assert text.count("#EXTINF:") == session.total

            # 拖动到末段: 触发 -ss 重起转, 段必须能拿到.
            last_index = session.total - 1
            last = await server.ensure_segment(session, last_index)
            assert last is not None and last.stat().st_size > 0
            assert session.encoder_start == last_index
            # 起转命令里确实带 -ss (重起转发生在末段起点).
            command = session.make_command(last_index)
            assert "-ss" in command
            ss_at = command.index("-ss")
            assert abs(float(command[ss_at + 1]) - last_index * seconds) < 0.01
        finally:
            await server.aclose()

    asyncio.run(main())
