# amane-play-online

> **实验功能**：真实 115 / 夸克 / WebDAV 各源的直链与本地回退链路尚未实测联调，
> 联调验证前请勿用于重要场景。已知边界见下文「resolver 行为说明」与 PR 待确认问题。

[Amane](https://github.com/sqzw-x/amane) 网盘挂载播放源插件：**直链直连 + 转码双通路**。

输入与社区样本 `nas.local` 一致——条目已索引的**本地路径**（docker 挂载点）。片子物理上在
115 / 夸克等网盘，由 AList / WebDAV 挂载进容器，本插件在播放时把本地路径解析回**网盘直链**：

- **直连通路**：容器与编码浏览器都能解码时（mp4 / m4v / mov / webm / ogv，视频 h264 / vp8 /
  vp9 / av1，音频 aac / opus / mp3 …），经挂载映射解析成网盘直链，返回
  `UpstreamPlaybackTarget` 跳上游，不转码、画质不变、任意跳转。
- **转码通路**：其余文件由本插件调用 ffmpeg 实时转成 fMP4 HLS（完整 VOD 清单、`-ss` 拖动
  重起转、分片缓存空闲回收、NVENC / QSV 硬件编码自动探测回退、AAC 重编码对齐分片边界）。
  ffmpeg **优先从网盘直链 URL 拉流**（避免走 FUSE 双重中转），resolver 失败时回退读本机
  挂载路径。
- 同目录同名 `.srt` / `.vtt` / `.ass` 字幕自动列轨并转 WebVTT（与样本一致）。

路径 → 后端的映射由插件设置项 `mounts` 决定（**最长前缀匹配**，多组）：

| 本地前缀 | 后端 | 说明 |
|---|---|---|
| `/mnt/115` | `alist: http://alist:5244` | AList 挂载的 115 网盘，`POST /api/fs/get` 现签直链 |
| `/mnt/quark` | `alist: http://alist:5244` | 同一 AList 的另一个挂载点 |
| `/mnt/dav` | `webdav: https://dav.xxx/dav` | WebDAV 直接拼 HTTP 地址 |

AList 直链带时效 sign，**不缓存，每次播放请求现解析现跳**；WebDAV 支持 Basic 认证。每个挂载
可单独关闭直链（`allow_direct=False`），该挂载一律读本机路径。

架构参照社区样本 `nas.local`（本机文件播放源，GPL-3.0）；样本的「原画直出」通路依赖本机
mp4 关键帧表，本插件不实现——浏览器可解码文件由**直连通路**覆盖。

## 安装

插件目录放入 Amane 数据目录 `plugins/sources/upt0lsen.play-online/`（目录名必须与插件 ID
一致），然后在「管理 → 插件」页重新加载；或把整个目录打成 zip 从插件页上传。目录内文件：

```
upt0lsen.play-online/
├── plugin.py      # 插件本体（唯一必需文件）
└── README.md      # 可留可删
```

## 配置

### mounts（挂载映射，核心配置）

每组映射字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `local_prefix` | —（必填） | docker 内挂载路径前缀，如 `/mnt/115`；最长前缀匹配 |
| `backend` | `alist` | `alist` / `webdav` |
| `address` | （空） | alist: `http://alist:5244`；webdav: `https://dav.xxx/dav`（到挂载根为止） |
| `remote_prefix` | `/` | 后端内该挂载对应的起始路径，如 `/115` |
| `token` | （空） | alist: AList 令牌；webdav: 密码。**只填运行配置，绝不入库** |
| `allow_direct` | `true` | 关闭后该挂载一律读本机路径（不走直链） |
| `prefer_url_transcode` | `true` | 转码时 ffmpeg 优先读 http 直链，解析失败自动回退本地 |

### 转码与直连（全局）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `direct_play` | `true` | 允许浏览器可解码的文件直连播放；关闭则一律转码 |
| `ffmpeg_path` | （空） | 留空自动在 PATH / 插件数据目录 / Jellyfin / 常见位置查找 |
| `ffmpeg_bundle_path` | （空） | 本机 ffmpeg 压缩包路径，找不到时自动解压部署（无需 opt-in） |
| `ffmpeg_download_url` | （空） | 固定版本下载地址（不要用 latest），与哈希配套 |
| `allow_ffmpeg_download` | `false` | **默认关闭**。开启后仍须配置固定版本地址 + SHA-256，校验通过才执行 |
| `ffmpeg_download_sha256` | （空） | 64 位十六进制；留空或非 64 位一律拒绝下载 |
| `encoder` | auto | auto / nvenc / qsv / cpu |
| `nvenc_codec` / `qsv_codec` | auto | 硬件编码家族内格式选择 |
| `preset` | veryfast | 转码预设 |
| `crf` | 23 | 转码画质 |
| `max_height` | 0 | 0 保持原分辨率 |
| `idle_timeout` | 900 | 缓存保留（秒），超时删分片 |

### resolver 行为说明

- **alist**：`POST {address}/api/fs/get`，body `{"path": "{remote_prefix 下的路径}"}`，取
  `data.raw_url`（签名直链），`data.url` 兜底。响应非 200 或网络失败 → 回退本机路径。
  返回的 URL 必须是绝对 http(s)，否则拒绝（防篡改后把 `file://` 喂给 ffmpeg）。
- **webdav**：`{address}/{逐段转义的远端路径}`；地址必须是绝对 http(s)；有用户名/密码时加
  Basic 认证头。
- **路径安全**：本地路径含 `.` / `..` 段一律不解析（回退本机），防止越出挂载点。
- **直链 sign 时效**：不缓存解析结果，每次播放请求现签现跳；`UpstreamPlaybackTarget`
  不声明 `cache_ttl`。
- **3xx 预检（重要）**：amane v0.28.0 的播放代理 `follow_redirects=False`，上游 3xx 一律
  502。因此插件在返回直链前自己做重定向预检：跟随最多 5 跳，终点必须 200/206 且是媒体流
  （错误页 Content-Type 判不可用）；预检不过自动回退本机路径，不把 3xx 甩给主机。
- **凭据策略**：重定向**同源才带敏感头**——任何一跳 origin（scheme://host:port）变化，
  立即剥离 `Authorization` 等敏感头再继续，不把 Basic/Bearer 转发给第三方域。
- **302 说明**：amane SDK 的 `UpstreamPlaybackTarget` 是主机反代转发，不是浏览器直连的
  真 302；115/夸克 `/d/` 链接浏览器直访可行性**待实测**（见 PR 待确认问题）。
- **非法 key**：`selected_key` 认不出时插件拒绝（`SourceError`），不会改播别的文件。

## 测试

```bash
python -m venv .venv && .venv/bin/pip install pydantic pytest
.venv/bin/python -m pytest tests/ -v
```

42 个用例：设置/挂载映射校验、alist 现签（本机假 HTTP 服务验证请求路径与鉴权头）、WebDAV
地址拼接、双通路判定、字幕转换、清单生成，外加真实 ffmpeg 端到端（HTTP 直链拉流转 HLS +
本机路径回退转 HLS，拖动末段 `-ss` 重起转），需要本机装有 ffmpeg，未装自动跳过。

## 回滚

插件无数据库迁移、不改主机数据。卸载：插件页删除（或直接删
`plugins/sources/upt0lsen.play-online/` 目录）→ 重新加载。运行期残留：转码缓存位于插件
数据目录 `transcode/`，随目录一并删除；残留 ffmpeg 进程在插件重载时由 reaper 自动回收。

## 许可

GPL-3.0（与 Amane 及 `nas.local` 样本一致）。架构与转码管线思路参照社区样本
`nas.local`，按 GPL-3.0 继续使用。
