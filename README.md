# Douyin Local Private

内部自用的本地内容同步工具：把抖音、微博、小宇宙和公众号内容整理成 Obsidian Markdown，并支持周报和二创草稿。

稳定入口是本地网页 Dashboard。macOS App 壳暂不作为推荐入口。

## 当前能力

- 内容源库：维护抖音、微博、小宇宙、公众号等来源。
- URL 补全：自动读取昵称、简介、分类、标签和平台内部 ID。
- 抓取入库：抖音视频转录、微博正文、小宇宙播客转录、公众号公开文章解析。
- AI 笔记：可选择生成 DeepSeek 总结，也可只保存原文或逐字稿。
- 运行记录：查看嗅探数量、成功、等待、失败原因，并支持失败项重爬。
- 候选抓取：先嗅探候选内容，再手动勾选需要抓取的条目。
- 二创工作台：从本地 Obsidian 笔记生成小红书、抖音对谈、Twitter/X、公众号草稿。
- 自动任务：周日同步所有可抓取来源，周一生成周报并发送 Hermes/Telegram 精简版。

当前已正式接入抓取的平台：

| 平台 | 内容类型 | 默认输出目录 |
| --- | --- | --- |
| 抖音 | 视频转录和总结 | `Douyin/口播博主` |
| 微博 | 正文和轻量整理 | `Weibo/内容源` |
| 小宇宙 | 播客音频转录和长内容整理 | `Podcast/小宇宙` |
| 公众号 | 单篇公开文章和 RSS 文章 | `WeChat/公众号` |

YouTube、B站、TikTok 在前端保留为内容源档案，抓取适配器后续再接入。

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
5. 登录 `https://www.douyin.com` 或 `https://weibo.com`。
6. 点击插件图标，把 Cookie 同步到本地 Dashboard。

Cookie 默认保存到：

```text
local_tools/douyin_cookie.txt
local_tools/weibo_cookie.txt
```

这些文件只保存在本地，不提交到 GitHub。

## Dashboard 用法

- 在「内容源库」添加主页 URL，然后点击「URL 补全」。
- 在「抓取中心」选择内容源。
- 「同步选中来源」用于日常增量。
- 「只嗅探候选内容」用于先预览候选列表，不下载、不转录、不写入。
- 嗅探完成后，可在运行记录里勾选几条，再点「抓取选中内容」。
- 「全量抓取该来源」会深度回溯历史内容；已成功处理过的内容会自动跳过。
- 「生成 AI 总结」默认开启；取消后只保存原文或逐字稿。
- 失败项可以在运行记录中一键重爬。

## 笔记结构

抖音笔记偏重型，包含摘要、逻辑树、结构导图、分段解析、核心观点、可复用表达、行动项、关键词和逐字稿。

微博笔记更克制，默认保留原文、要点、必要脉络、关键词、我的标注和相关链接，避免把短内容过度蒸馏。

小宇宙播客会先分块整理长逐字稿，再生成最终笔记。默认可在 `podcast_summary` 中使用更强模型，例如 `deepseek-v4-pro`。

公众号单篇公开文章使用内置解析器抽取正文，不依赖外部 Docker 服务。公众号历史列表或持续更新建议通过 RSS URL 接入。

单篇笔记文件名：

```text
短标题-博主名-日期.md
```

视频 ID 不放在文件名里，会保存在 Markdown frontmatter、状态库和运行记录中。

## 二创工作台

入口在 Dashboard 的「二创」。

- 可按主题、平台和输出类型筛选本地素材。
- 输出类型支持全套草稿、小红书图文、抖音对谈脚本、Twitter/X 和公众号文章。
- 素材来自 Obsidian 已生成的 Markdown，不重新抓取平台内容。
- 草稿默认写入：

```text
/Users/steven/Documents/Obsidian/MyVault/创作工坊/
```

默认使用 `creative.model`，当前配置为 `deepseek-v4-pro`。

## 自动任务

安装或更新 macOS 定时任务：

```bash
bash local_tools/install_weekly_launchd.sh
```

当前定时逻辑：

- 周日 22:00：同步所有可抓取且启用的来源。
- 周一 11:00：生成周报，并通过 Hermes `telegram2` profile 发送 Telegram 精简版。

自动任务不依赖 Dashboard 页面，也不依赖 `8787` 端口。电脑需要处于开机、联网状态。

定时任务使用 `launchd` 的 `/bin/bash -lc` inline 命令启动，避免 macOS 拦截 `Documents` 目录里的脚本文件执行。

手动同步所有可抓取且启用的来源：

```bash
bash local_tools/run_weekly_content_sync.sh
```

手动生成周报，不发送 Hermes：

```bash
.venv/bin/python local_tools/obsidian_sync/weekly_brief.py --no-hermes
```

## 日志

Dashboard 日志：

```text
local_tools/obsidian_sync/work/logs/dashboard_sync.log
```

周日同步日志：

```text
local_tools/obsidian_sync/work/logs/weekly_content_sync.log
```

周报日志：

```text
local_tools/obsidian_sync/work/logs/weekly_brief.log
```

launchd 启动日志：

```text
local_tools/obsidian_sync/work/logs/weekly_content_launchd.err.log
local_tools/obsidian_sync/work/logs/weekly_brief_launchd.err.log
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

周报没发 Telegram：优先看 `weekly_brief.log` 和 `weekly_brief_launchd.err.log`。当前 Hermes 发送使用 `telegram2` profile，并设置了 120 秒超时。
