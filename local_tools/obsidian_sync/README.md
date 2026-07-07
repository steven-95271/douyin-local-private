# Douyin -> Obsidian Sync

内部自用工具：把已配置抖音口播博主的视频、微博博主的文字内容、X 推文、小宇宙播客单集、公众号文章和小红书内容自动转成 Obsidian Markdown 笔记。

当前可抓取平台是抖音、微博、X、小宇宙、公众号和小红书；YouTube、B站、TikTok、快手、贴吧、知乎已在 Dashboard 里预留为内容源档案，抓取适配器后续接入。

## 能做什么

- 维护一个「内容源库」。
- 自动抓取启用抖音来源的新视频、微博来源的新内容、X 推文、小宇宙公开播客单集、公众号公开文章。
- 跳过已经成功处理过的内容。
- 抖音会临时下载视频、抽音频、本地 Whisper 转录；微博和 X 直接读取正文；小宇宙会临时下载公开音频并本地转录。
- 调用配置中的 AI 模型生成结构化笔记，普通摘要默认是 `glm-5-turbo`，长播客默认是 `glm-5.2`。
- 写入 Obsidian Vault。
- 删除临时视频和音频，只保留 Markdown。
- 每天定时同步内容，同步完成后生成日报；每周生成周报发送到 Hermes/Telegram。

## 常用入口

启动 Dashboard：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/start_obsidian_dashboard.sh
```

打开：

```text
http://127.0.0.1:8787
```

Dashboard 用来做这些事：

- 添加、补全、启用、停用内容源。URL 补全使用本地脚本和平台页面/RSS 数据，不调用大模型。
- 抖音和小红书建议使用扫码登录；微博/公众号后台仍通过 Cookie 或 token 接入。
- X 可填 `https://x.com/username` 或 `@username`；抓取通过 OpenCLI/Agent-Reach 复用本机 X 登录态。
- 公众号可直接填名称补全；需要先登录 `mp.weixin.qq.com` 公众号后台，并用 Chrome 插件导入公众号后台 Cookie。
- 启动单个可抓取来源。
- 「全量嗅探候选内容」用于先预览全部候选列表，不下载、不转录、不写入；嗅探完成后可在运行记录里勾选几条，再点「抓取选中内容」。
- 「全量抓取该来源」会直接深度回溯并处理历史内容。微博全量默认最多扫描约 350 页，可用 `fetch.weibo_full_max_pages` 调整。小宇宙如果识别到 RSS，会用 RSS 回溯更多历史；公众号可通过后台 `fakeid` 或 RSS 回溯历史。
- 「生成 AI 总结」默认开启。取消勾选后，本轮只保存原文或逐字稿，不调用模型 API。
- 「素材保留」可控制是否额外保留视频、音频、逐字稿 TXT、平台原始 JSON，以及 Markdown 内是否保留原文/逐字稿。
- 查看最近一次任务进度、失败/等待/进行中的视频状态。
- 对最近一次任务的失败视频一键重爬；成功和跳过的视频默认折叠隐藏，不进入重爬队列。
- 打开输出目录。

## 输出位置

Dashboard 的「输出设置」可以按平台配置子目录。默认是：

| 平台 | 默认子目录 |
| --- | --- |
| 抖音 | `Douyin/口播博主` |
| 微博 | `Weibo/内容源` |
| X | `X/内容源` |
| 小宇宙 | `Podcast/小宇宙` |
| 公众号 | `WeChat/公众号` |
| 小红书 | `Xiaohongshu/内容源` |
| YouTube | `YouTube/视频博主` |
| B站 | `Bilibili/视频博主` |
| TikTok | `TikTok/视频博主` |
| 快手 | `Kuaishou/视频博主` |
| 贴吧 | `Tieba/贴吧` |
| 知乎 | `Zhihu/内容源` |

目前抖音视频抓取、微博文本抓取、X 推文抓取、小宇宙公开播客抓取、公众号公开文章抓取和小红书内容抓取已接入；YouTube、B站、TikTok、快手、贴吧、知乎下一阶段接入。

周报：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/周报
```

单篇笔记文件名：

```text
短标题-博主名-日期.md
```

视频 ID 不放在文件名里，会保存在 frontmatter、状态库和运行记录中。若同一天同博主出现同名短标题，系统会自动追加 `-2`、`-3` 防止覆盖。

素材保留默认较克制：只保留 Markdown，临时视频和音频会删除。需要溯源时，可在 Dashboard 的「输出设置」里勾选额外保留视频、音频、逐字稿 TXT 或平台原始 JSON。额外文件会保存在 Markdown 同目录旁边。

## 笔记结构

抖音视频笔记偏重型，新生成或重新处理的笔记会包含：

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

微博笔记偏轻量，默认包含：

- 原文
- 要点
- 必要时才出现的脉络
- 关键词
- 我的标注
- 相关链接

微博不会默认生成逐字稿、逻辑树、结构导图、分段解析和行动项，避免把短内容过度蒸馏。
微博轻量总结的约束在 `local_tools/obsidian_sync/prompts/weibo_summarize.md`。

小宇宙播客笔记偏长内容整理，默认包含 Shownotes、速览、内容地图、分段笔记、核心观点、关键概念、可复用表达、行动项、关键词和逐字稿。
普通文章、视频摘要默认使用更快的 `glm-5-turbo`；长内容会先分块整理，再生成最终笔记，默认使用 `glm-5.2`。两者都走 GLM Coding Plan 的 `https://open.bigmodel.cn/api/coding/paas/v4`；如需切回 DeepSeek 或其他 OpenAI 兼容模型，可改 `summary` 和 `podcast_summary` 配置。
小宇宙公开节目页通常只暴露首屏单集；如果要回溯完整历史，建议在内容源里补充 `RSS URL`。

公众号笔记偏文章整理，默认保留导语、AI 摘要、原文、我的标注和相关链接。单篇 `mp.weixin.qq.com` 文章 URL 可直接抓取；填公众号名称并补全后，会通过公众号后台 `searchbiz`/`appmsgpublish` 接口同步历史文章；RSS URL 仍然可用。

公众号单篇公开文章使用内置解析器直接抽取正文，不依赖外部 Docker 服务。历史列表同步仍建议通过 RSS URL 接入。

已经成功生成过的旧笔记默认不会重复处理。需要升级旧笔记格式时，在 Dashboard 勾选「重新处理」后再跑。

## 登录态

抖音和小红书建议在 Dashboard「账号与模型」里使用「扫码登录」。系统会打开真实 Chrome 窗口，使用本地 browser profile 保存登录态，并在登录成功后把当前会话同步给抓取流程。

首次使用如果提示 Playwright 缺失，运行：

```bash
.venv/bin/python -m pip install -r requirements-obsidian.txt
```

## Cookie

抖音扫码登录成功后会同步到：

```text
local_tools/douyin_cookie.txt
```

小红书扫码登录成功后会同步到：

```text
local_tools/xiaohongshu_cookie.txt
```

微博 Cookie 文件：

```text
local_tools/weibo_cookie.txt
```

公众号后台 Cookie/token 文件：

```text
local_tools/wechat_mp_cookie.txt
local_tools/wechat_mp_token.txt
```

Chrome 插件目录：

```text
/Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private/chrome_extension
```

导入步骤：

1. 浏览器登录 `https://www.douyin.com`、`https://weibo.com`，或 `https://mp.weixin.qq.com` 公众号后台。
2. 打开 `chrome://extensions/`。
3. 加载或重新加载上面的插件目录。
4. 点击插件图标，导入抖音、微博或公众号后台 Cookie。

多账号可以配置多个 Cookie Profile：

```yaml
douyin_cookie_profiles:
  default: local_tools/douyin_cookie.txt
  spare: local_tools/douyin_cookie_spare.txt

creators:
  - name: 某某博主
    url: https://www.douyin.com/user/...
    enabled: true
    cookie_profile: default
```

不配置 `cookie_profile` 时使用默认 Cookie。建议按博主绑定账号，不要高频轮换。

## 内容源库

新增抖音、微博、小宇宙或公众号来源时，通常填主页 URL 后点「URL 补全」。公众号也可以直接填公众号名称；名称搜索需要先导入公众号后台 Cookie/token。微博需要先导入微博 Cookie，小宇宙和公众号公开文章/RSS 链接不需要 Cookie。

系统会自动尝试读取：

- 昵称
- 简介
- 最近视频标题
- 分类
- 语言
- 内容类型
- 标签
- 后台内部 key

`key` 是程序内部 ID，前端默认不展示。

## 自动任务

已配置两个 macOS `launchd` 任务：

- 每天 00:00：同步所有可抓取且启用的来源，只处理最近 3 天发布的内容，每个来源最多 20 条，自动跳过已处理内容；同步完成后生成日报并发送 Telegram 精简版。
- 每天 11:00：如果同步还在运行，自动停止抓取并补发一版日报，避免白天占用内存。
- 周一 11:00：生成周报，并通过 Hermes 发送 Telegram 精简版。

自动任务不依赖 Dashboard 页面，也不依赖 `8787` 端口。电脑需要处于开机且网络可用状态。不需要运行 Docker。

手动安装或更新定时任务：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/install_weekly_launchd.sh
```

手动跑一次所有可抓取且启用的来源：

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

## 日志和状态

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

本地状态库：

```text
local_tools/obsidian_sync/state.sqlite
```

## 注意

- `creators.yaml` 是内容源和任务配置文件；不要把 API Key、Cookie 或密码写进去。
- `.env`、Cookie、API Key 都只保存在本地。
- Dashboard 默认处理所有可扫描的新视频，已成功处理过的视频会自动跳过。
- 默认每个抖音来源最多 2 条视频并发处理，来源之间仍然串行。
- 报错视频会记录在运行记录里，重新跑时会再次尝试。
