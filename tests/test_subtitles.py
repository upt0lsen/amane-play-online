"""字幕转换 (srt/ass/vtt -> WebVTT) 测试."""

from __future__ import annotations

from plugin import _ass_to_vtt, _subtitle_to_vtt, _srt_to_vtt


def test_srt_to_vtt() -> None:
    srt = "1\n00:00:01,000 --> 00:00:03,500\n你好\n\n2\n00:00:04,000 --> 00:00:06,000\n世界\n"
    vtt = _srt_to_vtt(srt)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.500" in vtt
    assert "00:00:04.000 --> 00:00:06.000" in vtt
    assert "你好" in vtt


def test_ass_to_vtt() -> None:
    ass = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        'Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}顶部字幕\n'
        'Dialogue: 0,0:00:04.50,0:00:06.00,Default,,0,0,0,,第一行\\N第二行\n'
    )
    vtt = _ass_to_vtt(ass)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.000" in vtt
    assert "顶部字幕" in vtt  # 样式标签被剥掉
    assert "第一行\n第二行" in vtt  # \N 转成换行


def test_vtt_passthrough() -> None:
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n"
    assert _subtitle_to_vtt(vtt, ".vtt") == vtt


def test_subtitle_to_vtt_dispatch() -> None:
    assert _subtitle_to_vtt("1\n00:00:01,000 --> 00:00:02,000\nx\n", ".srt").startswith("WEBVTT")
    assert "WEBVTT" in _subtitle_to_vtt("Dialogue: 0,0:00:01.00,0:00:02.00,,,0,0,0,,x", ".ass")
