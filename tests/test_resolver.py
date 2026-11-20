"""resolver 响应解析测试."""

from __future__ import annotations

from plugin import _parse_resolver_payload

BASE_HEADERS = {"Authorization": "Bearer t"}


def test_plain_text_single_url() -> None:
    streams = _parse_resolver_payload("https://cdn.example.com/a.mp4", BASE_HEADERS)
    assert len(streams) == 1
    assert streams[0].url == "https://cdn.example.com/a.mp4"
    assert streams[0].headers == BASE_HEADERS
    assert streams[0].subtitles == ()


def test_json_single_stream() -> None:
    text = '{"url": "https://cdn.example.com/a.mkv", "name": "蓝光"}'
    streams = _parse_resolver_payload(text, BASE_HEADERS)
    assert len(streams) == 1
    assert streams[0].name == "蓝光"


def test_json_multi_streams_with_headers_and_subtitles() -> None:
    text = (
        '{"streams": ['
        '{"url": "https://a/1.mkv", "headers": {"X-Token": "abc"}, '
        ' "subtitles": [{"url": "https://a/1.srt", "label": "中字", "language": "chi"}]},'
        '{"url": "https://a/2.mp4"}'
        ']}'
    )
    streams = _parse_resolver_payload(text, BASE_HEADERS)
    assert len(streams) == 2
    # 流级 headers 合并到基础 headers 之上
    assert streams[0].headers == {"Authorization": "Bearer t", "X-Token": "abc"}
    assert streams[1].headers == BASE_HEADERS
    # 字幕轨
    assert len(streams[0].subtitles) == 1
    sub = streams[0].subtitles[0]
    assert sub.label == "中字"
    assert sub.language == "chi"
    assert sub.suffix == ".srt"
    assert streams[1].subtitles == ()


def test_top_level_list() -> None:
    streams = _parse_resolver_payload('[{"url": "https://a/1.mp4"}]', BASE_HEADERS)
    assert len(streams) == 1


def test_invalid_json_returns_empty() -> None:
    assert _parse_resolver_payload("{not json", BASE_HEADERS) == ()


def test_empty_payload_returns_empty() -> None:
    assert _parse_resolver_payload("", BASE_HEADERS) == ()
    assert _parse_resolver_payload("   \n", BASE_HEADERS) == ()


def test_stream_without_url_skipped() -> None:
    text = '{"streams": [{"name": "无地址"}, {"url": "https://a/1.mp4"}]}'
    streams = _parse_resolver_payload(text, BASE_HEADERS)
    assert len(streams) == 1


def test_subtitle_id_sanitized() -> None:
    text = (
        '{"url": "https://a/1.mkv", "subtitles": ['
        '{"url": "https://a/1.vtt", "id": "bad/id"},'
        '{"url": "https://a/2.vtt", "id": "zh.01"}'
        ']}'
    )
    streams = _parse_resolver_payload(text, BASE_HEADERS)
    subs = streams[0].subtitles
    assert subs[0].id == "s0x0"  # 非法 id 退回兜底值
    assert subs[1].id == "zh.01"
    assert subs[0].suffix == ".vtt"
