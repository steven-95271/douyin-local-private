# Douyin Local Private

内部自用工具：把抖音口播博主的视频自动转成 Obsidian Markdown 笔记，并按周生成简报。

基于 `Evil0ctal/Douyin_TikTok_Download_API` 做了本地封装，当前主流程面向 Douyin。请仅用于自己有权访问和整理的内容。

## 功能

- 维护本地「博主库」。
- 通过 Chrome 插件导入抖音 Cookie。
- 自动补全博主昵称、简介、分类、标签和内部 ID。
- 抓取启用博主的新视频，已成功处理过的视频会跳过。
- 临时下载视频，抽音频，本地 Whisper 转录。
- 调用 DeepSeek 生成结构化 Markdown 笔记。
- 写入本地 Obsidian Vault。
- 处理完成后删除临时视频和音频，只保留文字稿和总结。
- 支持周日自动同步、周一自动生成周报并发送到 Hermes/Telegram。

## 快速启动

```bash
git clone https://github.com/steven-95271/douyin-local-private.git
cd douyin-local-private

bash local_tools/setup_obsidian_sync.sh
bash local_tools/start_obsidian_dashboard.sh
```

打开 Dashboard：

```text
http://127.0.0.1:8787
```

`127.0.0.1` 和 `localhost` 都表示本机；这里固定写 `127.0.0.1` 是为了和插件权限、浏览器本地服务保持一致。

## Chrome 插件

插件目录：

```text
chrome_extension
```

安装方式：

1. 打开 Chrome：`chrome://extensions/`
2. 开启「开发者模式」。
3. 点击「加载已解压的扩展程序」。
4. 选择本仓库下的 `chrome_extension` 目录。
5. 登录 `https://www.douyin.com`。
6. 点击插件图标，将 Cookie 同步到本地 Dashboard。

Cookie 默认保存到：

```text
local_tools/douyin_cookie.txt
```

## Dashboard 用法

Dashboard 主要做这些事：

- 添加博主主页 URL。
- 点击「URL 补全」，自动读取昵称、简介、分类和标签。
- 启用或停用博主。
- 启动单个博主抓取。
- 一键串行跑所有启用博主。
- 查看最近一次任务进度、每条视频状态、失败原因和输出文件。
- 打开输出目录和日志目录。

DeepSeek API Key 保存一次即可，后续会复用，存放在本地 `.env` 文件里，不提交到 GitHub。

## 输出

视频笔记默认输出到：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/口播博主
```

周报默认输出到：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/周报
```

单篇笔记文件名：

```text
短标题-博主名-日期.md
```

示例：

```text
第15集-解读段永平本分理念-波咕思考笔记-2026-04-27.md
```

视频 ID 不放在文件名里，会保存在 Markdown frontmatter、状态库和运行记录中。若同一天同博主出现同名短标题，系统会自动追加 `-2`、`-3` 防止覆盖。

## 笔记结构

新生成或重新处理的笔记包含：

- 一句话总结
- 摘要
- 逻辑树
- 结构导图
- 分段解析
- 核心观点
- 关键概念
- 可复用表达
- 行动项
- 关键词
- 逐字稿

已经成功生成过的旧笔记默认不会重复处理。需要升级旧笔记格式时，在 Dashboard 勾选「重新处理」后再跑。

## 自动任务

安装或更新 macOS 定时任务：

```bash
bash local_tools/install_weekly_launchd.sh
```

当前定时逻辑：

- 周日 22:00：同步所有启用博主的新视频。
- 周一 11:00：生成周报，并通过 Hermes 发送 Telegram 精简版。

自动任务不依赖 Dashboard 页面，也不依赖 `8787` 端口。电脑需要处于开机、联网状态。

手动跑一次所有启用博主：

```bash
bash local_tools/run_weekly_content_sync.sh
```

手动生成周报，不发送 Hermes：

```bash
.venv/bin/python local_tools/obsidian_sync/weekly_brief.py --no-hermes
```

## 日志和状态

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

本地状态库：

```text
local_tools/obsidian_sync/state.sqlite
```

## 本地私有文件

这些文件只保存在本机，不应该提交：

- `local_tools/obsidian_sync/creators.yaml`
- `local_tools/obsidian_sync/.env`
- `local_tools/douyin_cookie.txt`
- `local_tools/douyin_cookie_*.txt`
- `local_tools/obsidian_sync/state.sqlite`
- `local_tools/obsidian_sync/work/`

## 常见问题

Cookie 缺失：确认已经登录抖音，刷新抖音页面后重新点击插件同步。

抓取慢：主要耗时来自视频下载、本地 Whisper 转录和大模型总结。默认每个博主最多 2 条视频并发处理，博主之间串行。

临时视频占空间：处理时会短暂落盘，完成后会删除。最终长期保存的是 Markdown 文件。

Dashboard 关闭后任务是否继续：已经启动的后台任务会继续跑；周日、周一的自动任务由 macOS `launchd` 触发，不依赖网页是否打开。
