from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.request import Request, urlopen


def project_path(project_root: Path, value: str) -> Path:
    path = Path(str(value or "")).expanduser()
    return path if path.is_absolute() else project_root / path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def secret_file_status(path: Path, *, markers: Iterable[str] = (), placeholder_markers: Iterable[str] = ()) -> Dict[str, Any]:
    text = read_text(path)
    exists = path.exists()
    placeholder = any(marker in text for marker in placeholder_markers)
    ready = bool(text) and not placeholder
    if markers:
        ready = ready and any(marker in text for marker in markers)
    return {
        "exists": exists,
        "ready": ready,
        "bytes": len(text.encode("utf-8")) if text else 0,
    }


def command_status(command: list[str], *, timeout: float = 6.0) -> Dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {"ready": False, "exists": False, "message": f"未找到命令：{command[0]}"}
    try:
        result = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ready": False, "exists": True, "path": executable, "message": "命令检查超时"}
    except Exception as exc:  # noqa: BLE001 - health check must never break dashboard.
        return {"ready": False, "exists": True, "path": executable, "message": f"{type(exc).__name__}: {exc}"}
    message = (result.stderr or result.stdout or "").strip().splitlines()
    return {
        "ready": result.returncode == 0,
        "exists": True,
        "path": executable,
        "returncode": result.returncode,
        "message": message[0] if message else "",
    }


def opencli_daemon_status(timeout: float = 2.0) -> Dict[str, Any]:
    request = Request("http://127.0.0.1:19825/status", headers={"X-OpenCLI": "1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(200_000).decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - dashboard status must stay best-effort.
        return {
            "daemon_running": False,
            "bridge_connected": False,
            "message": f"OpenCLI daemon 未连接：{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {"daemon_running": False, "bridge_connected": False, "message": "OpenCLI daemon 返回异常"}
    return {
        "daemon_running": bool(payload.get("ok")),
        "bridge_connected": bool(payload.get("extensionConnected")),
        "daemon_version": payload.get("daemonVersion"),
        "extension_version": payload.get("extensionVersion"),
        "message": "",
    }


def x_backend_status() -> Dict[str, Any]:
    status = command_status(["opencli", "twitter", "tweets", "--help"])
    if status.get("ready"):
        daemon = opencli_daemon_status()
        status.update(daemon)
        if daemon.get("bridge_connected"):
            status["message"] = "OpenCLI Twitter 可用，Browser Bridge 已连接；抓取会复用当前 Chrome/X 登录态"
            return status
        status["ready"] = False
        status["message"] = (
            "OpenCLI 已安装，但 Browser Bridge 扩展未连接。请打开 Chrome，确认 OpenCLI Browser Bridge 扩展已启用；"
            "如果已启用，运行 opencli daemon stop && opencli doctor 后重试。"
        )
        return status
    fallback = command_status(["twitter", "search", "test", "-n", "1"])
    if fallback.get("ready"):
        fallback["message"] = "twitter CLI 可用"
        return fallback
    message = status.get("message") or fallback.get("message") or "未找到 OpenCLI/Twitter CLI"
    return {
        "ready": False,
        "exists": bool(status.get("exists") or fallback.get("exists")),
        "message": f"{message}。X 抓取需要先安装/配置 Agent-Reach 或 OpenCLI。",
    }


def collect_platform_health(config: Dict[str, Any], project_root: Path) -> Dict[str, Dict[str, Any]]:
    placeholders = ("paste_your", "PASTE_YOUR")
    douyin = secret_file_status(
        project_path(project_root, str(config.get("douyin_cookie_file", "local_tools/douyin_cookie.txt"))),
        markers=("sessionid=", "sessionid_ss=", "sid_guard=", "passport_csrf_token="),
        placeholder_markers=placeholders,
    )
    weibo = secret_file_status(
        project_path(project_root, str(config.get("weibo_cookie_file", "local_tools/weibo_cookie.txt"))),
        placeholder_markers=placeholders,
    )
    wechat_cookie = secret_file_status(
        project_path(project_root, str(config.get("wechat_mp_cookie_file", "local_tools/wechat_mp_cookie.txt"))),
        placeholder_markers=placeholders,
    )
    wechat_token = secret_file_status(
        project_path(project_root, str(config.get("wechat_mp_token_file", "local_tools/wechat_mp_token.txt"))),
        placeholder_markers=placeholders,
    )
    wechat = {
        **wechat_cookie,
        "token_ready": bool(wechat_token.get("ready")),
        "ready": bool(wechat_cookie.get("ready") and wechat_token.get("ready")),
    }
    youtube_ready = importlib.util.find_spec("yt_dlp") is not None
    return {
        "douyin": {**douyin, "label": "抖音"},
        "weibo": {**weibo, "label": "微博", "message": "Cookie 文件存在时允许抓取；服务端是否认可会在抓取时确认"},
        "wechat": {**wechat, "label": "公众号"},
        "xiaoyuzhou": {"ready": True, "exists": True, "label": "小宇宙", "message": "公开 RSS/页面无需登录"},
        "youtube": {
            "ready": youtube_ready,
            "exists": youtube_ready,
            "label": "YouTube",
            "message": "公开频道优先读取字幕；缺少 yt-dlp 时请安装项目依赖",
        },
        "x": {"label": "X", **x_backend_status()},
    }
