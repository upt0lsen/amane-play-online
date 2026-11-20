"""PluginSettings 校验与归一化测试."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugin import PluginSettings


def test_defaults_valid() -> None:
    settings = PluginSettings()
    assert settings.encoder == "auto"
    assert settings.preset == "veryfast"
    assert settings.direct_play is True
    assert settings.resolver_url == ""


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


def test_resolver_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        PluginSettings(resolver_timeout=0.5)
    with pytest.raises(ValidationError):
        PluginSettings(resolver_timeout=61.0)
    assert PluginSettings(resolver_timeout=5.0).resolver_timeout == 5.0


def test_crf_bounds() -> None:
    with pytest.raises(ValidationError):
        PluginSettings(crf=-1)
    with pytest.raises(ValidationError):
        PluginSettings(crf=52)


def test_idle_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        PluginSettings(idle_timeout=30)
