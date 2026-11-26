"""PluginSettings / MountConfig 校验与归一化测试."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugin import MountConfig, PluginSettings


def test_defaults_valid() -> None:
    settings = PluginSettings()
    assert settings.encoder == "auto"
    assert settings.preset == "veryfast"
    assert settings.direct_play is True
    assert settings.mounts == []


def test_mount_config_defaults() -> None:
    mount = MountConfig(local_prefix="/mnt/115")
    assert mount.backend == "alist"
    assert mount.allow_direct is True
    assert mount.prefer_url_transcode is False
    assert mount.remote_prefix == "/"


def test_mount_local_prefix_required() -> None:
    with pytest.raises(ValidationError):
        MountConfig()  # type: ignore[call-arg]


def test_mounts_in_settings() -> None:
    settings = PluginSettings(
        mounts=[
            {"local_prefix": "/mnt/115", "backend": "alist", "address": "http://alist:5244"},
            {"local_prefix": "/mnt/dav", "backend": "webdav", "address": "https://d/dav"},
        ]
    )
    assert len(settings.mounts) == 2
    assert settings.mounts[1].backend == "webdav"


def test_encoder_aliases_normalized() -> None:
    assert PluginSettings(encoder="hevc-nvenc").encoder == "nvenc"
    assert PluginSettings(encoder="x264").encoder == "cpu"
    assert PluginSettings(encoder="quicksync").encoder == "qsv"
    assert PluginSettings(encoder="N卡").encoder == "nvenc"


def test_unknown_encoder_falls_back_to_auto() -> None:
    assert PluginSettings(encoder="not-a-encoder").encoder == "auto"


def test_unknown_preset_falls_back_to_veryfast() -> None:
    assert PluginSettings(preset="p9").preset == "veryfast"
    assert PluginSettings(preset="slow").preset == "medium"


def test_extra_keys_ignored() -> None:
    settings = PluginSettings(video_codec="h265", unknown_key=1)  # type: ignore[call-arg]
    assert settings.encoder == "auto"


def test_crf_bounds() -> None:
    with pytest.raises(ValidationError):
        PluginSettings(crf=-1)
    with pytest.raises(ValidationError):
        PluginSettings(crf=52)


def test_idle_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        PluginSettings(idle_timeout=30)
