# Douyin Local Private

内部自用的本地内容同步工具：把抖音、微博、X、小宇宙、公众号和小红书内容整理成 Obsidian Markdown，并支持周报。

稳定入口是本地网页 Dashboard。

## 当前能力

- 内容源库：维护抖音、微博、X、小宇宙、公众号等来源。
- URL 补全：用本地脚本自动读取昵称、简介、分类、标签和平台内部 ID，不调用大模型。
- 抓取入库：抖音视频转录、微博正文、X 推文、小宇宙播客转录、公众号公开文章解析。
- AI 笔记：普通摘要默认使用 `glm-5-turbo`，长播客默认使用 `glm-5.2`，也可只保存原文或逐字稿。
- 运行记录：查看嗅探数量、成功、等待、失败原因，并支持失败项重爬。
- 候选抓取：先嗅探候选内容，再手动勾选需要抓取的条目。
- 自动任务：每天 00:00-11:00 同步所有可抓取来源，同步完成后生成日报；11:00 仍未完成则自动停止并补发日报；周一生成周报并发送 Hermes/Telegram 精简版。

当前已正式接入抓取的平台：

| 平台 | 内容类型 | 默认输出目录 |
| --- | --- | --- |
| 抖音 | 视频转录和总结 | `Douyin/口播博主` |
| 微博 | 正文和轻量整理 | `Weibo/内容源` |
| X | 推文原文和轻量整理 | `X/内容源` |
| 小宇宙 | 播客音频转录和长内容整理 | `Podcast/小宇宙` |
| 公众号 | 单篇公开文章和 RSS 文章 | `WeChat/公众号` |
| 小红书 | 图文、图片和视频逐字稿 | `Xiaohongshu/内容源` |

YouTube、B站、TikTok、快手、贴吧、知乎在前端保留为内容源档案，抓取适配器后续再接入。

## 启动

首次安装：

```bash
git clone https://github.com/steven-95271/douyin-local-private.git
cd douyin-local-private
bash local_tools/setup_obsidian_sync.sh
```

启动 Dashboard：

```bash
bash local_tools/start_obsidian_dashboard.sh
```

打开：

```text
http://127.0.0.1:8787
```

`127.0.0.1` 和 `localhost` 都表示本机。这里固定使用 `127.0.0.1`，是为了和 Chrome 插件、本地服务权限保持一致。

## Chrome 插件

插件目录：

```text
chrome_extension
```

安装步骤：

1. 打开 Chrome：`chrome://extensions/`
2. 开启「开发者模式」。
3. 点击「加载已解压的扩展程序」。
4. 选择本仓库下的 `chrome_extension` 目录。
5. 登录 `https://weibo.com` 或 `https://mp.weixin.qq.com` 公众号后台。
6. 点击插件图标，把微博或公众号后台 Cookie 同步到本地 Dashboard。
7. 小红书博主建议打开博主主页后，点击插件里的「导入当前小红书博主」。

Cookie 默认保存到：

```text
local_tools/douyin_cookie.txt
local_tools/weibo_cookie.txt
local_tools/wechat_mp_cookie.txt
local_tools/wechat_mp_token.txt
local_tools/xiaohongshu_cookie.txt
```

这些文件只保存在本地，不提交到 GitHub。

抖音和小红书建议使用 Dashboard「账号与模型」里的「扫码登录」。系统会打开真实 Chrome 窗口并使用本地浏览器 profile 保存登录态；登录成功后会自动同步当前会话给抓取流程。首次使用前如果提示 Playwright 缺失，运行：

```bash
.venv/bin/python -m pip install -r requirements-obsidian.txt
```

## 小红书增强解析

小红书详情和媒体解析优先调用本地 XHS-Downloader API：

```bash
zsh local_tools/start_xhs_downloader_api.sh
```

启动后保持这个终端窗口打开，服务地址是：

```text
http://127.0.0.1:5556
```

Dashboard 仍然负责内容源、任务、Markdown、逐字稿和 Obsidian 入库。XHS-Downloader 只作为本地详情/媒体解析 sidecar 使用；如果它没启动，系统会回退到内置解析。

## Dashboard 用法

- 在「内容源库」添加主页 URL，然后点击「URL 补全」。X 可填 `https://x.com/username` 或 `@username`；公众号来源可以直接填公众号名称；需要先登录 `mp.weixin.qq.com` 后台并用插件导入公众号后台 Cookie。
- 在「抓取中心」选择内容源。
- 「全量嗅探候选内容」用于先预览全部候选列表，不下载、不转录、不写入。
- 嗅探完成后，可在运行记录里勾选几条，再点「抓取选中内容」。
- 「全量抓取该来源」会深度回溯历史内容；已成功处理过的内容会自动跳过。
- 「生成 AI 总结」默认开启；取消后只保存原文或逐字稿。
- 「素材保留」可决定是否额外保留视频、音频、逐字稿 TXT、平台原始 JSON，以及 Markdown 内是否保留原文/逐字稿。
- 失败项可以在运行记录中一键重爬。

## 笔记结构

抖音笔记偏重型，包含摘要、逻辑树、结构导图、分段解析、核心观点、可复用表达、行动项、关键词和逐字稿。

微博笔记更克制，默认保留原文、要点、必要脉络、关键词、我的标注和相关链接，避免把短内容过度蒸馏。

普通文章、视频摘要默认使用更快的 `glm-5-turbo`；小宇宙播客会先分块整理长逐字稿，再生成最终笔记，默认使用 `glm-5.2`。两者都走 GLM Coding Plan 的 `https://open.bigmodel.cn/api/coding/paas/v4`；如需切回 DeepSeek 或其他 OpenAI 兼容模型，可改 `summary` 和 `podcast_summary` 配置。

公众号支持三种来源：公众号名称、单篇公开文章 URL、RSS URL。按名称抓取时会使用已登录的公众号后台搜索接口拿到 `fakeid`，再分页同步历史文章列表；文章正文仍通过公开文章链接解析。

单篇笔记文件名：

```text
短标题-博主名-日期.md
```

视频 ID 不放在文件名里，会保存在 Markdown frontmatter、状态库和运行记录中。

## 素材保留

默认只保留 Markdown，处理用的视频和音频会在任务结束后删除，避免占用太多本地空间。

在 Dashboard 的「输出设置」里可以改成额外保留：

- 视频文件：保存为同名 `.video.mp4`
- 音频文件：保存为同名 `.audio.wav`；小宇宙还会保存源音频
- 逐字稿 TXT：保存为同名 `.transcript.txt`
- 平台原始数据：保存为同名 `.source.json`
- Markdown 原文/逐字稿：默认开启，可关闭

## 自动任务

安装或更新 macOS 定时任务：

```bash
bash local_tools/install_weekly_launchd.sh
```

当前定时逻辑：

- 每天 00:00：同步所有可抓取且启用的来源，只处理最近 3 天发布的内容，每个来源最多 20 条，自动跳过已处理内容；同步完成后生成日报并发送 Telegram 精简版。
- 每天 11:00：如果同步还在运行，自动停止抓取并补发一版日报，避免白天占用内存。
- 每天 11:10：执行每日自检，检查当天同步是否启动、来源是否漏扫、失败项是否可自动重试；网络中断/超时/AI 总结失败会自动补跑一次，登录态失效会通过 Telegram 提醒你处理。
- 周一、周三、周五 18:00：发送公众号后台登录态刷新提醒。打开 `mp.weixin.qq.com` 确认登录后，用插件同步公众号后台 Cookie/token。
- 周一 11:00：生成周报，并通过 Hermes `secretary` profile 发送 Telegram 精简版。

自动任务不依赖 Dashboard 页面，也不依赖 `8787` 端口。电脑需要处于开机、联网状态。不需要运行 Docker。

定时任务使用 `launchd` 的 `/bin/bash -lc` inline 命令启动，避免 macOS 拦截 `Documents` 目录里的脚本文件执行。

手动同步所有可抓取且启用的来源：

```bash
bash local_tools/run_weekly_content_sync.sh
```

手动生成周报，不发送 Hermes：

```bash
.venv/bin/python local_tools/obsidian_sync/weekly_brief.py --no-hermes
```

手动生成日报，不发送 Hermes：

```bash
.venv/bin/python local_tools/obsidian_sync/weekly_brief.py --period daily --no-hermes
```

手动执行自检，不发送 Hermes：

```bash
.venv/bin/python local_tools/obsidian_sync/daily_health_check.py --no-hermes
```

只检查不自动重试：

```bash
.venv/bin/python local_tools/obsidian_sync/daily_health_check.py --no-hermes --no-auto-retry
```

手动发送公众号登录态刷新提醒：

```bash
bash local_tools/run_wechat_login_reminder.sh
```

## 日志

Dashboard 日志：

```text
local_tools/obsidian_sync/work/logs/dashboard_sync.log
```

自动同步日志：

```text
~/Library/Logs/douyin-local-private/content_sync.log
```

周报日志：

```text
~/Library/Logs/douyin-local-private/weekly_brief.log
```

日报日志：

```text
~/Library/Logs/douyin-local-private/daily_brief.log
```

每日自检日志：

```text
~/Library/Logs/douyin-local-private/daily_health_check.log
~/Library/Logs/douyin-local-private/health_retry.log
```

公众号登录态提醒日志：

```text
~/Library/Logs/douyin-local-private/wechat_login_reminder.log
```

launchd 启动日志：

```text
~/Library/Logs/douyin-local-private/content_sync_launchd_daily.err.log
~/Library/Logs/douyin-local-private/content_sync_window.log
~/Library/Logs/douyin-local-private/daily_health_check.err.log
~/Library/Logs/douyin-local-private/wechat_login_reminder_monday.err.log
~/Library/Logs/douyin-local-private/wechat_login_reminder_wednesday.err.log
~/Library/Logs/douyin-local-private/wechat_login_reminder_friday.err.log
~/Library/Logs/douyin-local-private/weekly_brief_launchd.err.log
```

本地状态库：

```text
local_tools/obsidian_sync/state.sqlite
```

## 本地私有文件

这些文件不应该提交：

- `local_tools/obsidian_sync/.env`
- `local_tools/douyin_cookie.txt`
- `local_tools/douyin_cookie_*.txt`
- `local_tools/weibo_cookie.txt`
- `local_tools/obsidian_sync/state.sqlite`
- `local_tools/obsidian_sync/work/`

`local_tools/obsidian_sync/creators.yaml` 是本地配置文件，目前仓库内保留一份内部配置；不要把 API Key 或 Cookie 写进去。

## 常见问题

Cookie 缺失：确认已经登录对应网站，刷新页面后重新点击插件同步。

抓取慢：主要耗时来自视频下载、本地 Whisper 转录和大模型总结。默认每个抖音来源最多 2 条视频并发处理，来源之间串行。

临时视频占空间：处理时会短暂落盘，完成后会删除。最终长期保存的是 Markdown 文件。

周报没发 Telegram：优先看 `~/Library/Logs/douyin-local-private/weekly_brief.log` 和 `~/Library/Logs/douyin-local-private/weekly_brief_launchd.err.log`。当前 Hermes 发送使用 `secretary` profile，并通过 `@Steven_Secretary_bot` 发送，设置了 120 秒超时。

自检显示需要处理：优先看 Dashboard 的「自检」面板。网络中断、请求超时、AI 总结失败会自动重试；如果提示微博/公众号/小红书/抖音登录态失效，需要重新登录并用插件或扫码方式更新登录态，然后在运行记录里重爬失败项。
