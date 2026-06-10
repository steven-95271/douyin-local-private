#!/usr/bin/env python3
"""
Local dashboard for managing Douyin -> Obsidian sync.

The server binds to 127.0.0.1 by default and never returns secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "creators.yaml"
KNOWN_PLATFORMS = {"douyin", "weibo", "youtube", "bilibili", "tiktok"}
RUNNABLE_PLATFORMS = {"douyin"}
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

settings: Optional[argparse.Namespace] = None
worker_process: Optional[subprocess.Popen] = None
worker_log: Optional[Path] = None
worker_run_id: Optional[str] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def get_config_path() -> Path:
    assert settings is not None
    return project_path(str(settings.config))


def load_config() -> Dict[str, Any]:
    return read_yaml(get_config_path())


def save_config(data: Dict[str, Any]) -> None:
    write_yaml(get_config_path(), data)


def file_ready(path: Path, placeholder_markers: list[str]) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "ready": False, "bytes": 0}
    text = path.read_text(encoding="utf-8").strip()
    ready = bool(text) and not any(marker in text for marker in placeholder_markers)
    return {"exists": True, "ready": ready, "bytes": len(text.encode("utf-8"))}


def cookie_status(path: Path) -> Dict[str, Any]:
    status = file_ready(path, ["paste_your", "PASTE_YOUR"])
    if not status["ready"]:
        status["login_ready"] = False
        return status
    text = path.read_text(encoding="utf-8").strip()
    has_login = any(
        marker in text
        for marker in ["sessionid=", "sessionid_ss=", "sid_guard=", "passport_csrf_token="]
    )
    status["login_ready"] = has_login
    status["ready"] = has_login
    return status


def env_status(env_path: Path, key: str) -> Dict[str, Any]:
    if not env_path.exists():
        return {"exists": False, "ready": False}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            value = value.strip().strip("'\"")
            return {"exists": True, "ready": bool(value) and not value.startswith("sk-your-")}
    return {"exists": True, "ready": False}


def read_env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def update_env_value(env_path: Path, key: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out = []
    replaced = False
    for raw in lines:
        if raw.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def normalize_key(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:40]


def pinyin_initials(value: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError as exc:
        raise RuntimeError("缺少 pypinyin，请先运行 bash local_tools/setup_obsidian_sync.sh") from exc

    parts = lazy_pinyin(value, style=Style.FIRST_LETTER, errors="ignore")
    key = normalize_key("".join(parts))
    return key or normalize_key(value)


def unique_creator_key(base_key: str, url: str, data: Dict[str, Any]) -> str:
    creators = data.get("creators") or []
    used = {
        str(item.get("key", "")).strip(): str(item.get("url", "")).strip()
        for item in creators
        if isinstance(item, dict)
    }
    if base_key not in used or used[base_key] == url:
        return base_key
    index = 2
    while f"{base_key}_{index}" in used and used[f"{base_key}_{index}"] != url:
        index += 1
    return f"{base_key}_{index}"


def infer_platform_from_url(url: str) -> str:
    text = str(url or "").lower()
    if "weibo.com" in text:
        return "weibo"
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    if "bilibili.com" in text or "b23.tv" in text:
        return "bilibili"
    if "tiktok.com" in text:
        return "tiktok"
    return "douyin"


def normalize_platform(value: Any, url: str = "") -> str:
    raw = str(value or "").strip().lower()
    inferred = infer_platform_from_url(url)
    platform = inferred if inferred != "douyin" and raw in {"", "douyin"} else (raw or inferred)
    return platform if platform in KNOWN_PLATFORMS else "douyin"


def default_tags_for_platform(platform: str) -> list[str]:
    defaults = {
        "douyin": ["douyin", "口播"],
        "weibo": ["weibo", "文字"],
        "youtube": ["youtube", "视频"],
        "bilibili": ["bilibili", "视频"],
        "tiktok": ["tiktok", "视频"],
    }
    return defaults.get(platform, ["内容源"])


def platform_label(platform: str) -> str:
    labels = {
        "douyin": "抖音",
        "weibo": "微博",
        "youtube": "YouTube",
        "bilibili": "B站",
        "tiktok": "TikTok",
    }
    return labels.get(platform, platform)


def clean_creator_name(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    replacements = [
        "的抖音主页",
        "的主页",
        "- 抖音",
        "_抖音",
        " - Douyin",
        " | 抖音",
    ]
    for item in replacements:
        value = value.replace(item, "")
    return value.strip(" -_|")


def extract_meta(html_text: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def extract_author_name_from_item(item: Dict[str, Any]) -> str:
    author = item.get("author") or item.get("author_user_info") or item.get("user") or {}
    if isinstance(author, dict):
        for key in ("nickname", "name", "unique_id", "short_id"):
            value = str(author.get(key, "")).strip()
            if value:
                return clean_creator_name(value)
    return ""


def extract_author_bio_from_item(item: Dict[str, Any]) -> str:
    author = item.get("author") or item.get("author_user_info") or item.get("user") or {}
    if isinstance(author, dict):
        for key in ("signature", "desc", "description", "bio"):
            value = str(author.get(key, "")).strip()
            if value:
                return clean_creator_name(value)[:300]
    return ""


def extract_sec_user_id_from_url(url: str) -> str:
    match = re.search(r"/user/([^/?#]+)", url)
    return match.group(1) if match else ""


def extract_name_from_html(html_text: str) -> str:
    candidates = [
        extract_meta(html_text, "og:title"),
        extract_meta(html_text, "twitter:title"),
    ]
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if title_match:
        candidates.append(title_match.group(1))
    for pattern in (
        r'"nickname"\s*:\s*"([^"]+)"',
        r'"name"\s*:\s*"([^"]+)"',
        r'"user_name"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html_text)
        if match:
            candidates.append(match.group(1))
    for candidate in candidates:
        name = clean_creator_name(candidate)
        if name and name not in {"抖音", "douyin", "douyin_creator"}:
            return name
    return ""


def extract_bio_from_html(html_text: str) -> str:
    candidates = [
        extract_meta(html_text, "og:description"),
        extract_meta(html_text, "description"),
        extract_meta(html_text, "twitter:description"),
    ]
    for pattern in (
        r'"signature"\s*:\s*"([^"]+)"',
        r'"description"\s*:\s*"([^"]+)"',
        r'"desc"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html_text)
        if match:
            candidates.append(match.group(1))
    for candidate in candidates:
        bio = clean_creator_name(candidate)
        if bio and bio not in {"抖音", "Douyin"}:
            return bio[:300]
    return ""


async def fetch_douyin_creator_profile(url: str, cookie: str) -> Dict[str, Any]:
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler
    from local_tools.batch_download import apply_runtime_cookies

    apply_runtime_cookies(cookie or None, None)
    crawler = DouyinWebCrawler()
    sec_user_id = extract_sec_user_id_from_url(url) or await crawler.get_sec_user_id(url)
    response = await crawler.fetch_user_post_videos(str(sec_user_id), 0, 8)
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    items = data.get("aweme_list") or data.get("aweme_list_v2") or data.get("items") or []
    name = ""
    titles = []
    bio = ""
    if items and isinstance(items[0], dict):
        name = extract_author_name_from_item(items[0])
        bio = extract_author_bio_from_item(items[0])
    for item in items:
        if isinstance(item, dict):
            title = clean_creator_name(str(item.get("desc") or item.get("caption") or ""))
            if title:
                titles.append(title[:120])
    return {"sec_user_id": sec_user_id, "name": name, "bio": bio, "recent_titles": titles}


def fallback_creator_classification(name: str, bio: str, titles: list[str]) -> Dict[str, Any]:
    text = " ".join([name, bio, *titles])
    category = "综合"
    if any(word in text for word in ["投资", "财富", "巴菲特", "芒格", "资产", "商业", "创业"]):
        category = "投资商业"
    elif any(word in text for word in ["教育", "学习", "读书", "孩子"]):
        category = "教育学习"
    elif any(word in text for word in ["科技", "AI", "模型", "产品"]):
        category = "科技产品"
    elif any(word in text for word in ["职场", "工作", "职业"]):
        category = "职业成长"
    language = "英文" if re.search(r"\b[a-zA-Z]{4,}\b", text) and not re.search(r"[\u4e00-\u9fff]", text) else "中文"
    return {
        "category": category,
        "language": language,
        "content_type": "口播",
        "tags": ["douyin", "口播", category],
    }


def classify_creator_with_ai(name: str, bio: str, titles: list[str], data: Dict[str, Any]) -> Dict[str, Any]:
    fallback = fallback_creator_classification(name, bio, titles)
    summary_cfg = data.get("summary") or {}
    env_file = project_path(str(data.get("env_file", "local_tools/obsidian_sync/.env")))
    api_key_env = str(summary_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = read_env_value(env_file, api_key_env)
    if not api_key:
        return fallback

    base_url = str(summary_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
    model = str(summary_cfg.get("model", "deepseek-v4-flash"))
    prompt = (
        "你是内容分类助手。请根据抖音博主昵称和最近视频标题判断账号类型，只返回 JSON，"
        "字段为 category, language, content_type, tags。"
        "category 用 2-6 个中文；language 为 中文/英文/中英混合；"
        "content_type 为 口播/访谈/教程/剧情/带货/混合；tags 为 3-8 个中文标签。\n\n"
        f"昵称：{name}\n简介：{bio or '未获取到'}\n最近视频标题：\n" + "\n".join(f"- {title}" for title in titles[:8])
    )
    try:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(45.0)) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", str(content), re.DOTALL)
        parsed = json.loads(match.group(0) if match else str(content))
        tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else fallback["tags"]
        return {
            "category": str(parsed.get("category") or fallback["category"])[:24],
            "language": str(parsed.get("language") or fallback["language"])[:12],
            "content_type": str(parsed.get("content_type") or fallback["content_type"])[:12],
            "tags": [str(tag)[:24] for tag in tags if str(tag).strip()][:8] or fallback["tags"],
        }
    except Exception:
        return fallback


def resolve_creator_from_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_url = str(payload.get("url", "")).strip()
    if not raw_url:
        raise ValueError("url is required")
    url = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
    parsed = urlparse(url)
    platform = normalize_platform(payload.get("platform"), url)
    if platform != "douyin":
        raise ValueError(f"{platform_label(platform)} 内容源可先手动保存；URL 补全和抓取适配器下一阶段接入。")
    if "douyin.com" not in parsed.netloc:
        raise ValueError("Only douyin.com creator URLs are supported")

    data = load_config()
    cookie_file = project_path(str(data.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))
    cookie = cookie_file.read_text(encoding="utf-8").strip() if cookie_file.exists() else ""
    profile: Dict[str, Any] = {}
    errors = []
    try:
        profile = asyncio.run(fetch_douyin_creator_profile(url, cookie))
    except Exception as exc:
        errors.append(f"profile:{type(exc).__name__}:{exc}")
        profile = {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie

    final_url = url
    html_text = ""
    try:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=15) as response:
            final_url = response.geturl()
            html_text = response.read(2_000_000).decode("utf-8", errors="replace")
    except Exception as exc:
        errors.append(f"html:{type(exc).__name__}:{exc}")
        final_url = url

    name = clean_creator_name(str(profile.get("name", ""))) or extract_name_from_html(html_text)
    bio = clean_creator_name(str(profile.get("bio", ""))) or extract_bio_from_html(html_text)
    if not name:
        detail = "；".join(errors[-2:])
        if detail:
            raise ValueError(f"没有获取到博主昵称。内部原因：{detail}")
        raise ValueError("没有获取到博主昵称。请确认主页 URL 正确，并先用 Chrome 插件导入抖音 Cookie。")

    key = pinyin_initials(name)
    if not key:
        raise ValueError(f"无法根据博主名称生成 Key: {name}")
    key = unique_creator_key(key, final_url, data)
    recent_titles = profile.get("recent_titles") if isinstance(profile.get("recent_titles"), list) else []
    classification = classify_creator_with_ai(name, bio, [str(item) for item in recent_titles], data)

    return {
        "key": key,
        "platform": "douyin",
        "name": name,
        "url": final_url,
        "sec_user_id": profile.get("sec_user_id", ""),
        "enabled": True,
        "category": classification.get("category", "综合"),
        "language": classification.get("language", "中文"),
        "content_type": classification.get("content_type", "口播"),
        "bio": bio,
        "tags": classification.get("tags", ["douyin", "口播"]),
    }


def public_config(data: Dict[str, Any]) -> Dict[str, Any]:
    creators = data.get("creators") or []
    if not isinstance(creators, list):
        creators = []
    public_creators = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        item = dict(creator)
        item["platform"] = normalize_platform(item.get("platform"), str(item.get("url", "")))
        if not item.get("tags"):
            item["tags"] = default_tags_for_platform(item["platform"])
        public_creators.append(item)
    return {
        "vault_path": data.get("vault_path", ""),
        "output_subdir": data.get("output_subdir", ""),
        "fetch": data.get("fetch", {}),
        "summary": {
            "model": (data.get("summary") or {}).get("model", ""),
        },
        "creators": public_creators,
    }


def status_payload() -> Dict[str, Any]:
    data = load_config()
    cookie_file = project_path(str(data.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))
    weibo_cookie_file = project_path(str(data.get("weibo_cookie_file", "local_tools/weibo_cookie.txt")))
    env_file = project_path(str(data.get("env_file", "local_tools/obsidian_sync/.env")))
    api_key_env = str((data.get("summary") or {}).get("api_key_env", "DEEPSEEK_API_KEY"))
    vault_path = Path(str(data.get("vault_path", ""))).expanduser()
    output_dir = vault_path / str(data.get("output_subdir", ""))
    douyin_cookie = cookie_status(cookie_file)
    weibo_cookie = file_ready(weibo_cookie_file, ["paste_your", "PASTE_YOUR"])
    return {
        "time": utc_now(),
        "config_path": str(get_config_path()),
        "cookie": douyin_cookie,
        "accounts": {
            "douyin": {
                "label": platform_label("douyin"),
                "runnable": "douyin" in RUNNABLE_PLATFORMS,
                "cookie": douyin_cookie,
            },
            "weibo": {
                "label": platform_label("weibo"),
                "runnable": "weibo" in RUNNABLE_PLATFORMS,
                "cookie": weibo_cookie,
            },
        },
        "deepseek": env_status(env_file, api_key_env),
        "vault": {"path": str(vault_path), "exists": vault_path.exists()},
        "output": {"path": str(output_dir), "exists": output_dir.exists()},
        "worker": worker_status(),
    }


def output_base_path(data: Dict[str, Any]) -> Path:
    vault_path = Path(str(data.get("vault_path", ""))).expanduser()
    return vault_path / str(data.get("output_subdir", ""))


def configured_log_path(data: Optional[Dict[str, Any]] = None) -> Path:
    data = data or load_config()
    work_dir = project_path(str(data.get("work_dir", "local_tools/obsidian_sync/work")))
    return work_dir / "logs" / "dashboard_sync.log"


def configured_state_path(data: Optional[Dict[str, Any]] = None) -> Path:
    data = data or load_config()
    return project_path(str(data.get("state_db", "local_tools/obsidian_sync/state.sqlite")))


def make_run_id() -> str:
    return "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def classify_log_lines(text: str) -> Dict[str, Any]:
    warnings = []
    errors = []
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower()
        if not line:
            continue
        if "warning" in lower or "warn " in lower:
            warnings.append(line)
        if (
            line.startswith("ERROR ")
            or "traceback" in lower
            or "exception" in lower
            or "failed" in lower
            or "失败" in line
        ):
            errors.append(humanize_log_line(line))
    return {
        "warning_count": len(warnings),
        "error_count": len(errors),
        "warnings": [],
        "errors": errors[-8:],
    }


def humanize_log_line(line: str) -> str:
    lower = line.lower()
    video_match = re.search(r"\b(\d{12,})\b", line)
    video_id = video_match.group(1) if video_match else ""
    prefix = f"视频 {video_id}：" if video_id else ""

    if "remoteprotocolerror" in lower or "peer closed connection without sending complete message body" in lower:
        return (
            f"{prefix}视频下载到一半连接断开。新版任务会自动删除半截文件并重试；"
            "如果多次仍失败，通常是网络波动、抖音临时限流，或该视频链接短时间失效。"
        )
    if "视频下载中断" in line:
        return f"{prefix}{line.split('RuntimeError:', 1)[-1].strip()}"
    if line.startswith("RETRY "):
        return f"{prefix}{line}"
    if "timeout" in lower or "timed out" in lower or "readtimeout" in lower:
        return f"{prefix}请求超时。一般是网络慢或抖音响应慢，重新跑同一个博主通常可以继续处理。"
    if "no playable video url" in lower:
        return f"{prefix}没有拿到可播放的视频地址。常见原因是作品权限限制、视频失效，或 Cookie 登录状态不够完整。"
    if "not a video post" in lower:
        return f"{prefix}这条内容不是普通视频，系统已跳过。"
    if "ffmpeg failed" in lower:
        return f"{prefix}音频提取失败。通常是视频文件不完整或格式异常，建议重新跑一次该博主。"
    if "transcription returned empty" in lower:
        return f"{prefix}转录结果为空。可能是视频没有清晰人声、音量太低，或音频提取失败。"
    if "faster-whisper is not installed" in lower:
        return "本地转录组件没有安装完整。需要重新运行安装脚本后再试。"
    if "missing deepseek_api_key" in lower or "missing glm" in lower:
        return "AI 总结密钥缺失。逐字稿流程会受影响，请检查本地 .env 配置。"
    if "httpstatuserror" in lower or "401" in line or "403" in line:
        return f"{prefix}请求被拒绝。常见原因是 Cookie 失效、登录态不足，或接口临时限制。"
    if "traceback" in lower:
        return "程序出现未预期异常。请查看下方原始日志；如果重复出现，把这段日志发给我定位。"
    if "summary failed" in lower or "ai 总结失败" in line:
        return f"{prefix}逐字稿已生成，但 AI 总结失败。可以稍后重新处理该视频。"
    return line


def parse_run_history(text: str) -> list[Dict[str, Any]]:
    runs = []
    current: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        start_match = re.match(r"^\[([^\]]+)\] START (.*)$", raw)
        if start_match:
            if current:
                runs.append(current)
            current = {
                "started_at": start_match.group(1),
                "command": start_match.group(2),
                "status": "running",
                "seen": None,
                "processed": None,
                "output": "",
                "errors": [],
                "warnings": [],
                "wrote": [],
                "detected_count": 0,
                "candidate_count": 0,
                "success_count": 0,
                "error_count": 0,
                "dry_count": 0,
                "current_creator": "",
            }
            continue
        if not current:
            continue
        line = raw.strip()
        lower = line.lower()
        if line.startswith("CREATOR "):
            match = re.match(r"^CREATOR\s+\d+/\d+\s+(.+)$", line)
            if match:
                current["current_creator"] = match.group(1)
        elif line.startswith("FETCH "):
            current["current_creator"] = line.replace("FETCH ", "", 1)
        elif line.startswith("FOUND "):
            match = re.search(r"FOUND\s+(\d+)", line)
            if match:
                current["detected_count"] += int(match.group(1))
        elif line.startswith("CANDIDATES "):
            match = re.search(r"count=(\d+)", line)
            if match:
                current["candidate_count"] += int(match.group(1))
        elif line.startswith("DONE "):
            seen = re.search(r"seen=(\d+)", line)
            processed = re.search(r"processed=(\d+)", line)
            output = re.search(r"output=(.*)$", line)
            current["seen"] = int(seen.group(1)) if seen else None
            current["processed"] = int(processed.group(1)) if processed else None
            current["output"] = output.group(1) if output else ""
            if not current["detected_count"] and current["seen"] is not None:
                current["detected_count"] = current["seen"]
            current["status"] = "done_with_errors" if current["errors"] else "done"
        elif line.startswith("ERROR ") or "traceback" in lower or "exception" in lower or "failed" in lower or "失败" in line:
            current["status"] = "error"
            current["errors"].append(humanize_log_line(line))
            current["error_count"] += 1
        elif "warning" in lower or "warn " in lower:
            current["warnings"].append(line)
        elif line.startswith("WROTE "):
            current["wrote"].append(line.replace("WROTE ", "", 1))
            current["success_count"] += 1
        elif line.startswith("DRY "):
            current["dry_count"] += 1
    if current:
        runs.append(current)
    for run in runs:
        run["failure_reasons"] = list(dict.fromkeys(run.get("errors", [])))[:3]
        if not run.get("candidate_count") and run.get("processed") is not None:
            run["candidate_count"] = int(run.get("processed") or 0)
        if not run.get("candidate_count"):
            run["candidate_count"] = (
                int(run.get("success_count") or 0)
                + int(run.get("error_count") or 0)
                + int(run.get("dry_count") or 0)
            )
    return runs[-12:]


def latest_run_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start_index = 0
    for index, raw in enumerate(lines):
        if re.match(r"^\[([^\]]+)\] START (.*)$", raw):
            start_index = index
    return lines[start_index:]


def parse_progress(text: str, data: Dict[str, Any], running: bool) -> Dict[str, Any]:
    lines = latest_run_lines(text)
    enabled_creators = [creator for creator in data.get("creators", []) if creator.get("enabled", True)]
    total_creators = len(enabled_creators)
    current_creator = ""
    current_video = ""
    fetched_creators = 0
    total_items = 0
    has_candidate_total = False
    completed_items = 0
    found_items = 0
    retry_count = 0
    error_count = 0
    last_progress = ""
    has_structured_creator = False

    for raw in lines:
        line = raw.strip()
        if line.startswith("CREATOR "):
            match = re.match(r"^CREATOR\s+(\d+)/(\d+)\s+(.+)$", line)
            if match:
                has_structured_creator = True
                fetched_creators = max(fetched_creators, int(match.group(1)))
                total_creators = max(total_creators, int(match.group(2)))
                current_creator = match.group(3)
        elif line.startswith("FETCH "):
            if not has_structured_creator:
                fetched_creators += 1
            current_creator = line.replace("FETCH ", "", 1)
        elif line.startswith("FOUND "):
            match = re.search(r"FOUND\s+(\d+)", line)
            if match:
                found_items += int(match.group(1))
        elif line.startswith("CANDIDATES "):
            match = re.search(r"count=(\d+)", line)
            if match:
                total_items += int(match.group(1))
                has_candidate_total = True
        elif line.startswith("PROGRESS "):
            last_progress = line.replace("PROGRESS ", "", 1)
            parts = last_progress.split()
            if len(parts) > 1:
                current_video = parts[1]
        elif line.startswith("PROCESS "):
            parts = line.split()
            if len(parts) >= 3:
                current_video = parts[2]
        elif line.startswith("WROTE ") or line.startswith("ERROR ") or line.startswith("DRY "):
            completed_items += 1
            if line.startswith("ERROR "):
                error_count += 1
        elif line.startswith("RETRY "):
            retry_count += 1

    if not has_candidate_total and found_items:
        total_items = found_items

    if has_candidate_total and total_items == 0:
        percent = 100
    elif total_items > 0:
        percent = min(100, int((completed_items / total_items) * 100))
    else:
        percent = 0

    if running:
        if current_creator and total_items:
            label = f"正在处理 {current_creator}：{completed_items}/{total_items}"
        elif current_creator:
            label = f"正在扫描 {current_creator}"
        else:
            label = "准备启动任务"
    elif completed_items or total_items:
        label = f"最近任务：{completed_items}/{total_items or '-'}"
    else:
        label = "暂无进度"

    return {
        "label": label,
        "percent": percent,
        "running": running,
        "current_creator": current_creator,
        "current_video": current_video,
        "last_progress": last_progress,
        "completed_items": completed_items,
        "total_items": total_items if (has_candidate_total or found_items) else None,
        "fetched_creators": fetched_creators,
        "total_creators": total_creators,
        "retry_count": retry_count,
        "error_count": error_count,
    }


def recent_markdown_files(base_dir: Path) -> list[Dict[str, Any]]:
    if not base_dir.exists():
        return []
    files = sorted(base_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "path": str(path),
            "name": path.name,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for path in files[:12]
    ]


def humanize_item_error(value: Optional[str]) -> str:
    if not value:
        return ""
    return humanize_log_line(str(value))


def read_dashboard_runs(data: Dict[str, Any], limit: int = 12) -> list[Dict[str, Any]]:
    state_path = configured_state_path(data)
    if not state_path.exists():
        return []
    try:
        with sqlite3.connect(str(state_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            runs: list[Dict[str, Any]] = []
            for row in rows:
                run = dict(row)
                item_rows = conn.execute(
                    """
                    SELECT video_id, creator_key, creator_name, title, source_url, status, stage,
                           error, markdown_path, updated_at
                    FROM run_items
                    WHERE run_id = ?
                    ORDER BY updated_at ASC, video_id ASC
                    """,
                    (run["run_id"],),
                ).fetchall()
                items = [dict(item) for item in item_rows]
                for item in items:
                    item["error_human"] = humanize_item_error(item.get("error"))
                run["items"] = items
                run["failure_reasons"] = list(
                    dict.fromkeys(item["error_human"] for item in items if item.get("error_human"))
                )[:3]
                run["detected_count"] = int(run.get("detected_count") or 0)
                run["planned_count"] = int(run.get("planned_count") or 0)
                run["success_count"] = int(run.get("success_count") or 0)
                run["failed_count"] = int(run.get("failed_count") or 0)
                run["skipped_count"] = int(run.get("skipped_count") or 0)
                runs.append(run)
            return runs
    except sqlite3.Error:
        return []


def latest_run_progress(runs: list[Dict[str, Any]], running: bool) -> Dict[str, Any]:
    if not runs:
        return {"label": "暂无进度", "percent": 0, "running": running}
    run = runs[0]
    total = int(run.get("planned_count") or 0)
    done = int(run.get("success_count") or 0) + int(run.get("failed_count") or 0)
    percent = 100 if total == 0 and run.get("status") in {"done", "done_with_errors"} else 0
    if total:
        percent = min(100, int((done / total) * 100))
    label = f"{run.get('current_stage') or '最近任务'}"
    current_creator = run.get("current_creator") or ""
    if current_creator:
        label = f"{current_creator}：{label}"
    return {
        "label": label,
        "percent": percent,
        "running": running,
        "current_creator": current_creator,
        "current_video": run.get("current_video_id") or "",
        "completed_items": done,
        "total_items": total,
        "fetched_creators": None,
        "total_creators": run.get("total_creators"),
        "retry_count": 0,
        "error_count": run.get("failed_count") or 0,
    }


def worker_status() -> Dict[str, Any]:
    global worker_process, worker_log
    data = load_config()
    if worker_log is None:
        worker_log = configured_log_path(data)
    running = bool(worker_process and worker_process.poll() is None)
    code = None if running or worker_process is None else worker_process.returncode
    log_text = ""
    if worker_log and worker_log.exists():
        text = worker_log.read_text(encoding="utf-8", errors="replace")
        log_text = text[-12000:]
    else:
        text = ""
    analysis = classify_log_lines(text)
    db_runs = read_dashboard_runs(data)
    history = db_runs or list(reversed(parse_run_history(text)))
    progress = latest_run_progress(db_runs, running) if db_runs else parse_progress(text, data, running)
    if history and not running and history[0]["status"] == "running":
        history[0]["status"] = "unknown"
    output_base = output_base_path(data)
    return {
        "running": running,
        "returncode": code,
        "pid": worker_process.pid if running and worker_process else None,
        "log_path": str(worker_log) if worker_log else None,
        "log_tail": log_text,
        "analysis": analysis,
        "progress": progress,
        "history": history,
        "output_path": str(output_base),
        "recent_files": recent_markdown_files(output_base),
    }


def normalize_creator(raw: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    key = str(raw.get("key", "")).strip()
    name = str(raw.get("name", "")).strip()
    url = str(raw.get("url", "")).strip()
    platform = normalize_platform(raw.get("platform"), url)
    if not name:
        raise ValueError("Creator name is required")
    if not url:
        raise ValueError(f"Creator URL is required for {name}")
    if not key:
        key = unique_creator_key(pinyin_initials(name), url, data)
    tags = raw.get("tags") or default_tags_for_platform(platform)
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not isinstance(tags, list):
        tags = default_tags_for_platform(platform)
    creator = {
        "key": key,
        "platform": platform,
        "name": name,
        "url": url,
        "enabled": bool(raw.get("enabled", True)),
        "tags": [str(tag) for tag in tags],
    }
    for field in ("category", "language", "content_type", "bio"):
        value = str(raw.get(field, "")).strip()
        if value:
            creator[field] = value
    sec_user_id = str(raw.get("sec_user_id", "")).strip()
    if sec_user_id:
        creator["sec_user_id"] = sec_user_id
    cookie_profile = str(raw.get("cookie_profile", "")).strip()
    if cookie_profile:
        creator["cookie_profile"] = cookie_profile
    return creator


def save_public_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = load_config()
    if "vault_path" in payload:
        data["vault_path"] = str(payload.get("vault_path", "")).strip()
    if "output_subdir" in payload:
        data["output_subdir"] = str(payload.get("output_subdir", "")).strip()
    if "creators" in payload:
        creators = payload.get("creators")
        if not isinstance(creators, list):
            raise ValueError("creators must be a list")
        data["creators"] = [normalize_creator(item, data) for item in creators if isinstance(item, dict)]
    save_config(data)
    return public_config(data)


def save_secrets(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = load_config()
    cookie = str(payload.get("douyin_cookie", "")).strip()
    api_key = str(payload.get("deepseek_api_key", "")).strip()
    if cookie:
        cookie_file = project_path(str(data.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookie_file.write_text(cookie + "\n", encoding="utf-8")
    if api_key:
        env_file = project_path(str(data.get("env_file", "local_tools/obsidian_sync/.env")))
        api_key_env = str((data.get("summary") or {}).get("api_key_env", "DEEPSEEK_API_KEY"))
        update_env_value(env_file, api_key_env, api_key)
    return status_payload()


def failed_run_items(data: Dict[str, Any], run_id: str = "") -> tuple[str, list[Dict[str, Any]]]:
    state_path = configured_state_path(data)
    if not state_path.exists():
        return "", []
    with sqlite3.connect(str(state_path)) as conn:
        conn.row_factory = sqlite3.Row
        selected_run_id = run_id.strip()
        if not selected_run_id:
            row = conn.execute(
                """
                SELECT run_id
                FROM runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            selected_run_id = str(row["run_id"]) if row else ""
        if not selected_run_id:
            return "", []
        rows = conn.execute(
            """
            SELECT video_id, creator_key, creator_name, title, source_url, error
            FROM run_items
            WHERE run_id = ? AND status = 'failed'
            ORDER BY updated_at ASC, video_id ASC
            """,
            (selected_run_id,),
        ).fetchall()
        return selected_run_id, [dict(row) for row in rows]


def start_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    global worker_process, worker_log, worker_run_id
    if worker_process and worker_process.poll() is None:
        raise RuntimeError("A sync process is already running")

    data = load_config()
    work_dir = project_path(str(data.get("work_dir", "local_tools/obsidian_sync/work")))
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_log = log_dir / "dashboard_sync.log"
    worker_run_id = make_run_id()

    cmd = [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / "local_tools" / "obsidian_sync" / "sync.py"),
        "--config",
        str(get_config_path()),
        "--run-id",
        worker_run_id,
    ]
    creator = str(payload.get("creator", "")).strip()
    if creator:
        cmd.extend(["--creator", creator])
    limit = int(payload.get("limit", 0) or 0)
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    if payload.get("dry_run"):
        cmd.append("--dry-run")
    if payload.get("skip_summary"):
        cmd.append("--skip-summary")
    if payload.get("force"):
        cmd.append("--force")

    with worker_log.open("a", encoding="utf-8") as log:
        log.write(f"\n[{utc_now()}] START {worker_run_id} {' '.join(cmd)}\n")
        log.flush()
        worker_process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log)
    return worker_status()


def retry_failed_items(payload: Dict[str, Any]) -> Dict[str, Any]:
    global worker_process, worker_log, worker_run_id
    if worker_process and worker_process.poll() is None:
        raise RuntimeError("A sync process is already running")

    data = load_config()
    source_run_id = str(payload.get("run_id", "")).strip()
    resolved_run_id, items = failed_run_items(data, source_run_id)
    if not items:
        raise ValueError("最近一次任务没有失败视频可重爬")

    video_ids = sorted({str(item.get("video_id", "")).strip() for item in items if item.get("video_id")})
    creator_keys = sorted({str(item.get("creator_key", "")).strip() for item in items if item.get("creator_key")})
    if not video_ids:
        raise ValueError("失败记录里没有可重爬的视频 ID")

    work_dir = project_path(str(data.get("work_dir", "local_tools/obsidian_sync/work")))
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_log = log_dir / "dashboard_sync.log"
    worker_run_id = "retry_" + datetime.now().strftime("%Y%m%d_%H%M%S")

    cmd = [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / "local_tools" / "obsidian_sync" / "sync.py"),
        "--config",
        str(get_config_path()),
        "--run-id",
        worker_run_id,
        "--video-id",
        ",".join(video_ids),
    ]
    if len(creator_keys) == 1:
        cmd.extend(["--creator", creator_keys[0]])
    if payload.get("force", True):
        cmd.append("--force")

    with worker_log.open("a", encoding="utf-8") as log:
        log.write(
            f"\n[{utc_now()}] RETRY_FAILED source_run={resolved_run_id} "
            f"count={len(video_ids)} {worker_run_id} {' '.join(cmd)}\n"
        )
        log.flush()
        worker_process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log)
    status = worker_status()
    status["retry"] = {
        "source_run_id": resolved_run_id,
        "video_count": len(video_ids),
        "creator_count": len(creator_keys),
    }
    return status


def stop_sync() -> Dict[str, Any]:
    global worker_run_id
    if worker_process and worker_process.poll() is None:
        worker_process.terminate()
        if worker_run_id:
            data = load_config()
            state_path = configured_state_path(data)
            if state_path.exists():
                with sqlite3.connect(str(state_path)) as conn:
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'stopped', current_stage = '已停止', ended_at = ?, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (utc_now(), utc_now(), worker_run_id),
                    )
                    conn.commit()
    return worker_status()


def open_local_target(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = load_config()
    target = str(payload.get("target", "output")).strip()
    if target == "log":
        path = configured_log_path(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
    elif target == "vault":
        path = Path(str(data.get("vault_path", ""))).expanduser()
        path.mkdir(parents=True, exist_ok=True)
    else:
        path = output_base_path(data)
        path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["open", str(path)])
    return {"target": target, "path": str(path)}


def read_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length).decode("utf-8") if length else "{}"
    payload = json.loads(body or "{}")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def read_static(relative: str) -> bytes:
    path = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "dashboard_static" / relative
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(relative)
    return path.read_bytes()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ObsidianSyncDashboard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (utc_now(), fmt % args))

    def send_bytes(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self.send_bytes(HTTPStatus.OK, read_static("index.html"), "text/html; charset=utf-8")
            elif path == "/app.css":
                self.send_bytes(HTTPStatus.OK, read_static("app.css"), "text/css; charset=utf-8")
            elif path == "/app.js":
                self.send_bytes(HTTPStatus.OK, read_static("app.js"), "application/javascript; charset=utf-8")
            elif path == "/api/config":
                self.send_json(HTTPStatus.OK, {"ok": True, "config": public_config(load_config())})
            elif path == "/api/status":
                self.send_json(HTTPStatus.OK, {"ok": True, "status": status_payload()})
            elif path == "/api/run/status":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": worker_status()})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = read_body(self)
            if path == "/api/config":
                self.send_json(HTTPStatus.OK, {"ok": True, "config": save_public_config(payload)})
            elif path == "/api/secrets":
                self.send_json(HTTPStatus.OK, {"ok": True, "status": save_secrets(payload)})
            elif path == "/api/creator/resolve":
                self.send_json(HTTPStatus.OK, {"ok": True, "creator": resolve_creator_from_url(payload)})
            elif path == "/api/run":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": start_sync(payload)})
            elif path == "/api/run/retry-failed":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": retry_failed_items(payload)})
            elif path == "/api/run/stop":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": stop_sync()})
            elif path == "/api/open":
                self.send_json(HTTPStatus.OK, {"ok": True, "opened": open_local_target(payload)})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_OPTIONS(self) -> None:
        self.send_bytes(HTTPStatus.NO_CONTENT, b"", "text/plain; charset=utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web dashboard for Douyin -> Obsidian sync.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    return parser.parse_args()


def main() -> None:
    global settings
    settings = parse_args()
    server = ThreadingHTTPServer((settings.host, settings.port), DashboardHandler)
    print(f"Listening on http://{settings.host}:{settings.port}")
    print(f"Config: {get_config_path()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
