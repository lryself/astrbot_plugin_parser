# NAS 视频归档

本 fork 基于 Zhalslar/astrbot_plugin_parser，保留上游解析、下载与 QQ 发送功能。
归档是下载后的一次本机文件复制，不需要 OpenClaw、Mac mini 或第二次下载。

## 配置与触发

- `archive_directory`：容器内的绝对目录，留空关闭。必须与插件 cache 目录分开。
- `archive_users`：允许归档的发送者，例如 `aiocqhttp:123456789`。留空无人可归档；不授予 AstrBot 管理员权限。
- `archive_groups`：允许归档的完整群会话 unified_msg_origin；空列表只允许私聊。
- `enable_reply_parse`：使用回复卡片归档时需开启。

白名单用户在私聊分享视频链接或卡片时默认归档。群聊需在允许的群中以 `归档` 或 `保存到NAS` 开头：

- `归档 https://b23.tv/xxxx`
- 回复 B 站小程序卡片，发送 `归档`
- `请归档这个视频 https://www.bilibili.com/video/BV...`

群聊中的单纯分享、`下载`、`不要下载` 均不触发永久归档。原解析插件的会话白名单、黑名单和群仲裁仍然生效。
先分享卡片、随后回复归档不会被原链接防抖静默跳过；解析结果仍使用已有缓存任务。

## 容器挂载

在 AstrBot 服务的 volumes 中增加一条目标影音目录挂载，例如：

```yaml
volumes:
  - ./data:/AstrBot/data
  - /your/nas/video-directory:/media-archive
```

配置 `archive_directory=/media-archive`。修改挂载后只重建 AstrBot 容器，保留其 data 卷与 NapCat 容器。
不应把归档目录挂载到插件 cache 下，否则启动时拒绝该配置。

## 保存语义

- 只归档 `VideoContent` 完整视频；封面、临时音轨、GIF、图片、音频不进入视频库。
- 按平台分目录，文件名包含标题、来源 URL 的末段、资源指纹、媒体序号；多分 P 来源的 URL/资源指纹区分各集。
- 标题不参与资源指纹，同资源改标题再次归档仍能找到已存文件。
- 临时文件复制完后校验 SHA-256，使用同目录硬链接原子发布，不覆盖既有文件。目标文件系统需支持硬链接。
- 已有相同身份的文件会比对内容；内容不同报失败，保留原件，不自动更新画质或覆盖视频。
- 下载、消息处理和缓存清理协调执行；归档目录不参与清理。
- 归档完成后，QQ 视频发送读取永久文件路径；发送失败不撤销归档。若 QQ 完全不可用，通知也可能发不出去，磁盘文件和日志是核验依据。
- 单条归档部分失败时仍处理其他视频，回复新增、已存在和失败数量，具体错误留在 AstrBot 日志。
- 大小、时长、Cookie 与平台权限仍沿用原插件设置。此功能不会绕过这些限制。
- 归档文件没有自动过期策略，由用户通过 NAS 管理。磁盘不足或挂载不可写会报归档失败。

## 更新与回滚

`origin` 指向个人 fork，`upstream` 指向原作者。默认分支包含已验证的 NAS 改造。
插件 metadata 与安装目录的 Git remote 都必须指向 fork，避免一键更新回原作者版本。

更新在独立分支进行：

```bash
git fetch upstream
git switch -c sync/upstream-YYYYMMDD origin/main
git merge upstream/main
```

浅克隆缺少合并基线时逐步 fetch deepen。发生冲突时重点核对 `main.py` 的归档调用顺序、
`core/download.py` 的下载租约及临时文件发布、`core/utils.py` 的完整合并输出、`core/clean.py` 的清理协调，以及配置和 metadata，保留 fork 仓库地址。
不自动硬重置上游，不把新版本直接覆盖到正在运行的插件。

验证命令：

```bash
python -m pytest -q tests
# 在具备 AstrBot 依赖的隔离候选目录中执行，不会向真实 QQ 发送消息：
python tests/runtime_archive_check.py
python tests/runtime_download_check.py
python tests/runtime_merge_check.py
```

通过后再合并 fork 主分支、备份 NAS 插件目录及配置并部署。回滚时恢复整套插件目录和配置；
归档目录保留，不能随代码回滚删除。升级后再验证一次真实卡片归档和重复请求。
