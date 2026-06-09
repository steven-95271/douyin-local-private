# Douyin -> Obsidian Sync

内部自用工具：把已配置抖音口播博主的视频自动转成 Obsidian Markdown 笔记。

## 能做什么

- 维护一个「博主库」。
- 自动抓取启用博主的新视频。
- 跳过已经成功处理过的视频。
- 临时下载视频，抽音频，本地 Whisper 转录。
- 调用 DeepSeek 生成结构化笔记。
- 写入 Obsidian Vault。
- 删除临时视频和音频，只保留 Markdown。
- 每周定时同步内容，并生成周报发送到 Hermes/Telegram。

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

- 添加、补全、启用、停用博主。
- 导入 Cookie 和保存 DeepSeek API Key。
- 启动单个博主抓取。
- 串行跑所有启用博主。
- 查看最近一次任务进度和每条视频状态。
- 打开输出目录和日志。

## 输出位置

视频笔记：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/口播博主
```

周报：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/周报
```

单篇笔记文件名：

```text
视频名-博主名-日期-视频ID.md
```

## 笔记结构

新生成或重新处理的笔记会包含：

- 一句话总结
- 摘要
- 逻辑树
- Mermaid 思维导图
- 分段解析
- 核心观点
- 关键概念
- 可复用表达
- 行动项
- 关键词
- 逐字稿

已经成功生成过的旧笔记默认不会重复处理。需要升级旧笔记格式时，在 Dashboard 勾选「重新处理」后再跑。

## Cookie

默认 Cookie 文件：

```text
local_tools/douyin_cookie.txt
```

Chrome 插件目录：

```text
/Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private/chrome_extension
```

导入步骤：

1. 浏览器登录 `https://www.douyin.com`。
2. 打开 `chrome://extensions/`。
3. 加载或重新加载上面的插件目录。
4. 点击插件图标，导入抖音 Cookie。

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

## 博主库

新增博主时，只需要填主页 URL，然后点「URL 补全」。

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

- 周日 22:00：同步所有启用博主的新视频。
- 周一 11:00：生成周报，并通过 Hermes 发送 Telegram 精简版。

自动任务不依赖 Dashboard 页面，也不依赖 `8787` 端口。电脑需要处于开机且网络可用状态。

手动安装或更新定时任务：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/install_weekly_launchd.sh
```

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

## 注意

- `creators.yaml` 是本地配置，不要提交到 GitHub。
- `.env`、Cookie、API Key 都只保存在本地。
- Dashboard 默认处理所有可扫描的新视频，已成功处理过的视频会自动跳过。
- 默认每个博主最多 2 条视频并发处理，博主之间仍然串行。
- 报错视频会记录在运行记录里，重新跑时会再次尝试。
