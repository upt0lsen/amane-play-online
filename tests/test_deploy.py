"""ffmpeg 自动部署安全门禁测试 (萱儿复核项 6)."""

from __future__ import annotations

from plugin import PluginSettings, _deploy_ffmpeg


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
