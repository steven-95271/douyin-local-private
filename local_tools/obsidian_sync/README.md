# Obsidian Sync

这个工具用于把指定抖音博主的新视频转成 Obsidian Markdown 笔记。

默认流程：

1. 拉取 `creators.yaml` 里启用的博主作品列表。
2. 跳过 `state.sqlite` 里已经处理成功的视频。
3. 临时下载视频。
4. 用 `ffmpeg` 抽音频。
5. 用 `faster-whisper` 本地转录。
6. 用 DeepSeek 生成总结。
7. 写入 Obsidian Vault。
8. 删除临时视频和音频。

## 初始化

安装基础依赖后，再安装转录依赖：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/setup_obsidian_sync.sh
```

确保本机有 `ffmpeg`：

```bash
ffmpeg -version
```

如果没有，可以用 Homebrew 安装：

```bash
brew install ffmpeg
```

## 配置博主

编辑：

```text
local_tools/obsidian_sync/creators.yaml
```

把 `creators` 里的示例改成真实博主：

```yaml
creators:
  - key: someone
    name: 某某博主
    url: https://www.douyin.com/user/...
    enabled: true
    tags:
      - douyin
      - 口播
```

## 运行

推荐使用本地 Dashboard：

```bash
cd /Users/steven/Documents/Codex/2026-06-04/evil0ctal-douyin-tiktok-download-api-https/outputs/douyin-local-private
bash local_tools/start_obsidian_dashboard.sh
```

然后打开：

```text
http://127.0.0.1:8787
```

页面里可以维护博主、保存 Cookie/API Key、启动 dry-run 或正式同步。

新增博主时可以只填「主页 URL」，再点「URL 补全」。工具会尝试读取抖音主页标题，自动生成：

- `key`：本地唯一 ID，基于主页 URL 稳定生成。
- `name`：用于 Obsidian 文件夹和笔记 frontmatter 的博主名称。

Cookie 可以手动粘贴，也可以通过 Chrome 插件导入。插件导入方式：

1. Chrome 登录 `https://www.douyin.com`。
2. 打开 `chrome://extensions/`，重新加载本项目的插件。
3. 点击插件图标。
4. 点击「导入抖音 Cookie」。

DeepSeek API Key 保存过一次后会一直沿用；后续留空保存不会覆盖旧 Key。

命令行也可以直接使用：

先做 dry-run，只看会发现哪些视频：

```bash
.venv/bin/python local_tools/obsidian_sync/sync.py --dry-run --limit 5
```

正式处理前 3 条新视频：

```bash
.venv/bin/python local_tools/obsidian_sync/sync.py --limit 3
```

只转录不调用 DeepSeek 总结：

```bash
.venv/bin/python local_tools/obsidian_sync/sync.py --limit 3 --skip-summary
```

输出目录默认是：

```text
/Users/steven/Documents/Obsidian/MyVault/Douyin/口播博主
```

## 定时同步

先手动跑通几次，再配置 `launchd`。第一版先保留手动运行，避免 Cookie 失效、转录模型下载、API 额度等问题混在后台排查。
