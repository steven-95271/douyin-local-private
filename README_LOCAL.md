# 本地私用封装

这个目录是在原项目基础上加的本地私用包装。用途是：你自己在本机输入 URL 清单，低并发批量解析并下载到本地目录。

请只用于你拥有或已获得授权下载的内容。这个封装不包含代理池、绕风控、验证码处理、账号规避或反检测逻辑。

## 1. 初始化

建议使用 Python 3.10+：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你只想启动原项目 API：

```bash
python start.py
```

本地版已经把 `config.yaml` 里的 `API.Host_Port` 改成 `8000`，启动后访问 `http://127.0.0.1:8000/docs`。

也可以用脚本初始化：

```bash
bash local_tools/setup_local.sh
source .venv/bin/activate
```

## 2. 准备 Cookie 和 URL

复制样例文件：

```bash
cp local_tools/urls.example.txt local_tools/urls.txt
cp local_tools/douyin_cookie.example.txt local_tools/douyin_cookie.txt
```

把你浏览器里已登录抖音网页版后的 Cookie 放到：

```text
local_tools/douyin_cookie.txt
```

把要处理的链接一行一个放到：

```text
local_tools/urls.txt
```

脚本只在运行时读取 Cookie，不会写回 `crawlers/douyin/web/config.yaml`。

## 3. 先小样本测试

先解析前 3 条，不下载：

```bash
python local_tools/batch_download.py --input local_tools/urls.txt --douyin-cookie-file local_tools/douyin_cookie.txt --limit 3 --dry-run
```

下载前 3 条，默认保留水印：

```bash
python local_tools/batch_download.py --input local_tools/urls.txt --douyin-cookie-file local_tools/douyin_cookie.txt --limit 3
```

下载目录默认是：

```text
download/private_batch
```

结果清单默认是：

```text
download/private_batch/manifest.jsonl
```

## 4. 批量运行建议

默认参数比较保守：`--concurrency 1 --delay 2 --retries 2`。1000+ 条建议先维持这个设置，跑通后再考虑：

```bash
python local_tools/batch_download.py --input local_tools/urls.txt --douyin-cookie-file local_tools/douyin_cookie.txt --concurrency 2 --delay 3
```

如果要处理图集：

```bash
python local_tools/batch_download.py --input local_tools/urls.txt --douyin-cookie-file local_tools/douyin_cookie.txt --include-images
```

如果是你拥有或已获授权的内容，需要无水印版本：

```bash
python local_tools/batch_download.py --input local_tools/urls.txt --douyin-cookie-file local_tools/douyin_cookie.txt --watermark remove
```

## 5. 常见问题

- 大量失败或 401/429：Cookie 失效、账号触发风控或请求太快。降低并发和频率，重新登录网页版后更新 Cookie。
- 只有部分能下载：作品权限、可见性、地区、视频状态和播放流有效期都会影响结果。
- 重跑会重复吗：默认会读取 `manifest.jsonl` 跳过已经成功的 URL，也会跳过已存在的目标文件。
- 磁盘空间：1000+ 视频可能占几十 GB 到上百 GB，先确认剩余空间。

## 6. Chrome 插件模式

本地版还包含一个 Chrome 插件：

```text
chrome_extension/
```

它只负责从当前抖音页面收集已经加载到页面里的作品链接，不读取、不导出你的浏览器 Cookie。登录抖音账号的作用是让你能在浏览器里看到对应页面和链接；本地下载服务是否能解析下载，仍可能需要你准备 `local_tools/douyin_cookie.txt`。

启动本地接收服务：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
source .venv/bin/activate
python local_tools/extension_server.py --douyin-cookie-file local_tools/douyin_cookie.txt
```

更简单的方式：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/start_extension_server.sh
```

服务默认只监听：

```text
http://127.0.0.1:8765
```

安装插件：

1. 打开 Chrome 的 `chrome://extensions/`。
2. 打开右上角「开发者模式」。
3. 点击「加载已解压的扩展程序」。
4. 选择这个目录：`/Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private/chrome_extension`。

使用方式：

1. 在 Chrome 登录抖音网页版。
2. 打开用户主页、收藏页、搜索结果页或作品详情页。
3. 先手动滚动页面，让更多作品卡片加载出来。
4. 点击插件图标，点「收集已加载链接」。
5. 需要插件慢速滚动时，设置「滚动轮数」后点「慢速滚动收集」。
6. 点「发送到本地」写入本地队列。
7. 勾选「发送后启动下载」时，会直接启动本地批量下载任务。

插件发送的队列文件是：

```text
local_tools/extension_queue.txt
```

本地服务启动的下载日志在：

```text
download/private_batch/extension_worker.log
download/private_batch/extension_worker.err.log
```

你也可以不用启动下载，只用插件「导出 TXT」，然后手动把导出的文件作为 `batch_download.py --input` 的输入。

## 7. Obsidian 口播博主同步

本地版新增了 Obsidian 同步工具：

```text
local_tools/obsidian_sync/
```

用途：

- 按博主拉取新视频。
- 临时下载视频并抽音频。
- 用本地 Whisper 转文字。
- 用 DeepSeek 总结。
- 生成 Markdown 到 Obsidian。
- 删除临时视频和音频，只保留文字稿和总结。

先安装转录依赖：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/setup_obsidian_sync.sh
```

启动本地管理页面：

```bash
bash local_tools/start_obsidian_dashboard.sh
```

然后访问：

```text
http://127.0.0.1:8787
```

以后新增博主、更新 Cookie、启动 dry-run/同步都可以在这个页面完成。

编辑博主配置：

```text
local_tools/obsidian_sync/creators.yaml
```

默认 Obsidian 输出位置是：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/口播博主
```

先 dry-run：

```bash
.venv/bin/python local_tools/obsidian_sync/sync.py --dry-run --limit 5
```

正式处理前 3 条：

```bash
.venv/bin/python local_tools/obsidian_sync/sync.py --limit 3
```

详细说明见：

```text
local_tools/obsidian_sync/README.md
```
