"""OnlineProvider 双通路判定与取流测试.

探测 (_codecs) 与解析 (_streams) 被打桩, 不触网、不依赖 ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FailureReason,
    HlsPlaybackTarget,
    PlaybackQuery,
    PluginContext,
    SourceError,
    UpstreamPlaybackTarget,
)

from plugin import (
    MediaCodecs,
    OnlineProvider,
    PluginSettings,
    ResolvedStream,
    Tools,
)

H264_AAC = MediaCodecs("h264", 1080, "aac", 3600.0, fps=25.0)
HEVC_AAC = MediaCodecs("hevc", 1080, "aac", 3600.0, fps=25.0)


class _StubProvider(OnlineProvider):
    """打桩解析与探测的 Provider."""

    def __init__(self, streams, codecs_map, settings=None, tmp=None):
        context = PluginContext(
            source_id="upt0lsen.play-online",
            http_client=None,
            web_client=None,
            data_dir=Path(tmp or "/tmp/amane-play-online-test"),
        )
        super().__init__(context, settings or PluginSettings())
        self._stub_streams = streams
        self._stub_codecs = codecs_map

    async def _streams(self, query):
        return self._stub_streams

    async def _resolve_tools(self):
        if self._tools_ready:
            return self._tools
        return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")

    async def _codecs(self, stream, tools):
        return self._stub_codecs.get(stream.url)


def _mp4() -> ResolvedStream:
    return ResolvedStream(url="https://cdn.example.com/a.mp4", name="MP4 直连", headers={}, subtitles=())


def _mkv() -> ResolvedStream:
    return ResolvedStream(url="https://cdn.example.com/b.mkv", name="MKV 转码", headers={}, subtitles=())


def _query(number="ABC-123", selected_key=None) -> PlaybackQuery:
    return PlaybackQuery(metadata_id=1, number=number, selected_key=selected_key)


@pytest.mark.asyncio
async def test_probe_direct_for_browser_safe() -> None:
    provider = _StubProvider([_mp4()], {"https://cdn.example.com/a.mp4": H264_AAC})
    offers = await provider.probe(_query())
    assert len(offers) == 1
    assert offers[0].key == "s0"
    assert offers[0].unavailable is None


@pytest.mark.asyncio
async def test_probe_unavailable_for_non_media() -> None:
    iso = ResolvedStream(url="https://cdn.example.com/a.iso", name="", headers={}, subtitles=())
    provider = _StubProvider([iso], {})
    offers = await provider.probe(_query())
    assert offers[0].unavailable is not None


@pytest.mark.asyncio
async def test_probe_raises_when_resolver_empty() -> None:
    provider = _StubProvider([], {})
    with pytest.raises(SourceError) as excinfo:
        await provider.probe(_query())
    assert excinfo.value.reason == FailureReason.NO_USABLE_METADATA


@pytest.mark.asyncio
async def test_resolve_direct_returns_upstream_target() -> None:
    provider = _StubProvider([_mp4()], {"https://cdn.example.com/a.mp4": H264_AAC})
    target = await provider.resolve(_query())
    assert isinstance(target, UpstreamPlaybackTarget)
    assert target.url == "https://cdn.example.com/a.mp4"
    assert target.content_type == "video/mp4"
    assert target.cache_ttl and target.cache_ttl > 0


@pytest.mark.asyncio
async def test_resolve_hevc_mkv_would_transcode(tmp_path) -> None:
    """hevc+mkv 计划走转码; 真正的转码目标在集成测试里覆盖, 这里只验证计划分支
    会在没有 ffmpeg 时拒绝, 避免起真进程."""
    provider = _StubProvider([_mkv()], {"https://cdn.example.com/b.mkv": HEVC_AAC})
    provider._tools = None
    provider._tools_ready = True  # 模拟找不到 ffmpeg
    with pytest.raises(SourceError):
        await provider.resolve(_query())


@pytest.mark.asyncio
async def test_direct_play_disabled_forces_hls() -> None:
    """direct_play=False 且浏览器不可解时必须计划转码; 找不到 ffmpeg 时拒绝."""
    settings = PluginSettings(direct_play=False)
    provider = _StubProvider([_mp4()], {"https://cdn.example.com/a.mp4": HEVC_AAC}, settings=settings)
    provider._tools = None
    provider._tools_ready = True
    with pytest.raises(SourceError):
        await provider.resolve(_query())


@pytest.mark.asyncio
async def test_no_ffmpeg_browser_safe_still_direct() -> None:
    """无 ffmpeg 时, 浏览器可解的容器仍走直连 (直连不需要转码)."""
    provider = _StubProvider([_mp4()], {"https://cdn.example.com/a.mp4": H264_AAC})
    provider._tools = None
    provider._tools_ready = True
    target = await provider.resolve(_query())
    assert isinstance(target, UpstreamPlaybackTarget)


@pytest.mark.asyncio
async def test_selected_key_picks_stream() -> None:
    """s1 选中第二条流; 越界 key 退回第一条. 两条都是浏览器可解的 mp4, 走直连."""
    second = ResolvedStream(
        url="https://cdn.example.com/c.mp4", name="第二轨", headers={}, subtitles=()
    )
    provider = _StubProvider(
        [_mp4(), second],
        {
            "https://cdn.example.com/a.mp4": H264_AAC,
            "https://cdn.example.com/c.mp4": H264_AAC,
        },
    )
    target = await provider.resolve(_query(selected_key="s1"))
    assert isinstance(target, UpstreamPlaybackTarget)
    assert target.url == "https://cdn.example.com/c.mp4"

    target = await provider.resolve(_query(selected_key="s99"))
    assert isinstance(target, UpstreamPlaybackTarget)
    assert target.url == "https://cdn.example.com/a.mp4"


@pytest.mark.asyncio
async def test_subtitle_track_from_resolver(monkeypatch) -> None:
    import plugin as plugin_module

    stream = ResolvedStream(
        url="https://cdn.example.com/c.mkv",
        name="",
        headers={},
        subtitles=(),
    )
    subtitle = plugin_module.ResolvedSubtitle(
        id="s0x0", label="中字", language="chi",
        url="https://cdn.example.com/c.srt", suffix=".srt",
    )
    stream = ResolvedStream(
        url=stream.url, name="", headers={},
        subtitles=(subtitle,),
    )
    monkeypatch.setattr(
        plugin_module, "_fetch_text",
        lambda url, headers=None: "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
    )
    provider = _StubProvider([stream], {"https://cdn.example.com/c.mkv": HEVC_AAC})
    vtt = await provider.subtitle(_query(), "s0x0")
    assert vtt is not None and vtt.startswith("WEBVTT")
    assert await provider.subtitle(_query(), "不存在") is None
