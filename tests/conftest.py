"""测试套件: 为 plugin.py 提供 amane.plugin SDK 的最小桩实现.

桩只覆盖 plugin.py 用到的契约 (PlaybackPlugin / PlaybackProvider / PlaybackQuery /
PlaybackOffer / UpstreamPlaybackTarget / HlsPlaybackTarget / HlsLocator / HlsPlaylist /
SubtitleTrack / SourceDescriptor / SourceCapability / PluginContext /
SourceError / FailureReason), 行为对齐 amane 0.28.0 的 SDK 定义.
"""

from __future__ import annotations

import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field


def pytest_configure(config):  # noqa: ANN001, ANN202 - pytest 钩子签名
    config.addinivalue_line("markers", "asyncio: 由 conftest 的 pytest_pyfunc_call 运行")


def pytest_pyfunc_call(pyfuncitem):  # noqa: ANN001, ANN202 - pytest 钩子签名
    """让 ``async def`` 测试用例直接跑, 免装 pytest-asyncio."""
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(pyfuncitem.function):
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(pyfuncitem.function(**kwargs))
        return True
    return None

# ---------------------------------------------------------------------------
# 桩: amane.plugin
# ---------------------------------------------------------------------------


class FailureReason(StrEnum):
    NETWORK = "network"
    NO_USABLE_METADATA = "no_usable_metadata"


class SourceError(Exception):
    def __init__(self, reason: FailureReason, detail: str | None = None) -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


class SourceCapability(StrEnum):
    FILM_METADATA = "film_metadata"
    PLAYBACK = "playback"


class SourceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    version: str = "builtin"
    api_version: str = "1"
    capabilities: frozenset[str] = frozenset({SourceCapability.FILM_METADATA})


class SubtitleTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]{1,64}$")
    label: str
    language: str | None = None


class PlaybackMediaFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    path: str
    size: int | None = None
    content_type: str | None = None
    has_subtitle: bool = False
    library_id: int = 0


class PlaybackOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]{1,64}$")
    name: str = Field(min_length=1)
    content_type: str
    seekable: bool = True
    unavailable: str | None = Field(default=None, min_length=1)
    subtitles: tuple[SubtitleTrack, ...] = ()


class FilePlaybackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    kind: Literal["file"] = "file"
    path: Path
    content_type: str
    cache_ttl: float | None = Field(default=None, gt=0)


class PlaybackQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata_id: int
    number: str
    title: str | None = None
    selected_key: str | None = None
    files: tuple = ()


class UpstreamPlaybackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["upstream"] = "upstream"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = None
    cache_ttl: float | None = Field(default=None, gt=0)


class HlsPlaylist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    base_url: str


class HlsLocator(ABC):
    @abstractmethod
    async def load_playlist(self, query: PlaybackQuery) -> HlsPlaylist: ...

    @abstractmethod
    async def locate(self, query: PlaybackQuery, uri: str) -> UpstreamPlaybackTarget: ...


@dataclass(frozen=True, slots=True)
class HlsPlaybackTarget:
    locator: HlsLocator
    kind: Literal["hls"] = "hls"
    cache_ttl: float | None = None


@dataclass(frozen=True, slots=True)
class PluginContext:
    source_id: str
    http_client: object | None
    web_client: object | None
    data_dir: Path


class EmptyPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaybackProvider(ABC):
    @abstractmethod
    async def probe(self, query: PlaybackQuery) -> tuple[PlaybackOffer, ...]: ...

    @abstractmethod
    async def resolve(self, query: PlaybackQuery): ...

    async def subtitle(self, query: PlaybackQuery, track_id: str):
        return None


class PlaybackPlugin(ABC):
    config_model: ClassVar[type[BaseModel]] = EmptyPluginConfig

    @classmethod
    @abstractmethod
    def descriptor(cls) -> SourceDescriptor: ...

    @abstractmethod
    def build_playback(self, context: PluginContext, config: BaseModel) -> PlaybackProvider: ...


def _install_stub() -> None:
    amane = types.ModuleType("amane")
    plugin = types.ModuleType("amane.plugin")
    for name, value in {
        "FailureReason": FailureReason,
        "SourceError": SourceError,
        "SourceCapability": SourceCapability,
        "SourceDescriptor": SourceDescriptor,
        "SubtitleTrack": SubtitleTrack,
        "PlaybackMediaFile": PlaybackMediaFile,
        "FilePlaybackTarget": FilePlaybackTarget,
        "PlaybackOffer": PlaybackOffer,
        "PlaybackQuery": PlaybackQuery,
        "UpstreamPlaybackTarget": UpstreamPlaybackTarget,
        "HlsPlaylist": HlsPlaylist,
        "HlsLocator": HlsLocator,
        "HlsPlaybackTarget": HlsPlaybackTarget,
        "PluginContext": PluginContext,
        "PlaybackProvider": PlaybackProvider,
        "PlaybackPlugin": PlaybackPlugin,
    }.items():
        setattr(plugin, name, value)
    amane.plugin = plugin
    amane.__path__ = []  # 标记为包, 让 from amane.plugin import ... 走子模块导入
    sys.modules.setdefault("amane", amane)
    sys.modules.setdefault("amane.plugin", plugin)


_install_stub()

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import plugin as plugin_module  # noqa: E402

pytest_plugins = ()
