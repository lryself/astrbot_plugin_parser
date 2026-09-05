# NAS 视频归档

本 fork 基于 Zhalslar/astrbot_plugin_parser。QQ 预览与 NAS 归档分别选流、下载和存储，
无需 OpenClaw 或 Mac mini。首次处理可能下载两个清晰度版本；有效归档再次出现时不再下载。

## 触发与范围

- `archive_directory`：容器内绝对目录，留空关闭，必须与 cache 分开。
- `archive_users`：发送者白名单，例如 `aiocqhttp:123456789`；`*` 允许所有私聊用户，
  不扩大群聊权限，也不授予 AstrBot 管理员权限。留空无人可归档。
- `archive_groups`：允许归档的完整群会话 unified_msg_origin，空列表不允许群聊归档。
- `enable_reply_parse`：使用回复卡片归档或重新下载时需要开启。

允许的私聊用户直接分享视频或卡片，默认归档。群聊需由明确列出的用户，在允许的群内发送：

- `归档 https://b23.tv/xxxx`
- 回复卡片后发送 `归档` 或 `保存到NAS`

`重新下载` 是明确的重置操作，支持回复卡片或携带链接：删除对应视频的缓存、永久文件及去重记录，
然后重新下载、归档。不会删除其他视频；若新下载失败，先前删除的文件不会自动恢复。
群聊中的单纯分享、`下载`、`不要下载` 不触发永久归档。原插件的会话过滤、群仲裁仍然生效。

## 默认媒体规格

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `cache_video_quality` | `1080P` | QQ 预览选流上限 |
| `source_max_size` | `300` MB | 预览缓存单视频大小限制，合并后也检查 |
| `source_max_minute` | 上游默认值 | 预览时长限制，已有用户设置保留 |
| `archive_video_quality` | `BEST` | 账号权限范围内的最高可用清晰度 |
| `archive_max_size` | `0` | 归档大小不限；可单独设限 |
| `archive_max_minute` | `0` | 归档单 P 时长不限；可单独设限 |

清晰度配置作用于 B 站和 yt-dlp 支持的选流路径。只提供单一媒体地址的平台仍取其可用资源，
不会承诺不存在的清晰度，也不会把低分辨率视频放大成 1080P。登录或会员权限限制仍然有效。
B 站未登录或登录失效会在处理时主动提示；网络异常只提示暂时无法确认，不误判成退出登录。
登录可通过插件 Cookie 配置，或 AstrBot 管理员的 `/blogin` 扫码流程完成。

## 存储与去重

- 预览缓存：`cache/preview`；归档下载暂存：`cache/archive`。两个清晰度互不复用。
- 永久视频按平台分目录。B 站多 P 归档会下载全部分 P，目录名为视频标题，内部为
  `P01－分集标题--标识.mp4`、`P02－分集标题--标识.mp4`，保持分集顺序。
  QQ 预览只发送分享时指定的分 P；永久目录保存整部视频。
- 非多 P 文件名包含标题、来源标识、资源指纹和媒体序号。
- 复制完成后校验 SHA-256，再原子发布；不覆盖同身份但内容不同的既有文件。
- `archive-index.sqlite` 保存在插件数据目录中，独立于可清理缓存。完整归档通过文件校验后，
  重复请求直接回复“已归档，跳过重复下载”。B 站短链、AV、BV 归一化到同一整部视频身份。
- 缺失或损坏的文件不能作为去重依据；内容冲突需要明确 `重新下载`，不会偷偷覆盖。
- 多 P 部分失败时，保留成功部分的记录；重试从 NAS 复用这些部分，只补齐失败部分。
- 下载、归档与缓存清理协调执行。归档完成后清除高画质暂存；预览继续使用 AstrBot/NapCat 共享缓存。
- QQ 发送失败不会撤销已经完成的归档；QQ 完全不可用时通知也可能失败，应检查 NAS 和日志。
- 不归档封面、临时音轨、独立音频、GIF 或图片。永久文件无自动过期策略，由用户管理。

## Docker 挂载

仅给 AstrBot 服务增加永久目录挂载，NapCat 继续使用原来共享的 `/AstrBot/data`：

```yaml
volumes:
  - ./data:/AstrBot/data
  - /your/nas/video-directory:/media-archive
```

配置 `archive_directory=/media-archive`。只重建 AstrBot 容器并保留 data 卷，验证挂载和 NAS 用户读权限。
归档目录不能放在 cache 内；目标文件系统需支持硬链接。

## 跟进上游与回滚

`origin` 指向个人 fork，`upstream` 指向原作者。metadata 的 repo 保持个人 fork 地址。
在独立分支合并上游，浅克隆需要时逐步 deepen；不直接覆盖正在运行的版本：

```bash
git fetch upstream
git switch -c sync/upstream-YYYYMMDD origin/main
git merge upstream/main
```

冲突重点：main 消息处理顺序、清理租约、下载临时文件发布、B 站选流和多 P、配置及 metadata。
不要自动改回原作者仓库地址。验证后再合并 fork 主分支，备份 NAS 插件目录和配置后部署。

```bash
python -m pytest -q tests
# 在有 AstrBot 依赖的隔离候选目录执行，不会向真实 QQ 发消息：
python tests/runtime_archive_check.py
python tests/runtime_download_check.py
python tests/runtime_merge_check.py
python tests/runtime_bilibili_check.py
```

回滚恢复插件和配置，保留永久视频与索引；不能把新归档目录随代码回滚删除。
升级后仍应验证一次真实 B 站分享、重复分享和明确重新下载。
