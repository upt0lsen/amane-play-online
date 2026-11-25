"""分片数学、VOD 清单与主清单生成测试."""

from __future__ import annotations

from pathlib import Path

from plugin import (
    MediaCodecs,
    PluginSettings,
    SourceContext,
    Tools,
    TranscodeSession,
    _master_text,
    _parse_range,
    _warm_segment_index,
    segment_name,
    segment_seconds,
    total_segments,
)


class _FakeProcess:
    """模拟活着的 ffmpeg 进程."""

    def poll(self) -> None:
        return None


def _session(duration: float = 10.0, seconds: float = 2.0) -> TranscodeSession:
    directory = Path("/nonexistent")
    return TranscodeSession(
        token="t",
        key="k",
        directory=directory,
        run_playlist=directory / "run.m3u8",
        log=directory / "ffmpeg.log",
        process=_FakeProcess(),
        duration=duration,
        start_index=0,
        idle_seconds=900.0,
        seconds=seconds,
        make_command=lambda index: [],
    )


def test_segment_name() -> None:
    assert segment_name(0) == "seg00000.m4s"
    assert segment_name(42) == "seg00042.m4s"


def test_segment_seconds_rounds_to_frame() -> None:
    assert segment_seconds(None) == 2.0
    assert segment_seconds(MediaCodecs("h264", 1080, "aac", 100.0, fps=25.0)) == 2.0
    fps = 29.97
    seconds = segment_seconds(MediaCodecs("h264", 1080, "aac", 100.0, fps=fps))
    assert seconds == round(2.0 * fps) / fps


def test_total_segments() -> None:
    assert total_segments(10.0, 2.0) == 5
    assert total_segments(10.1, 2.0) == 5
    assert total_segments(0.5, 2.0) == 1


def test_playlist_is_vod_with_endlist() -> None:
    session = _session(duration=10.0, seconds=2.0)
    text = session.playlist_text()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
    assert "#EXT-X-ENDLIST" in text
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert "#EXT-X-TARGETDURATION:3" in text  # ceil(2.0 + 0.001)
    assert text.count("#EXTINF:") == 5
    assert "seg00000.m4s" in text
    assert "seg00004.m4s" in text


def test_playlist_segments_capped_at_duration() -> None:
    session = _session(duration=5.0, seconds=2.0)
    assert session.segment_duration(0) == 2.0
    assert session.segment_duration(2) == 1.0  # 最后一段只有 1 秒
    assert session.total == 3


def _tools() -> Tools:
    return Tools(ffmpeg=Path("/usr/bin/ffmpeg"), encoder="libx264")


def _source(codecs: MediaCodecs | None, subtitles=()) -> SourceContext:
    return SourceContext(
        token="tok",
        cache_root=Path("/nonexistent"),
        url="http://upstream/video.mkv",
        headers={},
        local_path=Path("/mnt/115/video.mkv"),
        codecs=codecs,
        duration=100.0,
        seconds=2.0,
        settings=PluginSettings(),
        tools=_tools(),
        subtitles=subtitles,
        subtitle_sources={},
    )


def test_master_text_variants() -> None:
    from conftest import SubtitleTrack

    source = _source(
        MediaCodecs("hevc", 720, "aac", 100.0, fps=25.0),
        subtitles=(SubtitleTrack(id="s0x0", label="中字"),),
    )
    text = _master_text(source)
    assert text.startswith("#EXTM3U")
    # 720p 源: 1080P 档不出现, 原画/720P/480P/360P 出现
    assert '"1080P"' not in text
    assert '"720P"' not in text  # 样本语义: 档位必须低于源分辨率
    assert '"480P"' in text
    # 每档 HEVC + H.264 两条变体
    assert "hevc/run.m3u8" in text
    assert "h264/run.m3u8" in text
    assert "hevc/480/run.m3u8" in text
    # 字幕轨声明
    assert 'TYPE=SUBTITLES' in text
    assert 'URI="subtitle/s0x0.vtt"' in text
    # RESOLUTION 与 FRAME-RATE 声明
    assert "RESOLUTION=1280x720" in text
    assert "FRAME-RATE=25.000" in text


def test_parse_range() -> None:
    assert _parse_range("bytes=0-99", 200) == (0, 99)
    assert _parse_range("bytes=100-", 200) == (100, 199)
    assert _parse_range("bytes=-50", 200) == (150, 199)
    assert _parse_range("bytes=0-9999", 200) == (0, 199)  # end 截断
    assert _parse_range("bytes=200-", 200) is None  # start 越界
    assert _parse_range("bytes=50-10", 200) is None  # end < start
    assert _parse_range("items=0-10", 200) is None  # 非 bytes 单位
    assert _parse_range("bytes=0-10,20-30", 200) is None  # 多段


def test_warm_segment_index() -> None:
    session = _session(duration=20.0, seconds=2.0)  # 10 段
    assert _warm_segment_index("/v/t/warm?seg=3", session) == 3
    assert _warm_segment_index("/v/t/warm?t=7.0", session) == 3
    assert _warm_segment_index("/v/t/warm", session) == 0
    # 越界收敛到末段
    assert _warm_segment_index("/v/t/warm?seg=99", session) == 9
    assert _warm_segment_index("/v/t/warm?seg=-1", session) is None
