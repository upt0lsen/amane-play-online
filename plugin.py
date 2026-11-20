"""在线媒体播放源.

把可配置 resolver 解析出的**在线媒体 URL** 交给主机播放. 架构参照社区样本插件
``nas.local`` (本机文件播放源, GPL-3.0) 的双通路设计, 输入从本机路径换成了在线 URL:

- **直连**: 容器与编码浏览器都能解码时 (mp4 / m4v / mov / webm / ogv, 视频 h264 / vp8 /
  vp9 / av1, 音频 aac / opus / mp3 ...), 原样返回 ``UpstreamPlaybackTarget`` 跳上游,
  不经过转码, 画质不变, 可以任意跳转.
- **转码**: 其余来源由本插件调用 ffmpeg 从 URL 拉流, 实时转成 HLS (fMP4 分片), 分片经
  回环 HTTP 交给 Amane 反代给播放器. aac 音轨重编码对齐分片边界; 视频一律重编码成
  h264/hevc (NVENC / QSV 硬件编码优先, 自动探测与回退), 并按分辨率加码率上限.

转码流交给播放器的是一份**完整的 VOD 清单**: 总时长由 ffmpeg 读出的时长决定, 一次给全,
播放器可以拖到任意位置 —— 要哪一段, 插件就把 ffmpeg 用 ``-ss`` 起转到哪一段. 播放器关掉后
ffmpeg 由清理任务结束, 已转好的分片留在缓存里复用, 超过 ``idle_timeout`` 秒没人用才删除.

媒体 URL 不由本插件猜: 由设置项 ``resolver_url`` 指向的解析接口按条目番号
(``{number}``) 返回, 返回 JSON 或纯文本 URL; 解析器也可以为每条流附带字幕地址
(srt / vtt / ass 自动转 WebVTT). 具体上游完全由配置决定, 插件本身不内置任何站点.

样本 ``nas.local`` 的「原画直出」通路依赖本机 mp4 关键帧表, 对在线源不可用, 本插件不实现;
浏览器可解码的在线源由**直连通路**覆盖, 不再保留 HLS copy 档.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # 打包版同样提供这两个入口, 优先用作者 SDK 路径
    from amane.plugin import (  # type: ignore[import-not-found]
        FailureReason,
        HlsLocator,
        HlsPlaybackTarget,
        HlsPlaylist,
        PlaybackOffer,
        PlaybackPlugin,
        PlaybackProvider,
        PlaybackQuery,
        PluginContext,
        SourceCapability,
        SourceDescriptor,
        SourceError,
        SubtitleTrack,
        UpstreamPlaybackTarget,
    )
except ImportError:  # pragma: no cover
    from amane.net.errors import FailureReason, SourceError  # type: ignore[no-redef]
    from amane.plugins.api import (  # type: ignore[no-redef]
        HlsLocator,
        HlsPlaybackTarget,
        HlsPlaylist,
        PlaybackOffer,
        PlaybackPlugin,
        PlaybackProvider,
        PlaybackQuery,
        PluginContext,
        SubtitleTrack,
        UpstreamPlaybackTarget,
    )
    from amane.plugins.models import (  # type: ignore[no-redef]
        SourceCapability,
        SourceDescriptor,
    )

logger = logging.getLogger(__name__)

SOURCE_ID = "upt0lsen.play-online"

HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"
#: 浏览器能直接解码的容器. 其余容器一律由本插件转码.
DIRECT_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
}

#: 浏览器能直接解码的编码. 容器对但编码不对 (HEVC / ProRes 的 mov) 仍要转码.
BROWSER_SAFE_VIDEO = frozenset({"h264", "vp8", "vp9", "av1", "theora"})
BROWSER_SAFE_AUDIO = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})

#: 能直接装进 HLS 的编码: 不必重新编码, 只换容器.

#: 明确不是视频的后缀, 不交给 ffmpeg 试.
NOT_MEDIA: dict[str, str] = {
    ".iso": "ISO 镜像需要挂载后播放",
    ".zip": "不是视频文件",
    ".rar": "不是视频文件",
    ".7z": "不是视频文件",
    ".txt": "不是视频文件",
    ".nfo": "不是视频文件",
    ".jpg": "不是视频文件",
    ".jpeg": "不是视频文件",
    ".png": "不是视频文件",
    ".webp": "不是视频文件",
}

SUBTITLE_SUFFIXES = (".srt", ".vtt", ".ass", ".ssa")
RESOLVE_TTL_SECONDS = 600.0

#: 分片时长 (秒). 实际值按片源帧率取整到帧, 见 ``_segment_seconds``.
#: 1.0.6: 4 -> 2. 分片越短, 拖动时"等首段"的绝对时间越短 (硬编 3-5x 实时, 2 秒内容约
#: 0.4-0.6 秒出片), 段间时间戳误差的绝对值也减半; 代价是请求数翻倍, 靠前端 30 秒缓冲抵消.
SEGMENT_SECONDS = 2
#: 拖动后允许"等顺序转码"的时间预算 (秒). 超过就换位置重转: 实测重启只要 0.34s,
#: 而干等一次顺序转要按段长 4s 起算, 完全不合算.
SEEK_WAIT_BUDGET_SECONDS = 1.2
#: 被更靠后的目标接管后的补等时间 (秒): 宁可服务端多等一下, 也别把 503 甩给播放器.
SEEK_FOLLOWUP_SECONDS = 3.0
#: fMP4 分片的初始化段与命名. 用 fMP4 而不是 MPEG-TS: Amarok 这类原生 HLS 播放器
#: (Edge/WebView2 走的就是它) 只在 fMP4 上稳定, TS 会在取到第一段后失败.
INIT_NAME = "init.mp4"
SEGMENT_PATTERN = re.compile(r"^seg(\d{5})\.m4s$")
#: ffmpeg 自己写的清单, 只用来判断哪些分片已经写完, 不交给播放器.
RUN_PLAYLIST = "run.m3u8"

#: 1.0.27: 预热请求的文件名. 不是媒体, 只为让服务端提前把目标段转出来.
WARM_NAME = "warm"
#: 1.0.27: 当前正在处理的请求标签 (id(writer) -> "GET /v/..."), 只为把 5xx 打进日志.
#: 切档 5xx 一度无法定位, 就是因为不知道是哪一路请求返回的什么码.
_REQUEST_LABELS: dict[int, str] = {}
STREAM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

LOOPBACK = "127.0.0.1"
REQUEST_TIMEOUT_SECONDS = 15.0
STARTUP_TIMEOUT_SECONDS = 30.0
#: 等一个尚未转出的分片的上限. 播放器取分片有自己的超时, 超过就当这一段取不到.
#: 0.9.5 起从 18 秒降到 10 秒: 桌面 hls.js 的分片超时比 18 秒短, 等满 18 秒只会把 amane
#: 侧的播放通道占满 (2 秒抢不到槽就回 503), 表现就是"拖一下整片都放不出来".
#: 1.0.27: 4 -> 7 秒. amane 代理层读超时 30 秒, 插件 4 秒就放弃、把 503 甩给代理
#: (代理再翻成 502 "播放来源失败"), 这是切档瞬间 5xx 的直接来源. 起转实测 0.34s、
#: 首段 0.4-1.2s, 7 秒足够覆盖两轮重试; 真等不到才回 503 让 hls 自己退避.
WAIT_SEGMENT_SECONDS = 7.0
#: 进程活着、分片号也在前进时的额外宽限: 属于"慢但在动", 不该被固定超时掐死.
SEGMENT_PROGRESS_GRACE_SECONDS = 6.0
#: warm 端点最多替播放器等多久 (秒).
WARM_WAIT_SECONDS = 12.0
PROBE_TIMEOUT_SECONDS = 20.0
#: 同时保留的转码会话数; 超出时先停最久没被访问的那条.
MAX_SESSIONS = 3
#: 这么久没有任何请求就结束 ffmpeg (播放器已关掉), 已转分片留着.
KILL_AFTER_IDLE_SECONDS = 45.0
#: 清理任务的周期. 只做查询与判断, 很轻.
SWEEP_SECONDS = 15.0
#: 播放头离当前转码位置还有这么多段时按"正在产出"处理, 避免预取请求把转码打入反复重启.
COVER_MARGIN_SEGMENTS = 6
#: 当前这一轮 ffmpeg 最多"覆盖"到已产出段之后这么多个段. 播放器顺序预读 30 秒 (8 段) 落在
#: 窗口内, 等它转就行; 超出窗口说明用户把进度条拖到了别处, 必须把 ffmpeg 挪过去.
COVER_LOOKAHEAD_SEGMENTS = 12
#: 会话目录里记录 ffmpeg 进程的文件, 用来在插件重载后回收上一个实例起的进程.
PID_FILE = "pid"
READ_CHUNK = 262144
_CODEC_CACHE_SIZE = 256

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: ``preset`` 到 NVENC 预设的换算: 两套预设的档位含义接近, 按顺序对应.
NVENC_PRESETS: dict[str, str] = {
    "ultrafast": "p1",
    "superfast": "p2",
    "veryfast": "p3",
    "faster": "p4",
    "fast": "p5",
    "medium": "p6",
}

Preset = Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
NvencCodec = Literal["auto", "av1", "hevc", "h264"]
QsvCodec = Literal["auto", "hevc", "h264"]
Encoder = Literal["auto", "nvenc", "qsv", "cpu"]


def _setting_key(value: str) -> str:
    """配置值归一化: 去首尾空白、转小写、连字符与空格统一成下划线."""
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


#: 1.0.4: 编码器配置的别名表. 社区里常见的写法是 ``nvenc`` / ``qsv`` / ``x264`` / ``h265``
#: 或 ffmpeg 的 ``hevc-nvenc`` 这类带连字符的形式. 以前这些值会命中 Literal 校验,
#: 报 ``literal_error`` 导致整份插件配置校验失败、插件根本装不上; 现在统一归一化到合法值.
ENCODER_ALIASES: dict[str, str] = {
    # 1.0.8: 档位只有 4 个. 任何具体的 ffmpeg 编码器名都收敛到对应家族,
    # 家族内部再按 nvenc_codec / qsv_codec 选具体格式.
    "": "auto",
    "auto": "auto",
    "default": "auto",
    "hw": "auto",
    "hardware": "auto",
    "自动": "auto",
    "硬件": "auto",
    "硬件编码": "auto",
    "cpu": "cpu",
    "libx264": "cpu",
    "x264": "cpu",
    "h264": "cpu",
    "avc": "cpu",
    "软编": "cpu",
    "软件编码": "cpu",
    "libx265": "cpu",
    "x265": "cpu",
    "h265": "cpu",
    "hevc": "cpu",
    "软编hevc": "cpu",
    "nvenc": "nvenc",
    "nv": "nvenc",
    "nvidia": "nvenc",
    "cuda": "nvenc",
    "n卡": "nvenc",
    "nvidia_nvenc": "nvenc",
    "h264_nvenc": "nvenc",
    "nvenc_h264": "nvenc",
    "h264_nvidia": "nvenc",
    "hevc_nvenc": "nvenc",
    "h265_nvenc": "nvenc",
    "nvenc_hevc": "nvenc",
    "nvenc_h265": "nvenc",
    "av1_nvenc": "nvenc",
    "av1": "nvenc",
    "av1nvenc": "nvenc",
    "nvenc_av1": "nvenc",
    "nvidia_av1": "nvenc",
    "av1_nvidia": "nvenc",
    "av01": "nvenc",
    "qsv": "qsv",
    "intel": "qsv",
    "intel_qsv": "qsv",
    "quicksync": "qsv",
    "vaapi": "qsv",
    "核显": "qsv",
    "h264_qsv": "qsv",
    "qsv_h264": "qsv",
    "hevc_qsv": "qsv",
    "h265_qsv": "qsv",
    "qsv_hevc": "qsv",
    "qsv_h265": "qsv",
}

#: 1.0.8: N 卡 / 核显内部转码格式的写法归一化.
NVENC_CODEC_ALIASES: dict[str, str] = {
    "": "auto", "auto": "auto", "自动": "auto", "默认": "auto",
    "av1": "av1", "av01": "av1", "av1_nvenc": "av1", "nvenc_av1": "av1",
    "hevc": "hevc", "h265": "hevc", "hevc_nvenc": "hevc", "h265_nvenc": "hevc",
    "h264": "h264", "h.264": "h264", "avc": "h264", "h264_nvenc": "h264",
}
QSV_CODEC_ALIASES: dict[str, str] = {
    "": "auto", "auto": "auto", "自动": "auto", "默认": "auto",
    "hevc": "hevc", "h265": "hevc", "hevc_qsv": "hevc", "h265_qsv": "hevc",
    "h264": "h264", "h.264": "h264", "avc": "h264", "h264_qsv": "h264",
}

#: 1.0.4: 转码预设的别名表. 有人直接填 ffmpeg 的 ``slow`` / ``veryslow`` 或 NVENC 的
#: ``p1``..``p7``; 越慢的档位对 NAS 实时转码并不友好, 统一收敛到本插件支持的六档.
PRESET_ALIASES: dict[str, str] = {
    "": "veryfast",
    "ultrafast": "ultrafast",
    "superfast": "superfast",
    "veryfast": "veryfast",
    "faster": "faster",
    "fast": "fast",
    "medium": "medium",
    "slow": "medium",
    "slower": "medium",
    "veryslow": "medium",
    "placebo": "medium",
    "p1": "ultrafast",
    "p2": "superfast",
    "p3": "veryfast",
    "p4": "faster",
    "p5": "fast",
    "p6": "medium",
    "p7": "medium",
    "最快": "ultrafast",
    "快": "veryfast",
    "中": "medium",
    "慢": "medium",
}

#: 源编码 -> NVIDIA CUVID 硬解器. 只用于真正转码 (非 copy) 且编码器为 nvenc 的情况.
#: 不用 ``-hwaccel cuda``: jellyfin-ffmpeg 8.x 上该参数会静默回退到软解
#: (日志里显示 ``h264 (native) -> hevc (hevc_nvenc)``), 必须显式指定 cuvid 解码器才真走 GPU.
CUVID_DECODERS: dict[str, str] = {
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "mpeg2": "mpeg2_cuvid",
    "mpeg4": "mpeg4_cuvid",
    "vc1": "vc1_cuvid",
    "vp9": "vp9_cuvid",
    "av1": "av1_cuvid",
}

#: 走 NVIDIA 硬件编码的编码器名字; 与 CUVID 硬解配套使用.
NVENC_ENCODERS = ("h264_nvenc", "hevc_nvenc", "av1_nvenc")
#: Intel 核显 / 独显的 Quick Sync 编码器 (VAAPI 之上的 QSV 链路).
QSV_ENCODERS = ("h264_qsv", "hevc_qsv")
#: QSV 不认识 x264 的 ultrafast/superfast, 统一映射到它能接受的最快档.
QSV_PRESETS = {
    "ultrafast": "veryfast",
    "superfast": "veryfast",
    "veryfast": "veryfast",
    "faster": "faster",
    "fast": "fast",
    "medium": "medium",
}

#: 找不到 ffmpeg 时内置的默认下载源: gh-proxy.com 是国内可达的 GitHub 发布包代理,
#: 2026-11 实测 HEAD 200、可完整下载 (约 88KB/s, 140MB 包需 20 分钟左右, 仅首装触发一次).
FFMPEG_DEFAULT_URL = (
    "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
)
#: 备份下载源 (顺序尝试).
FFMPEG_FALLBACK_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
    "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
)

#: 播放清晰度档位: (目标高度, 展示名). 0 表示原画. 直通文件只有原画一档.
QUALITY_LEVELS: tuple[tuple[int, str], ...] = (
    (0, "原画"),
    (1080, "1080P"),
    (720, "720P"),
    (480, "480P"),
    (360, "360P"),
)

class PluginSettings(BaseModel):
    """插件配置. 全部有默认值, 不填也能用.

    校验策略与样本一致: ``extra`` 用 ``ignore`` (别处拷来的多余键不导致整份配置失败),
    ``encoder`` / ``preset`` / ``nvenc_codec`` / ``qsv_codec`` 写前归一化, 认不出的值
    退回默认并记 warning, 不抛 ``literal_error``.
    """

    model_config = ConfigDict(extra="ignore")

    resolver_url: str = Field(
        default="",
        title="解析接口地址",
        description=(
            "必填. GET 请求地址, 用 {number} 占位条目番号, 例如 http://nas:8096/resolve/{number}."
            " 返回 JSON: {\"streams\": [{\"url\", \"name\"?, \"headers\"?, "
            "\"subtitles\": [{\"id\"?, \"label\", \"language\"?, \"url\"}]}]} 或直接返回纯文本媒体 URL."
        ),
    )
    resolver_token: str = Field(
        default="",
        title="解析接口令牌",
        description="非空时作为 Authorization: Bearer <令牌> 发给解析接口; 令牌只写在运行配置里, 绝不入库",
    )
    resolver_timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        title="解析超时 (秒)",
        description="解析接口多久不响应就放弃",
    )
    direct_play: bool = Field(
        default=True,
        title="允许直连播放",
        description=(
            "开启后, 浏览器能解码的在线源 (容器/编码直通) 直接跳上游播放, 不转码;"
            " 关闭则所有来源一律走转码 HLS"
        ),
    )
    ffmpeg_path: str = Field(
        default="",
        title="ffmpeg 路径",
        description="留空则依次在 PATH、插件数据目录的 bin/、Jellyfin ffmpeg 与常见安装位置查找",
    )
    ffmpeg_bundle_path: str = Field(
        default="",
        title="ffmpeg 压缩包路径",
        description="可选: 填本机已有的 ffmpeg 压缩包 (zip / tar.gz / tar.xz) 绝对路径, 插件找不到 ffmpeg 时自动解压部署到插件数据目录的 bin/, 无需网络下载",
    )
    ffmpeg_download_url: str = Field(
        default="",
        title="ffmpeg 下载地址 (留空用内置国内镜像)",
        description="找不到 ffmpeg 时自动从此地址下载静态版 ffmpeg 压缩包并部署; 留空使用内置国内高速镜像 (gh-proxy.com 代理 GitHub 发布包)",
    )
    encoder: Encoder = Field(
        default="auto",
        title="编码器",
        description="auto: 先试 N 卡 HEVC/H264, 再试 Intel 核显 HEVC/H264, 都不行用 CPU 软编; nvenc: 只用 N 卡 (格式见下一项); qsv: 只用 Intel 核显 (格式见「核显转码格式」); cpu: 软编 H.264",
    )
    nvenc_codec: NvencCodec = Field(
        default="auto",
        title="N 卡转码格式 (encoder=nvenc 时生效)",
        description="auto 优先 HEVC (同码率画质更好), 失败退 H.264; av1 只有 RTX 40 系及以上的 AV1 硬编可用, 老卡会自动退 HEVC / H.264",
    )
    qsv_codec: QsvCodec = Field(
        default="auto",
        title="核显转码格式 (encoder=qsv 时生效)",
        description="auto 优先 HEVC, 不支持时退 H.264",
    )
    preset: Preset = Field(
        default="veryfast",
        title="转码预设",
        description="越慢越清晰; 只影响需要重新编码的格式",
    )
    crf: int = Field(
        default=23,
        ge=0,
        le=51,
        title="转码画质 (CRF)",
        description="越小越清晰, 越大越省 CPU; 只影响需要重新编码的格式",
    )
    max_height: int = Field(
        default=0,
        ge=0,
        le=4320,
        title="最大高度",
        description="0 保持原分辨率; 填 1080 可明显降低 4K 素材的转码压力",
    )
    idle_timeout: int = Field(
        default=900,
        ge=60,
        le=86400,
        title="缓存保留 (秒)",
        description="这么久没有请求就结束转码进程, 并删除已转好的分片缓存",
    )

    @field_validator("encoder", mode="before")
    @classmethod
    def _normalize_encoder(cls, value: object) -> str:
        """把各种写法的编码器名收敛到 :data:`Encoder` 的合法值.

        认不出的值退回 ``auto`` 并写 warning: 用户宁可要一个能用的默认值, 也不要因为一个
        拼错的枚举值导致插件装不上.
        """
        if value is None:
            return "auto"
        if not isinstance(value, str):
            logger.warning("encoder 配置不是字符串 (%r), 退回 auto", value)
            return "auto"
        key = _setting_key(value)
        mapped = ENCODER_ALIASES.get(key)
        if mapped is not None:
            return mapped
        logger.warning(
            "未识别的 encoder 配置 %r, 退回 auto (可用值: %s)",
            value,
            ", ".join(sorted(set(ENCODER_ALIASES.values()))),
        )
        return "auto"

    @field_validator("nvenc_codec", mode="before")
    @classmethod
    def _normalize_nvenc_codec(cls, value: object) -> str:
        if not isinstance(value, str):
            return "auto"
        mapped = NVENC_CODEC_ALIASES.get(_setting_key(value))
        if mapped is None:
            logger.warning("未识别的 nvenc_codec 配置 %r, 退回 auto", value)
            return "auto"
        return mapped

    @field_validator("qsv_codec", mode="before")
    @classmethod
    def _normalize_qsv_codec(cls, value: object) -> str:
        if not isinstance(value, str):
            return "auto"
        mapped = QSV_CODEC_ALIASES.get(_setting_key(value))
        if mapped is None:
            logger.warning("未识别的 qsv_codec 配置 %r, 退回 auto", value)
            return "auto"
        return mapped

    @field_validator("preset", mode="before")
    @classmethod
    def _normalize_preset(cls, value: object) -> str:
        """同上: 兼容 ffmpeg / NVENC 的预设写法."""
        if value is None:
            return "veryfast"
        if not isinstance(value, str):
            logger.warning("preset 配置不是字符串 (%r), 退回 veryfast", value)
            return "veryfast"
        key = _setting_key(value)
        mapped = PRESET_ALIASES.get(key)
        if mapped is not None:
            return mapped
        logger.warning("未识别的 preset 配置 %r, 退回 veryfast", value)
        return "veryfast"
@dataclass(frozen=True, slots=True)
class Tools:
    """本机可用的 ffmpeg 与选定的视频编码器."""

    ffmpeg: Path
    encoder: str
    #: 该 ffmpeg 实际支持的 CUVID 硬解器名字 (空 = 不支持硬解, 只能软解).
    hw_decoders: tuple[str, ...] = ()
    #: 是否带 scale_cuda: 有它才能让解码后的帧留在显存里直接缩放 + 编码, 全程不落内存.
    hw_scale: bool = False
    #: h264_nvenc 是否真的能跑: 配置选 hevc_nvenc 时, H.264 兜底档也应该走硬件,
    #: 否则老浏览器一播就是 libx264 软编, 4K 源直接把 CPU 打满.
    h264_nvenc: bool = False
    #: hevc_nvenc 是否真的能跑: 配置选 av1 / h264 时, HEVC 档也应该留在显卡上.
    #: 否则那一档会退到 libx265 软编, 而显存里的帧喂不进软编 (1.0.21 修复的根因).
    hevc_nvenc: bool = False
    #: h264_qsv 是否真的能跑: 选了 hevc_qsv 时, H.264 兜底档也应该留在核显上,
    #: 否则老浏览器一播就是 libx264 软编.
    h264_qsv: bool = False


@dataclass(frozen=True, slots=True)
class MediaCodecs:
    """ffmpeg 认出的编码、时长与帧率; ``None`` 字段表示没认出来."""

    video: str | None
    height: int | None
    audio: str | None
    duration: float | None
    fps: float | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """一条文件该怎么播."""

    kind: Literal["direct", "hls", "unavailable"]
    content_type: str = "video/mp4"
    unavailable: str | None = None
    seekable: bool = True


# ---------------------------------------------------------------------------
# 工具链探测
# ---------------------------------------------------------------------------


def _configured_tool(raw: str, name: str) -> Path | None:
    text = raw.strip().strip('"')
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_dir():
        candidate = candidate / f"{name}.exe" if os.name == "nt" else candidate / name
    return candidate


def _auto_locations(data_dir: Path, name: str) -> list[Path]:
    """按可能性从高到低列出自动查找位置."""
    binary = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = [data_dir / "bin" / binary, data_dir / binary]
    if os.name != "nt":
        # NAS / Jellyfin 常见部署位置优先于 PATH: run-ffmpeg.sh 是带 NVENC 库的
        # jellyfin ffmpeg, 硬件编码比 PATH 里的静态版 (无 NVENC) 快一个数量级.
        candidates += [
            Path("/opt/ffmpeg_jf/run-ffmpeg.sh"),
            Path("/opt/ffmpeg_jf/ffmpeg"),
            Path("/usr/lib/jellyfin-ffmpeg/ffmpeg"),
        ]
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    environment = os.environ.get("FFMPEG_PATH", "").strip()
    if environment:
        base = Path(environment)
        candidates.append(base / binary if base.is_dir() else base)
    if os.name != "nt":
        candidates += [
            Path("/usr/local/bin/ffmpeg"),
            Path("/usr/bin/ffmpeg"),
        ]
    roots = [Path("C:/ffmpeg/bin"), Path("C:/ffmpeg")]
    for variable, tail in (
        ("ProgramFiles", ("ffmpeg", "bin")),
        ("LOCALAPPDATA", ("Programs", "ffmpeg", "bin")),
        ("LOCALAPPDATA", ("Microsoft", "WinGet", "Links")),
        ("ProgramData", ("chocolatey", "bin")),
    ):
        root = os.environ.get(variable, "").strip()
        if root:
            roots.append(Path(root).joinpath(*tail))
    for root in roots:
        candidates.append(root / binary)
    return candidates


def _runs(binary: Path) -> bool:
    """真的执行一次 ``-version``: 存在但跑不起来的文件不能算找到."""
    try:
        completed = subprocess.run(
            [str(binary), "-hide_banner", "-version"],
            capture_output=True,
            timeout=10.0,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _available_decoders(ffmpeg: Path) -> tuple[str, ...]:
    """列出该 ffmpeg 支持的 CUVID 硬解器 (h264_cuvid / hevc_cuvid ...).

    没有 N 卡驱动时 jellyfin-ffmpeg 也会把 cuvid 列出来, 真正能不能用由调用时的
    失败回退兜底; 这里只负责排除根本没有硬解的构建 (如静态版 johnvansickle).
    """
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-decoders"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    text = completed.stdout.decode("utf-8", "replace")
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith("_cuvid"):
            names.add(parts[1])
    return tuple(sorted(names))


def _has_filter(ffmpeg: Path, name: str) -> bool:
    """该 ffmpeg 是否带某个滤镜 (用来判断 scale_cuda 这类 GPU 缩放能力)."""
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-filters"],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = completed.stdout.decode("utf-8", "replace")
    return any(parts[1] == name for parts in (line.split() for line in text.splitlines()) if len(parts) >= 2)


def _encoder_works(ffmpeg: Path, encoder: str) -> bool:
    """空跑三帧, 确认这个编码器在本机真的能用.

    只是列在 ``-encoders`` 里不够: nvenc 在没有 N 卡、驱动太旧或显存被占满时都会失败.
    """
    if encoder == "libx264":
        return True
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360:r=10:d=0.4",
        "-frames:v",
        "3",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _choose_encoder(settings: PluginSettings, ffmpeg: Path) -> str:
    """按配置与可用性挑视频编码器.

    1.0.8: 档位收敛为 4 档 (auto / nvenc / qsv / cpu):

    * auto  —— N 卡 HEVC/H264 -> Intel 核显 HEVC/H264 -> CPU 软编;
    * nvenc —— 只用 N 卡, 具体格式由 ``nvenc_codec`` 决定 (auto 时 HEVC 优先);
    * qsv   —— 只用 Intel 核显, 具体格式由 ``qsv_codec`` 决定 (auto 时 HEVC 优先);
    * cpu   —— 软编 H.264 (兼容性优先).

    选中的编码器跑不起来时一律退 ``libx264``: 硬件编码再快, 也不能让视频播不出来.
    """
    choice = settings.encoder
    if choice == "cpu":
        return "libx264"
    if choice == "nvenc":
        if settings.nvenc_codec == "h264":
            return "h264_nvenc" if _encoder_works(ffmpeg, "h264_nvenc") else "libx264"
        if settings.nvenc_codec == "av1":
            # AV1 硬编只有 Ada / Blackwell 有; 老卡会报 "No capable devices found",
            # 必须先空跑确认, 再按 HEVC -> H.264 退.
            for candidate in ("av1_nvenc", "hevc_nvenc", "h264_nvenc"):
                if _encoder_works(ffmpeg, candidate):
                    return candidate
            return "libx264"
        for candidate in ("hevc_nvenc", "h264_nvenc"):
            if _encoder_works(ffmpeg, candidate):
                return candidate
        return "libx264"
    if choice == "qsv":
        if settings.qsv_codec == "h264":
            return "h264_qsv" if _encoder_works(ffmpeg, "h264_qsv") else "libx264"
        for candidate in ("hevc_qsv", "h264_qsv"):
            if _encoder_works(ffmpeg, candidate):
                return candidate
        return "libx264"
    # auto: N 卡 HEVC/H264 -> 核显 HEVC/H264 -> CPU
    for candidate in ("hevc_nvenc", "h264_nvenc", "hevc_qsv", "h264_qsv"):
        if _encoder_works(ffmpeg, candidate):
            return candidate
    return "libx264"


def _discover_tools(settings: PluginSettings, data_dir: Path) -> Tools | None:
    candidates: list[Path] = []
    configured = _configured_tool(settings.ffmpeg_path, "ffmpeg")
    if configured is not None:
        candidates.append(configured)
    candidates.extend(_auto_locations(data_dir, "ffmpeg"))
    ffmpeg: Path | None = None
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        if _runs(candidate):
            ffmpeg = candidate
            break
    if ffmpeg is None:
        return None
    encoder = _choose_encoder(settings, ffmpeg)
    hardware = encoder in NVENC_ENCODERS
    qsv = encoder in QSV_ENCODERS
    return Tools(
        ffmpeg=ffmpeg,
        encoder=encoder,
        hw_decoders=_available_decoders(ffmpeg) if hardware else (),
        hw_scale=hardware and _has_filter(ffmpeg, "scale_cuda"),
        h264_nvenc=(
            _encoder_works(ffmpeg, "h264_nvenc")
            if hardware and encoder != "h264_nvenc"
            else hardware
        ),
        hevc_nvenc=(
            _encoder_works(ffmpeg, "hevc_nvenc")
            if hardware and encoder != "hevc_nvenc"
            else hardware
        ),
        h264_qsv=(
            _encoder_works(ffmpeg, "h264_qsv")
            if qsv and encoder != "h264_qsv"
            else qsv
        ),
    )


# ---------------------------------------------------------------------------
# ffmpeg 自动部署 (完全本地化: 上传的包 / 本机包 / 国内镜像下载, 无其它外部依赖)
# ---------------------------------------------------------------------------

_BUNDLE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.xz")


def _bundle_suffix(bundle: Path) -> str | None:
    name = bundle.name.casefold()
    for suffix in _BUNDLE_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return None


def _candidate_bundles(settings: PluginSettings, data_dir: Path) -> list[Path]:
    """按优先级列出待部署的 ffmpeg 压缩包: 设置里指定的 -> 插件数据目录里的 -> 数据目录 bin 旁."""
    candidates: list[Path] = []
    configured = settings.ffmpeg_bundle_path.strip().strip('"')
    if configured:
        candidates.append(Path(configured))
    for root in (data_dir, data_dir / "bin"):
        with contextlib.suppress(OSError):
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if entry.is_file() and "ffmpeg" in entry.name.casefold() and _bundle_suffix(entry):
                    candidates.append(entry)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _unpack_bundle(bundle: Path, dest_dir: Path) -> Path | None:
    """把 ffmpeg 压缩包解到 dest_dir, 返回解出的 ffmpeg 可执行文件路径.

    支持三种常见包形态: 顶层是 bin/ffmpeg 的发布包、包内直接是 ffmpeg 单文件、
    Windows 的 ffmpeg.exe. 返回的二进制若有问题由后续 _runs 把关.
    """
    suffix = _bundle_suffix(bundle)
    if suffix is None:
        return None
    import tarfile
    import zipfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        if suffix == ".zip":
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(dest_dir)
        else:
            with tarfile.open(bundle, "r:*") as archive:
                archive.extractall(dest_dir, filter="data")
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return None

    binary = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found: Path | None = None
    for candidate in dest_dir.rglob(binary):
        if not candidate.is_file():
            continue
        if os.name == "nt":
            found = candidate
            break
        try:
            candidate.chmod(0o755)
        except OSError:
            pass
        if _runs(candidate):
            found = candidate
            break
    if found is None:
        return None
    # 统一放到 bin/ffmpeg: 后续探测路径稳定, 升级包时直接覆盖.
    target = dest_dir / "bin" / binary
    target.parent.mkdir(parents=True, exist_ok=True)
    if found != target:
        if target.exists():
            target.unlink()
        shutil.move(str(found), target)
    try:
        target.chmod(0o755)
    except OSError:
        pass
    return target


def _fetch_bundle(url: str, dest_dir: Path) -> Path | None:
    """下载压缩包 (流式写盘, 不占内存) 并返回路径; 失败返回 None."""
    import urllib.request

    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / f"ffmpeg-bundle-{int(time.time())}.tmp"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 amane-plugin"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(local, "wb") as out:
            shutil.copyfileobj(response, out, length=1 << 20)
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):  # type: ignore[attr-defined]
        with contextlib.suppress(OSError):
            local.unlink()
        return None
    return local if local.exists() else None


def _deploy_ffmpeg(settings: PluginSettings, data_dir: Path) -> Path | None:
    """部署流程: 尝试所有候选压缩包 (含下载), 返回部署好的 ffmpeg 路径或 None.

    全程不引入网络之外的其它外部依赖; 下载只作为最后手段, 且源由用户配置或内置国内镜像.
    """
    for bundle in _candidate_bundles(settings, data_dir):
        deployed = _unpack_bundle(bundle, data_dir)
        if deployed is not None and _runs(deployed):
            return deployed
    urls = [settings.ffmpeg_download_url.strip().strip('"'), FFMPEG_DEFAULT_URL, *FFMPEG_FALLBACK_URLS]
    tried: set[str] = set()
    for url in urls:
        if not url or url in tried:
            continue
        tried.add(url)
        bundle = _fetch_bundle(url, data_dir / "downloads")
        if bundle is None:
            continue
        deployed = _unpack_bundle(bundle, data_dir)
        with contextlib.suppress(OSError):
            bundle.unlink()
        if deployed is not None and _runs(deployed):
            return deployed
    return None


# ---------------------------------------------------------------------------
# 编码与时长探测
# ---------------------------------------------------------------------------

_CODEC_CACHE: dict[str, MediaCodecs | None] = {}

#: ``ffmpeg -i`` 的流信息行, 例如 ``Stream #0:0: Video: h264 (High), yuv420p, 1920x1080 ...``.
_STREAM_LINE = re.compile(r"Stream #\d+:\d+[^:]*: (Video|Audio): ([A-Za-z0-9_]+)")
_STREAM_SIZE = re.compile(r", (\d{2,5})x(\d{2,5})[\s,\[]")
_STREAM_FPS = re.compile(r"(\d+(?:\.\d+)?) fps")
_DURATION = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _cache_codecs(key: str, codecs: MediaCodecs | None) -> None:
    if len(_CODEC_CACHE) >= _CODEC_CACHE_SIZE:
        _CODEC_CACHE.clear()
    _CODEC_CACHE[key] = codecs


def _parse_streams(output: str) -> MediaCodecs | None:
    """从 ``ffmpeg -i`` 的输出里取出正片编码与总时长.

    不用 ffprobe: 它不保证和 ffmpeg 一起安装 (例如 gyan 的 essentials 单文件分发就只有 ffmpeg),
    而 ``ffmpeg -i`` 打印的流信息已经够判断能不能直通.
    """
    video: str | None = None
    height: int | None = None
    audio: str | None = None
    duration: float | None = None
    fps: float | None = None
    for line in output.splitlines():
        if duration is None:
            found = _DURATION.search(line)
            if found is not None:
                duration = int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3))
        match = _STREAM_LINE.search(line)
        if match is None:
            continue
        kind, codec = match.group(1), match.group(2)
        if kind == "Video":
            # 封面图 (mkv 常见) 也是 video 流, 不能当成正片.
            if video is not None or "attached pic" in line:
                continue
            video = codec
            size = _STREAM_SIZE.search(line)
            if size is not None:
                height = int(size.group(2))
            rate = _STREAM_FPS.search(line)
            if rate is not None:
                fps = float(rate.group(1))
        elif audio is None:
            audio = codec
    if video is None and audio is None:
        return None
    return MediaCodecs(video=video, height=height, audio=audio, duration=duration, fps=fps)


def _headers_args(headers: dict[str, str] | None) -> list[str]:
    """把请求头转成 ffmpeg 的 ``-headers`` 参数 (必须放在 ``-i`` 之前)."""
    if not headers:
        return []
    text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    return ["-headers", text]


def _probe_codecs(ffmpeg: Path, source: str, headers: dict[str, str] | None = None) -> MediaCodecs | None:
    """读流信息取出编码与时长. ``source`` 是在线媒体 URL.

    ``ffmpeg -i`` 不带输出文件必然以非零码退出, 有用的是它打印的信息, 不是退出码.
    需要鉴权的上游经 ``headers`` 传请求头 (ffmpeg 自己会对 HTTP 源发这些头).
    """
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        *_headers_args(headers),
        "-i",
        source,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_streams(completed.stderr.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# 分片命名与时长
# ---------------------------------------------------------------------------


def segment_name(index: int) -> str:
    return f"seg{index:05d}.m4s"


def segment_seconds(codecs: MediaCodecs | None) -> float:
    """每段时长, 取整到帧.

    关键帧只能落在帧上: 29.97 fps 下 2 秒是 59.94 帧, ffmpeg 会把关键帧放在第 60 帧 (2.002
    秒). 清单里的 ``#EXTINF`` 与分片的绝对时间戳必须用这个"整帧时长", 否则每段差几毫秒, 放到
    两小时的片子上就是几秒的错位.
    """
    fps = codecs.fps if codecs is not None else None
    if not fps or fps <= 0:
        return float(SEGMENT_SECONDS)
    frames = max(1, round(SEGMENT_SECONDS * fps))
    return frames / fps


def total_segments(duration: float, seconds: float) -> int:
    """整部片子按 ``seconds`` 一刀切会有多少段."""
    return max(1, math.ceil(duration / seconds - 0.05))
def _mp4_boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos, pos + header, pos + size
        pos += size


def _mp4_child(data: bytes, kind: bytes, start: int, end: int):
    for name, box_start, payload, box_end in _mp4_boxes(data, start, end):
        if name == kind:
            return box_start, payload, box_end
    return None


def track_timescales(init_path: Path) -> dict[int, int]:
    """从 init.mp4 里读 ``track_id -> timescale``."""
    try:
        data = init_path.read_bytes()
    except OSError:
        return {}
    moov = _mp4_child(data, b"moov", 0, len(data))
    if moov is None:
        return {}
    _, moov_payload, moov_end = moov
    scales: dict[int, int] = {}
    for kind, _, trak_payload, trak_end in _mp4_boxes(data, moov_payload, moov_end):
        if kind != b"trak":
            continue
        tkhd = _mp4_child(data, b"tkhd", trak_payload, trak_end)
        mdia = _mp4_child(data, b"mdia", trak_payload, trak_end)
        if tkhd is None or mdia is None:
            continue
        _, tkhd_payload, _ = tkhd
        offset = 20 if data[tkhd_payload] == 1 else 12
        track_id = struct.unpack_from(">I", data, tkhd_payload + offset)[0]
        _, mdia_payload, mdia_end = mdia
        mdhd = _mp4_child(data, b"mdhd", mdia_payload, mdia_end)
        if mdhd is None:
            continue
        _, mdhd_payload, _ = mdhd
        mdhd_offset = 20 if data[mdhd_payload] == 1 else 12
        timescale = struct.unpack_from(">I", data, mdhd_payload + mdhd_offset)[0]
        if timescale:
            scales[track_id] = timescale
    return scales


def fragment_first_tfdt(path: Path) -> tuple[int, int] | None:
    """取分片第一个 traf 的 ``(track_id, tfdt)``."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    moof = _mp4_child(data, b"moof", 0, len(data))
    if moof is None:
        return None
    _, moof_payload, moof_end = moof
    for kind, _, traf_payload, traf_end in _mp4_boxes(data, moof_payload, moof_end):
        if kind != b"traf":
            continue
        tfhd = _mp4_child(data, b"tfhd", traf_payload, traf_end)
        tfdt = _mp4_child(data, b"tfdt", traf_payload, traf_end)
        if tfhd is None or tfdt is None:
            continue
        _, tfhd_payload, _ = tfhd
        track_id = struct.unpack_from(">I", data, tfhd_payload + 4)[0]
        _, tfdt_payload, _ = tfdt
        if data[tfdt_payload] == 1:
            value = struct.unpack_from(">Q", data, tfdt_payload + 4)[0]
        else:
            value = struct.unpack_from(">I", data, tfdt_payload + 4)[0]
        return track_id, int(value)
    return None


def shift_fragment(path: Path, shift_seconds: float, scales: dict[int, int]) -> bool:
    """把分片里每个 ``traf`` 的时间戳整体平移 ``shift_seconds``.

    平移而不是设成绝对值: 一轮转码内部的时间轴本来就是连续的实数时长, 整体挪一下就能对上清单,
    各段之间不会出现几十毫秒的空洞.
    """
    if not scales or shift_seconds == 0:
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    data = bytearray(raw)
    moof = _mp4_child(bytes(data), b"moof", 0, len(data))
    if moof is None:
        return False
    _, moof_payload, moof_end = moof
    changed = False
    for kind, _, traf_payload, traf_end in _mp4_boxes(bytes(data), moof_payload, moof_end):
        if kind != b"traf":
            continue
        tfhd = _mp4_child(bytes(data), b"tfhd", traf_payload, traf_end)
        tfdt = _mp4_child(bytes(data), b"tfdt", traf_payload, traf_end)
        if tfhd is None or tfdt is None:
            continue
        _, tfhd_payload, _ = tfhd
        track_id = struct.unpack_from(">I", data, tfhd_payload + 4)[0]
        timescale = scales.get(track_id)
        if not timescale:
            continue
        delta = int(round(shift_seconds * timescale))
        _, tfdt_payload, _ = tfdt
        version = data[tfdt_payload]
        if version == 1:
            value = struct.unpack_from(">Q", data, tfdt_payload + 4)[0]
            struct.pack_into(">Q", data, tfdt_payload + 4, max(0, value + delta))
        else:
            value = struct.unpack_from(">I", data, tfdt_payload + 4)[0]
            struct.pack_into(">I", data, tfdt_payload + 4, max(0, min(0xFFFFFFFF, value + delta)))
        changed = True
    if changed:
        try:
            path.write_bytes(bytes(data))
        except OSError:
            return False
    return changed


def _video_encoder(tools: Tools, codec: str) -> str:
    """``codec`` 这一档最终落地的编码器 (硬件不可用时按家族内退, 绝不跨界软编).

    1.0.21 修复的根因就在这里被发现: 以前调用方只看 ``tools.encoder`` 就决定"解码帧
    留在显存", 但真正落地的编码器可能已经退成 libx265/libx264 —— 显存帧 (cuda / qsv)
    喂不进软编, ffmpeg 在编码前直接报
    ``Impossible to convert between the formats supported by the filter 'Parsed_null_0'``
    并退出, 一个分片都不产出, 播放器表现就是"点播放没反应 / 无法播放此码流".
    """
    if codec == "av1":
        if tools.encoder == "av1_nvenc":
            return "av1_nvenc"
        return _video_encoder(tools, "hevc")
    if codec == "hevc":
        if tools.encoder == "hevc_nvenc":
            return "hevc_nvenc"
        if tools.encoder in NVENC_ENCODERS and tools.hevc_nvenc:
            return "hevc_nvenc"
        if tools.encoder in QSV_ENCODERS:
            return "hevc_qsv"
        return "libx265"
    if tools.encoder == "h264_nvenc" or (
        tools.encoder in NVENC_ENCODERS and tools.h264_nvenc
    ):
        return "h264_nvenc"
    if tools.encoder in QSV_ENCODERS and tools.h264_qsv:
        return "h264_qsv"
    return "libx264"


def _video_args(
    tools: Tools,
    settings: PluginSettings,
    seconds: float,
    codec: str = "hevc",
    *,
    encoder: str | None = None,
    cuda_frames: bool = False,
    qsv_frames: bool = False,
    fps: float | None = None,
    bitrate: int = 0,
) -> list[str]:
    """视频重编码参数: 关键帧对齐分片边界, 播放器才不会等到固定时长才切段.

    1.0.22: ``bitrate`` 是这一档的**码率上限** (kbps, 已经按编码器的省码特性缩放过),
    由调用方按 ``target_height`` 算好传进来. 以前上限是调用方统一追加的
    ``-maxrate/-bufsize``, 但那只对 NVENC 真生效:

    * QSV 走的是 ``-global_quality`` (ICQ) —— ICQ 模式直接忽略 maxrate, 实测 1080p
      噪声素材能冲到 26~35Mbps, 是声明带宽的 4 倍以上; 手机端一个 4 秒分片十几 MB,
      切档/拖动的请求根本追不上, 表现就是"切码率卡住、无法播放此码流".
    * 软编走 ``-crf``, 通用 `-maxrate` 对 libx264/libx265 不生效, 必须写进
      ``-x264-params`` / ``-x265-params`` 的 VBV 里才压得住.

    所以码率控制各编码器各自落实: 上限 ``cap``, 目标平均 ≈ ``0.72 * cap``
    (每个分片不追满上限, 给复杂画面留 VBV 冲刺余量, 同时保证"真码率"远低于上限).

    ``codec`` 决定输出编码: "hevc" 输出 HEVC (NVENC 或 libx265), "h264" 输出 H.264
    (NVENC 或 libx264). HEVC 是默认档, H.264 是给无 HEVC 解码的浏览器的兜底档.

    ``encoder`` 是这一档**实际落地**的编码器 (见 ``_video_encoder``); 编码参数一律按它
    选分支, 不再自己拿 ``tools.encoder`` 猜, 否则会出现"参数按 NVENC 给, 实际跑软编".
    """
    if encoder is None:
        encoder = _video_encoder(tools, codec)
    keyframes = ["-force_key_frames", f"expr:gte(t,n_forced*{seconds:.6f})"]
    # 1.0.5: 关键帧间隔 (按帧计) 必须与分片时长严格同源. 只给 -force_key_frames 时,
    # NVENC / 软编仍可能因为场景切换 (scenecut) 在段内插入额外的 I 帧, 或在段边界前
    # 一个 GOP 内塞进 B 帧反向参考 —— 分片首帧就不是 I 帧, 播放器每段要等参考帧到齐,
    # 表现就是"每几秒顿一下". 统一: GOP = 分片帧数, 场景切换关闭, B 帧关闭.
    gop = max(1, round((fps or 25.0) * seconds))
    # cuda 帧 (硬解硬缩, 帧全程留在显存) 不能指定 yuv420p: 那会强制把帧拉回内存再转一遍,
    # GPU 解码就白做了. 只有软解路径才需要显式 pix_fmt.
    pix = [] if (cuda_frames or qsv_frames) else ["-pix_fmt", "yuv420p"]
    # 1.0.22: 码率上限 / 目标平均. ``cap <= 0`` 表示调用方没给上限, 保持旧行为(只给质量参数).
    cap = max(int(bitrate or 0), 0)
    target = max(int(cap * 0.72), 500) if cap else 0

    def _nvenc_rate() -> list[str]:
        """NVENC 的 VBR 码率约束: ``-b:v 0`` (纯 CQ) 会让码率随画面复杂度乱窜."""
        if not cap:
            return ["-b:v", "0"]
        return [
            "-b:v",
            _rate_text(target),
            "-maxrate",
            _rate_text(cap),
            "-bufsize",
            _rate_text(cap * 2),
        ]
    # AV1 只是"期望"的输出格式: 编码器回退到 HEVC/H.264 时必须跟着降级, 否则会拿
    # av1_nvenc 之外的编码器去产 av1, 直接报错. 降级后按真正落地的编码器重算.
    if codec == "av1" and encoder != "av1_nvenc":
        codec = "h264" if encoder in ("h264_nvenc", "h264_qsv", "libx264") else "hevc"
        encoder = _video_encoder(tools, codec)
    # Intel 核显: 编码走 QSV. 硬解时帧本来就在显存 (qsv 格式), 不能再指定 -pix_fmt,
    # 否则会把帧拉回内存白解一次.
    if encoder in QSV_ENCODERS:
        qsv_encoder = "hevc_qsv" if codec == "hevc" else "h264_qsv"
        if qsv_encoder:
            # 1.0.22: QSV 必须用 VBR 目标码率, **不能**用 -global_quality (ICQ).
            # ICQ 是恒定量化模式, 它会直接无视 -maxrate/-bufsize —— 实测 1080p 高动态
            # 素材上 ICQ23 能跑到 35Mbps, 是声明带宽的 5 倍, 一个 4 秒分片十几 MB,
            # 手机端切档/拖动请求追不上, 表现为卡住或 "无法播放此码流".
            # 实测 -b:v 目标 + -maxrate 上限可稳稳压在声明值附近 (误差 3~5%).
            rate = (
                [
                    "-b:v",
                    _rate_text(target),
                    "-maxrate",
                    _rate_text(cap),
                    "-bufsize",
                    _rate_text(cap * 2),
                ]
                if cap
                else ["-global_quality", str(settings.crf)]
            )
            # QSV 不响应 -force_key_frames (实测关键帧仍按自身 GOP 走), 只能按帧数给 -g;
            # 分片靠关键帧切, GOP 必须正好等于分片时长 (按帧取整, 与 _segment_seconds 同源).
            return [
                "-c:v",
                qsv_encoder,
                "-preset",
                QSV_PRESETS.get(settings.preset, "veryfast"),
                *rate,
                "-g",
                str(gop),
                # 分片首帧必须是 IDR, 否则播放器从段中间起播会花屏.
                "-forced_idr",
                "1",
                # QSV 默认开 B 帧 (含 b-pyramid), HLS 分片跨段参考会让部分播放器
                # 在段首解码失败 ("无法播放此码流"). 关掉 B 帧换兼容性.
                "-bf",
                "0",
                *pix,
                *keyframes,
            ]
    if encoder == "av1_nvenc":
        # 1.0.5: NVENC AV1. 分片独立播放的前提同 HEVC: 关 B 帧、固定 GOP、强制 IDR.
        # AV1 的 -cq 区间与 HEVC 一致, 但同画质 q 值要略降才能对上观感.
        return [
            "-c:v",
            "av1_nvenc",
            "-preset",
            NVENC_PRESETS.get(settings.preset, "p5"),
            "-rc",
            "vbr",
            "-cq",
            str(settings.crf),
            *_nvenc_rate(),
            *pix,
            "-g",
            str(gop),
            "-bf",
            "0",
            "-forced-idr",
            "1",
            *keyframes,
        ]
    if encoder in ("hevc_nvenc", "libx265"):
        if encoder == "hevc_nvenc":
            return [
                "-c:v",
                "hevc_nvenc",
                "-preset",
                NVENC_PRESETS.get(settings.preset, "p5"),
                "-rc",
                "vbr",
                "-cq",
                str(settings.crf),
                *_nvenc_rate(),
                *pix,
                # 1.0.5: GOP 与分片时长同源 + 关 B 帧, 段首才是干净 IDR.
                # 硬件编码默认不响应强制关键帧, 不加这个的话分片会按自己的 GOP 切, 时长不可控.
                "-g",
                str(gop),
                "-bf",
                "0",
                "-forced-idr",
                "1",
                *keyframes,
            ]
        return [
            "-c:v",
            "libx265",
            "-preset",
            settings.preset,
            "-crf",
            str(settings.crf),
            "-pix_fmt",
            "yuv420p",
            # x265 的 scene cut 会在段内插 I 帧 / 让 GOP 提前收尾, 与分片边界错位.
            "-x265-params",
            # 1.0.22: 软编的码率上限必须写进 x265 的 VBV, 通用的 -maxrate 对它不生效
            # (实测 CRF23 声明 8M 的档位能跑到 12M+), 手机端一追不上就卡住.
            f"keyint={gop}:min-keyint={gop}:scenecut=0:open-gop=0:bframes=0"
            + (f":vbv-maxrate={cap}:vbv-bufsize={cap * 2}" if cap else ""),
            "-g",
            str(gop),
            "-bf",
            "0",
            *keyframes,
        ]
    # codec == "h264"
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            NVENC_PRESETS.get(settings.preset, "p3"),
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(settings.crf),
            *_nvenc_rate(),
            *pix,
            "-g",
            str(gop),
            "-bf",
            "0",
            # 硬件编码默认不响应强制关键帧, 不加这个的话分片会按自己的 GOP 切, 时长不可控.
            "-forced-idr",
            "1",
            *keyframes,
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
        "-pix_fmt",
        "yuv420p",
        # 1.0.5: 同上, 软编也要关场景切换 / B 帧, 否则段边界对不齐分片.
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        # 1.0.22: 同 x265, libx264 的码率上限只能写进 x264 的 VBV.
        *(["-x264-params", f"vbv-maxrate={cap}:vbv-bufsize={cap * 2}"] if cap else []),
        "-bf",
        "0",
        *keyframes,
    ]


# ---------------------------------------------------------------------------
# ffmpeg 命令行
# ---------------------------------------------------------------------------


def _ffmpeg_command(
    tools: Tools,
    source: str,
    headers: dict[str, str] | None,
    settings: PluginSettings,
    codecs: MediaCodecs | None,
    start_index: int,
    seconds: float,
    *,
    target_height: int = 0,
    codec: str = "hevc",
) -> list[str]:
    """组装 ffmpeg 命令行: 从在线 URL 拉流, 实时转码成 fMP4 HLS.

    输出名一律用相对路径: ffmpeg 以会话目录为工作目录运行 (见 ``StreamServer._spawn``),
    分片才会落在同一处, 清单里的 URI 也才是播放器能拼的短名字.

    ``start_index`` 大于 0 时从该分片的起点起转 (``-ss`` 放在 ``-i`` 前): 播放器拖到哪里
    就对哪里起转, 不必从头转. 在线源拖动依赖 HTTP Range 回溯, 主流服务器 (nginx /
    cloud storage) 都支持; 不支持 Range 的上游拖动会退到从头解码, 只是慢, 不会错.
    """
    command = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        # 探测开头 1MB 就够判流: 默认探测会把片头几十 MB 扫一遍, 实测首段白耗 42ms.
        "-probesize",
        "1000000",
        "-analyzeduration",
        "1000000",
        # 鉴权上游: 请求头必须放在 -i 之前才对输入生效.
        *_headers_args(headers),
    ]
    # 真转码且走 NVIDIA 硬件编码时, 解码也一并交给 GPU, 避免软解吃满 CPU.
    # 只写 ``-hwaccel cuda`` 是不够的 —— 没有 ``-hwaccel_output_format cuda`` 时
    # jellyfin-ffmpeg 8.x 会静默退回软解 (日志里是 ``h264 (native) -> hevc (hevc_nvenc)``),
    # CPU 照样打满. 只有解码输出格式也指定 cuda, 帧才留在显存, 由 scale_cuda + nvenc 接力.
    #
    # 先算出这一档**真正落地**的编码器, 再决定解码帧是否留在显存: 只看 tools.encoder 会
    # 让"退到软编的那一档"仍然带着显存帧参数, ffmpeg 在编码前直接报
    # "Impossible to convert between the formats supported by the filter ..." 并退出.
    if codec == "av1" and tools.encoder != "av1_nvenc":
        # 不会产 AV1 时先降级, 后续口令/码率/tag 都按降级后的编码算.
        codec = "h264" if tools.encoder in ("h264_nvenc", "h264_qsv") else "hevc"
    video_encoder = _video_encoder(tools, codec)
    hw_cuda_frames = bool(
        codecs is not None
        and codecs.video in ("h264", "hevc", "av1")
        and video_encoder in NVENC_ENCODERS
        and tools.hw_scale
        and codec in ("hevc", "h264", "av1")
    )
    # Intel 核显全链路: 解码帧留在 QSV 显存里, 由 vpp_qsv 缩放后直接交给 *_qsv 编码.
    hw_qsv_frames = bool(
        codecs is not None
        and codecs.video in ("h264", "hevc", "av1")
        and video_encoder in QSV_ENCODERS
        and codec in ("hevc", "h264", "av1")
    )
    hw_decode_codecs = {"h264", "hevc", "av1", "mpeg2video", "mpeg2", "mpeg4", "vc1"}
    if hw_qsv_frames:
        command += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    elif hw_cuda_frames:
        command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif codecs is not None and codecs.video is not None and video_encoder in NVENC_ENCODERS:
        decoder = CUVID_DECODERS.get(codecs.video)
        if decoder is not None and decoder in tools.hw_decoders:
            # 输入侧解码器: 必须写在 -i 之前才对输入生效 (输出侧另有 -c:v 指定 nvenc).
            command += ["-c:v", decoder]
        elif codecs.video in hw_decode_codecs:
            # 没有 cuvid 时的兜底 (部分自编译 ffmpeg 只带 -hwaccel cuda).
            command += ["-hwaccel", "cuda"]
    if start_index > 0:
        # 放在 -i 前面: 直接跳到关键帧, 不必先解码前面的内容.
        command += ["-ss", f"{start_index * seconds:.6f}"]
    command += [
        "-i",
        source,
        # ``V`` 排除封面图这类附加图片流, 只取正片.
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
    ]
    height = codecs.height if codecs is not None else None
    effective = target_height or settings.max_height
    # 给 VBR 加码率上限: 只给 -cq 时, 4K 高动态画面能冲到 30Mbps, 单段十几 MB,
    # 手机端拖动根本追不上.
    budget_height = effective or (height or 1080)
    maxrate = _maxrate_kbps(budget_height)
    if codec == "av1":
        # AV1 同画质比 H.264 省约 35%, 上限按同一比例降, 才能和主清单里
        # 声明的 BANDWIDTH 对上, 否则 ABR 会误判"带宽还有余量"而反复切档.
        maxrate = max(int(maxrate * 0.65), 700)
    elif codec == "hevc":
        # 同画质 HEVC 比 H.264 省约 25%: 上限也按这个比例降, 才对得上主清单里声明的带宽.
        maxrate = max(int(maxrate * 0.75), 800)
    # 上限由 ``_video_args`` 按各编码器自己的参数落实
    # (QSV 用 VBR, 软编用 x264/x265 的 VBV, NVENC 用 -b:v + -maxrate).
    command += _video_args(
        tools,
        settings,
        seconds,
        codec,
        encoder=video_encoder,
        cuda_frames=hw_cuda_frames,
        qsv_frames=hw_qsv_frames,
        fps=codecs.fps if codecs is not None else None,
        bitrate=maxrate,
    )
    if codec == "hevc":
        # fMP4 默认把 HEVC 标成 hev1, 而 Safari 只认 hvc1, Chrome 的 MSE 也偏向 hvc1:
        # 声明写 hvc1 实际是 hev1 时, 客户端会直接把这条变体过滤掉.
        command += ["-tag:v", "hvc1"]
    if effective and height is not None and height > effective:
        # 帧已在显存时用 scale_cuda, 否则 4K->1080P 的 swscale 会重新吃掉两三个核.
        if hw_qsv_frames:
            # vpp_qsv 只认 -1 保持比例 (不支持 -2); h264 输出固定 8bit,
            # 10bit 源的 p010 帧在 vpp 里顺手转 nv12.
            if codec == "h264":
                command += ["-vf", f"vpp_qsv=format=nv12:w=-1:h={effective}"]
            else:
                command += ["-vf", f"vpp_qsv=w=-1:h={effective}"]
        elif hw_cuda_frames:
            # h264 输出固定 8bit: 10bit 源的 cuda 帧是 p010, 直接喂 h264_nvenc 会失败,
            # 在 scale_cuda 里顺手转 nv12, 帧全程留在显存.
            if codec == "h264":
                command += ["-vf", f"scale_cuda=-2:{effective}:format=nv12"]
            else:
                command += ["-vf", f"scale_cuda=-2:{effective}"]
        else:
            command += ["-vf", f"scale=-2:{effective}"]
    elif hw_qsv_frames and codec == "h264":
        command += ["-vf", "vpp_qsv=format=nv12"]
    elif hw_cuda_frames and codec == "h264":
        # 不缩放时也要保证喂给 h264_nvenc 的是 8bit nv12.
        # 10bit 源的 cuda 帧是 p010, 直接给 h264_nvenc 会 "Frame format not supported".
        command += ["-vf", "scale_cuda=format=nv12"]
    # 音频一律重新编码: AAC 帧长 1024 采样 (48k 下 21.3ms), ffmpeg 切分片只能落在帧边界上,
    # 而视频分片边界按整帧时长对齐 —— 每段首个音频帧就会漂 0~21ms, 且逐段累积跳变.
    # hls.js 在段接缝处会丢弃/重复 1~2 个音频帧, 表现是每段一次极短的声音断续 + 画面微顿.
    # 重编码时让 aac 与视频同一时间基重采样对齐, 并强制 first_pts=0, 接缝处的空隙就消失了.
    command += [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-ar",
        "48000",
    ]
    command += [
        "-f",
        "hls",
        "-hls_time",
        f"{seconds:.6f}",
        "-hls_list_size",
        "0",
        # 这份清单只用来判断哪些分片已经写完. 必须用 event: vod 类型的清单要等整片转完才落盘,
        # 那样第一段就永远等不到.
        "-hls_playlist_type",
        "event",
        "-hls_segment_type",
        "fmp4",
        "-hls_fmp4_init_filename",
        INIT_NAME,
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        "seg%05d.m4s",
        "-start_number",
        str(start_index),
        RUN_PLAYLIST,
    ]
    return command


def _session_key(
    url: str,
    codecs: MediaCodecs | None,
    settings: PluginSettings,
    encoder: str,
    target_height: int = 0,
    codec: str = "hevc",
) -> str:
    """一条流对应的转码会话标识: 来源变了、参数变了或清晰度档位变了就重新建会话."""
    codec_tag = f"{codecs.video if codecs else ''}/{codecs.audio if codecs else ''}"
    return "|".join(
        [
            url,
            codec_tag,
            encoder,
            settings.preset,
            str(settings.crf),
            str(target_height or settings.max_height),
            codec,
            # 解码链路标记: 由软解/`-hwaccel cuda` 换成显式 CUVID 后, 旧分片缓存必须作废.
            "dec-cuda",
        ]
    )
# ---------------------------------------------------------------------------
# 进程控制
# ---------------------------------------------------------------------------

_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _open_process(pid: int, access: int) -> int:
    if os.name != "nt":
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    handle = kernel32.OpenProcess(access, 0, pid)
    return int(handle) if handle else 0


def _close_handle(handle: int) -> None:
    if os.name != "nt" or not handle:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _process_image(pid: int, handle: int) -> str | None:
    """取进程的可执行文件路径, 用于核对 pid 有没有被复用."""
    if os.name != "nt":
        try:
            return os.path.realpath(f"/proc/{pid}/exe")
        except OSError:
            return None
    if not handle:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    size = ctypes.c_uint32(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


def _record_pid(directory: Path, process: subprocess.Popen[bytes], image: str) -> None:
    """把子进程的 pid 与可执行文件写进会话目录, 供下一个插件实例回收."""
    try:
        (directory / PID_FILE).write_text(f"{process.pid}\n{image}\n", encoding="utf-8")
    except OSError:
        logger.warning("cannot record transcode pid")


def _kill_recorded(directory: Path) -> None:
    """结束会话目录里记录的那个 ffmpeg.

    只按 pid 杀是不安全的: pid 会被系统复用, 可能杀到别的程序. 因此比对可执行文件路径, 对不上
    就不动. 这里回收的是插件重载/重装后失去归属、却仍在满速转码的旧进程.
    """
    try:
        lines = (directory / PID_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) < 2 or not lines[0].strip().isdigit():
        return
    pid = int(lines[0].strip())
    expected = lines[1].strip().casefold()
    if not expected:
        return
    if os.name == "nt":
        handle = _open_process(pid, _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return
        try:
            image = _process_image(pid, handle)
            if image is None or image.casefold() != expected:
                return
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
            # 等它真的退出: 进程没走完, 它占着的分片就删不掉.
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 5000)
        finally:
            _close_handle(handle)
        return
    image = _process_image(pid, 0)
    if image is None or image.casefold() != expected:
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.05)


def _purge_cache(cache_root: Path) -> None:
    """清掉上一个插件实例留下的转码缓存, 顺手结束它还在跑的 ffmpeg.

    插件重载或重装会换一个模块实例: 旧实例起的 ffmpeg 与它的分片目录不再有人使用, 而那个进程
    会继续满速转码, 一条两小时的片子能占掉几十 GB, CPU 也一直顶满.
    """
    try:
        children = list(cache_root.iterdir())
    except OSError:
        return
    for child in children:
        _kill_recorded(child)
        shutil.rmtree(child, ignore_errors=True)
        if child.exists():
            # 进程刚退出时文件可能还被系统握着一瞬间, 稍等再删一次.
            time.sleep(0.2)
            shutil.rmtree(child, ignore_errors=True)


_reaper_started = False


def _reap_leftovers(cache_root: Path) -> None:
    """只结束遗留进程, 不删目录: 删目录留到第一次开转码时和它一起做."""
    try:
        children = list(cache_root.iterdir())
    except OSError:
        return
    for child in children:
        _kill_recorded(child)


def _start_reaper(cache_root: Path) -> None:
    """插件一被使用就回收上一个实例留下的 ffmpeg, 不等用户点播放.

    在后台线程里跑: ``build_playback`` 由主机在事件循环上调用, 不能在那里杀进程等退出.
    每个模块实例只跑一次 —— 同一实例里重建 provider (改配置) 时不能去杀正在播的会话.
    """
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    threading.Thread(
        target=_reap_leftovers,
        args=(cache_root,),
        name="amane-playback-reaper",
        daemon=True,
    ).start()


class TranscodeSession:
    """一条流的转码会话: 一个按需起停的 ffmpeg 加它的分片目录."""

    def __init__(
        self,
        *,
        token: str,
        key: str,
        directory: Path,
        run_playlist: Path,
        log: Path,
        process: subprocess.Popen[bytes],
        duration: float,
        start_index: int,
        idle_seconds: float,
        seconds: float,
        make_command: Callable[[int], list[str]],
    ) -> None:
        self.token = token
        self.key = key
        self.directory = directory
        self.run_playlist = run_playlist
        self.log = log
        self.process = process
        self.duration = duration
        self.seg_seconds = seconds
        self.total = total_segments(duration, seconds)
        #: 清单里的 TARGETDURATION 必须不小于**最长**的那一段.
        self.target_duration = max(1, math.ceil(seconds + 0.001))
        #: 平均段长, 用于估"等它顺序转到目标段要多久".
        self.avg_segment = (duration / self.total) if self.total else seconds
        self.encoder_start = start_index
        self.idle_seconds = idle_seconds
        self.make_command = make_command
        self.last_used = time.monotonic()
        #: 已写完的分片序号. ffmpeg 每写完一段才写进它自己的清单, 因此按清单判定.
        self.complete: set[int] = set()
        #: 已经改写过 tfdt 的分片序号.
        self.patched: set[int] = set()
        #: ``track_id -> timescale``, 从 init.mp4 读出来, 用于把 tfdt 写成绝对时间.
        self.timescales: dict[int, int] = {}
        #: 分片的真实时长与它属于哪一轮起转, 用于把 tfdt 写成连续的时间轴.
        self.extinf: dict[int, float] = {}
        self.fragment_run: dict[int, int] = {}
        #: 起转 / 换位置时要串行, 否则两个请求会各起一个 ffmpeg.
        self.encoder_lock = asyncio.Lock()
        #: 最近一次"想从哪一段起转". 拖动时会有多个分片请求同时到达, 只让最后一个目标生效,
        #: 否则几个请求轮番杀进程, 谁也等不到自己的段 (表现就是拖两下彻底不动).
        self.desired: int | None = None
        #: 会话已被回收: 之后的清单与分片请求都必须失败.
        self.stopped = False
        #: 本轮起转的时刻与序号. 用于识别"刚起转、正在出片"的窗口 —— 窗口内不许
        #: 被别的请求当成卡住而杀掉重起, 反复重启才是切档 5xx 的真凶.
        self.last_spawn = 0.0
        self.spawn_serial = 0
        #: 被同源其它档位暂时停掉 (分片保留). 再被请求时照常按需起转.
        self.parked = False
        #: ffmpeg 清单的 (mtime_ns, size): 没变就不必重解析. 轮询是 25ms 级的, 一部长片的
        #: 清单几千行, 不缓存的话光正则就把 CPU 吃掉了.
        self.playlist_stamp: tuple[int, int] = (-1, -1)

    # -- 状态 -----------------------------------------------------------

    def alive(self) -> bool:
        return self.process.poll() is None

    def produced_max(self) -> int:
        """本轮已经**连续**供上的最大段号.

        必须从本轮起点往后连续数, 不能取磁盘上最大的段号: 拖动过后目录里会留着之前某一轮
        转好的远处分片 (比如 240 那轮留下了 300/310), 直接取 max 会让 covers() 误以为
        "已经转到 310 了", 于是请求 240 时既不换位置起转、也等不到段, 白等到超时 —— 表现就是
        拖一下卡 3 秒然后报错.
        """
        index = self.encoder_start - 1
        complete = self.complete
        while index + 1 in complete:
            index += 1
        return index

    def covers(self, index: int) -> bool:
        """当前这个 ffmpeg 会不会在合理时间内转到这一段, 等它就行.

        同一轮起转是顺序往后转的, 播放器顺序预读 30 秒 (约 15 段) 时目标都落在
        ``produced_max() + COVER_LOOKAHEAD_SEGMENTS`` 之内, 等它转就行 —— 以前用固定
        margin 判, 预读一超就把进程杀掉重起, 一路重启才是卡顿与 CPU 波动.

        但上界不能省: 用户把进度条拖到后面几十分钟的位置时, 请求不能还被当成"覆盖",
        干等一个永远轮不到的段 —— 远超窗口的目标必须换位置重转.
        """
        if self.stopped or not self.alive():
            return False
        if index < self.encoder_start:
            return False
        ahead = index - self.produced_max()
        if ahead <= 1:
            # 正在编的就是它 / 紧接着的下一段: 顺序播放的常态, 等它转出来.
            return True
        if ahead > COVER_LOOKAHEAD_SEGMENTS:
            return False
        # 超出 1 段就按时间算: 等 4 秒远不如换位置重转 (实测 0.34 秒).
        return ahead * self.avg_segment <= SEEK_WAIT_BUDGET_SECONDS

    def finished(self) -> bool:
        """整片都转完了."""
        return not self.alive() and self.produced_max() >= self.total - 1

    def reusable(self) -> bool:
        """会话还能接着用.

        ffmpeg 已经退出没关系: 分片还在, 需要的位置按需再起转即可, 不必丢掉已转好的缓存.
        """
        return not self.stopped

    # -- 清单 -----------------------------------------------------------

    def segment_duration(self, index: int) -> float:
        remaining = self.duration - index * self.seg_seconds
        return max(0.04, min(self.seg_seconds, remaining))

    def playlist_text(self) -> str:
        """完整的 VOD 清单: 一次给全总时长, 播放器才肯任意跳转.

        ``#EXT-X-PLAYLIST-TYPE:VOD`` 与 ``#EXT-X-ENDLIST`` 是重点: 缺了它们播放器会把清单当成
        直播, 一旦播放头落后"直播边缘"超过若干秒就被拽回边缘, 表现就是拖了没反应.
        """
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{self.target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f'#EXT-X-MAP:URI="{INIT_NAME}"',
        ]
        for index in range(self.total):
            # 分片的实际时长不是整数秒 (关键帧只能落在帧上), 有真实值就用真实值,
            # 否则长片会越播越偏.
            duration = self.extinf.get(index) or self.segment_duration(index)
            lines.append(f"#EXTINF:{duration:.6f},")
            lines.append(segment_name(index))
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    # -- 分片 -----------------------------------------------------------

    def segment_path(self, index: int) -> Path:
        return self.directory / segment_name(index)

    def refresh_produced(self) -> None:
        """从 ffmpeg 自己的清单里记下已经写完的分片, 连同它的真实时长.

        分片的实际时长不是整数秒 (关键帧只能落在帧上), 所以补时间戳要按真实时长累加, 否则每段
        之间会留一个几十毫秒的空洞, 播放器会卡在那里.
        """
        try:
            stat = self.run_playlist.stat()
        except OSError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.playlist_stamp:
            return
        self.playlist_stamp = stamp
        try:
            text = self.run_playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        pending = None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#EXTINF:"):
                try:
                    pending = float(stripped[8:].split(",", 1)[0])
                except ValueError:
                    pending = None
                continue
            if stripped.startswith("#"):
                continue
            match = SEGMENT_PATTERN.fullmatch(stripped)
            if match is None:
                continue
            index = int(match.group(1))
            if index not in self.complete:
                self.complete.add(index)
                self.fragment_run[index] = self.encoder_start
                if pending is not None:
                    self.extinf[index] = pending
            pending = None

    def fragment_start_seconds(self, index: int) -> float:
        """分片在清单时间轴上的起点: 它那一轮的起点 + 之前各段的真实时长."""
        run = self.fragment_run.get(index, self.encoder_start)
        total = run * self.seg_seconds
        for produced in range(run, index):
            total += self.extinf.get(produced, self.seg_seconds)
        return total

    def fragment(self, index: int) -> Path | None:
        """已经存在且写完了的分片; 否则 ``None``."""
        if index not in self.complete:
            return None
        candidate = self.segment_path(index)
        return candidate if candidate.is_file() else None

    def failure_tail(self) -> str:
        try:
            lines = self.log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return ""
        return lines[-1] if lines else ""

    def touch(self) -> None:
        self.last_used = time.monotonic()

# ---------------------------------------------------------------------------
# 回环 HTTP 服务
# ---------------------------------------------------------------------------


@dataclass
class SourceContext:
    """一次 resolve 注册的"来源级"播放源.

    一个源对应一个在线媒体 URL, 旗下按清晰度档位懒创建多个转码会话
    (``sessions[(height, codec)]``). 播放器加载 master 清单后, 播放器会按需拉取所选档位的
    媒体清单, 插件再为该档位起转码.
    """

    token: str
    cache_root: Path
    url: str
    headers: dict[str, str]
    codecs: MediaCodecs | None
    duration: float
    seconds: float
    settings: PluginSettings
    tools: Tools
    subtitles: tuple[SubtitleTrack, ...]
    #: 字幕轨 id -> (字幕 URL, 后缀). 由 resolver 给出, 在线源没有"同目录扫描"可用.
    subtitle_sources: dict[str, tuple[str, str]] = field(default_factory=dict)
    preferred_height: int = 0
    sessions: dict[tuple[int, str], TranscodeSession] = field(default_factory=dict)


class StreamServer:
    """只在回环上监听的小 HTTP 服务, 供 Amane 反代取分片.

    插件不直接向浏览器输出字节: 分片先由这里的 ``127.0.0.1`` 服务读出, 再由 Amane 反代给
    播放器, 与上游播放源走同一条通道.
    """

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        #: 按会话标识索引, 用于复用同一条流的转码.
        self._sessions: dict[str, TranscodeSession] = {}
        #: 按 URL 里的 token 索引到"来源级"播放源, 分片请求先定位源再按档位找会话.
        self._tokens: dict[str, SourceContext] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # 跨档位起转互斥. 各会话的 encoder_lock 只收敛同一档内的请求;
        # 切档时两档会各自 terminate + spawn, 消费级编码器往往只支持 2 路, 后起的那路
        # 直接以 "no packets" 退出, 表现就是切一次档吃一串 502.
        self._spawn_lock = asyncio.Lock()
        #: 本实例是否已经清过残留缓存; 每个模块实例只清一次.
        self._purged = False

    def base_url(self, token: str) -> str:
        return f"http://{LOOPBACK}:{self._port}/v/{token}/"

    @property
    def port(self) -> int:
        return self._port

    async def _ensure_started(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, host=LOOPBACK, port=0)
        sockets = self._server.sockets or ()
        self._port = int(sockets[0].getsockname()[1]) if sockets else 0
        self._sweeper = asyncio.create_task(self._sweep())

    # -- 会话 -----------------------------------------------------------

    async def register_source(
        self,
        *,
        cache_root: Path,
        url: str,
        headers: dict[str, str],
        codecs: MediaCodecs | None,
        duration: float,
        seconds: float,
        settings: PluginSettings,
        tools: Tools,
        subtitles: tuple[SubtitleTrack, ...],
        subtitle_sources: dict[str, tuple[str, str]],
        preferred_height: int = 0,
    ) -> SourceContext:
        await self._ensure_started()
        source = SourceContext(
            token=secrets.token_hex(8),
            cache_root=cache_root,
            url=url,
            headers=dict(headers),
            codecs=codecs,
            duration=duration,
            seconds=seconds,
            settings=settings,
            tools=tools,
            subtitles=subtitles,
            subtitle_sources=dict(subtitle_sources),
            preferred_height=preferred_height,
        )
        self._tokens[source.token] = source
        return source

    async def session_for(
        self,
        source: SourceContext,
        height: int,
        codec: str = "hevc",
        *,
        wait_first: bool = True,
    ) -> TranscodeSession:
        """取某个清晰度档位的转码会话; 没有就为该档位懒创建.

        ``wait_first=False`` 供清单 / 分片请求使用: 分片请求自己会把进程挪到目标段
        (见 ``ensure_segment``), 清单根本不需要分片, 所以没必要先等第 0 段 —— 那等于
        每次拖动都白起一次"从 0 开始"的转码再被杀掉重来.
        """
        session = source.sessions.get((height, codec))
        if session is not None and not session.stopped:
            session.touch()
            return session
        await self._ensure_started()
        if not self._purged:
            self._purged = True
            await asyncio.to_thread(_purge_cache, source.cache_root)

        def make_command(start_index: int) -> list[str]:
            return _ffmpeg_command(
                source.tools,
                source.url,
                source.headers,
                source.settings,
                source.codecs,
                start_index,
                source.seconds,
                target_height=height,
                codec=codec,
            )

        # 新档起转前先让同源其它档位让出编码器与读盘带宽.
        await self._park_siblings(source, source.sessions.get((height, codec)))
        session = await self.open_session(
            cache_root=source.cache_root,
            key=_session_key(
                source.url,
                source.codecs,
                source.settings,
                source.tools.encoder,
                height,
                codec,
            ),
            make_command=make_command,
            duration=source.duration,
            seconds=source.seconds,
            idle_seconds=float(source.settings.idle_timeout),
            wait_seconds=STARTUP_TIMEOUT_SECONDS,
            wait_first=wait_first,
        )
        source.sessions[(height, codec)] = session
        return session

    async def open_session(
        self,
        *,
        cache_root: Path,
        key: str,
        make_command: Callable[[int], list[str]],
        duration: float,
        seconds: float,
        idle_seconds: float,
        wait_seconds: float,
        wait_first: bool = True,
    ) -> TranscodeSession:
        await self._ensure_started()
        if not self._purged:
            self._purged = True
            await asyncio.to_thread(_purge_cache, cache_root)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is None:
                await self._enforce_limit()
                existing = await asyncio.to_thread(
                    self._spawn, cache_root, key, make_command, duration, seconds, idle_seconds
                )
                self._sessions[key] = existing
            else:
                existing.touch()
        session = existing
        if wait_first and not session.complete:
            if not session.alive():
                # 复用的会话里 ffmpeg 早已退出 (空闲被回收): 重新起转再等第一段.
                await self._restart(session, 0)
            # 新会话: 等第一段写出来, 播放器才有东西可请求.
            if not await self._wait_segment(session, 0, wait_seconds):
                await self._stop(session)
                raise SourceError(FailureReason.NO_USABLE_METADATA, detail="转码失败, 无法解码该来源")
        return session

    def _spawn(
        self,
        cache_root: Path,
        key: str,
        make_command: Callable[[int], list[str]],
        duration: float,
        seconds: float,
        idle_seconds: float,
    ) -> TranscodeSession:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        directory = cache_root / digest
        # 同一目录里可能躺着上一代同一条流的分片: 清掉, 免得新旧混在一起.
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "ffmpeg.log"
        command = make_command(0)
        with log.open("ab") as handle:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=handle,
                # 以会话目录为工作目录: 清单里的 seg*.m4s / init.mp4 是相对名字, 必须落在同一处.
                cwd=str(directory),
                creationflags=NO_WINDOW,
            )
        _record_pid(directory, process, command[0])
        return TranscodeSession(
            token=secrets.token_hex(8),
            key=key,
            directory=directory,
            run_playlist=directory / RUN_PLAYLIST,
            log=log,
            process=process,
            duration=duration,
            start_index=0,
            idle_seconds=idle_seconds,
            seconds=seconds,
            make_command=make_command,
        )

    async def _restart(self, session: TranscodeSession, index: int, *, force: bool = False) -> bool:
        """把 ffmpeg 换到这个分片的起点重新起转.

        拖动时播放器会一次要好几段, 这些请求必须收敛到"最后一个目标": 每个请求只是把自己的
        段号记进 ``session.desired``, 真正起转的只有一个进程, 且用的是最新的那个段号.
        再加一道**跨档位**互斥 (``_spawn_lock``): 同一档内靠 ``encoder_lock`` 收敛,
        但切档时两个档位各有各的锁, 会同时 terminate + spawn —— 消费级显卡的硬件编码器
        通常只支持两路, 后起的一路拿不到会话, ffmpeg 一个包都写不出来就退出
        ("Nothing was written into output file ... received no packets"), 播放器看到的就是
        切档瞬间一串 502/503.

        返回 ``False`` 表示这次没有起转, 自己的段已被更靠后的目标接管, 调用方放弃即可.
        """
        session.desired = index
        if not force and session.covers(index):
            return True
        async with session.encoder_lock:
            if session.stopped:
                return False
            target = index if session.desired is None else session.desired
            if target != index and not force:
                # 等锁期间有更新的目标进来了: 让那一个去起转, 自己别插一脚.
                return False
            if not force and session.covers(target):
                return True
            async with self._spawn_lock:
                if session.stopped:
                    return False
                # 等全局锁期间可能已经有人把 ffmpeg 起在能覆盖该段的位置上了:
                # 这时直接复用, 不要再 terminate 一遍.
                if not force and session.covers(index):
                    return True
                target = index if session.desired is None else session.desired
                if target != index and not force:
                    return False
                alive = await asyncio.to_thread(self._terminate_process, session)
                if not alive:
                    # 旧进程没死透就再起一个, 只会两路写同一个分片目录, 必然一起失败.
                    logger.warning("previous transcode still alive, skip restart (start=%s)", target)
                    return False
                process = await asyncio.to_thread(self._spawn_process, session, target)
                session.process = process
                session.encoder_start = target
                session.desired = target
                # 起转健康检查. 硬件编码器初始化失败 / 输出目录被删, ffmpeg 会在几百毫秒内
                # 退出, 不能等到 _wait_segment 超时才判失败. 这里每次起转后 0.4s / 1.2s 查一次
                # 进程, 秒退就清掉该段残留重新起, 最多 3 次.
                for attempt in range(1, 4):
                    if alive:
                        await asyncio.sleep(0.4 if attempt == 1 else 0.8)
                    if session.process.poll() is None:
                        break
                    await asyncio.to_thread(session.refresh_produced)
                    if session.fragment(target) is not None:
                        # 这一段其实已经转出来了 (进程正常收尾), 不用重起.
                        break
                    alive = True
                    tail = await asyncio.to_thread(session.failure_tail)
                    logger.warning(
                        "transcode spawn attempt %s failed (start=%s): %s", attempt, target, tail[:300]
                    )
                    await asyncio.to_thread(self._drop_fragment, session, target)
                    session.process = await asyncio.to_thread(self._spawn_process, session, target)
                    session.encoder_start = target
                session.desired = target
                return True

    async def _park_siblings(
        self, source: "SourceContext", keep: "TranscodeSession | None"
    ) -> None:
        """起新档之前停掉同源其它档位的 ffmpeg (分片保留).

        切档后旧档的 ffmpeg 若还活着, 新档又起一路: 两路同时拉同一个上游流, 硬件编码器
        会话也常常只支持两路, 新档于是起转失败 (日志里的 "Error opening output files" /
        "no packets"). 这里只停进程不动目录 —— 停掉的档再被请求会按需重新起转.
        """
        for key, sibling in list(source.sessions.items()):
            if sibling is keep or sibling.stopped or not sibling.alive():
                continue
            stopped = await asyncio.to_thread(self._terminate_process, sibling)
            sibling.parked = True
            logger.info(
                "park sibling transcode height=%s codec=%s ok=%s", key[0], key[1], stopped
            )

    @staticmethod
    def _terminate_process(session: TranscodeSession) -> bool:
        """结束这一轮的 ffmpeg, 并**确认它真的死了**.

        进程若卡在硬件编码器或写盘上, kill 之后要几秒才真的退出, 而调用方已经开始起新进程
        —— 两路 ffmpeg 同时写同一个 segNNNNN 文件, 新进程就会报
        "Error opening output files: Generic error in an external library",
        表现就是切档反复 502. 先 SIGTERM 给它 2 秒收尾, 再 SIGKILL, 并返回是否确认退出.
        """
        process = session.process
        if process.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.terminate()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=2)
        if process.poll() is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=6)
        if process.poll() is None:
            logger.warning("transcode pid %s still alive after SIGKILL", process.pid)
            return False
        return True

    @staticmethod
    def _drop_fragment(session: TranscodeSession, index: int) -> None:
        """删掉某一段的残留文件.

        起转失败后重试前必须先清掉这一段 —— 上一次进程可能已经把半截 segNNNNN.m4s
        留在磁盘上, 新进程复用同名文件时 hls muxer 会打不开输出.
        """
        candidates = [
            session.directory / f"seg{index:05d}.m4s",
            session.directory / f"seg{index:05d}.m4s.tmp",
        ]
        for path in candidates:
            with contextlib.suppress(OSError):
                path.unlink()
        session.complete.discard(index)
        session.patched.discard(index)

    @staticmethod
    def _spawn_process(session: TranscodeSession, index: int) -> subprocess.Popen[bytes]:
        # 新的一次运行会重写它自己的清单: 先删掉, 免得把上一轮的段号当成这一轮的.
        with contextlib.suppress(OSError):
            session.run_playlist.unlink()
        # 会话目录可能已经被清理任务删掉 (空闲回收与并发起转交错时会发生),
        # 此时 hls muxer 打不开输出清单, 报的就是 "Error opening output files".
        with contextlib.suppress(OSError):
            session.directory.mkdir(parents=True, exist_ok=True)
        command = session.make_command(index)
        with session.log.open("ab") as handle:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=handle,
                cwd=str(session.directory),
                creationflags=NO_WINDOW,
            )
        _record_pid(session.directory, process, command[0])
        return process

    async def _wait_segment(self, session: TranscodeSession, index: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        start_at_begin = session.encoder_start
        # 进度观察. 进程还活着、已产出段号还在前进, 说明这一轮真在转, 固定超时
        # 不该把它掐死 (掐死的后果是换进程重来, 而重来又要 0.34s 起转; 叠加播放器并发
        # 预取就是一路 5xx). 这里按"最后一次前进"顺延, 最多顺延
        # SEGMENT_PROGRESS_GRACE_SECONDS 秒.
        produced_last = session.produced_max()
        while True:
            await asyncio.to_thread(session.refresh_produced)
            if session.fragment(index) is not None:
                return True
            if session.stopped:
                return False
            produced_now = session.produced_max()
            if produced_now > produced_last:
                produced_last = produced_now
                deadline = max(
                    deadline,
                    time.monotonic() + min(seconds, SEGMENT_PROGRESS_GRACE_SECONDS),
                )
            if not session.alive() and index > session.produced_max():
                # 进程已经退出又没转出这一段: 要么整片转完了, 要么解码失败.
                if session.produced_max() < session.total - 1:
                    tail = await asyncio.to_thread(session.failure_tail)
                    logger.warning("transcode stopped before segment %s: %s", index, tail)
                return False
            if session.encoder_start != start_at_begin and not session.covers(index):
                # 期间有别的请求把转码挪走了: 不立刻放弃 (那会一路 503 甩给播放器),
                # 而是接着等新目标那一轮转过来, 直到超时.
                start_at_begin = session.encoder_start
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.025)

    async def _ensure_init(self, session: TranscodeSession) -> Path | None:
        """初始化段: ffmpeg 起转后很快就会写出, 等它一下."""
        init = session.directory / INIT_NAME
        deadline = time.monotonic() + WAIT_SEGMENT_SECONDS
        while True:
            if init.is_file() and init.stat().st_size > 0:
                session.touch()
                return init
            if session.stopped:
                return None
            if not session.alive() and not await self._wait_segment(session, 0, 0.1):
                return init if init.is_file() else None
            if time.monotonic() >= deadline:
                return init if init.is_file() else None
            await asyncio.sleep(0.05)

    async def _prepare_fragment(self, session: TranscodeSession, index: int, path: Path) -> Path:
        """把分片整体平移到它在清单里的位置, 只做一次."""
        if index in session.patched:
            return path
        if not session.timescales:
            init = session.directory / INIT_NAME
            session.timescales = await asyncio.to_thread(track_timescales, init)
        shift = await self._fragment_shift(session, index, path)
        if shift:
            try:
                await asyncio.to_thread(shift_fragment, path, shift, session.timescales)
            except OSError:
                # 分片被清理任务删掉或磁盘不可写: 让播放器按清单位置自己校正, 不要 500.
                logger.warning("patch fragment failed: index=%s", index)
        session.patched.add(index)
        return path

    async def _fragment_shift(self, session: TranscodeSession, index: int, path: Path) -> float:
        """这一段要平移多少秒, 才能让它落在清单时间轴的对应位置上.

        ffmpeg 的 fMP4 HLS 输出本身就是"从 0 开始、按真实段长累加"的时间戳, 与清单
        ``#EXTINF`` 累加天然一致 —— 只有 seek 起转 (``-ss``) 的那一轮才会从 0 重新计时.
        这里做**绝对定位**: ``清单目标位置 - 文件当前 tfdt``.

        * ffmpeg 已经写对 -> 结果为 0, 一个字节都不改;
        * seek 起转那一轮 -> 自动补上该轮应有的偏移;
        * 天然幂等, 重复调用不会越推越远.
        """
        target = session.fragment_start_seconds(index)
        video = await asyncio.to_thread(fragment_first_tfdt, path)
        if video is None or not session.timescales:
            return 0.0
        scale = session.timescales.get(video[0])
        if not scale:
            return 0.0
        actual = video[1] / scale
        shift = target - actual
        if abs(shift) < 0.001:
            # 已经对齐, 不要重写分片.
            return 0.0
        if abs(shift) > max(session.segment_duration(index), session.seg_seconds) * 1.5:
            # 大幅位移有一种合法来源: seek 起转的那一轮 ffmpeg 用 -ss 重新计时, tfdt 从 0 起,
            # 此时 shift 为正且等于该段的清单位置, 属正常; 分片反而跑到清单位置**之前**
            # (shift 为负) 才说明哪里不对, 留一条 warning 方便排查.
            report = logger.warning if shift < 0 else logger.debug
            report(
                "fragment timeline rebase: index=%s target=%.3f actual=%.3f shift=%.3f",
                index,
                target,
                actual,
                shift,
            )
        return shift

    async def ensure_segment(self, session: TranscodeSession, index: int) -> Path | None:
        """取第 ``index`` 段; 没转过就把 ffmpeg 挪到那里现转."""
        if index < 0 or index >= session.total:
            # 越界段不能拿磁盘上碰巧同名的残留文件糊弄过去: 时长只有几毫秒, 播放器解码会报错.
            return None
        existing = session.fragment(index)
        if existing is not None:
            session.touch()
            return await self._prepare_fragment(session, index, existing)
        if not session.covers(index):
            if not await self._restart(session, index):
                # 这个段被更靠后的拖动接管了: 先等一轮让那一轮起转完成, 别急着回 503.
                # 实测不能一发现"被超越"就放手 —— 并发预取时那会把本来等得到的分片直接判死,
                # 所以这里老实等满.
                if await self._wait_segment(session, index, SEEK_FOLLOWUP_SECONDS):
                    session.touch()
                    produced = session.fragment(index)
                    if produced is not None:
                        return await self._prepare_fragment(session, index, produced)
                if session.desired is not None and session.desired != index:
                    # 仍由更靠后的目标接管: 交给那一轮, 自己不插一脚.
                    return None
                # 最新目标就是自己却还等不到 —— 说明那一轮起转卡住了, 必须自己重起.
                logger.warning(
                    "segment %s still missing after follow-up wait (start=%s produced=%s alive=%s)",
                    index,
                    session.encoder_start,
                    session.produced_max(),
                    session.alive(),
                )
                await self._restart(session, index, force=True)
                if await self._wait_segment(session, index, WAIT_SEGMENT_SECONDS):
                    session.touch()
                    produced = session.fragment(index)
                    if produced is not None:
                        return await self._prepare_fragment(session, index, produced)
                return None
        if await self._wait_segment(session, index, WAIT_SEGMENT_SECONDS):
            session.touch()
            produced = session.fragment(index)
            if produced is None:
                return None
            return await self._prepare_fragment(session, index, produced)
        # 第一次没等到: 记下现场, 换一个进程再试一次. 卡在读盘或 seek 上的话, 换进程就好了.
        if session.desired is not None and session.desired != index:
            # 期间播放器又换了目标: 别再杀进程, 交给新目标那一轮去转.
            return None
        logger.warning(
            "segment %s not produced in %.0fs (start=%s produced=%s alive=%s tail=%s)",
            index,
            WAIT_SEGMENT_SECONDS,
            session.encoder_start,
            session.produced_max(),
            session.alive(),
            await asyncio.to_thread(session.failure_tail),
        )
        await self._restart(session, index, force=True)
        if await self._wait_segment(session, index, WAIT_SEGMENT_SECONDS):
            session.touch()
            produced = session.fragment(index)
            if produced is None:
                return None
            return await self._prepare_fragment(session, index, produced)
        return None

    async def _enforce_limit(self) -> None:
        while self._sessions and len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda item: item.last_used)
            await self._stop(oldest)

    async def _stop(self, session: TranscodeSession) -> None:
        session.stopped = True
        self._sessions.pop(session.key, None)
        # 从所属播放源里摘掉这个档位会话; 源本身保留, 播放器再拉该档位时懒创建新会话.
        for source in self._tokens.values():
            for key, candidate in list(source.sessions.items()):
                if candidate is session:
                    source.sessions.pop(key, None)
        await asyncio.to_thread(self._discard, session)

    @staticmethod
    def _discard(session: TranscodeSession) -> None:
        StreamServer._terminate_process(session)
        shutil.rmtree(session.directory, ignore_errors=True)

    async def _sweep_once(self) -> None:
        """跑一轮清理: 结束空闲的 ffmpeg, 清掉太久没用的会话.

        关掉播放器后 ffmpeg 不该继续跑: 没有请求就结束进程 (已转好的分片留着, 下次播放直接命中,
        需要没转过的位置再起转). 超过 ``idle_timeout`` 才连分片一起删.
        """
        now = time.monotonic()
        for session in list(self._sessions.values()):
            idle = now - session.last_used
            if idle > session.idle_seconds:
                await self._stop(session)
            elif idle > KILL_AFTER_IDLE_SECONDS and session.alive():
                await asyncio.to_thread(self._terminate_process, session)
                logger.info("transcode stopped after idle")

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self._sweep_once()
            except Exception:  # pragma: no cover - 清理失败不能连带停掉清理任务
                logger.exception("playback session sweep failed")

    async def aclose(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for session in list(self._sessions.values()):
            await self._stop(session)
        self._tokens.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(OSError):
                await self._server.wait_closed()
            self._server = None

    # -- HTTP -----------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        except Exception:  # pragma: no cover - 客户端断开等意外
            logger.exception("playback stream server error")
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_TIMEOUT_SECONDS)
        lines = head.decode("latin-1").split("\r\n")
        request = lines[0].split(" ")
        if len(request) < 2:
            await self._respond(writer, "400 Bad Request", {})
            return
        method = request[0].upper()
        target = request[1]
        if method not in {"GET", "HEAD"}:
            await self._respond(writer, "405 Method Not Allowed", {})
            return
        range_header = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().casefold() == "range":
                range_header = value.strip()
                break
        # warm 端点要读 query (seg= / t=), 先留一份原始 target.
        raw_target = target
        _REQUEST_LABELS[id(writer)] = f"{method} {raw_target}"
        parts = target.split("?", 1)[0].strip("/").split("/")
        if len(parts) < 3 or parts[0] != "v":
            await self._respond(writer, "404 Not Found", {})
            return
        source = self._tokens.get(parts[1])
        if source is None:
            await self._respond(writer, "404 Not Found", {})
            return

        # 字幕轨: /v/{token}/subtitle/{track_id}.vtt
        if len(parts) == 4 and parts[2] == "subtitle":
            track_id = parts[3].removesuffix(".vtt")
            vtt = await _subtitle_vtt(source, track_id)
            if vtt is None:
                await self._respond(writer, "404 Not Found", {})
                return
            body = vtt.encode("utf-8")
            await self._respond(writer, "200 OK", {
                "Content-Type": "text/vtt; charset=utf-8",
                "Content-Length": str(len(body)),
            })
            if method == "GET":
                writer.write(body)
            return

        # 主清单 (多档位 + 字幕): /v/{token}/master.m3u8
        if len(parts) == 3 and parts[2] == "master.m3u8":
            text = _master_text(source)
            body = text.encode("utf-8")
            await self._respond(writer, "200 OK", {
                "Content-Type": "application/vnd.apple.mpegurl",
                "Content-Length": str(len(body)),
            })
            if method == "GET":
                writer.write(body)
            return

        # 档位媒体清单 / 分片:
        #   新地址: /v/{token}/{codec}/[{height}/]{name}  (codec = hevc | h264 | av1)
        #   旧地址: /v/{token}/[{height}/]{name}  (默认 hevc)
        codec = "hevc"
        if len(parts) >= 4 and parts[2] in ("hevc", "h264", "av1"):
            codec = parts[2]
            rest = parts[3:]
        else:
            rest = parts[2:]
        if len(rest) == 2 and rest[0].isdigit():
            height = int(rest[0])
            name = rest[1]
        elif len(rest) == 1:
            height = 0
            name = rest[0]
        else:
            await self._respond(writer, "404 Not Found", {})
            return
        # 清单与分片都不阻塞在建会话等首段上: 首段等待会让每次拖动白耗 0.36s.
        session = await self.session_for(source, height, codec, wait_first=False)
        if name == WARM_NAME:
            # 预热: 前端切档前先请求它, 服务端同步把目标段转出来再回 200 ——
            # 真正切过去时 ensure_segment 直接命中磁盘, 不产生任何 5xx.
            # 永远回 200 (ready=0 表示没等到): 预热失败不该让前端进错误页.
            begin = time.monotonic()
            seg = _warm_segment_index(raw_target, session)
            path = None
            if seg is not None:
                path = await self.ensure_segment(session, seg)
            payload = {
                "ready": 1 if path is not None else 0,
                "seg": -1 if seg is None else seg,
                "start": session.encoder_start,
                "produced": session.produced_max(),
                "ms": int((time.monotonic() - begin) * 1000),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            await self._respond(writer, "200 OK", {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            })
            if method == "GET":
                writer.write(body)
                await writer.drain()
            return
        if name == RUN_PLAYLIST:
            text = session.playlist_text()
            body = text.encode("utf-8")
            await self._respond(writer, "200 OK", {
                "Content-Type": "application/vnd.apple.mpegurl",
                "Content-Length": str(len(body)),
            })
            if method == "GET":
                writer.write(body)
            return
        if name == INIT_NAME:
            path = await self._ensure_init(session)
        else:
            match = SEGMENT_PATTERN.fullmatch(name)
            if match is None:
                await self._respond(writer, "404 Not Found", {})
                return
            path = await self.ensure_segment(session, int(match.group(1)))
        if path is None:
            # 这一段没转出来 (还没轮到 / 被更靠后的拖动接管 / 起转失败). 不能回 404:
            # hls.js 把 4xx 当致命错误, 一次就 bufferAppend 失败并弹"不能播放此码流";
            # 回 5xx 它会按 fragLoadingMaxRetry 退避重试, 用户再拖一下就能接上.
            body = b"retrying"
            await self._respond(writer, "503 Service Unavailable", {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
                "Retry-After": "1",
                "Cache-Control": "no-store",
            })
            writer.write(body)
            await writer.drain()
            return
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        start, end, status = 0, size - 1, "200 OK"
        if range_header:
            span = _parse_range(range_header, size)
            if span is None:
                await self._respond(
                    writer,
                    "416 Range Not Satisfiable",
                    {"Content-Range": f"bytes */{size}"},
                )
                return
            start, end = span
            status = "206 Partial Content"
        length = max(end - start + 1, 0)
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
        if status.startswith("206"):
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        await self._respond(writer, status, headers)
        if method == "HEAD" or length == 0:
            return
        await self._stream_body(writer, path, start, length)

    @staticmethod
    async def _stream_body(writer: asyncio.StreamWriter, path: Path, start: int, length: int) -> None:
        """逐块读出区间并写出.

        读盘交给线程池, 写回留在事件循环上: ``StreamWriter`` 不是线程安全的.
        """
        remaining = length
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            await asyncio.to_thread(handle.seek, start)
            while remaining > 0:
                chunk = await asyncio.to_thread(handle.read, min(READ_CHUNK, remaining))
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)
        finally:
            await asyncio.to_thread(handle.close)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: str, headers: dict[str, str]) -> None:
        label = _REQUEST_LABELS.pop(id(writer), "-")
        if not status.startswith(("200", "206")):
            logger.warning("stream %s -> %s", label, status)
        else:
            logger.debug("stream %s -> %s", label, status)
        lines = [f"HTTP/1.1 {status}"]
        if "Content-Length" not in headers:
            lines.append("Content-Length: 0")
        lines.extend(f"{name}: {value}" for name, value in headers.items())
        lines.append("Connection: close")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()


_SERVER = StreamServer()

def _warm_segment_index(target: str, session: TranscodeSession) -> int | None:
    """从 warm 请求里解出想预热的段号.

    ``?seg=N`` 优先; 只有 ``?t=秒`` 时按段长换算; 都没有就预热当前起转段.
    """
    query = target.split("?", 1)[1] if "?" in target else ""
    values: dict[str, str] = {}
    for item in query.split("&"):
        if not item:
            continue
        key, _, value = item.partition("=")
        values[key] = value
    index: int | None = None
    if values.get("seg"):
        try:
            index = int(values["seg"])
        except ValueError:
            index = None
    if index is None and values.get("t"):
        try:
            seconds = float(values["t"])
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            index = int(seconds // max(session.seg_seconds, 0.001))
    if index is None:
        index = session.encoder_start if session.encoder_start > 0 else 0
    if index < 0:
        return None
    return min(index, max(session.total - 1, 0))
def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """解析单段 ``Range``; 多段与语法错误一律返回 ``None``, 由调用方回 416."""
    spec = header.strip()
    if not spec.casefold().startswith("bytes="):
        return None
    span = spec.split("=", 1)[1].strip()
    if "," in span:
        return None
    start_text, _, end_text = span.partition("-")
    try:
        if not start_text:
            # 后缀区间: bytes=-N 表示最后 N 个字节.
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(size - suffix, 0), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _estimate_bandwidth(height: int) -> int:
    """主清单里声明的峰值带宽 (bps).

    1.0.22: 直接由 ``_maxrate_kbps`` 派生, 保证"声明的 BANDWIDTH"与"ffmpeg 实际
    ``-maxrate`` 上限"严格一致. 以前 480/360 声明 2.6M/1.5M 而实际上限是 3M,
    实测码率长期高于声明值, hls.js 的带宽估计一路向下修正, 于是刚切到高档就
    立刻被 ABR 打回低档 —— 这就是"老是跳 480P"的后端成因.
    """
    return _maxrate_kbps(height) * 1000


def _hevc_codec_string(height: int) -> str:
    """HEVC 的 CODECS: 必须 hvc1, 且 Level 要跟分辨率匹配, 否则播放器不认这条变体."""
    if height >= 2160:
        return "hvc1.2.4.L150.90"
    if height >= 1080:
        return "hvc1.1.6.L120.90"
    if height >= 720:
        return "hvc1.1.6.L93.90"
    return "hvc1.1.6.L63.90"


def _maxrate_kbps(height: int) -> int:
    """按输出高度给一个码率上限: 段体积可控, 拖动时一个分片才拉得动.

    1.0.22: 每个档位都要有**互不相同**的上限. 以前 480 与 360 (及以下) 都落进
    同一个 3M 兜底, 主清单里两档声明的 BANDWIDTH 一样, hls.js 的 ABR 分不出高低,
    在弱网里就会在两档之间来回跳 (每次跳都要重拉清单+等首段), 表现为"切码率卡住".
    """
    if height >= 2160:
        return 18_000
    if height >= 1440:
        return 12_000
    if height >= 1080:
        return 8_000
    if height >= 720:
        return 5_000
    if height >= 480:
        return 3_000
    if height >= 360:
        return 2_000
    return 1_500


def _rate_text(kbps: int) -> str:
    """把 kbps 写成 ffmpeg 的码率参数文本.

    1m 及以上统一用 m (8000 -> 8m, 6000 -> 6m, 3750 -> 3.75m, 2600 -> 2.6m),
    只有低于 1m 才写 k (800 -> 800k).
    """
    if kbps < 1000:
        return f"{kbps}k"
    text = f"{kbps / 1000:.2f}".rstrip("0").rstrip(".")
    return f"{text}m"


def _avc_codec_string(height: int) -> str:
    """H.264 High profile 的 CODECS: Level 也要跟分辨率匹配 (1080p 是 4.0)."""
    if height >= 2160:
        return "avc1.640033"
    if height >= 1080:
        return "avc1.640028"
    if height >= 720:
        return "avc1.64001F"
    return "avc1.64001E"


def _estimate_width(height: int) -> int:
    """按 16:9 粗估宽度, 只用于 HLS RESOLUTION 元数据 (播放器菜单显示高度)."""
    return int(height * 16 / 9 / 2) * 2 if height else 0


def _av1_codec_string(height: int) -> str:
    """AV1 的 CODECS: 格式 ``av01.P.LLT.DD``, LL 是 level, T 是 tier (M=Main), DD 是位深.

    Chrome / Edge / Safari 17+ / 现代 Android 都认这条. 老设备不认 AV1 时, HLS 的
    CODECS 过滤会让它自动落到同一档的 HEVC / H.264 变体.
    """
    if height >= 2160:
        return "av01.0.12M.10"
    if height >= 1080:
        return "av01.0.08M.08"
    if height >= 720:
        return "av01.0.05M.08"
    return "av01.0.04M.08"


def _master_text(source: SourceContext) -> str:
    """生成多档位 + 字幕的主清单.

    每个档位同时给出 HEVC 与 H.264 两条变体 (路径前缀 ``hevc/`` 与 ``h264/``):
    支持 HEVC 的播放器优先用 HEVC 变体, 不支持 HEVC 解码的浏览器 (桌面 Chrome 等)
    会被 ``CODECS`` 过滤自动落到 H.264 变体, 保证任何浏览器都能播. 切换档位时按相对 URI
    请求对应档位的媒体清单, 插件再为该档位 + 编码组合懒创建转码会话.
    """
    # Apple HLS Authoring Spec 要求 VIDEO 变体声明 FRAME-RATE. 部分 AVPlayer
    # (iPadOS 15/16、部分 Android TV) 缺这个属性时会把变体当成"未知帧率"排到最后,
    # 表现为"自动档总落在最低档"或选档后又跳回. 取 ffprobe 的源帧率; 读不到就不写.
    _fps = source.codecs.fps if source.codecs is not None else None
    frame = f",FRAME-RATE={_fps:.3f}" if _fps and _fps > 0 else ""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    if source.subtitles:
        for index, track in enumerate(source.subtitles):
            auto = "YES" if index == 0 else "NO"
            lang = track.language or "und"
            lines.append(
                f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{track.label}",'
                f'DEFAULT=NO,AUTOSELECT={auto},FORCED=NO,LANGUAGE="{lang}",'
                f'URI="subtitle/{track.id}.vtt"'
            )
    levels = list(QUALITY_LEVELS)
    # 不提供超过源分辨率的档位: 源 450p 时 1080P/720P 是无意义的放大档, 播放器菜单里不该出现.
    source_height = source.codecs.height if source.codecs is not None else None
    if source_height is not None:
        levels = [item for item in levels if item[0] == 0 or item[0] < source_height]
    preferred = source.preferred_height
    if preferred and preferred not in {height for height, _ in levels}:
        preferred = 0
    if preferred:
        levels = [item for item in levels if item[0] == preferred] + [
            item for item in levels if item[0] != preferred
        ]
    for height, label in levels:
        uri = "run.m3u8" if height == 0 else f"{height}/run.m3u8"
        bandwidth = _estimate_bandwidth(height)
        if height == 0:
            # 原画档按源分辨率给实际上限, 声明值必须跟实际峰值对齐,
            # 否则播放器会高估需求 —— 明明能播 1080, ABR 却一直缩在 720 档.
            bandwidth = _maxrate_kbps(source_height or 1080) * 1000
        # Apple HLS 要求每个 VIDEO 变体都带分辨率, 缺失时 Safari 会把它当成未知变体排到最后.
        res_height = height or source_height
        resolution = (
            f",RESOLUTION={_estimate_width(res_height)}x{res_height}" if res_height else ""
        )
        subtitles = ',SUBTITLES="subs"' if source.subtitles else ""
        # 原画档按源分辨率挑 Level, 取不到就按 1080p 兜底.
        level_height = height or (source_height if source_height else 1080)
        # HEVC 同画质大约省 25% 带宽; 不能写太低, 否则 ABR 会误判"还有余量"而卡顿.
        hevc_bandwidth = max(int(bandwidth * 0.75), 800_000)
        # 设备上真正跑得起 av1_nvenc 时, 每档额外挂一条 AV1 变体, 排在 HEVC 前面
        # (同码率画质最好, ABR 会优先选它). 不支持 AV1 的浏览器会被 CODECS 过滤掉,
        # 自动落到同一档的 HEVC / H.264 变体, 不影响兼容性.
        if source.tools.encoder == "av1_nvenc":
            av1_bandwidth = max(int(hevc_bandwidth * 0.85), 700_000)
            lines.append(
                f'#EXT-X-STREAM-INF:BANDWIDTH={av1_bandwidth},'
                f'AVERAGE-BANDWIDTH={int(av1_bandwidth * 0.8)}{resolution}{frame},'
                f'NAME="{label}",CODECS="{_av1_codec_string(level_height)}"{subtitles}'
            )
            lines.append(f"av1/{uri}")
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={hevc_bandwidth},'
            f'AVERAGE-BANDWIDTH={int(hevc_bandwidth * 0.8)}{resolution}{frame},'
            f'NAME="{label}",CODECS="{_hevc_codec_string(level_height)}"{subtitles}'
        )
        lines.append(f"hevc/{uri}")
        # H.264 变体兜底.
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
            f'AVERAGE-BANDWIDTH={int(bandwidth * 0.8)}{resolution}{frame},'
            f'NAME="{label}",CODECS="{_avc_codec_string(level_height)}"{subtitles}'
        )
        lines.append(f"h264/{uri}")
    return "\n".join(lines) + "\n"

_SUBTITLE_FETCH_TIMEOUT = 15.0


def _fetch_text(url: str, headers: dict[str, str] | None = None) -> str | None:
    """GET 一个文本资源 (字幕 / 解析接口). 失败返回 ``None``, 不抛异常."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 amane-plugin", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=_SUBTITLE_FETCH_TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return None


async def _subtitle_vtt(source: SourceContext, track_id: str) -> str | None:
    """按 track_id 拉取 resolver 给出的字幕 URL, 转成 WebVTT 文本."""
    located = source.subtitle_sources.get(track_id)
    if located is None:
        return None
    url, suffix = located
    text = await asyncio.to_thread(_fetch_text, url, source.headers or None)
    if text is None:
        return None
    return _subtitle_to_vtt(text, suffix)


class OnlineHlsLocator(HlsLocator):
    """把本插件转码出的主清单与分片交给主机反代."""

    def __init__(self, source: SourceContext) -> None:
        self._source = source

    async def load_playlist(self, query: PlaybackQuery) -> HlsPlaylist:
        source = self._source
        return HlsPlaylist(
            text=_master_text(source),
            base_url=_SERVER.base_url(source.token),
        )

    async def locate(self, query: PlaybackQuery, uri: str) -> UpstreamPlaybackTarget:
        parsed = urlparse(uri)
        if parsed.scheme != "http" or parsed.hostname != LOOPBACK or parsed.port != _SERVER.port:
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail="播放列表 URI 不受支持")
        return UpstreamPlaybackTarget(url=uri, content_type="video/mp4")
def _ass_ts_to_sec(ts: str) -> float:
    h, m, s = ts.split(":", 2)
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fmt_vtt_ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _ass_to_vtt(text: str) -> str:
    """把 ASS/SSA 字幕转成 WebVTT: 解析 [Events] 段的 Dialogue 行, 去掉样式标签."""
    import re as _re

    lines_out = ["WEBVTT"]
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start_s = _ass_ts_to_sec(parts[1].strip())
        end_s = _ass_ts_to_sec(parts[2].strip())
        content = parts[9].strip()
        if not content:
            continue
        content = _re.sub(r"\{[^}]*\}", "", content)
        content = content.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        lines_out.append("")
        lines_out.append(f"{_fmt_vtt_ts(start_s)} --> {_fmt_vtt_ts(end_s)}")
        lines_out.append(content)
    if len(lines_out) == 1:
        return "WEBVTT\n\n"
    return "\n".join(lines_out) + "\n"


def _subtitle_to_vtt(text: str, suffix: str) -> str:
    """把 srt/ass/ssa/vtt 统一转成 WebVTT 文本."""
    if suffix == ".vtt":
        return text if text.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + text
    if suffix in (".ass", ".ssa"):
        return _ass_to_vtt(text)
    return _srt_to_vtt(text)


def _srt_to_vtt(text: str) -> str:
    """把 SRT 转成 WebVTT: 只改时间戳分隔符并补文件头, 不解析样式标签."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if "-->" in stripped and "," in stripped:
            before, _, after = stripped.partition("-->")
            lines.append(f"{before.strip().replace(',', '.')} --> {after.strip().replace(',', '.')}")
        else:
            lines.append(line)
    return "WEBVTT\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 播放源
# ---------------------------------------------------------------------------


def _stream_key(index: int) -> str:
    """流的标识: 只进路径, 用序号而不是 URL (URL 含特殊字符, 不能进路径)."""
    return f"s{index}"


def _subtitle_suffix(url: str) -> str:
    """从字幕 URL 猜格式后缀; 认不出按 srt 处理 (srt -> vtt 转换对纯文本最保守)."""
    suffix = Path(urlparse(url).path).suffix.casefold()
    return suffix if suffix in SUBTITLE_SUFFIXES else ".srt"


def _safe_track_id(raw: object, fallback: str) -> str:
    """字幕轨 id 必须匹配 ``[a-zA-Z0-9._-]{1,64}``, 否则退到兜底值."""
    text = str(raw or "").strip()
    if text and re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", text):
        return text
    return fallback


@dataclass(frozen=True, slots=True)
class ResolvedSubtitle:
    """resolver 返回的一条字幕轨."""

    id: str
    label: str
    language: str | None
    url: str
    suffix: str


@dataclass(frozen=True, slots=True)
class ResolvedStream:
    """resolver 返回的一条可播流."""

    url: str
    name: str
    headers: dict[str, str]
    subtitles: tuple[ResolvedSubtitle, ...]


def _subtitles_from_json(raw: object, stream_index: int) -> tuple[ResolvedSubtitle, ...]:
    """解析 resolver 返回的字幕列表; 每项至少要有 ``url``."""
    if not isinstance(raw, list):
        return ()
    tracks: list[ResolvedSubtitle] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        fallback = f"s{stream_index}x{position}"
        name = str(item.get("label") or "").strip() or Path(urlparse(url).path).name or fallback
        language = item.get("language")
        tracks.append(
            ResolvedSubtitle(
                id=_safe_track_id(item.get("id"), fallback),
                label=name,
                language=str(language).strip() if language else None,
                url=url,
                suffix=_subtitle_suffix(url),
            )
        )
    return tuple(tracks)


def _streams_from_json(data: object, base_headers: dict[str, str]) -> tuple[ResolvedStream, ...]:
    """解析 resolver 的 JSON 响应.

    支持三种形态::

        {"streams": [{"url", "name"?, "headers"?, "subtitles"?}, ...]}
        {"url": "...", "name"?: ..., "subtitles"?: [...]}   # 单流
        [{"url": ...}, ...]                                 # 顶层数组

    每条流可以带自己的 ``headers`` (合并到解析请求头之上, 用于给具体上游带鉴权).
    """
    if isinstance(data, dict):
        if isinstance(data.get("streams"), list):
            items: object = data["streams"]
        elif data.get("url"):
            items = [data]
        else:
            return ()
    elif isinstance(data, list):
        items = data
    else:
        return ()
    streams: list[ResolvedStream] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        headers = dict(base_headers)
        extra = item.get("headers")
        if isinstance(extra, dict):
            headers.update({str(key): str(value) for key, value in extra.items()})
        streams.append(
            ResolvedStream(
                url=url,
                name=str(item.get("name") or "").strip(),
                headers=headers,
                subtitles=_subtitles_from_json(item.get("subtitles"), index),
            )
        )
    return tuple(streams)


def _parse_resolver_payload(text: str, base_headers: dict[str, str]) -> tuple[ResolvedStream, ...]:
    """解析 resolver 的响应体: JSON 按结构解析, 否则整段按纯文本媒体 URL 处理."""
    stripped = text.strip()
    if not stripped:
        return ()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except ValueError:
            logger.warning("resolver 返回的 JSON 无法解析")
            return ()
        return _streams_from_json(data, base_headers)
    return (ResolvedStream(url=stripped, name="", headers=dict(base_headers), subtitles=()),)


class OnlineProvider(PlaybackProvider):
    """把 resolver 解析出的在线媒体 URL 逐条列为可播流."""

    def __init__(self, context: PluginContext, settings: PluginSettings) -> None:
        self._settings = settings
        self._data_dir = Path(context.data_dir)
        self._cache_root = self._data_dir / "transcode"
        self._tools: Tools | None = None
        self._tools_ready = False
        self._tools_lock = asyncio.Lock()
        #: 番号 -> (解析时刻, 流列表). 按 ``RESOLVE_TTL_SECONDS`` 复用, 减轻解析接口压力.
        self._resolver_cache: dict[str, tuple[float, tuple[ResolvedStream, ...]]] = {}

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    # -- 工具链 ---------------------------------------------------------

    async def _resolve_tools(self) -> Tools | None:
        if self._tools_ready:
            return self._tools
        async with self._tools_lock:
            if not self._tools_ready:
                self._tools = await asyncio.to_thread(_discover_tools, self._settings, self._data_dir)
                if self._tools is None:
                    # 本机没有现成 ffmpeg: 自动部署 (上传的包 / 本机包 / 国内镜像), 全程本地化.
                    logger.warning("未找到 ffmpeg, 开始自动部署 (压缩包或国内镜像)")
                    await asyncio.to_thread(_deploy_ffmpeg, self._settings, self._data_dir)
                    self._tools = await asyncio.to_thread(_discover_tools, self._settings, self._data_dir)
                self._tools_ready = True
        return self._tools

    # -- 解析器 ---------------------------------------------------------

    def _resolver_headers(self) -> dict[str, str]:
        token = self._settings.resolver_token.strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _streams(self, query: PlaybackQuery) -> tuple[ResolvedStream, ...]:
        """按条目番号向解析接口要媒体 URL; 结果按番号缓存."""
        number = (query.number or "").strip()
        if not number:
            return ()
        template = self._settings.resolver_url.strip()
        if not template:
            return ()
        now = time.monotonic()
        cached = self._resolver_cache.get(number)
        if cached is not None and now - cached[0] < RESOLVE_TTL_SECONDS:
            return cached[1]
        url = template.replace("{number}", urllib.parse.quote(number, safe=""))
        headers = self._resolver_headers()
        try:
            text = await asyncio.to_thread(
                _fetch_text, url, {**headers, "Accept": "application/json, text/plain, */*"}
            )
        except Exception:  # pragma: no cover - _fetch_text 本身不抛, 防御
            text = None
        if text is None:
            logger.warning("解析接口请求失败: %s", url)
            return ()
        streams = _parse_resolver_payload(text, headers)
        if len(self._resolver_cache) >= _CODEC_CACHE_SIZE:
            self._resolver_cache.clear()
        self._resolver_cache[number] = (now, streams)
        return streams

    # -- 探测 -----------------------------------------------------------

    async def _codecs(self, stream: ResolvedStream, tools: Tools | None) -> MediaCodecs | None:
        if tools is None:
            return None
        header_tag = "|".join(f"{k}:{v}" for k, v in sorted(stream.headers.items()))
        cache_key = f"{stream.url}|{header_tag}"
        if cache_key in _CODEC_CACHE:
            return _CODEC_CACHE[cache_key]
        probed = await asyncio.to_thread(_probe_codecs, tools.ffmpeg, stream.url, stream.headers)
        _cache_codecs(cache_key, probed)
        return probed

    @staticmethod
    def _codecs_playable(codecs: MediaCodecs | None) -> bool:
        if codecs is None:
            # 探测失败不阻塞: 按可播处理, 让直连/上游自己去试 (直连失败播放器会报错,
            # 转码失败由 resolve 阶段的 SourceError 兜底).
            return True
        if codecs.video is not None and codecs.video not in BROWSER_SAFE_VIDEO:
            return False
        return codecs.audio is None or codecs.audio in BROWSER_SAFE_AUDIO

    async def _plan(self, stream: ResolvedStream, tools: Tools | None) -> Plan:
        """判定一条流怎么播: 直连 (浏览器可解码) / 转码 HLS / 不可播."""
        suffix = Path(urlparse(stream.url).path).suffix.casefold()
        reason = NOT_MEDIA.get(suffix)
        if reason is not None:
            return Plan("unavailable", unavailable=reason)
        direct_type = DIRECT_TYPES.get(suffix)
        if tools is not None and self._settings.direct_play and direct_type is not None:
            codecs = await self._codecs(stream, tools)
            if self._codecs_playable(codecs):
                return Plan("direct", content_type=direct_type)
        if tools is not None:
            return Plan("hls", content_type=HLS_CONTENT_TYPE)
        if direct_type is not None:
            codecs = await self._codecs(stream, tools)
            if self._codecs_playable(codecs):
                return Plan("direct", content_type=direct_type)
        detail = (
            "浏览器无法解码此来源, 且未找到 ffmpeg"
            if direct_type is None
            else "浏览器无法解码此来源的编码, 且未找到 ffmpeg"
        )
        return Plan("unavailable", content_type=direct_type or "video/mp4", unavailable=detail)

    # -- 探流 -----------------------------------------------------------

    async def probe(self, query: PlaybackQuery) -> tuple[PlaybackOffer, ...]:
        streams = await self._streams(query)
        if not streams:
            raise SourceError(
                FailureReason.NO_USABLE_METADATA,
                detail="解析器没有返回可播放的媒体地址",
            )
        tools = await self._resolve_tools()
        offers: list[PlaybackOffer] = []
        for index, stream in enumerate(streams):
            plan = await self._plan(stream, tools)
            subtitles = tuple(
                SubtitleTrack(id=sub.id, label=sub.label, language=sub.language)
                for sub in stream.subtitles
            )
            name = stream.name or Path(urlparse(stream.url).path).name or f"流 {index + 1}"
            offers.append(
                PlaybackOffer(
                    key=_stream_key(index),
                    name=name,
                    content_type=plan.content_type,
                    seekable=plan.seekable,
                    unavailable=plan.unavailable,
                    subtitles=subtitles,
                )
            )
        return tuple(offers)

    # -- 取流 -----------------------------------------------------------

    async def resolve(self, query: PlaybackQuery) -> UpstreamPlaybackTarget | HlsPlaybackTarget | None:
        """返回选中那条流对应的播放目标; 没指定时挑第一条."""
        streams = await self._streams(query)
        if not streams:
            return None
        index = 0
        key = (query.selected_key or "").strip()
        if key:
            match = re.fullmatch(r"s(\d+)", key)
            if match is not None:
                index = int(match.group(1))
        if index >= len(streams):
            index = 0
        stream = streams[index]
        tools = await self._resolve_tools()
        plan = await self._plan(stream, tools)
        if plan.kind == "unavailable":
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail=plan.unavailable or "没有可播放的流")
        if plan.kind == "direct":
            # 直连: 原样跳上游, 不经过转码. 由主机反代转发上游字节 (鉴权头由 resolver 给出).
            return UpstreamPlaybackTarget(
                url=stream.url,
                headers=stream.headers,
                content_type=plan.content_type,
                cache_ttl=RESOLVE_TTL_SECONDS,
            )
        return await self._transcode_target(stream, tools)

    async def _transcode_target(self, stream: ResolvedStream, tools: Tools | None) -> HlsPlaybackTarget:
        if tools is None:
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail="未找到 ffmpeg, 无法转码播放此来源")
        codecs = await self._codecs(stream, tools)
        duration = codecs.duration if codecs is not None else None
        if duration is None or duration <= 0:
            raise SourceError(FailureReason.NO_USABLE_METADATA, detail="读不出这部片子的时长, 无法转码播放")
        settings = self._settings
        seconds = segment_seconds(codecs)
        cache_root = await asyncio.to_thread(self._prepare_cache)
        source = await _SERVER.register_source(
            cache_root=cache_root,
            url=stream.url,
            headers=stream.headers,
            codecs=codecs,
            duration=duration,
            seconds=seconds,
            settings=settings,
            tools=tools,
            subtitles=tuple(
                SubtitleTrack(id=sub.id, label=sub.label, language=sub.language)
                for sub in stream.subtitles
            ),
            subtitle_sources={sub.id: (sub.url, sub.suffix) for sub in stream.subtitles},
            preferred_height=0,
        )
        # 预创建原画档会话: 起播即转好第一段, 其它档位等播放器切换时懒创建.
        await _SERVER.session_for(source, 0)
        return HlsPlaybackTarget(locator=OnlineHlsLocator(source))

    def _prepare_cache(self) -> Path:
        """转码输出用的目录, 不存在就建."""
        self._cache_root.mkdir(parents=True, exist_ok=True)
        return self._cache_root

    async def subtitle(self, query: PlaybackQuery, track_id: str) -> str | None:
        streams = await self._streams(query)
        for stream in streams:
            for sub in stream.subtitles:
                if sub.id != track_id:
                    continue
                text = await asyncio.to_thread(_fetch_text, sub.url, stream.headers or None)
                if text is None:
                    return None
                return _subtitle_to_vtt(text, sub.suffix)
        return None


class Plugin(PlaybackPlugin):
    """在线媒体播放源插件."""

    config_model = PluginSettings

    @classmethod
    def descriptor(cls) -> SourceDescriptor:
        return SourceDescriptor(
            id=SOURCE_ID,
            name="在线播放",
            version="0.1.0",
            api_version="1",
            capabilities=frozenset({SourceCapability.PLAYBACK}),
        )

    def build_playback(self, context: PluginContext, config: BaseModel) -> PlaybackProvider:
        settings = config if isinstance(config, PluginSettings) else PluginSettings()
        provider = OnlineProvider(context, settings)
        # 插件刚被这个实例加载: 上一个实例遗留的 ffmpeg 从此刻起没有主人, 立刻在后台回收.
        _start_reaper(provider.cache_root)
        return provider
