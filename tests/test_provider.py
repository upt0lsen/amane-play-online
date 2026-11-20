"""OnlineProvider 双通路判定与取流测试.

输入为本地文件 (与样本一致), 挂载解析打桩, 不触网、不依赖 ffmpeg.
文件全部建在本机临时目录, 挂载前缀指向该目录以满足 is_file 检查.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FailureReason,
    FilePlaybackTarget,
    PlaybackMediaFile,
    PlaybackQuery,
    PluginContext,
    SourceError,
    UpstreamPlaybackTarget,
)

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


def _make_provider(
    tmp_path: Path,
    *,
    mount_url: str | None,
    allow_direct: bool = True,
    direct_play: bool = True,
    codecs_map: dict | None = None,
) -> "StubProvider":
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
    return StubProvider(context, settings, codecs_map or {}, mount_url)


class StubProvider(OnlineProvider):
    """打桩工具链 / 编码探测 / 挂载解析的 Provider."""

    def __init__(self, context, settings, codecs_map, mount_url):
        super().__init__(context, settings)
        self._stub_codecs = codecs_map
        self._stub_mount_url = mount_url

    async def _resolve_tools(self):
        if self._tools_ready:
            return self._tools
        return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")

    async def _codecs(self, path, tools):
        return self._stub_codecs.get(str(path))

    async def _mount_target(self, path):
        if self._stub_mount_url is None:
            return None
        return MountTarget(url=self._stub_mount_url, headers={})


def _file(path: Path, file_id: int = 1) -> PlaybackMediaFile:
    path.write_bytes(b"fake-video")
    return PlaybackMediaFile(id=file_id, path=str(path), size=path.stat().st_size, library_id=1)


def _query(*files: PlaybackMediaFile, selected_key=None) -> PlaybackQuery:
    return PlaybackQuery(metadata_id=1, number="ABC-123", files=tuple(files), selected_key=selected_key)


@pytest.mark.asyncio
async def test_probe_direct_for_browser_safe(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url="https://cdn/x", codecs_map={str(video): H264_AAC})
    offers = await provider.probe(_query(_file(video)))
    assert len(offers) == 1
    assert offers[0].key == "f1"
    assert offers[0].unavailable is None
    assert offers[0].name == "a.mp4"


@pytest.mark.asyncio
async def test_probe_unavailable_for_non_media(tmp_path) -> None:
    iso = tmp_path / "a.iso"
    provider = _make_provider(tmp_path, mount_url="https://cdn/x")
    offers = await provider.probe(_query(_file(iso)))
    assert offers[0].unavailable is not None


@pytest.mark.asyncio
async def test_resolve_direct_uses_signed_url_no_cache(tmp_path) -> None:
    video = tmp_path / "a.mp4"
    provider = _make_provider(
        tmp_path, mount_url="https://cdn.115.com/signed?sign=x", codecs_map={str(video): H264_AAC}
    )
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, UpstreamPlaybackTarget)
    assert target.url == "https://cdn.115.com/signed?sign=x"
    assert target.content_type == "video/mp4"
    # sign 有时效: 不声明 cache_ttl, 每次现签现跳
    assert target.cache_ttl is None


@pytest.mark.asyncio
async def test_resolve_direct_falls_back_to_local_file(tmp_path) -> None:
    """挂载解析失败 -> FilePlaybackTarget 本机路径."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): H264_AAC})
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)
    assert target.path == video


@pytest.mark.asyncio
async def test_allow_direct_disabled_uses_local_file(tmp_path) -> None:
    """挂载关闭直链: 直连回退本机 FilePlaybackTarget."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(
        tmp_path, mount_url="https://cdn/x", allow_direct=False, codecs_map={str(video): H264_AAC}
    )
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)


@pytest.mark.asyncio
async def test_transcode_needs_ffmpeg(tmp_path) -> None:
    """mkv (hevc) 走转码; 无 ffmpeg 时没有可选流, resolve 返回 None (SDK 语义)."""
    video = tmp_path / "b.mkv"
    provider = _make_provider(tmp_path, mount_url="https://cdn/x", codecs_map={str(video): HEVC_AAC})
    provider._tools = None
    provider._tools_ready = True
    assert await provider.resolve(_query(_file(video))) is None
    # 明确选中不可播的文件时必须报 SourceError
    with pytest.raises(SourceError) as excinfo:
        await provider.resolve(_query(_file(video), selected_key="f1"))
    assert excinfo.value.reason == FailureReason.NO_USABLE_METADATA


@pytest.mark.asyncio
async def test_direct_play_disabled_forces_hls(tmp_path) -> None:
    """全局关闭直连且无 ffmpeg: 可解码文件也不再直给, resolve 返回 None."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(
        tmp_path, mount_url="https://cdn/x", direct_play=False, codecs_map={str(video): H264_AAC}
    )
    provider._tools = None
    provider._tools_ready = True
    assert await provider.resolve(_query(_file(video))) is None


@pytest.mark.asyncio
async def test_no_ffmpeg_browser_safe_still_direct(tmp_path) -> None:
    """无 ffmpeg 但直连开启时, 浏览器可解的文件仍走直连 (直连不需要转码)."""
    video = tmp_path / "a.mp4"
    provider = _make_provider(tmp_path, mount_url=None, codecs_map={str(video): H264_AAC})
    provider._tools = None
    provider._tools_ready = True
    target = await provider.resolve(_query(_file(video)))
    assert isinstance(target, FilePlaybackTarget)


@pytest.mark.asyncio
async def test_selected_key_picks_file(tmp_path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "c.mp4"
    provider = _make_provider(
        tmp_path,
        mount_url="https://cdn/u",
        codecs_map={str(first): H264_AAC, str(second): H264_AAC},
    )
    target = await provider.resolve(
        _query(_file(first), _file(second, file_id=2), selected_key="f2")
    )
    assert isinstance(target, UpstreamPlaybackTarget)


@pytest.mark.asyncio
async def test_subtitle_scans_local_dir(tmp_path) -> None:
    """字幕来自本机同目录扫描 (与样本一致)."""
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"fake")
    sub = tmp_path / "movie.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    provider = _make_provider(tmp_path, mount_url="https://cdn/x", codecs_map={str(video): HEVC_AAC})
    offers = await provider.probe(_query(_file(video)))
    assert len(offers[0].subtitles) == 1
    assert offers[0].subtitles[0].id == "s0"
    vtt = await provider.subtitle(_query(_file(video)), "s0")
    assert vtt is not None and vtt.startswith("WEBVTT")
    assert await provider.subtitle(_query(_file(video)), "s9") is None
