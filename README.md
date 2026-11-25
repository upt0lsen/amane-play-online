# amane-play-online

> **实验功能**：各网盘源的直链与本地回退链路尚未全部实测联调，联调验证前请勿用于重要场景。

[Amane](https://github.com/sqzw-x/amane) 网盘挂载播放源插件：**直链直连 + 转码双通路**。

## 它是什么

输入与社区样本 `nas.local` 一致——条目已索引的**本地路径**（容器挂载点）。媒体文件物理上
在网盘，由挂载工具映射进容器，本插件在播放时把本地路径解析回**网盘直链**：

- **直连通路**：容器与浏览器都能解码时（mp4 / m4v / mov / webm / ogv，视频 h264 / vp8 /
  vp9 / av1，音频 aac / opus / mp3 …），经挂载映射解析成网盘直链，返回
  `UpstreamPlaybackTarget` 跳上游，不转码、画质不变、任意跳转。
- **转码通路**：其余文件由插件调用 ffmpeg 实时转成 fMP4 HLS（完整 VOD 清单、`-ss` 拖动
  重起转、分片缓存空闲回收、NVENC / QSV 硬件编码自动探测回退、AAC 重编码对齐分片边界）。
  ffmpeg **优先从网盘直链 URL 拉流**（避免走 FUSE 双重中转），解析失败时回退读本机
  挂载路径。
- 同目录同名 `.srt` / `.vtt` / `.ass` 字幕自动列轨并转 WebVTT（与样本一致）。

## 技术路线

路径 → 后端的映射由插件设置项 `mounts` 决定（**最长前缀匹配**，可配多组）。支持三类后端：

| 后端 | 解析方式 |
|---|---|
| `alist` | `POST {address}/api/fs/get` 现签直链（`data.raw_url`，`data.url` 兜底） |
| `webdav` | `{address}/{逐段转义的远端路径}` 直接拼 HTTP 地址，支持 Basic 认证 |
| `clouddrive2` | gRPC `GetDownloadUrlPath(get_direct_url=true)` 取云盘 CDN 直链（零第三方依赖：gRPC-web+proto over HTTP/1.1 + 手写极简 protobuf 编解码） |

关键设计：

- **绕过挂载层**：直链通路的媒体流由 Amane 主机反代直拉网盘 CDN，不经过挂载层 FUSE 与
  网盘服务自身转发（媒体字节仍经过 Amane 主机网口）；转码通路的 ffmpeg 也优先从直链拉流。
- **直链不缓存**：签名直链有时效，每次播放请求现解析现跳（`cache_ttl=None`）。
- **失败回退本机**：任何一步解析/预检失败都回退读本机挂载路径，不会把错误甩给播放器。
- **凭据边界**：敏感凭据优先从容器环境变量注入，不落入配置文件；带凭据的直链不交给
  ffmpeg（见「resolver 行为说明」）。

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
| `local_prefix` | —（必填） | 容器内挂载路径前缀，如 `/media/mycloud`；最长前缀匹配 |
| `backend` | `alist` | `alist` / `webdav` / `clouddrive2` |
| `address` | （空） | alist: `http://alist:5244`；webdav: `https://dav.example.com/dav`；clouddrive2: `http://<主机>:19798`（gRPC 与 Web/WebDAV 同端口） |
| `remote_prefix` | `/` | 后端内该挂载对应的起始路径，如 `/mycloud` |
| `token` | （空） | alist: AList 令牌；webdav: 密码；clouddrive2: API 令牌。**优先环境变量注入**（见下）；写入本字段会被**持久化到配置文件且可被读回明文**，敏感凭据请勿写在这里，环境变量未设时才回退本字段 |
| `allow_direct` | `true` | 关闭后该挂载一律读本机路径（不走直链） |
| `prefer_url_transcode` | `false` | 转码时 ffmpeg 优先读 http 直链；**默认关闭**——签名直链本身即凭据，不给 ffmpeg；开启后解析失败自动回退本地 |

**环境变量注入凭据**：敏感凭据按挂载前缀派生变量名，规则为
`PLAYONLINE_<种类>_<local_prefix 派生后缀>`（路径转大写、非字母数字转下划线）。例如挂载
`/media/mycloud`：

| 后端 | 变量 |
|---|---|
| alist 令牌 | `PLAYONLINE_TOKEN_MEDIA_MYCLOUD` |
| webdav 用户名 / 密码 | `PLAYONLINE_WEBDAV_USER_MEDIA_MYCLOUD` / `PLAYONLINE_WEBDAV_PASS_MEDIA_MYCLOUD` |
| clouddrive2 令牌 | `PLAYONLINE_CD2_TOKEN_MEDIA_MYCLOUD` |

注意：容器环境变量在启动时固定，轮换凭据需重建容器；**写入配置文件的 token 会被持久化保存，任何能读到配置文件的人都能读回明文**。因此敏感凭据务必走环境变量注入，配置文件里只放地址、前缀、开关等非敏感项；`token` 字段仅为本地/测试场景的兑底，不建议在生产配置中使用。

### CloudDrive2 令牌要求

WebUI「系统管理 → API 令牌」创建，**必须勾选「直链」能力**（官方说明：打开之后拿到的是
云盘直连地址，数据不再经过远端机器转发），权限位含读即可。链路：
`GetDownloadUrlPath(path, get_direct_url=true)` → `directUrl`（CDN 直链，免鉴权）→ 浏览器
经 Amane 反代拉 CDN。返回的 `userAgent` / `additionalHeaders` 是云盘侧要求的辅助头
（非敏感），随直链一并带给上游——部分 CDN 强制校验 User-Agent，缺失会被拒绝；插件在
预检与 ffmpeg 转码（`-user_agent` 参数）中都会携带。未返回 `directUrl`（令牌未开直链
能力 / 云盘不支持）时**回退本机路径**，不静默降级到代理形态。

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

- **路径安全**：本地路径含 `.` / `..` 段一律不解析（回退本机），防止越出挂载点；后端返回
  的 URL 必须是绝对 http(s)，否则拒绝（防 `file://` 等被喂给 ffmpeg）。
- **3xx 预检**：Amane 的播放代理不自动跟随上游重定向（3xx 一律 502），因此插件在返回直链
  前自己做重定向预检：跟随最多 5 跳，终点必须 200/206 且 Content-Type 在媒体类型白名单内
  （缺失/畸形一律拒绝）；预检不过自动回退本机路径。
- **媒体类型按消费者分流**：直连（经 Amane 反代）只放行 `video/*`/`audio/*`——代理侧不转发
  `application/octet-stream`，预检放过也只会在代理侧 502，不如直接回退本机；转码拉流
  （ffmpeg）不挑 Content-Type，额外放行 `application/octet-stream`。
- **同源凭据策略**：任何一跳 origin（scheme://host:port）变化，立即剥离 `Authorization`
  等敏感头再继续，不把 Basic/Bearer 转发给第三方域（AList API 调用同样逐跳处理）。
- **ffmpeg 不是凭据边界**：预检只保护预检请求本身，ffmpeg 的真实 GET 遇跨源 302 不受插件
  控制，因此**带敏感凭据或不可信附加头的直链一律不交给 ffmpeg**（Cookie 永不交
  ffmpeg；后端附加头语义不可全信），转码输入回退本机路径；无凭据直链（签名链 / CDN
  直链）预检通过后正常使用，且默认关闭（`prefer_url_transcode=false`）。
- **直链过期续签**：云盘直链有时效（CloudDrive2 给出 `expiresIn`），转码会话在每次
  起转/拖动重转前检查有效期，临期或被 CDN 拒绝（401/403 秒退）时自动重签续转；续签失败
  才回退本机挂载路径。
- **非法 key**：`selected_key` 认不出时插件拒绝（`SourceError`），不会改播别的文件。

## 测试

```bash
python -m venv .venv && .venv/bin/pip install pydantic pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -v
```

覆盖设置/挂载映射校验、各后端解析（本机假 HTTP 服务验证请求路径与鉴权头）、双通路判定、
重定向与凭据边界回归、字幕转换、清单生成，以及真实 ffmpeg 端到端（HTTP 直链拉流转 HLS +
本机路径回退转 HLS，拖动末段 `-ss` 重起转），需要本机装有 ffmpeg，未装自动跳过。

## 回滚

插件无数据库迁移、不改主机数据。卸载：插件页删除（或直接删
`plugins/sources/upt0lsen.play-online/` 目录）→ 重新加载。运行期残留：转码缓存位于插件
数据目录 `transcode/`，随目录一并删除；残留 ffmpeg 进程在插件重载时自动回收。

## 许可

GPL-3.0（与 Amane 及 `nas.local` 样本一致）。架构与转码管线思路参照社区样本
`nas.local`，按 GPL-3.0 继续使用；归属说明见 `NOTICE`。
