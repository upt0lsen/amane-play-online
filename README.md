# amane-play-online

[Amane](https://github.com/sqzw-x/amane) 在线媒体播放源插件：**302 直连 + 转码双通路**。

- **直连通路**：浏览器可解码的在线源（mp4 / m4v / mov / webm / ogv，视频 h264 / vp8 / vp9 /
  av1，音频 aac / opus / mp3 …）原样返回 `UpstreamPlaybackTarget` 跳上游，不转码、画质无损、
  任意跳转。
- **转码通路**：其余来源由本插件调用 ffmpeg 从 URL 拉流，实时转成 fMP4 HLS（完整 VOD 清单，
  `-ss` 拖动重起转，分片缓存空闲回收，NVENC / QSV 硬件编码自动探测回退，AAC 重编码对齐分片
  边界，srt / ass / vtt 字幕自动转 WebVTT），分片经回环 HTTP 由 Amane 反代给播放器。

媒体 URL 完全由**可配置 resolver**（插件设置项 `resolver_url`）按条目番号解析返回，插件不
内置任何上游。架构参照社区样本插件 `nas.local`（本机文件播放源，GPL-3.0）；样本的「原画
直出」通路依赖本机 mp4 关键帧表，对在线源不适用，未实现——浏览器可解码的在线源由直连通路
覆盖。

## 安装

插件目录放入 Amane 数据目录 `plugins/sources/upt0lsen.play-online/`（目录名必须与插件 ID
一致），然后在「管理 → 插件」页重新加载；或把整个目录打成 zip 从插件页上传。目录内文件：

```
upt0lsen.play-online/
├── plugin.py      # 插件本体（唯一必需文件）
└── README.md      # 可留可删
```

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `resolver_url` | （空） | **必填**。GET 地址，用 `{number}` 占位条目番号。返回 JSON（见下）或纯文本媒体 URL |
| `resolver_token` | （空） | 非空时作为 `Authorization: Bearer <令牌>` 发给解析接口。令牌只填在运行配置里，**不要写进任何入库文件** |
| `resolver_timeout` | 10 | 解析接口超时（秒） |
| `direct_play` | true | 允许浏览器可解码的源直连播放；关闭则一律转码 |
| `ffmpeg_path` | （空） | 留空自动在 PATH / 插件数据目录 / Jellyfin / 常见位置查找 |
| `ffmpeg_bundle_path` | （空） | 本机 ffmpeg 压缩包路径，找不到时自动解压部署 |
| `ffmpeg_download_url` | （空） | 留空用内置国内镜像自动下载 |
| `encoder` | auto | auto / nvenc / qsv / cpu |
| `nvenc_codec` / `qsv_codec` | auto | 硬件编码家族内格式选择 |
| `preset` | veryfast | 转码预设 |
| `crf` | 23 | 转码画质 |
| `max_height` | 0 | 0 保持原分辨率 |
| `idle_timeout` | 900 | 缓存保留（秒），超时删分片 |

### resolver 接口约定

`GET {resolver_url}`（`{number}` 已替换为番号，URL 编码）。响应两种形式：

**JSON（推荐）**，单流或多流，流可带独立 `headers`（合并到解析请求头之上，用于给具体
上游带鉴权）与 `subtitles`：

```json
{
  "streams": [
    {
      "url": "https://cdn.example.com/ABC-123.mp4",
      "name": "MP4 直连",
      "headers": {"X-Upstream-Token": "..."},
      "subtitles": [
        {"url": "https://cdn.example.com/ABC-123.srt", "label": "中字", "language": "chi"}
      ]
    },
    {"url": "https://cdn.example.com/ABC-123.mkv", "name": "蓝光"}
  ]
}
```

**纯文本**：整段响应体就是一个媒体 URL。

解析结果按番号缓存 600 秒。字幕轨 id 若含路径非法字符会被替换为兜底值；后缀按 URL 猜测
（srt / vtt / ass / ssa），认不出按 srt 处理。

## 测试

```bash
python -m venv .venv && .venv/bin/pip install pydantic pytest
.venv/bin/python -m pytest tests/ -v
```

39 个用例：设置校验 / resolver 解析 / 字幕转换 / 清单生成 / 双通路判定（打桩，不触网），
外加真实 ffmpeg 端到端（本机起 Range 静态服务 → URL 探测 → 起转 → VOD 清单 → 拖动末段
`-ss` 重起转），需要本机装有 ffmpeg，未装自动跳过。

## 回滚

插件无数据库迁移、不改主机数据。卸载：插件页删除（或直接删
`plugins/sources/upt0lsen.play-online/` 目录）→ 重新加载。运行期残留：转码缓存位于插件
数据目录 `transcode/`，随目录一并删除；残留 ffmpeg 进程在插件重载时由 reaper 自动回收。

## 许可

GPL-3.0（与 Amane 及 `nas.local` 样本一致）。架构与转码管线思路参照社区样本
`nas.local`，按 GPL-3.0 继续使用。
