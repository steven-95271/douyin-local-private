#!/usr/bin/env python3
"""
Sync selected Douyin creators into an Obsidian vault.

The MVP intentionally runs as a manual/cron-style command. It keeps durable
state in SQLite and deletes temporary media after Markdown is written.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_tools.batch_download import (  # noqa: E402
    apply_runtime_cookies,
    headers_for_platform,
    read_secret_file,
    stream_to_file,
)
from local_tools.obsidian_sync.platforms.base import PlatformRunContext  # noqa: E402
from local_tools.obsidian_sync.platforms.registry import PlatformRegistry  # noqa: E402
from local_tools.obsidian_sync.subtitle_ocr import transcribe_subtitles_from_video  # noqa: E402
from local_tools.obsidian_sync.tagging import assign_tags  # noqa: E402


VIDEO_TYPES = {0, 4, 51, 55, 58, 61}
KNOWN_PLATFORMS = {
    "douyin",
    "weibo",
    "x",
    "xiaoyuzhou",
    "wechat",
    "xiaohongshu",
    "youtube",
    "bilibili",
    "tiktok",
    "kuaishou",
    "tieba",
    "zhihu",
}
RUNNABLE_PLATFORMS = {"douyin", "weibo", "x", "xiaoyuzhou", "wechat", "youtube"}
TRANSCRIPT_MODES = {"auto", "audio", "subtitle_ocr", "subtitle_ocr_fallback_audio"}
WHISPER_MODEL_CACHE: Dict[Tuple[str, str, str], Any] = {}
WHISPER_MODEL_LOCK = threading.Lock()
SYNC_LOCK_HANDLE: Optional[Any] = None
PROMOTION_FILTER_RULES: List[Tuple[str, int, str]] = [
    (r"(商单|恰饭|广子|接广|广告合作|品牌合作|商务合作|商业合作|推广合作|付费推广|本视频.{0,8}(广告|推广)|含推广)", 8, "商单/推广"),
    (r"(直播预告|直播通告|今晚.{0,12}直播|明天.{0,12}直播|今天.{0,12}直播|[0-2]?\d[:：点].{0,12}直播|预约直播|开播提醒)", 7, "直播通告"),
    (r"(来我直播间|进直播间|直播间见|直播间.{0,8}(等你|开播|下单|领取|福利))", 7, "直播导流"),
    (r"(小黄车|商品橱窗|购物车|同款链接|链接在.{0,10}(评论|主页|橱窗)|评论区.{0,12}(链接|领取|下单|购买|报名|进群)|私信.{0,12}(领取|资料|报名|进群|咨询|链接))", 7, "导流/带货"),
    (r"(领券|领取优惠券|优惠券.{0,12}(领取|链接|下单)|团购|秒杀|限时优惠|福利价|到手价|拼团|补货通知|新品上架|新品上市)", 6, "促销"),
    (r"(课程报名|训练营报名|社群招募|限时招生|一对一咨询|付费咨询|咨询入口|预约咨询|报名入口)", 6, "课程/咨询转化"),
    (r"(转发抽|评论抽|抽奖|粉丝福利|福利来了)", 5, "福利抽奖"),
]
NON_SPEECH_FILTER_RULES: List[Tuple[str, int, str]] = [
    (r"(唱歌|翻唱|清唱|原唱|合唱|伴奏|KTV|卡拉OK|cover|一首歌|弹唱|即兴唱|音乐现场|唱一段)", 8, "唱歌/音乐"),
    (r"(跳舞|舞蹈|热舞|手势舞|宅舞|广场舞|舞蹈挑战|扭一扭|变装|卡点|舞台表演)", 8, "跳舞/表演"),
    (r"(随拍|随手拍|街拍|对镜拍|自拍|美照|写真|颜值|氛围感|今日穿搭|OOTD|ootd|妆容|美甲|发型|变美|身材展示)", 6, "生活/颜值展示"),
    (r"(日常碎片|生活碎片|生活记录|记录生活|生活随拍|今日份快乐|随便拍拍|无聊拍|库存视频|路过拍一下|打卡|plog|vlog日常)", 6, "生活记录"),
    (r"(风景|夜景|夕阳|天空|花海|海边|旅行打卡|探店打卡|城市漫步)", 5, "风景/打卡"),
]
NON_SPEECH_EXEMPTION_PATTERN = (
    r"(怎么|如何|为什么|教程|技巧|方法|攻略|复盘|分析|拆解|解读|避坑|经验|逻辑|运营|增长).{0,16}"
    r"(唱歌|音乐|跳舞|舞蹈|穿搭|拍摄|vlog|短视频|表演|直播)"
    r"|"
    r"(唱歌|音乐|跳舞|舞蹈|穿搭|拍摄|vlog|短视频|表演|直播).{0,16}"
    r"(教程|技巧|方法|攻略|复盘|分析|拆解|解读|避坑|经验|逻辑|运营|增长)"
)
TALKING_CONTENT_CUE_PATTERN = r"(口播|干货|解读|分析|复盘|观点|认知|方法|教程|经验|思考|逻辑|为什么|怎么|如何|讲|聊|谈|分享|总结|建议|避坑|案例|问答|访谈|读书|商业|创业|投资|AI|工具|运营|增长|跨境|电商)"


@dataclass
class CreatorVideo:
    creator_key: str
    creator_name: str
    video_id: str
    source_url: str
    title: str
    create_time: Optional[int]
    raw: Dict[str, Any]
    tags: List[str]
    platform: str = "douyin"


def source_error_video(creator: Dict[str, Any], platform: str, run_id: str, message: str) -> CreatorVideo:
    key = str(creator.get("key") or creator.get("name") or "source").strip() or "source"
    title = f"来源失败：{str(creator.get('name') or key)}"
    return CreatorVideo(
        creator_key=key,
        creator_name=str(creator.get("name") or key),
        video_id=f"source_error_{run_id}_{key}",
        source_url=str(creator.get("url") or ""),
        title=title,
        create_time=None,
        raw={"error": message},
        tags=[platform, "来源失败"],
        platform=platform,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_transcript_mode(value: Any) -> str:
    mode = str(value or "auto").strip()
    return mode if mode in TRANSCRIPT_MODES else "auto"


def creator_runtime_options(creator: Dict[str, Any]) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    transcript_mode = normalize_transcript_mode(creator.get("transcript_mode"))
    if transcript_mode != "auto":
        options["transcript_mode"] = transcript_mode
    return options


def content_filter_config(config: Dict[str, Any]) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "enabled": True,
        "filter_promotions": True,
        "filter_non_speech": True,
        "min_score": 6,
    }
    raw = config.get("content_filter")
    if isinstance(raw, dict):
        for key in defaults:
            if key in raw:
                defaults[key] = raw.get(key)
    defaults["enabled"] = bool(defaults.get("enabled"))
    defaults["filter_promotions"] = bool(defaults.get("filter_promotions"))
    defaults["filter_non_speech"] = bool(defaults.get("filter_non_speech"))
    try:
        defaults["min_score"] = int(defaults.get("min_score") or 6)
    except (TypeError, ValueError):
        defaults["min_score"] = 6
    return defaults


def creator_promotion_filter_enabled(creator: Dict[str, Any], config: Dict[str, Any], args: argparse.Namespace) -> bool:
    if bool(getattr(args, "no_content_filter", False)):
        return False
    cfg = content_filter_config(config)
    if not cfg["enabled"] or not (cfg["filter_promotions"] or cfg["filter_non_speech"]):
        return False
    if creator.get("filter_promotions") is False:
        return False
    return True


def metadata_filter_parts(raw: Dict[str, Any]) -> List[str]:
    parts: List[str] = []
    for key in ("share_title", "share_desc", "seo_title", "keyword", "keywords"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key in ("text_extra", "cha_list", "challenge_position"):
        values = raw.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for child_key in ("hashtag_name", "cha_name", "desc", "name"):
                value = item.get(child_key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    music = raw.get("music")
    if isinstance(music, dict):
        for key in ("title", "author", "owner_nickname"):
            value = music.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return parts


def filter_text_parts(video: CreatorVideo) -> Tuple[str, str]:
    raw = video.raw if isinstance(video.raw, dict) else {}
    title = str(video.title or "")
    keys = (
        "original_text",
        "description_text",
        "desc",
        "caption",
        "text",
        "content",
        "full_text",
        "digest",
        "summary",
        "title",
    )
    parts: List[str] = []
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    parts.extend(metadata_filter_parts(raw))
    body = "\n".join(dict.fromkeys(parts))
    return title, body[:5000]


def score_filter_rules(title: str, body: str, rules: List[Tuple[str, int, str]]) -> Tuple[int, List[str]]:
    matched_labels: List[str] = []
    score = 0
    for pattern, weight, label in rules:
        title_hit = re.search(pattern, title, flags=re.IGNORECASE)
        body_hit = re.search(pattern, body, flags=re.IGNORECASE)
        if title_hit:
            score += weight * 2
        elif body_hit:
            score += weight
        if title_hit or body_hit:
            matched_labels.append(label)
    return score, matched_labels


def low_value_filter_reason(video: CreatorVideo, config: Dict[str, Any]) -> str:
    cfg = content_filter_config(config)
    min_score = int(cfg["min_score"])
    title, body = filter_text_parts(video)
    if cfg["filter_promotions"]:
        score, matched_labels = score_filter_rules(title, body, PROMOTION_FILTER_RULES)
        if score >= min_score:
            labels = "、".join(dict.fromkeys(matched_labels))
            return f"命中推广过滤：{labels}。已跳过下载、转录和 AI 总结。"

    video_like_platforms = {"douyin", "tiktok", "kuaishou", "bilibili", "youtube", "xiaohongshu"}
    if cfg["filter_non_speech"] and video.platform in video_like_platforms:
        combined = f"{title}\n{body}"
        if re.search(NON_SPEECH_EXEMPTION_PATTERN, combined, flags=re.IGNORECASE):
            return ""
        score, matched_labels = score_filter_rules(title, body, NON_SPEECH_FILTER_RULES)
        if re.search(TALKING_CONTENT_CUE_PATTERN, combined, flags=re.IGNORECASE):
            score = max(0, score - 3)
        if score >= min_score:
            labels = "、".join(dict.fromkeys(matched_labels))
            return f"命中非口播/低信息密度过滤：{labels}。已跳过下载、转录和 AI 总结。"
    return ""


def promotion_filter_reason(video: CreatorVideo, config: Dict[str, Any]) -> str:
    return low_value_filter_reason(video, config)


def transcript_mode_for_video(video: "CreatorVideo") -> str:
    return normalize_transcript_mode(video.raw.get("transcript_mode"))


def infer_platform_from_url(url: str) -> str:
    text = str(url or "").lower()
    if "mp.weixin.qq.com" in text or "weixin.qq.com" in text:
        return "wechat"
    if "xiaohongshu.com" in text or "xhslink.com" in text:
        return "xiaohongshu"
    if "xiaoyuzhoufm.com" in text or "feed.xyzfm.space" in text or "podcast.xyz" in text:
        return "xiaoyuzhou"
    if "weibo.com" in text or "weibo.cn" in text:
        return "weibo"
    if "x.com" in text or "twitter.com" in text:
        return "x"
    if "kuaishou.com" in text or "gifshow.com" in text:
        return "kuaishou"
    if "tieba.baidu.com" in text:
        return "tieba"
    if "zhihu.com" in text:
        return "zhihu"
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    if "bilibili.com" in text or "b23.tv" in text:
        return "bilibili"
    if "tiktok.com" in text:
        return "tiktok"
    return "douyin"


def creator_platform(creator: Dict[str, Any]) -> str:
    raw = str(creator.get("platform") or "").strip().lower()
    inferred = infer_platform_from_url(str(creator.get("url") or ""))
    return inferred if inferred != "douyin" and raw in {"", "douyin"} else (raw or inferred)


def default_output_subdirs(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    legacy_douyin = ""
    if isinstance(config, dict):
        legacy_douyin = str(config.get("output_subdir", "")).strip()
    return {
        "douyin": legacy_douyin or "Douyin/口播博主",
        "weibo": "Weibo/内容源",
        "x": "X/内容源",
        "xiaoyuzhou": "Podcast/小宇宙",
        "wechat": "WeChat/公众号",
        "youtube": "YouTube/视频博主",
        "bilibili": "Bilibili/视频博主",
        "tiktok": "TikTok/视频博主",
        "kuaishou": "Kuaishou/视频博主",
        "tieba": "Tieba/贴吧",
        "zhihu": "Zhihu/内容源",
    }


def output_subdirs_config(config: Dict[str, Any]) -> Dict[str, str]:
    output_subdirs = default_output_subdirs(config)
    raw = config.get("output_subdirs")
    if isinstance(raw, dict):
        for platform in output_subdirs:
            value = str(raw.get(platform, "")).strip()
            if value:
                output_subdirs[platform] = value
    return output_subdirs


def output_base_for_platform(vault_path: Path, config: Dict[str, Any], platform: str) -> Path:
    output_subdirs = output_subdirs_config(config)
    return vault_path / output_subdirs.get(platform, platform)


def cookie_profile_path(config: Dict[str, Any], creator: Optional[Dict[str, Any]] = None) -> Path:
    profile_name = str((creator or {}).get("cookie_profile") or config.get("default_cookie_profile") or "default")
    profiles = config.get("douyin_cookie_profiles") or config.get("cookie_profiles") or {}

    if isinstance(profiles, dict):
        entry = profiles.get(profile_name)
        if isinstance(entry, dict):
            value = entry.get("file") or entry.get("path") or entry.get("cookie_file")
            if value:
                return project_path(str(value))
        elif isinstance(entry, str):
            return project_path(entry)
    elif isinstance(profiles, list):
        for entry in profiles:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("key") or entry.get("profile") or "")
            if name == profile_name:
                value = entry.get("file") or entry.get("path") or entry.get("cookie_file")
                if value:
                    return project_path(str(value))

    return project_path(str(config.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))


def weibo_cookie_profile_path(config: Dict[str, Any], creator: Optional[Dict[str, Any]] = None) -> Path:
    profile_name = str((creator or {}).get("cookie_profile") or config.get("default_weibo_cookie_profile") or "default")
    profiles = config.get("weibo_cookie_profiles") or {}

    if isinstance(profiles, dict):
        entry = profiles.get(profile_name)
        if isinstance(entry, dict):
            value = entry.get("file") or entry.get("path") or entry.get("cookie_file")
            if value:
                return project_path(str(value))
        elif isinstance(entry, str):
            return project_path(entry)
    elif isinstance(profiles, list):
        for entry in profiles:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("key") or entry.get("profile") or "")
            if name == profile_name:
                value = entry.get("file") or entry.get("path") or entry.get("cookie_file")
                if value:
                    return project_path(str(value))

    return project_path(str(config.get("weibo_cookie_file", "local_tools/weibo_cookie.txt")))


def wechat_mp_cookie_path(config: Dict[str, Any]) -> Path:
    return project_path(str(config.get("wechat_mp_cookie_file", "local_tools/wechat_mp_cookie.txt")))


def wechat_mp_token_path(config: Dict[str, Any]) -> Path:
    return project_path(str(config.get("wechat_mp_token_file", "local_tools/wechat_mp_token.txt")))


def xiaohongshu_cookie_path(config: Dict[str, Any]) -> Path:
    return project_path(str(config.get("xiaohongshu_cookie_file", "local_tools/xiaohongshu_cookie.txt")))


def apply_cookie_for_creator(config: Dict[str, Any], creator: Optional[Dict[str, Any]] = None) -> None:
    if creator is None or creator_platform(creator) != "douyin":
        return
    cookie_path = cookie_profile_path(config, creator)
    try:
        cookie = read_secret_file(cookie_path)
    except FileNotFoundError as exc:
        raise RuntimeError("抖音 Cookie 缺失。请先在当前 Chrome 登录抖音，并用 Chrome 插件导入抖音 Cookie。") from exc
    apply_runtime_cookies(cookie, None)


def read_optional_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def creator_prerequisite_error(config: Dict[str, Any], creator: Dict[str, Any]) -> str:
    platform = creator_platform(creator)
    if platform == "douyin":
        text = read_optional_secret(cookie_profile_path(config, creator))
        if not any(marker in text for marker in ("sessionid=", "sessionid_ss=", "sid_guard=", "passport_csrf_token=")):
            return "抖音 Cookie 缺失。请先登录抖音并用 Chrome 插件导入 Cookie。"
    if platform == "weibo":
        if not read_optional_secret(weibo_cookie_profile_path(config, creator)):
            return "微博 Cookie 缺失。请先登录微博并用 Chrome 插件导入 Cookie。"
    if platform == "wechat" and wechat_fakeid_from_creator(creator):
        if not read_optional_secret(wechat_mp_cookie_path(config)) or not read_optional_secret(wechat_mp_token_path(config)):
            return "公众号后台 Cookie/token 缺失。请先登录 mp.weixin.qq.com 后台并用 Chrome 插件导入公众号后台 Cookie。"
    return ""


def ensure_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            creator_key TEXT NOT NULL,
            creator_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT,
            create_time INTEGER,
            status TEXT NOT NULL,
            markdown_path TEXT,
            error TEXT,
            transcript_chars INTEGER DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            processed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_videos_creator_status
        ON videos (creator_key, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            creator_filter TEXT,
            total_creators INTEGER DEFAULT 0,
            detected_count INTEGER DEFAULT 0,
            planned_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            current_stage TEXT,
            current_creator TEXT,
            current_video_id TEXT,
            error TEXT,
            output_path TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_items (
            run_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            creator_key TEXT NOT NULL,
            creator_name TEXT NOT NULL,
            title TEXT,
            source_url TEXT,
            status TEXT NOT NULL,
            stage TEXT,
            error TEXT,
            markdown_path TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, video_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_items_run_status
        ON run_items (run_id, status)
        """
    )
    conn.commit()


def default_run_id() -> str:
    return "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def pause_requested(args: argparse.Namespace) -> bool:
    pause_file = str(getattr(args, "pause_file", "") or "").strip()
    return bool(pause_file and Path(pause_file).expanduser().exists())


def recent_cutoff_timestamp(args: argparse.Namespace) -> int:
    days = int(getattr(args, "recent_days", 0) or 0)
    if days <= 0:
        return 0
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())


def update_run_counts(conn: sqlite3.Connection, run_id: str) -> None:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status IN ('pending', 'running', 'success', 'failed', 'dry_run') THEN 1 ELSE 0 END)
        FROM run_items
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    success_count, failed_count, skipped_count, planned_count = [int(value or 0) for value in row]
    conn.execute(
        """
        UPDATE runs SET
            success_count = ?,
            failed_count = ?,
            skipped_count = ?,
            planned_count = ?,
            updated_at = ?
        WHERE run_id = ?
        """,
        (success_count, failed_count, skipped_count, planned_count, utc_now(), run_id),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, run_id: str, creator_filter: Optional[str], total_creators: int) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR REPLACE INTO runs (
            run_id, started_at, ended_at, status, creator_filter, total_creators,
            detected_count, planned_count, success_count, failed_count, skipped_count,
            current_stage, current_creator, current_video_id, error, output_path, updated_at
        )
        VALUES (?, ?, NULL, 'running', ?, ?, 0, 0, 0, 0, 0, '准备启动', '', '', NULL, NULL, ?)
        """,
        (run_id, now, creator_filter, total_creators, now),
    )
    conn.commit()


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: Optional[str] = None,
    current_stage: Optional[str] = None,
    current_creator: Optional[str] = None,
    current_video_id: Optional[str] = None,
    detected_count: Optional[int] = None,
    output_path: Optional[Path] = None,
    error: Optional[str] = None,
    ended: bool = False,
) -> None:
    fields = ["updated_at = ?"]
    values: List[Any] = [utc_now()]
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if current_stage is not None:
        fields.append("current_stage = ?")
        values.append(current_stage)
    if current_creator is not None:
        fields.append("current_creator = ?")
        values.append(current_creator)
    if current_video_id is not None:
        fields.append("current_video_id = ?")
        values.append(current_video_id)
    if detected_count is not None:
        fields.append("detected_count = ?")
        values.append(detected_count)
    if output_path is not None:
        fields.append("output_path = ?")
        values.append(str(output_path))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if ended:
        fields.append("ended_at = ?")
        values.append(utc_now())
    values.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?", values)
    conn.commit()


def upsert_run_item(
    conn: sqlite3.Connection,
    run_id: str,
    video: CreatorVideo,
    status: str,
    stage: str,
    error: Optional[str] = None,
    markdown_path: Optional[Path] = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO run_items (
            run_id, video_id, creator_key, creator_name, title, source_url,
            status, stage, error, markdown_path, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, video_id) DO UPDATE SET
            creator_key = excluded.creator_key,
            creator_name = excluded.creator_name,
            title = excluded.title,
            source_url = excluded.source_url,
            status = excluded.status,
            stage = excluded.stage,
            error = excluded.error,
            markdown_path = excluded.markdown_path,
            updated_at = excluded.updated_at
        """,
        (
            run_id,
            video.video_id,
            video.creator_key,
            video.creator_name,
            video.title,
            video.source_url,
            status,
            stage,
            error,
            str(markdown_path) if markdown_path else None,
            now,
        ),
    )
    conn.commit()
    update_run_counts(conn, run_id)


def get_status(conn: sqlite3.Connection, video_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return str(row[0]) if row else None


def mark_seen(conn: sqlite3.Connection, video: CreatorVideo) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO videos (
            video_id, creator_key, creator_name, source_url, title, create_time,
            status, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'seen', ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            creator_key = excluded.creator_key,
            creator_name = excluded.creator_name,
            source_url = excluded.source_url,
            title = excluded.title,
            create_time = excluded.create_time,
            updated_at = excluded.updated_at
        """,
        (
            video.video_id,
            video.creator_key,
            video.creator_name,
            video.source_url,
            video.title,
            video.create_time,
            now,
            now,
        ),
    )
    conn.commit()


def mark_result(
    conn: sqlite3.Connection,
    video: CreatorVideo,
    status: str,
    markdown_path: Optional[Path],
    error: Optional[str],
    transcript_chars: int,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO videos (
            video_id, creator_key, creator_name, source_url, title, create_time,
            status, markdown_path, error, transcript_chars, first_seen_at,
            processed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            creator_key = excluded.creator_key,
            creator_name = excluded.creator_name,
            source_url = excluded.source_url,
            title = excluded.title,
            create_time = excluded.create_time,
            status = excluded.status,
            markdown_path = excluded.markdown_path,
            error = excluded.error,
            transcript_chars = excluded.transcript_chars,
            processed_at = excluded.processed_at,
            updated_at = excluded.updated_at
        """,
        (
            video.video_id,
            video.creator_key,
            video.creator_name,
            video.source_url,
            video.title,
            video.create_time,
            status,
            str(markdown_path) if markdown_path else None,
            error,
            transcript_chars,
            now,
            now,
            now,
        ),
    )
    conn.commit()


def safe_filename(text: str, limit: int = 96) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "untitled")[:limit].rstrip(". ")


def short_title_for_filename(text: str, default: str, limit: int = 32) -> str:
    text = re.sub(r"#(?!\d+\b)\S+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    episode = ""
    episode_match = re.match(r"^(第?\s*\d+\s*[集期讲节])\s*[:：.\-、]?\s*(.*)$", text)
    if episode_match:
        episode = re.sub(r"\s+", "", episode_match.group(1))
        text = episode_match.group(2).strip()
    for separator in ["：", ":", "。", "！", "？", "；", ";", "，", ","]:
        if separator in text:
            head = text.split(separator, 1)[0].strip()
            if len(head) >= 4:
                text = head
                break
    text = text.strip(" “”，,。.：:；;！!?？-—_")
    if episode and text:
        text = f"{episode}-{text}"
    elif episode:
        text = episode
    return (text or default)[:limit].rstrip(" -_")


def compact_title(text: str, default: str) -> str:
    text = re.sub(r"#(?!\d+\b)\S+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] or default


def format_date(ts: Optional[int]) -> str:
    if not ts:
        return "unknown-date"
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def format_frontmatter_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def extract_aweme_items(response: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    items = data.get("aweme_list") or data.get("aweme_list_v2") or data.get("items") or []
    if not isinstance(items, list):
        items = []

    next_cursor = data.get("max_cursor")
    if next_cursor is None:
        next_cursor = data.get("cursor")
    if next_cursor is not None:
        try:
            next_cursor = int(next_cursor)
        except (TypeError, ValueError):
            next_cursor = None

    has_more = bool(data.get("has_more") or data.get("hasMore"))
    return [item for item in items if isinstance(item, dict)], next_cursor, has_more


def aweme_to_video(creator: Dict[str, Any], item: Dict[str, Any]) -> Optional[CreatorVideo]:
    aweme_type = item.get("aweme_type")
    if aweme_type is not None:
        try:
            if int(aweme_type) not in VIDEO_TYPES:
                return None
        except (TypeError, ValueError):
            pass

    video_id = item.get("aweme_id") or item.get("item_id") or item.get("id")
    if not video_id:
        return None

    video_id = str(video_id)
    title = compact_title(str(item.get("desc") or item.get("caption") or ""), video_id)
    create_time = item.get("create_time")
    try:
        create_time = int(create_time) if create_time is not None else None
    except (TypeError, ValueError):
        create_time = None

    tags = creator.get("tags") or ["douyin", "口播"]
    if not isinstance(tags, list):
        tags = ["douyin", "口播"]

    raw = dict(item)
    raw.update(creator_runtime_options(creator))

    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=video_id,
        source_url=f"https://www.douyin.com/video/{video_id}",
        title=title,
        create_time=create_time,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform=creator_platform(creator),
    )


def strip_html_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_datetime_to_timestamp(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        raw = int(text)
        return raw // 1000 if raw > 10_000_000_000 else raw
    normalized = text.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        pass
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


X_RESERVED_PATHS = {
    "home",
    "explore",
    "i",
    "intent",
    "login",
    "logout",
    "messages",
    "notifications",
    "search",
    "settings",
    "share",
}


def extract_x_username_from_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return re.sub(r"[^A-Za-z0-9_]+", "", text[1:])[:50]
    if not text.startswith(("http://", "https://")) and re.fullmatch(r"[A-Za-z0-9_]{1,50}", text):
        return text
    parsed = urlparse(text if text.startswith(("http://", "https://")) else f"https://{text}")
    if "x.com" not in parsed.netloc and "twitter.com" not in parsed.netloc:
        return ""
    first = parsed.path.strip("/").split("/", 1)[0].strip()
    if not first or first.lower() in X_RESERVED_PATHS:
        return ""
    return re.sub(r"[^A-Za-z0-9_]+", "", first)[:50]


def x_username_for_creator(creator: Dict[str, Any]) -> str:
    for field in ("platform_id", "x_username", "twitter_username"):
        value = extract_x_username_from_url(str(creator.get(field) or ""))
        if value:
            return value
    return extract_x_username_from_url(str(creator.get("url") or ""))


def opencli_env() -> Dict[str, str]:
    env = dict(os.environ)
    extra_paths = [
        str(Path.home() / ".npm-global/bin"),
        str(Path.home() / ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    env["PATH"] = ":".join([*extra_paths, env.get("PATH", "")])
    return env


def find_opencli() -> str:
    env = opencli_env()
    for directory in env["PATH"].split(":"):
        candidate = Path(directory) / "opencli"
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("opencli", path=env["PATH"]) or ""


def opencli_daemon_status() -> Dict[str, Any]:
    try:
        response = httpx.get(
            "http://127.0.0.1:19825/status",
            headers={"X-OpenCLI": "1"},
            timeout=2.0,
        )
        if response.status_code != 200:
            return {}
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def open_chrome_for_opencli() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["open", "-ga", "Google Chrome"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return


def ensure_opencli_bridge_ready(opencli: str, timeout_seconds: float = 14.0) -> None:
    status = opencli_daemon_status()
    if status.get("extensionConnected") and int(status.get("pending") or 0) == 0:
        return

    open_chrome_for_opencli()
    subprocess.run(
        [opencli, "daemon", "restart"],
        capture_output=True,
        text=True,
        timeout=15,
        env=opencli_env(),
        cwd=str(PROJECT_ROOT),
    )
    deadline = time.time() + max(4.0, timeout_seconds)
    while time.time() < deadline:
        status = opencli_daemon_status()
        if status.get("extensionConnected") and int(status.get("pending") or 0) == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(
        "X 抓取前置检查失败：OpenCLI Browser Bridge 没有连上 Chrome。"
        "请确认 Chrome 已打开，OpenCLI Browser Bridge 扩展已启用，然后重试。"
    )


def extract_json_payload(text: str) -> Any:
    source = str(text or "").strip()
    for start_char, end_char in (("[", "]"), ("{", "}")):
        start = source.find(start_char)
        end = source.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(source[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise RuntimeError("OpenCLI 没有返回可解析的 JSON")


def x_tweet_id(tweet: Dict[str, Any], username: str) -> str:
    for key in ("id", "id_str", "tweet_id", "status_id"):
        value = str(tweet.get(key) or "").strip()
        if value:
            return value
    raw_url = str(tweet.get("url") or tweet.get("link") or "").strip()
    match = re.search(r"/status(?:es)?/(\d+)", raw_url)
    if match:
        return match.group(1)
    seed = "|".join(
        [
            username,
            str(tweet.get("created_at") or ""),
            str(tweet.get("text") or tweet.get("content") or ""),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def normalize_x_tweets(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("tweets", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in payload for key in ("text", "url", "created_at")):
            return [payload]
    return []


def x_media_text(tweet: Dict[str, Any]) -> str:
    media_urls = tweet.get("media_urls") or tweet.get("media") or []
    if isinstance(media_urls, str):
        media_urls = [item.strip() for item in re.split(r"[\s,]+", media_urls) if item.strip()]
    if not isinstance(media_urls, list):
        return ""
    urls = [str(item).strip() for item in media_urls if str(item).strip()]
    if not urls:
        return ""
    return "\n\n媒体：\n" + "\n".join(f"- {url}" for url in urls)


def x_tweet_url(username: str, tweet: Dict[str, Any], tweet_id: str) -> str:
    raw_url = str(tweet.get("url") or tweet.get("link") or "").strip()
    if raw_url.startswith("http"):
        return raw_url
    return f"https://x.com/{username}/status/{tweet_id}"


def x_tweet_to_video(creator: Dict[str, Any], tweet: Dict[str, Any], username: str) -> Optional[CreatorVideo]:
    text = strip_html_text(str(tweet.get("text") or tweet.get("content") or tweet.get("full_text") or ""))
    if not text:
        return None
    tweet_id = x_tweet_id(tweet, username)
    original_text = f"{text}{x_media_text(tweet)}".strip()
    tags = creator.get("tags") or ["x", "推文"]
    if not isinstance(tags, list):
        tags = ["x", "推文"]
    raw = dict(tweet)
    raw["original_text"] = original_text
    raw["x_username"] = username

    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or f"@{username}"),
        video_id=f"x_{tweet_id}",
        source_url=x_tweet_url(username, tweet, tweet_id),
        title=compact_title(text, tweet_id),
        create_time=parse_datetime_to_timestamp(tweet.get("created_at") or tweet.get("date") or tweet.get("time")),
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="x",
    )


async def fetch_x_posts(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    username = x_username_for_creator(creator)
    if not username:
        raise RuntimeError("X 来源缺少用户名。请填写 https://x.com/用户名 或 @用户名。")
    opencli = find_opencli()
    if not opencli:
        raise RuntimeError("未找到 OpenCLI。X 抓取需要 Agent-Reach/OpenCLI：请先确认 opencli twitter tweets --help 可运行。")
    fetch_cfg = config.get("fetch", {}) if isinstance(config.get("fetch"), dict) else {}
    default_limit = int(fetch_cfg.get("x_count_per_page", fetch_cfg.get("twitter_count_per_page", 50)) or 50)
    if full_history:
        limit = int(scan_limit or fetch_cfg.get("x_full_limit", 3200) or 3200)
    else:
        limit = int(scan_limit or default_limit)
    limit = max(1, min(limit, int(fetch_cfg.get("x_max_limit", 3200) or 3200)))
    timeout = float(fetch_cfg.get("x_timeout_seconds", 90))
    if conn and run_id:
        update_run(conn, run_id, current_stage=f"嗅探 X @{username}", current_creator=str(creator.get("name") or f"@{username}"))
    command = [opencli, "twitter", "tweets", username, "--limit", str(limit), "-f", "json"]
    ensure_opencli_bridge_ready(opencli)
    result: Optional[subprocess.CompletedProcess[str]] = None
    last_detail = ""
    for attempt in range(2):
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=opencli_env(),
                cwd=str(PROJECT_ROOT),
            )
        except subprocess.TimeoutExpired:
            last_detail = f"OpenCLI 超过 {int(timeout)} 秒没有返回"
            result = None
        if result and result.returncode == 0:
            break
        if result:
            last_detail = (result.stderr or result.stdout or "").strip()[:1000]
        if attempt == 0:
            if conn and run_id:
                update_run(conn, run_id, current_stage=f"重启 X 桥接后重试 @{username}")
            subprocess.run(
                [opencli, "daemon", "restart"],
                capture_output=True,
                text=True,
                timeout=15,
                env=opencli_env(),
                cwd=str(PROJECT_ROOT),
            )
            ensure_opencli_bridge_ready(opencli)
            continue
        if "BROWSER_CONNECT" in last_detail or "Browser Bridge extension not connected" in last_detail:
            raise RuntimeError(
                "X 抓取失败：OpenCLI 已安装，但 Chrome 里的 Browser Bridge 扩展没有连接。"
                "请打开 Chrome，确认 OpenCLI Browser Bridge 扩展已启用；如果已启用，先运行 "
                "`opencli daemon restart && opencli doctor` 后重试。"
            )
        if "aborted" in last_detail.lower() or "operation was aborted" in last_detail.lower():
            raise RuntimeError("X 抓取失败：OpenCLI 浏览器命令被 Chrome 中断。请重新加载 Browser Bridge 扩展，或重启 Chrome 后重试。")
        if "超时" in last_detail or "TimeoutExpired" in last_detail or "没有返回" in last_detail:
            raise RuntimeError("X 抓取失败：OpenCLI 长时间没有返回。系统已自动重启 daemon 并重试一次，仍失败；建议重启 Chrome 后再跑。")
        raise RuntimeError(f"X 抓取失败。请确认 Chrome 里 x.com 已登录，或先运行 opencli twitter tweets {username} --limit 5 -f json。{last_detail}")
    if not result or result.returncode != 0:
        raise RuntimeError("X 抓取失败：OpenCLI 没有返回有效结果。")
    tweets = normalize_x_tweets(extract_json_payload(result.stdout))
    videos: List[CreatorVideo] = []
    seen: set[str] = set()
    for tweet in tweets:
        video = x_tweet_to_video(creator, tweet, username)
        if not video or video.video_id in seen:
            continue
        videos.append(video)
        seen.add(video.video_id)
    if conn and run_id:
        update_run(conn, run_id, current_stage=f"嗅探 X 完成 @{username}", detected_count=len(videos))
    return videos


def xiaoyuzhou_headers(referer: str = "https://www.xiaoyuzhoufm.com/") -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
    }


def extract_xiaoyuzhou_ids_from_url(url: str) -> Tuple[str, str]:
    parsed = urlparse(str(url or ""))
    path = parsed.path.strip("/")
    podcast_match = re.search(r"(?:^|/)podcast/([A-Za-z0-9]+)", path)
    episode_match = re.search(r"(?:^|/)episode/([A-Za-z0-9]+)", path)
    return (
        podcast_match.group(1) if podcast_match else "",
        episode_match.group(1) if episode_match else "",
    )


def load_next_data(html_text: str) -> Dict[str, Any]:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        return {}
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def xiaoyuzhou_page_props(data: Dict[str, Any]) -> Dict[str, Any]:
    props = data.get("props") if isinstance(data.get("props"), dict) else {}
    page_props = props.get("pageProps") if isinstance(props.get("pageProps"), dict) else {}
    return page_props if isinstance(page_props, dict) else {}


def xiaoyuzhou_audio_url(episode: Dict[str, Any]) -> str:
    candidates: List[Any] = []
    enclosure = episode.get("enclosure")
    if isinstance(enclosure, dict):
        candidates.append(enclosure.get("url"))
    media = episode.get("media")
    if isinstance(media, dict):
        for key in ("source", "backupSource", "originalSource"):
            entry = media.get(key)
            if isinstance(entry, dict):
                candidates.append(entry.get("url"))
    candidates.extend([episode.get("audioUrl"), episode.get("mediaUrl")])
    for candidate in candidates:
        value = html.unescape(str(candidate or "")).strip()
        if value.startswith("//"):
            value = f"https:{value}"
        if value.startswith("http"):
            return value
    return ""


def find_rss_url_in_text(text: str) -> str:
    text = html.unescape(text or "")
    for match in re.finditer(r"https?://[^\s<>'\"，,。)）\]]+", text):
        url = match.group(0).rstrip(".;；")
        lowered = url.lower()
        if "feed.xyzfm.space" in lowered or "/feed" in lowered or lowered.endswith((".xml", ".rss")):
            return url
    return ""


def xiaoyuzhou_episode_url(episode: Dict[str, Any]) -> str:
    raw = str(episode.get("url") or episode.get("link") or "").strip()
    if raw.startswith("http"):
        return raw
    eid = str(episode.get("eid") or episode.get("id") or "").strip()
    if eid:
        return f"https://www.xiaoyuzhoufm.com/episode/{eid}"
    return ""


def xiaoyuzhou_episode_to_video(
    creator: Dict[str, Any],
    episode: Dict[str, Any],
    podcast: Optional[Dict[str, Any]] = None,
) -> Optional[CreatorVideo]:
    podcast = podcast or {}
    raw_id = str(episode.get("eid") or episode.get("id") or episode.get("guid") or "").strip()
    source_url = xiaoyuzhou_episode_url(episode)
    stable_seed = raw_id or source_url or str(episode.get("title") or "")
    if not stable_seed:
        return None
    episode_id = raw_id or hashlib.sha1(stable_seed.encode("utf-8")).hexdigest()[:16]
    title = compact_title(str(episode.get("title") or ""), episode_id)
    audio_url = xiaoyuzhou_audio_url(episode)
    description = strip_html_text(str(episode.get("shownotes") or episode.get("description") or ""))
    create_time = parse_datetime_to_timestamp(
        episode.get("pubDate")
        or episode.get("publishedAt")
        or episode.get("shownotesUpdatedAt")
        or episode.get("createdAt")
    )
    tags = creator.get("tags") or ["xiaoyuzhou", "播客"]
    if not isinstance(tags, list):
        tags = ["xiaoyuzhou", "播客"]
    raw = dict(episode)
    raw["audio_url"] = audio_url
    raw["description_text"] = description
    raw["podcast_title"] = str(podcast.get("title") or creator.get("name") or "").strip()
    raw["podcast_description"] = strip_html_text(str(podcast.get("description") or creator.get("bio") or ""))

    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or podcast.get("title") or creator["key"]),
        video_id=f"xiaoyuzhou_{episode_id}",
        source_url=source_url or str(creator.get("url") or ""),
        title=title,
        create_time=create_time,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="xiaoyuzhou",
    )


def rss_child_text(item: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        child = item.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def rss_enclosure_url(item: ET.Element) -> str:
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = str(enclosure.attrib.get("url") or "").strip()
        if url:
            return html.unescape(url)
    for child in item:
        if child.tag.endswith("enclosure"):
            url = str(child.attrib.get("url") or "").strip()
            if url:
                return html.unescape(url)
    return ""


def rss_item_to_video(creator: Dict[str, Any], item: ET.Element, podcast: Dict[str, Any]) -> Optional[CreatorVideo]:
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    title = rss_child_text(item, ["title"])
    link = rss_child_text(item, ["link"])
    guid = rss_child_text(item, ["guid"])
    description = rss_child_text(item, ["description", f"{content_ns}encoded"])
    pub_date = rss_child_text(item, ["pubDate", "published"])
    _, eid = extract_xiaoyuzhou_ids_from_url(link or guid)
    stable = eid or guid or link or title
    if not stable:
        return None
    raw_id = eid or hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]
    episode = {
        "eid": raw_id,
        "title": title,
        "description": description,
        "pubDate": pub_date,
        "url": link,
        "guid": guid,
        "enclosure": {"url": rss_enclosure_url(item)},
    }
    return xiaoyuzhou_episode_to_video(creator, episode, podcast)


async def fetch_xiaoyuzhou_html(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    response = await client.get(url, headers=xiaoyuzhou_headers(url), follow_redirects=True)
    response.raise_for_status()
    data = load_next_data(response.text)
    if not data:
        raise RuntimeError("小宇宙公开页面没有返回可解析的数据")
    return xiaoyuzhou_page_props(data)


async def fetch_xiaoyuzhou_rss(
    creator: Dict[str, Any],
    rss_url: str,
    config: Dict[str, Any],
    scan_limit: int = 0,
) -> List[CreatorVideo]:
    fetch_cfg = config.get("fetch", {})
    timeout = httpx.Timeout(float(fetch_cfg.get("xiaoyuzhou_timeout_seconds", 30)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(rss_url, headers=xiaoyuzhou_headers(rss_url), follow_redirects=True)
        response.raise_for_status()
    root = ET.fromstring(response.content)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS 里没有 channel")
    podcast = {
        "title": rss_child_text(channel, ["title"]) or creator.get("name") or creator.get("key"),
        "description": strip_html_text(rss_child_text(channel, ["description"])),
    }
    videos: List[CreatorVideo] = []
    seen_ids: set[str] = set()
    for item in channel.findall("item"):
        video = rss_item_to_video(creator, item, podcast)
        if video and video.video_id not in seen_ids:
            videos.append(video)
            seen_ids.add(video.video_id)
            if scan_limit and len(videos) >= scan_limit:
                break
    return videos


async def fetch_xiaoyuzhou_episodes(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    rss_url = str(creator.get("rss_url") or creator.get("feed_url") or "").strip()
    if rss_url:
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探小宇宙 RSS")
        videos = await fetch_xiaoyuzhou_rss(creator, rss_url, config, scan_limit if not full_history else 0)
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探小宇宙 RSS 完成", detected_count=len(videos))
        return videos

    url = str(creator.get("url") or "").strip()
    if not url:
        raise RuntimeError("小宇宙来源缺少 URL")
    timeout = httpx.Timeout(float(config.get("fetch", {}).get("xiaoyuzhou_timeout_seconds", 30)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        page_props = await fetch_xiaoyuzhou_html(client, url)

    videos: List[CreatorVideo] = []
    if isinstance(page_props.get("episode"), dict):
        episode = page_props["episode"]
        podcast = episode.get("podcast") if isinstance(episode.get("podcast"), dict) else {}
        video = xiaoyuzhou_episode_to_video(creator, episode, podcast)
        if video:
            videos.append(video)
    elif isinstance(page_props.get("podcast"), dict):
        podcast = page_props["podcast"]
        episodes = podcast.get("episodes") if isinstance(podcast.get("episodes"), list) else []
        rss_url = find_rss_url_in_text(
            "\n".join(
                [
                    str(podcast.get("description") or ""),
                    *[str(item.get("description") or "") for item in episodes if isinstance(item, dict)],
                ]
            )
        )
        if rss_url:
            videos = await fetch_xiaoyuzhou_rss(creator, rss_url, config, scan_limit if not full_history else 0)
        else:
            seen_ids: set[str] = set()
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                video = xiaoyuzhou_episode_to_video(creator, episode, podcast)
                if video and video.video_id not in seen_ids:
                    videos.append(video)
                    seen_ids.add(video.video_id)
                    if scan_limit and len(videos) >= scan_limit:
                        break
    else:
        raise RuntimeError("小宇宙页面里没有识别到节目或单集数据")

    if conn and run_id:
        stage = "嗅探小宇宙公开页"
        if full_history and not rss_url:
            stage = "嗅探小宇宙公开页（未发现 RSS，仅能处理公开页单集）"
        update_run(conn, run_id, current_stage=stage, detected_count=len(videos))
    return videos


def wechat_headers(referer: str = "https://mp.weixin.qq.com/") -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
    }


def wechat_mp_headers(cookie: str) -> Dict[str, str]:
    headers = wechat_headers("https://mp.weixin.qq.com/")
    headers["Accept"] = "application/json, text/plain, */*"
    headers["X-Requested-With"] = "XMLHttpRequest"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def clean_wechat_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" -_|")


def extract_wechat_biz_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    return str(query.get("__biz", [""])[0]).strip()


def extract_meta(html_text: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text or "", re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def wechat_fakeid_from_creator(creator: Dict[str, Any]) -> str:
    for field in ("wechat_fakeid", "platform_id", "wechat_biz"):
        value = str(creator.get(field) or "").strip()
        if value and value.lower() not in {"none", "null"}:
            return value
    url = str(creator.get("url") or "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("fakeid", "__biz"):
        value = str(query.get(key, [""])[0]).strip()
        if value:
            return value
    return ""


async def fetch_wechat_mp_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    cookie: str,
    token: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    query = dict(params)
    query["token"] = token
    query["lang"] = "zh_CN"
    query["f"] = "json"
    query["ajax"] = 1
    response = await client.get(endpoint, params=query, headers=wechat_mp_headers(cookie), follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("公众号后台接口返回了非 JSON 对象")
    resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else {}
    ret = resp.get("ret")
    if ret not in (0, "0", None):
        message = str(resp.get("err_msg") or resp.get("errmsg") or data.get("errmsg") or "")
        if str(ret) in {"200003", "-1"}:
            raise RuntimeError("公众号后台 Cookie/token 已失效。请打开 mp.weixin.qq.com 后台，并用 Chrome 插件重新导入公众号后台 Cookie。")
        raise RuntimeError(f"公众号后台接口错误：{ret} {message}".strip())
    return data


def extract_wechat_js_var(html_text: str, name: str) -> str:
    patterns = [
        rf"var\s+{re.escape(name)}\s*=\s*'((?:\\'|[^'])*)'",
        rf'var\s+{re.escape(name)}\s*=\s*"((?:\\"|[^"])*)"',
        rf"{re.escape(name)}\s*:\s*'((?:\\'|[^'])*)'",
        rf'{re.escape(name)}\s*:\s*"((?:\\"|[^"])*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if not match:
            continue
        value = match.group(1)
        value = value.replace("\\x26", "&").replace("\\/", "/")
        value = value.replace("\\'", "'").replace('\\"', '"')
        return html.unescape(value).strip()
    return ""


class ElementByIdParser(HTMLParser):
    def __init__(self, element_id: str) -> None:
        super().__init__(convert_charrefs=False)
        self.element_id = element_id
        self.capturing = False
        self.depth = 0
        self.parts: List[str] = []

    def _attrs_to_text(self, attrs: List[Tuple[str, Optional[str]]]) -> str:
        pieces = []
        for key, value in attrs:
            if value is None:
                pieces.append(key)
            else:
                pieces.append(f'{key}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(pieces)) if pieces else ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key: value for key, value in attrs}
        if not self.capturing and attrs_dict.get("id") == self.element_id:
            self.capturing = True
            self.depth = 1
            return
        if self.capturing:
            self.depth += 1
            self.parts.append(f"<{tag}{self._attrs_to_text(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.capturing:
            self.parts.append(f"<{tag}{self._attrs_to_text(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if not self.capturing:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.capturing = False
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.capturing:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.capturing:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.capturing:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self.capturing:
            self.parts.append(f"<!--{data}-->")


def extract_html_block_by_id(html_text: str, element_id: str) -> str:
    parser = ElementByIdParser(element_id)
    parser.feed(html_text or "")
    return "".join(parser.parts)


def html_block_to_markdown_text(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value or "", flags=re.DOTALL)
    value = re.sub(r"<script\b[^>]*>.*?</script>", "", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<style\b[^>]*>.*?</style>", "", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<img\b[^>]*?(?:data-src|src)=['\"]([^'\"]+)['\"][^>]*>", r"\n![](\1)\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</(p|div|section|article|blockquote|h[1-6]|li|ul|ol)\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_wechat_article_html(creator: Dict[str, Any], url: str, html_text: str) -> Optional[CreatorVideo]:
    title = clean_wechat_text(
        extract_wechat_js_var(html_text, "msg_title")
        or extract_meta(html_text, "og:title")
        or extract_meta(html_text, "twitter:title")
    )
    account = clean_wechat_text(
        extract_wechat_js_var(html_text, "nickname")
        or extract_wechat_js_var(html_text, "profile_nickname")
        or extract_meta(html_text, "author")
        or str(creator.get("name") or "")
    )
    description = strip_html_text(
        extract_wechat_js_var(html_text, "msg_desc")
        or extract_meta(html_text, "description")
        or extract_meta(html_text, "og:description")
    )
    content_html = extract_html_block_by_id(html_text, "js_content")
    original_text = html_block_to_markdown_text(content_html)
    if not original_text:
        return None

    publish_time = (
        extract_wechat_js_var(html_text, "publish_time")
        or extract_wechat_js_var(html_text, "ori_create_time")
        or extract_wechat_js_var(html_text, "ct")
    )
    create_time = parse_datetime_to_timestamp(publish_time)
    biz = extract_wechat_biz_from_url(url) or extract_wechat_js_var(html_text, "biz")
    article_seed = url or title or original_text[:80]
    article_id = hashlib.sha1(article_seed.encode("utf-8")).hexdigest()[:16]
    tags = creator.get("tags") or ["wechat", "公众号"]
    if not isinstance(tags, list):
        tags = ["wechat", "公众号"]
    raw: Dict[str, Any] = {
        "original_text": original_text,
        "description_text": description,
        "wechat_biz": biz,
    }
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=account or str(creator.get("name") or creator["key"]),
        video_id=f"wechat_{article_id}",
        source_url=url,
        title=compact_title(title, article_id),
        create_time=create_time,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="wechat",
    )


async def fetch_wechat_article(creator: Dict[str, Any], url: str, config: Dict[str, Any]) -> CreatorVideo:
    fetch_cfg = config.get("fetch", {})
    timeout = httpx.Timeout(float(fetch_cfg.get("wechat_timeout_seconds", 30)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=wechat_headers(url), follow_redirects=True)
        response.raise_for_status()
        final_url = str(response.url)
        video = parse_wechat_article_html(creator, final_url, response.text)
    if not video:
        raise RuntimeError("没有从公众号文章里解析到正文。请确认链接是公开的 mp.weixin.qq.com 文章。")
    return video


def rss_item_to_wechat_video(creator: Dict[str, Any], item: ET.Element) -> Optional[CreatorVideo]:
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    title = rss_child_text(item, ["title"])
    link = rss_child_text(item, ["link"])
    guid = rss_child_text(item, ["guid"])
    description = rss_child_text(item, [f"{content_ns}encoded", "description"])
    pub_date = rss_child_text(item, ["pubDate", "published", "updated"])
    stable = guid or link or title
    if not stable:
        return None
    article_id = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]
    original_text = strip_html_text(description)
    tags = creator.get("tags") or ["wechat", "公众号"]
    if not isinstance(tags, list):
        tags = ["wechat", "公众号"]
    raw = {
        "original_text": original_text,
        "description_text": original_text[:500],
        "rss_guid": guid,
    }
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=f"wechat_{article_id}",
        source_url=link or str(creator.get("url") or ""),
        title=compact_title(title, article_id),
        create_time=parse_datetime_to_timestamp(pub_date),
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="wechat",
    )


def wechat_mp_article_to_video(creator: Dict[str, Any], article: Dict[str, Any], fakeid: str) -> Optional[CreatorVideo]:
    title = clean_wechat_text(str(article.get("title") or ""))
    link = html.unescape(str(article.get("link") or "")).strip()
    stable = str(article.get("aid") or article.get("appmsgid") or article.get("msgid") or link or title).strip()
    if not stable:
        return None
    article_id = hashlib.sha1(f"{fakeid}:{stable}".encode("utf-8")).hexdigest()[:16]
    digest = strip_html_text(str(article.get("digest") or article.get("desc") or ""))
    tags = creator.get("tags") or ["wechat", "公众号"]
    if not isinstance(tags, list):
        tags = ["wechat", "公众号"]
    raw = dict(article)
    raw.update(
        {
            "original_text": "",
            "description_text": digest,
            "wechat_fakeid": fakeid,
            "wechat_biz": fakeid,
        }
    )
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=f"wechat_{article_id}",
        source_url=link,
        title=compact_title(title, article_id),
        create_time=parse_datetime_to_timestamp(article.get("create_time") or article.get("update_time")),
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="wechat",
    )


def extract_wechat_mp_articles(data: Dict[str, Any], creator: Dict[str, Any], fakeid: str) -> Tuple[List[CreatorVideo], int, int]:
    try:
        publish_page = json.loads(str(data.get("publish_page") or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("公众号后台文章列表返回格式异常：publish_page 不是 JSON") from exc
    publish_list = publish_page.get("publish_list") if isinstance(publish_page.get("publish_list"), list) else []
    total_count = int(publish_page.get("total_count") or 0)
    videos: List[CreatorVideo] = []
    message_count = 0
    for item in publish_list:
        if not isinstance(item, dict) or not item.get("publish_info"):
            continue
        try:
            publish_info = json.loads(str(item.get("publish_info") or "{}"))
        except json.JSONDecodeError:
            continue
        appmsgex = publish_info.get("appmsgex") if isinstance(publish_info.get("appmsgex"), list) else []
        if appmsgex:
            message_count += 1
        for article in appmsgex:
            if not isinstance(article, dict):
                continue
            video = wechat_mp_article_to_video(creator, article, fakeid)
            if video:
                videos.append(video)
    return videos, message_count, total_count


async def fetch_wechat_rss(creator: Dict[str, Any], rss_url: str, config: Dict[str, Any], scan_limit: int = 0) -> List[CreatorVideo]:
    fetch_cfg = config.get("fetch", {})
    timeout = httpx.Timeout(float(fetch_cfg.get("wechat_timeout_seconds", 30)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(rss_url, headers=wechat_headers(rss_url), follow_redirects=True)
        response.raise_for_status()
    root = ET.fromstring(response.content)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS 里没有 channel")
    videos: List[CreatorVideo] = []
    seen_ids: set[str] = set()
    for item in channel.findall("item"):
        video = rss_item_to_wechat_video(creator, item)
        if not video or video.video_id in seen_ids:
            continue
        if not str(video.raw.get("original_text") or "").strip() and "mp.weixin.qq.com" in video.source_url:
            try:
                video = await fetch_wechat_article(creator, video.source_url, config)
            except Exception:
                pass
        videos.append(video)
        seen_ids.add(video.video_id)
        if scan_limit and len(videos) >= scan_limit:
            break
    return videos


async def fetch_wechat_mp_articles(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    fakeid = wechat_fakeid_from_creator(creator)
    if not fakeid:
        raise RuntimeError("公众号来源缺少 fakeid。请在内容源里填写公众号名称并点 URL 补全。")
    cookie = read_secret_file(wechat_mp_cookie_path(config))
    token = read_secret_file(wechat_mp_token_path(config))
    if not cookie or not token:
        raise RuntimeError("公众号后台 Cookie/token 缺失。请先登录 mp.weixin.qq.com 后台，并用 Chrome 插件导入公众号后台 Cookie。")

    fetch_cfg = config.get("fetch", {})
    count_per_page = int(fetch_cfg.get("wechat_mp_count_per_page", 5))
    max_pages = int(fetch_cfg.get("wechat_mp_full_max_pages", 1000) if full_history else fetch_cfg.get("wechat_mp_max_pages", 10))
    delay = float(fetch_cfg.get("wechat_mp_delay_seconds", fetch_cfg.get("delay_seconds", 2)))
    timeout = httpx.Timeout(float(fetch_cfg.get("wechat_timeout_seconds", 30)), connect=10.0)

    videos: List[CreatorVideo] = []
    seen_ids: set[str] = set()
    begin = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for page in range(1, max_pages + 1):
            data = await fetch_wechat_mp_json(
                client,
                "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
                cookie=cookie,
                token=token,
                params={
                    "sub": "list",
                    "search_field": "null",
                    "begin": begin,
                    "count": count_per_page,
                    "query": "",
                    "fakeid": fakeid,
                    "type": "101_1",
                    "free_publish_type": 1,
                    "sub_action": "list_ex",
                },
            )
            page_videos, message_count, total_count = extract_wechat_mp_articles(data, creator, fakeid)
            for video in page_videos:
                if video.video_id in seen_ids:
                    continue
                videos.append(video)
                seen_ids.add(video.video_id)
                if scan_limit and len(videos) >= scan_limit:
                    if conn and run_id:
                        update_run(conn, run_id, current_stage=f"嗅探公众号后台 {page}/{max_pages}", detected_count=len(videos))
                    return videos
            if conn and run_id:
                stage_total = f" / {total_count}" if total_count else ""
                update_run(conn, run_id, current_stage=f"嗅探公众号后台 {page}/{max_pages}{stage_total}", detected_count=len(videos))
            print(f"WECHAT_MP_PAGE {page}/{max_pages} begin={begin} collected={len(videos)} total={total_count}")
            if not page_videos or message_count <= 0:
                break
            begin += message_count
            if total_count and begin >= total_count:
                break
            if page < max_pages and delay > 0:
                await asyncio.sleep(delay)
    return videos


async def fetch_wechat_articles(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    rss_url = str(creator.get("rss_url") or creator.get("feed_url") or "").strip()
    if rss_url:
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探公众号 RSS")
        videos = await fetch_wechat_rss(creator, rss_url, config, scan_limit if not full_history else 0)
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探公众号 RSS 完成", detected_count=len(videos))
        return videos

    if wechat_fakeid_from_creator(creator):
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探公众号后台")
        videos = await fetch_wechat_mp_articles(creator, config, scan_limit, full_history, conn, run_id)
        if conn and run_id:
            update_run(conn, run_id, current_stage="嗅探公众号后台完成", detected_count=len(videos))
        return videos

    url = str(creator.get("url") or "").strip()
    if "mp.weixin.qq.com" not in url:
        raise RuntimeError("公众号缺少可抓取信息。请填写单篇文章 URL、RSS URL，或输入公众号名称后点 URL 补全。")
    video = await fetch_wechat_article(creator, url, config)
    if conn and run_id:
        update_run(conn, run_id, current_stage="嗅探公众号文章", detected_count=1)
    return [video]


def xiaohongshu_headers(config: Dict[str, Any], referer: str = "https://www.xiaohongshu.com/") -> Dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
    }
    cookie = read_secret_file(xiaohongshu_cookie_path(config))
    if cookie:
        headers["Cookie"] = cookie
    return headers


def xhs_downloader_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("xhs_downloader")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "api_url": str(raw.get("api_url") or "http://127.0.0.1:5556/xhs/detail").strip(),
        "download": bool(raw.get("download", False)),
        "timeout_seconds": float(raw.get("timeout_seconds", config.get("fetch", {}).get("xiaohongshu_timeout_seconds", 30))),
        "proxy": str(raw.get("proxy") or "").strip(),
    }


def iter_nested_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_nested_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_nested_values(item)
    else:
        yield value


def first_nested_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in keys):
                text = str(item or "").strip() if not isinstance(item, (dict, list)) else ""
                if text:
                    return strip_html_text(text)
        for item in value.values():
            text = first_nested_text(item, keys)
            if text:
                return text
    elif isinstance(value, list):
        for item in value:
            text = first_nested_text(item, keys)
            if text:
                return text
    return ""


def nested_media_urls(value: Any, *, kind: str) -> list[str]:
    if kind == "video":
        pattern = re.compile(r"https?://[^\s\"'<>\\]+(?:mp4|m3u8)[^\s\"'<>\\]*", re.IGNORECASE)
    else:
        pattern = re.compile(r"https?://[^\s\"'<>\\]+(?:jpg|jpeg|png|webp|heic)[^\s\"'<>\\]*", re.IGNORECASE)
    urls: list[str] = []
    for item in iter_nested_values(value):
        if not isinstance(item, str):
            continue
        text = html.unescape(item).replace("\\u002F", "/").replace("\\/", "/")
        for match in pattern.findall(text):
            if "xhscdn" in match or "xiaohongshu" in match:
                urls.append(match)
    return list(dict.fromkeys(urls))


def nested_timestamp(value: Any) -> Optional[int]:
    for item in iter_nested_values(value):
        if isinstance(item, (int, float)):
            number = int(item)
            if 1_000_000_000_000 <= number <= 4_000_000_000_000:
                return number // 1000
            if 1_000_000_000 <= number <= 4_000_000_000:
                return number
        if isinstance(item, str) and item.isdigit():
            number = int(item)
            if 1_000_000_000_000 <= number <= 4_000_000_000_000:
                return number // 1000
            if 1_000_000_000 <= number <= 4_000_000_000:
                return number
    return None


def build_xiaohongshu_video_from_payload(creator: Dict[str, Any], url: str, payload: Dict[str, Any]) -> CreatorVideo:
    note_id = extract_xiaohongshu_note_id(url)
    if not note_id:
        for key in ("note_id", "noteId", "作品ID", "id"):
            value = first_nested_text(payload, (key.lower(),))
            if value and re.fullmatch(r"[A-Za-z0-9]{8,64}", value):
                note_id = value
                break
    note_id = note_id or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    title = (
        first_nested_text(payload, ("title", "displaytitle", "作品标题", "笔记标题"))
        or first_nested_text(payload, ("desc", "description", "作品描述", "笔记描述"))
    )
    title = clean_wechat_text(title).replace(" - 小红书", "").replace(" | 小红书", "").strip()
    original_text = (
        first_nested_text(payload, ("desc", "description", "content", "text", "作品描述", "笔记描述"))
        or title
    )
    author = (
        first_nested_text(payload, ("nickname", "author", "作者昵称", "用户名"))
        or str(creator.get("name") or creator["key"])
    )
    image_urls = nested_media_urls(payload, kind="image")
    video_urls = [item for item in nested_media_urls(payload, kind="video") if ".m3u8" not in item.lower()]
    tags = creator.get("tags") or ["xiaohongshu", "图文"]
    if not isinstance(tags, list):
        tags = ["xiaohongshu", "图文"]
    raw = {
        "original_text": strip_html_text(original_text),
        "description_text": strip_html_text(original_text),
        "image_urls": image_urls,
        "video_url": video_urls[0] if video_urls else "",
        "xhs_downloader": payload,
        "xhs_downloader_used": True,
    }
    raw.update(creator_runtime_options(creator))
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=clean_wechat_text(author) or str(creator.get("name") or creator["key"]),
        video_id=f"xhs_{note_id}",
        source_url=url,
        title=compact_title(title or strip_html_text(original_text)[:40] or "小红书笔记", note_id),
        create_time=nested_timestamp(payload),
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="xiaohongshu",
    )


async def fetch_xiaohongshu_note_with_xhs_downloader(creator: Dict[str, Any], url: str, config: Dict[str, Any]) -> Optional[CreatorVideo]:
    downloader = xhs_downloader_config(config)
    if not downloader["enabled"] or not downloader["api_url"]:
        return None
    cookie = read_secret_file(xiaohongshu_cookie_path(config))
    body: Dict[str, Any] = {
        "url": url,
        "download": downloader["download"],
        "skip": False,
    }
    if cookie:
        body["cookie"] = cookie
    if downloader["proxy"]:
        body["proxy"] = downloader["proxy"]
    timeout = httpx.Timeout(float(downloader["timeout_seconds"]), connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(str(downloader["api_url"]), json=body)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not payload:
        return None
    return build_xiaohongshu_video_from_payload(creator, url, payload)


def extract_xiaohongshu_note_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    for pattern in (r"/explore/([A-Za-z0-9]+)", r"/discovery/item/([A-Za-z0-9]+)", r"/item/([A-Za-z0-9]+)"):
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)
    query = parse_qs(parsed.query)
    for key in ("note_id", "noteId", "id"):
        value = str(query.get(key, [""])[0]).strip()
        if value:
            return value
    return ""


def xiaohongshu_note_url(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def is_xiaohongshu_profile_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return "xiaohongshu.com" in parsed.netloc and parsed.path.startswith("/user/profile/")


def cookie_header_to_playwright_cookies(cookie_header: str, domains: Iterable[str]) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for part in str(cookie_header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        for domain in domains:
            key = (domain, name)
            if key in seen:
                continue
            seen.add(key)
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
    return cookies


def extract_xiaohongshu_links(html_text: str, base_url: str) -> list[str]:
    links: list[str] = []
    for raw in re.findall(r'href=["\']([^"\']+)["\']', html_text or "", flags=re.IGNORECASE):
        if "/explore/" in raw or "/discovery/item/" in raw:
            link = urljoin(base_url, html.unescape(raw))
            if extract_xiaohongshu_note_id(link):
                links.append(link)
    for note_id in re.findall(r'/(?:explore|discovery/item)/([A-Za-z0-9]{8,32})', html_text or ""):
        links.append(xiaohongshu_note_url(note_id))
    return list(dict.fromkeys(links))


def browser_profile_dir(config: Dict[str, Any], platform: str) -> Path:
    raw = config.get("browser_profiles") if isinstance(config.get("browser_profiles"), dict) else {}
    value = str(raw.get(platform) or f"local_tools/obsidian_sync/work/browser_profiles/{platform}").strip()
    return project_path(value)


def normalize_xiaohongshu_note_link(url: str) -> str:
    note_id = extract_xiaohongshu_note_id(url)
    return xiaohongshu_note_url(note_id) if note_id else ""


async def xiaohongshu_page_shows_login(page: Any) -> bool:
    try:
        return bool(
            await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        const el = document.querySelector('#login-btn');
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                    }"""
                ),
                timeout=2,
            )
        )
    except Exception:
        return False


async def close_xiaohongshu_overlays(page: Any) -> None:
    try:
        clicked = await asyncio.wait_for(
            page.evaluate(
                """(texts) => {
                    const nodes = Array.from(document.querySelectorAll('button, [role="button"], .reds-button, .el-button'));
                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '').trim();
                        if (!texts.includes(text)) continue;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        if (style.visibility === 'hidden' || style.display === 'none' || rect.width <= 0 || rect.height <= 0) continue;
                        node.click();
                        return true;
                    }
                    return false;
                }""",
                ["我知道了", "知道了", "同意", "同意并继续"],
            ),
            timeout=2,
        )
        if clicked:
            await page.wait_for_timeout(500)
    except Exception:
        return


async def fetch_xiaohongshu_links_with_browser(
    creator: Dict[str, Any],
    url: str,
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> list[dict[str, str]]:
    fetch_cfg = config.get("fetch", {}) if isinstance(config.get("fetch"), dict) else {}
    cookie_header = read_secret_file(xiaohongshu_cookie_path(config))
    if not cookie_header or "web_session=" not in cookie_header:
        raise RuntimeError("小红书 Cookie 缺失。请先在当前 Chrome 登录小红书，再用本项目 Chrome 插件导入小红书 Cookie。")
    max_items = int(
        scan_limit
        or fetch_cfg.get("xiaohongshu_max_notes", 50)
        or 50
    )
    if full_history and not scan_limit:
        max_items = int(fetch_cfg.get("xiaohongshu_full_max_notes", 1000) or 1000)
    max_scrolls = int(fetch_cfg.get("xiaohongshu_scroll_pages", 12) or 12)
    if full_history and not scan_limit:
        max_scrolls = int(fetch_cfg.get("xiaohongshu_full_scroll_pages", 80) or 80)
    wait_ms = int(float(fetch_cfg.get("xiaohongshu_scroll_wait_seconds", 1.4) or 1.4) * 1000)
    headless = bool(fetch_cfg.get("xiaohongshu_browser_headless", False))
    creator_name = str(creator.get("name") or creator.get("key") or "小红书")

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        print("WARN playwright not installed; skip xiaohongshu browser sniff")
        return []

    items_by_id: dict[str, dict[str, str]] = {}
    async with async_playwright() as playwright:
        browser: Any = None
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "timeout": int(float(fetch_cfg.get("xiaohongshu_launch_timeout_seconds", 30) or 30) * 1000),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            try:
                browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1100},
                user_agent=xiaohongshu_headers(config).get("User-Agent"),
            )
            cookies = cookie_header_to_playwright_cookies(
                cookie_header,
                [".xiaohongshu.com", "www.xiaohongshu.com", "xiaohongshu.com", ".xhslink.com", "xhslink.com"],
            )
            if cookies:
                await context.add_cookies(cookies)
        except Exception as exc:
            raise RuntimeError("小红书临时浏览器会话启动失败。请确认 Chrome 可正常打开，并重新导入小红书 Cookie 后再试。") from exc
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            goto_timeout_ms = int(float(fetch_cfg.get("xiaohongshu_goto_timeout_seconds", 25) or 25) * 1000)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
            except Exception as exc:
                raise RuntimeError(
                    "小红书页面打开超时。通常是 Cookie 未被页面认可、页面卡在验证，或网络请求被小红书限制；请在当前 Chrome 登录小红书并用插件重新导入 Cookie。"
                ) from exc
            await page.wait_for_timeout(2500)
            await close_xiaohongshu_overlays(page)
            if await xiaohongshu_page_shows_login(page):
                raise RuntimeError("小红书 Cookie 未被页面认可：页面仍显示登录按钮。请在当前 Chrome 登录小红书，并用本项目 Chrome 插件重新导入小红书 Cookie。")
            stable_rounds = 0
            last_count = 0
            for scroll_index in range(max_scrolls + 1):
                if conn and run_id:
                    update_run(
                        conn,
                        run_id,
                        current_stage=f"浏览器嗅探小红书 {scroll_index + 1}/{max_scrolls + 1}",
                        current_creator=creator_name,
                        detected_count=len(items_by_id),
                    )
                try:
                    raw_items = await asyncio.wait_for(
                        page.evaluate(
                            """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                                href: a.href || '',
                                text: (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim()
                            }))"""
                        ),
                        timeout=5,
                    )
                except Exception:
                    raw_items = []
                if isinstance(raw_items, list):
                    for raw in raw_items:
                        if not isinstance(raw, dict):
                            continue
                        link = normalize_xiaohongshu_note_link(str(raw.get("href") or ""))
                        if not link:
                            continue
                        note_id = extract_xiaohongshu_note_id(link)
                        if not note_id:
                            continue
                        title = clean_wechat_text(str(raw.get("text") or "")).splitlines()[0].strip()
                        items_by_id.setdefault(note_id, {"url": link, "title": title})
                try:
                    html_text = await asyncio.wait_for(page.content(), timeout=5)
                except Exception:
                    html_text = ""
                for link in extract_xiaohongshu_links(html_text, str(page.url)):
                    note_id = extract_xiaohongshu_note_id(link)
                    if note_id:
                        items_by_id.setdefault(note_id, {"url": normalize_xiaohongshu_note_link(link), "title": ""})
                if len(items_by_id) >= max_items:
                    break
                if len(items_by_id) == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_count = len(items_by_id)
                if stable_rounds >= 6:
                    break
                try:
                    await asyncio.wait_for(page.mouse.wheel(0, 2400), timeout=3)
                except Exception:
                    break
                await page.wait_for_timeout(wait_ms)
        finally:
            await context.close()
            if browser:
                await browser.close()

    items = list(items_by_id.values())
    if max_items:
        items = items[:max_items]
    return items


def extract_media_urls(html_text: str, *, kind: str) -> list[str]:
    if kind == "video":
        pattern = r'https?://[^"\'\\\s<>]+(?:mp4|m3u8)[^"\'\\\s<>]*'
    else:
        pattern = r'https?://[^"\'\\\s<>]+(?:jpg|jpeg|png|webp)[^"\'\\\s<>]*'
    urls = []
    for raw in re.findall(pattern, html_text or "", flags=re.IGNORECASE):
        value = html.unescape(raw).replace("\\u002F", "/").replace("\\/", "/")
        if "xhscdn" in value or "xiaohongshu" in value:
            urls.append(value)
    return list(dict.fromkeys(urls))


def parse_xiaohongshu_note_html(creator: Dict[str, Any], url: str, html_text: str) -> CreatorVideo:
    note_id = extract_xiaohongshu_note_id(url) or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text or "", flags=re.IGNORECASE | re.DOTALL)
    title = clean_wechat_text(
        extract_meta(html_text, "og:title")
        or extract_meta(html_text, "twitter:title")
        or (re.sub(r"<[^>]+>", "", title_match.group(1)) if title_match else "")
    )
    title = title.replace(" - 小红书", "").replace(" | 小红书", "").strip()
    description = strip_html_text(
        extract_meta(html_text, "description")
        or extract_meta(html_text, "og:description")
        or extract_meta(html_text, "twitter:description")
    )
    names = re.findall(r'"nickname"\s*:\s*"([^"]+)"', html_text or "")
    creator_name = clean_wechat_text(names[0]) if names else str(creator.get("name") or creator["key"])
    image_urls = extract_media_urls(html_text, kind="image")
    video_urls = [url for url in extract_media_urls(html_text, kind="video") if ".m3u8" not in url.lower()]
    tags = creator.get("tags") or ["xiaohongshu", "图文"]
    if not isinstance(tags, list):
        tags = ["xiaohongshu", "图文"]
    raw = {
        "original_text": description,
        "description_text": description,
        "image_urls": image_urls,
        "video_url": video_urls[0] if video_urls else "",
    }
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=creator_name,
        video_id=f"xhs_{note_id}",
        source_url=url,
        title=compact_title(title or description[:40] or "小红书笔记", note_id),
        create_time=None,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="xiaohongshu",
    )


async def fetch_xiaohongshu_note(creator: Dict[str, Any], url: str, config: Dict[str, Any]) -> CreatorVideo:
    try:
        video = await fetch_xiaohongshu_note_with_xhs_downloader(creator, url, config)
        if video:
            return video
    except Exception as exc:  # noqa: BLE001 - keep current parser as fallback.
        print(f"WARN xhs_downloader unavailable, fallback to built-in parser: {type(exc).__name__}: {exc}")
    timeout = httpx.Timeout(float(config.get("fetch", {}).get("xiaohongshu_timeout_seconds", 30)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=xiaohongshu_headers(config, url), follow_redirects=True)
        response.raise_for_status()
        final_url = str(response.url)
        if "/404" in urlparse(final_url).path or "你访问的页面不见了" in response.text[:5000]:
            raise RuntimeError("小红书详情页返回 404/验证页，请用 Chrome 插件在真实页面收集候选链接后再抓取")
        return parse_xiaohongshu_note_html(creator, final_url, response.text)


def xiaohongshu_placeholder_video(creator: Dict[str, Any], url: str, title: str = "") -> CreatorVideo:
    note_id = extract_xiaohongshu_note_id(url) or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    tags = creator.get("tags") or ["xiaohongshu", "图文"]
    if not isinstance(tags, list):
        tags = ["xiaohongshu", "图文"]
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=f"xhs_{note_id}",
        source_url=url,
        title=compact_title(title or f"小红书笔记 {note_id}", note_id),
        create_time=None,
        raw={
            "original_text": "",
            "image_urls": [],
            "video_url": "",
            "manual_candidate": True,
        },
        tags=[str(tag) for tag in tags],
        platform="xiaohongshu",
    )


async def fetch_xiaohongshu_notes(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    url = str(creator.get("url") or "").strip()
    manual_items = creator.get("manual_items") or []
    if isinstance(manual_items, list) and manual_items:
        links = []
        title_by_url: dict[str, str] = {}
        for item in manual_items:
            if not isinstance(item, dict):
                continue
            item_url = str(item.get("url") or "").strip()
            if not item_url:
                continue
            links.append(item_url)
            title_by_url[item_url] = clean_wechat_text(str(item.get("title") or ""))
        if scan_limit:
            links = links[:scan_limit]
        videos: List[CreatorVideo] = []
        seen: set[str] = set()
        for link in links:
            video = xiaohongshu_placeholder_video(creator, link, title_by_url.get(link, ""))
            if video.video_id not in seen:
                videos.append(video)
                seen.add(video.video_id)
        return videos

    manual_urls = creator.get("manual_urls") or []
    if isinstance(manual_urls, str):
        manual_urls = [item.strip() for item in manual_urls.splitlines() if item.strip()]
    if isinstance(manual_urls, list) and manual_urls:
        links = [str(item).strip() for item in manual_urls if str(item).strip()]
        if scan_limit:
            links = links[:scan_limit]
        videos = []
        seen = set()
        for link in links:
            video = xiaohongshu_placeholder_video(creator, link)
            if video.video_id not in seen:
                videos.append(video)
                seen.add(video.video_id)
        return videos
    else:
        links = []
    if not links:
        browser_items = await fetch_xiaohongshu_links_with_browser(
            creator,
            url,
            config,
            scan_limit,
            full_history,
            conn,
            run_id,
        )
        if browser_items:
            videos = []
            seen = set()
            for item in browser_items:
                link = str(item.get("url") or "").strip()
                if not link:
                    continue
                video = xiaohongshu_placeholder_video(creator, link, str(item.get("title") or ""))
                if video.video_id not in seen:
                    videos.append(video)
                    seen.add(video.video_id)
            return videos
        if is_xiaohongshu_profile_url(url):
            raise RuntimeError(
                "小红书页面已打开，但没有嗅探到任何笔记。通常是当前浏览器登录态未被小红书认可、页面卡在验证，"
                "或瀑布流没有正常渲染；请在当前 Chrome 确认小红书已登录，并用本项目 Chrome 插件重新导入小红书 Cookie。"
            )
        timeout = httpx.Timeout(float(config.get("fetch", {}).get("xiaohongshu_timeout_seconds", 30)), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=xiaohongshu_headers(config, url), follow_redirects=True)
            response.raise_for_status()
            final_url = str(response.url)
            html_text = response.text
        if extract_xiaohongshu_note_id(final_url):
            return [parse_xiaohongshu_note_html(creator, final_url, html_text)]
        links = extract_xiaohongshu_links(html_text, final_url)
        if scan_limit:
            links = links[:scan_limit]
        elif not full_history:
            links = links[: int(config.get("fetch", {}).get("xiaohongshu_max_notes", 50))]
    videos: List[CreatorVideo] = []
    seen: set[str] = set()
    delay = float(config.get("fetch", {}).get("xiaohongshu_delay_seconds", config.get("fetch", {}).get("delay_seconds", 2)))
    for index, link in enumerate(links, start=1):
        if conn and run_id:
            update_run(conn, run_id, current_stage=f"嗅探小红书 {index}/{len(links)}", detected_count=len(videos))
        video = xiaohongshu_placeholder_video(creator, link)
        if video.video_id not in seen:
            videos.append(video)
            seen.add(video.video_id)
        if delay > 0 and index < len(links):
            await asyncio.sleep(delay)
    return videos


def build_platform_registry() -> PlatformRegistry:
    registry = PlatformRegistry()
    return registry


def extract_weibo_uid_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    value = query.get("uid", [""])[0].strip()
    if value.isdigit():
        return value
    path = parsed.path.strip("/")
    for pattern in (r"^u/(\d+)", r"^p/100505(\d+)", r"^(\d{5,})$"):
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    return ""


def extract_weibo_custom_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    path = parsed.path.strip("/")
    if not path:
        return ""
    if re.match(r"^(u|p|n)/", path) or re.match(r"^\d{5,}$", path):
        return ""
    first = path.split("/", 1)[0].strip()
    if first in {"ajax", "login", "signup", "tv", "newlogin"}:
        return ""
    return first


def parse_weibo_created_at(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        raw = int(text)
        return raw // 1000 if raw > 10_000_000_000 else raw
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def is_weibo_login_required(data: Dict[str, Any]) -> bool:
    if data.get("ok") == -100:
        return True
    redirect_url = str(data.get("url") or "").lower()
    return any(marker in redirect_url for marker in ("weibo.com/login", "passport.weibo.com", "login.sina.com.cn"))


def weibo_headers(cookie: str, referer: str = "https://weibo.com/") -> Dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name in {"XSRF-TOKEN", "X-CSRF-TOKEN"} and value:
                headers["X-XSRF-TOKEN"] = value
                headers["X-CSRF-TOKEN"] = value
                break
    return headers


async def fetch_weibo_json(client: httpx.AsyncClient, url: str, cookie: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = await client.get(url, params=params, headers=weibo_headers(cookie), follow_redirects=True)
    response.raise_for_status()
    text = response.text
    if "Sina Visitor System" in text:
        raise RuntimeError("微博要求访客验证。请重新用 Chrome 插件导入微博 Cookie 后再试。")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("微博接口返回了非 JSON 对象")
    if is_weibo_login_required(data):
        raise RuntimeError("微博 Cookie 已失效或不被服务端认可。请重新登录 weibo.com 后用插件导入微博 Cookie。")
    return data


async def resolve_weibo_uid(creator: Dict[str, Any], cookie: str) -> str:
    for field in ("weibo_uid", "platform_id"):
        value = str(creator.get(field) or "").strip()
        if value.isdigit():
            return value

    uid = extract_weibo_uid_from_url(str(creator.get("url") or ""))
    if uid:
        return uid

    custom = str(creator.get("weibo_custom") or "").strip() or extract_weibo_custom_from_url(str(creator.get("url") or ""))
    custom = custom.strip("/")
    if custom.startswith("u/"):
        candidate = custom.split("/", 1)[1]
        if candidate.isdigit():
            return candidate
    if not custom:
        raise RuntimeError(f"微博来源缺少 uid: {creator.get('name') or creator.get('url')}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        data = await fetch_weibo_json(
            client,
            "https://weibo.com/ajax/profile/info",
            cookie,
            params={"custom": custom},
        )
    user = (data.get("data") or {}).get("user") if isinstance(data.get("data"), dict) else None
    if isinstance(user, dict):
        uid = str(user.get("idstr") or user.get("id") or "").strip()
        if uid.isdigit():
            return uid
    raise RuntimeError(f"无法解析微博 uid: {creator.get('name') or creator.get('url')}")


def extract_weibo_statuses(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    statuses: List[Dict[str, Any]] = []
    has_more = False
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if isinstance(inner.get("list"), list):
        statuses = [item for item in inner["list"] if isinstance(item, dict)]
        has_more = bool(inner.get("since_id") or inner.get("next_cursor") or len(statuses) > 0)
    elif isinstance(inner.get("cards"), list):
        for card in inner["cards"]:
            if isinstance(card, dict) and isinstance(card.get("mblog"), dict):
                statuses.append(card["mblog"])
        cardlist = inner.get("cardlistInfo") if isinstance(inner.get("cardlistInfo"), dict) else {}
        has_more = bool(cardlist.get("since_id") or len(statuses) > 0)
    return statuses, has_more


def weibo_image_urls(status: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    pics = status.get("pics")
    if isinstance(pics, list):
        for pic in pics:
            if not isinstance(pic, dict):
                continue
            candidates = [
                ((pic.get("large") or {}).get("url") if isinstance(pic.get("large"), dict) else ""),
                ((pic.get("mw2000") or {}).get("url") if isinstance(pic.get("mw2000"), dict) else ""),
                pic.get("url"),
            ]
            for candidate in candidates:
                if candidate:
                    urls.append(str(candidate))
                    break
    return list(dict.fromkeys(urls))


def weibo_original_text(status: Dict[str, Any]) -> str:
    text = strip_html_text(str(status.get("text_raw") or status.get("text") or ""))
    retweeted = status.get("retweeted_status")
    if isinstance(retweeted, dict):
        author = ""
        user = retweeted.get("user")
        if isinstance(user, dict):
            author = str(user.get("screen_name") or user.get("name") or "").strip()
        repost_text = strip_html_text(str(retweeted.get("text_raw") or retweeted.get("text") or ""))
        if repost_text:
            prefix = f"转发原文（{author}）" if author else "转发原文"
            text = f"{text}\n\n> {prefix}：{repost_text}".strip()
    images = weibo_image_urls(status)
    if images:
        image_lines = "\n".join(f"- {url}" for url in images)
        text = f"{text}\n\n图片：\n{image_lines}".strip()
    return text


def weibo_status_url(uid: str, status: Dict[str, Any], post_id: str) -> str:
    mblogid = str(status.get("mblogid") or status.get("bid") or "").strip()
    if uid and mblogid:
        return f"https://weibo.com/{uid}/{mblogid}"
    raw_url = str(status.get("url") or "").strip()
    if raw_url.startswith("http"):
        return raw_url
    return f"https://m.weibo.cn/detail/{post_id}"


def weibo_status_to_video(creator: Dict[str, Any], status: Dict[str, Any], uid: str) -> Optional[CreatorVideo]:
    raw_id = status.get("idstr") or status.get("id") or status.get("mid")
    if not raw_id:
        return None
    post_id = str(raw_id)
    original_text = weibo_original_text(status)
    if not original_text:
        return None
    title = compact_title(original_text, post_id)
    create_time = parse_weibo_created_at(status.get("created_at") or status.get("created_timestamp"))
    tags = creator.get("tags") or ["weibo", "文字"]
    if not isinstance(tags, list):
        tags = ["weibo", "文字"]
    raw = dict(status)
    raw["original_text"] = original_text
    raw["weibo_uid"] = uid

    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=f"weibo_{post_id}",
        source_url=weibo_status_url(uid, status, post_id),
        title=title,
        create_time=create_time,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="weibo",
    )


async def fetch_weibo_posts(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    cookie = read_secret_file(weibo_cookie_profile_path(config, creator))
    if not cookie:
        raise RuntimeError("微博 Cookie 缺失。请先用 Chrome 插件导入微博 Cookie。")
    uid = await resolve_weibo_uid(creator, cookie)
    fetch_cfg = config.get("fetch", {})
    if full_history:
        max_pages = int(fetch_cfg.get("weibo_full_max_pages", 350))
    else:
        max_pages = int(fetch_cfg.get("weibo_max_pages", 5))
    count_per_page = int(fetch_cfg.get("weibo_count_per_page", fetch_cfg.get("count_per_page", 20)))
    delay = float(fetch_cfg.get("weibo_delay_seconds", fetch_cfg.get("delay_seconds", 2)))

    videos: List[CreatorVideo] = []
    seen_ids: set[str] = set()
    timeout = httpx.Timeout(float(fetch_cfg.get("weibo_timeout_seconds", 20)), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for page in range(1, max_pages + 1):
            data = await fetch_weibo_json(
                client,
                "https://weibo.com/ajax/statuses/mymblog",
                cookie,
                params={"uid": uid, "page": page, "feature": 0, "page_size": count_per_page},
            )
            statuses, has_more = extract_weibo_statuses(data)
            for status in statuses:
                video = weibo_status_to_video(creator, status, uid)
                if video and video.video_id not in seen_ids:
                    videos.append(video)
                    seen_ids.add(video.video_id)
                    if scan_limit and len(videos) >= scan_limit:
                        if conn and run_id:
                            update_run(
                                conn,
                                run_id,
                                current_stage=f"嗅探微博 {page}/{max_pages}",
                                detected_count=len(videos),
                            )
                        return videos
            if conn and run_id:
                update_run(
                    conn,
                    run_id,
                    current_stage=f"嗅探微博 {page}/{max_pages}",
                    detected_count=len(videos),
                )
            print(f"WEIBO_PAGE {page}/{max_pages} collected={len(videos)}")
            if not has_more or not statuses:
                break
            if page < max_pages and delay > 0:
                await asyncio.sleep(delay)
    return videos


def import_yt_dlp() -> Any:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("YouTube 抓取需要 yt-dlp。请运行 `.venv/bin/python -m pip install -r requirements-obsidian.txt`。") from exc
    return yt_dlp


def youtube_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("youtube") if isinstance(config.get("youtube"), dict) else {}
    fetch_cfg = config.get("fetch") if isinstance(config.get("fetch"), dict) else {}
    return {
        "max_videos": int(raw.get("max_videos", fetch_cfg.get("youtube_max_videos", 30)) or 30),
        "full_max_videos": int(raw.get("full_max_videos", fetch_cfg.get("youtube_full_max_videos", 300)) or 300),
        "subtitle_languages": raw.get("subtitle_languages") or ["en", "en-US", "en-GB", "zh-Hans", "zh-CN", "zh"],
        "audio_timeout_seconds": float(raw.get("audio_timeout_seconds", 600) or 600),
    }


def normalize_youtube_video_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.netloc:
        query = parse_qs(parsed.query)
        video_id = query.get("v", [""])[0].strip()
        if video_id:
            return video_id
        path = parsed.path.strip("/")
        if parsed.netloc.endswith("youtu.be"):
            return path.split("/")[0]
        if path.startswith("shorts/"):
            return path.split("/", 1)[1].split("/")[0]
    if re.fullmatch(r"[\w-]{8,}", text):
        return text
    return ""


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def ytdlp_extract_info(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    yt_dlp = import_yt_dlp()
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "skip_download": True,
    }
    base_opts.update(opts)
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp 没有返回 YouTube 数据。请确认 URL 可访问。")
    return info


def youtube_entry_to_video(creator: Dict[str, Any], entry: Dict[str, Any]) -> Optional[CreatorVideo]:
    video_id = normalize_youtube_video_id(entry.get("id") or entry.get("url") or entry.get("webpage_url"))
    if not video_id:
        return None
    title = compact_title(str(entry.get("title") or ""), video_id)
    timestamp = entry.get("timestamp") or entry.get("release_timestamp")
    try:
        create_time = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        create_time = None
    tags = creator.get("tags") or ["youtube", "视频"]
    if not isinstance(tags, list):
        tags = ["youtube", "视频"]
    raw = dict(entry)
    raw.update(creator_runtime_options(creator))
    raw["creator_language"] = str(creator.get("language") or "")
    raw["source_language"] = str(creator.get("source_language") or creator.get("language") or "auto")
    raw["output_language"] = str(creator.get("output_language") or "中文")
    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=f"youtube_{video_id}",
        source_url=youtube_watch_url(video_id),
        title=title,
        create_time=create_time,
        raw=raw,
        tags=[str(tag) for tag in tags],
        platform="youtube",
    )


async def fetch_youtube_videos(
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    cfg = youtube_config(config)
    max_items = int(scan_limit or (cfg["full_max_videos"] if full_history else cfg["max_videos"]))
    max_items = max(1, max_items)
    url = str(creator.get("url") or "").strip()
    if not url:
        raise RuntimeError("YouTube 内容源 URL 为空")
    if "youtube.com/@" in url and not re.search(r"/(videos|shorts|streams)(?:$|[/?#])", url):
        url = url.rstrip("/") + "/videos"
    info = await asyncio.to_thread(
        ytdlp_extract_info,
        url,
        {
            "extract_flat": "in_playlist",
            "playlistend": max_items,
        },
    )
    entries = info.get("entries") if isinstance(info.get("entries"), list) else None
    if entries is None:
        entries = [info]
    videos: List[CreatorVideo] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        if conn and run_id:
            update_run(conn, run_id, current_stage=f"嗅探 YouTube {index}/{len(entries)}", detected_count=len(videos))
        video = youtube_entry_to_video(creator, entry)
        if video and video.video_id not in seen:
            videos.append(video)
            seen.add(video.video_id)
        if len(videos) >= max_items:
            break
    return videos


async def fetch_creator_videos(
    crawler: Any,
    creator: Dict[str, Any],
    config: Dict[str, Any],
    scan_limit: int = 0,
    full_history: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> List[CreatorVideo]:
    platform = creator_platform(creator)
    registry = build_platform_registry()
    if registry.has(platform):
        return await registry.get(platform).sniff(
            creator,
            PlatformRunContext(
                config=config,
                scan_limit=scan_limit,
                full_history=full_history,
                conn=conn,
                run_id=run_id,
            ),
        )
    if platform == "weibo":
        return await fetch_weibo_posts(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "x":
        return await fetch_x_posts(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "xiaoyuzhou":
        return await fetch_xiaoyuzhou_episodes(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "wechat":
        return await fetch_wechat_articles(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "youtube":
        return await fetch_youtube_videos(creator, config, scan_limit, full_history, conn, run_id)

    fetch_cfg = config.get("fetch", {})
    sec_user_id = creator.get("sec_user_id")
    if not sec_user_id:
        sec_user_id = await crawler.DouyinWebCrawler.get_sec_user_id(str(creator["url"]))

    count = int(fetch_cfg.get("count_per_page", 20))
    if full_history:
        max_pages = int(fetch_cfg.get("douyin_full_max_pages", fetch_cfg.get("max_pages", 50)))
    else:
        max_pages = int(fetch_cfg.get("max_pages", 2))
    delay = float(fetch_cfg.get("delay_seconds", 2))

    videos: List[CreatorVideo] = []
    cursor = int(creator.get("max_cursor", 0) or 0)
    seen_ids = set()

    for page in range(max_pages):
        response = await crawler.DouyinWebCrawler.fetch_user_post_videos(str(sec_user_id), cursor, count)
        items, next_cursor, has_more = extract_aweme_items(response)
        for item in items:
            video = aweme_to_video(creator, item)
            if video and video.video_id not in seen_ids:
                videos.append(video)
                seen_ids.add(video.video_id)

        if not has_more or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
        if page < max_pages - 1 and delay > 0:
            await asyncio.sleep(delay)

    return videos


async def download_video_to_temp(crawler: Any, video: CreatorVideo, cfg: Dict[str, Any], temp_dir: Path) -> Path:
    parsed = await crawler.hybrid_parsing_single_video(video.source_url, minimal=True)
    if parsed.get("type") != "video":
        raise RuntimeError(f"Not a video post: {video.video_id}")

    video_data = parsed.get("video_data") or {}
    watermark = str(cfg.get("watermark", "keep"))
    key = "wm_video_url_HQ" if watermark == "keep" else "nwm_video_url_HQ"
    video_url = video_data.get(key) or video_data.get(key.replace("_HQ", ""))
    if not video_url:
        raise RuntimeError(f"No playable video URL returned for {video.video_id}")

    temp_path = temp_dir / f"{video.video_id}.mp4"
    headers = await headers_for_platform(crawler, "douyin")
    timeout = float(cfg.get("timeout_seconds", 120))
    retry_attempts = max(1, int(cfg.get("retry_attempts", 3)))
    retry_delay = max(0.0, float(cfg.get("retry_delay_seconds", 5)))
    retryable_errors = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.HTTPError,
    )

    for attempt in range(1, retry_attempts + 1):
        try:
            await stream_to_file(str(video_url), temp_path, headers, timeout)
            return temp_path
        except retryable_errors as exc:
            for partial_path in (temp_path, temp_path.with_suffix(temp_path.suffix + ".part")):
                if partial_path.exists():
                    partial_path.unlink()
            if attempt >= retry_attempts:
                raise RuntimeError(
                    f"视频下载中断，已自动重试 {retry_attempts} 次仍失败。"
                    "通常是网络波动、抖音临时限流，或该视频链接短时间失效。"
                    f"原始错误：{type(exc).__name__}: {exc}"
                ) from exc
            print(
                f"RETRY {video.video_id} 下载中断，"
                f"第 {attempt}/{retry_attempts} 次失败，{retry_delay:g} 秒后自动重试"
            )
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)

    return temp_path


def audio_suffix_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in (".mp3", ".m4a", ".mp4", ".aac", ".wav", ".ogg"):
        if path.endswith(suffix):
            return suffix
    return ".audio"


async def download_audio_to_temp(video: CreatorVideo, cfg: Dict[str, Any], temp_dir: Path) -> Path:
    audio_url = str(video.raw.get("audio_url") or "").strip()
    if not audio_url:
        raise RuntimeError(f"没有拿到播客音频地址：{video.video_id}")

    temp_path = temp_dir / f"{video.video_id}{audio_suffix_from_url(audio_url)}"
    headers = xiaoyuzhou_headers(video.source_url)
    timeout = float(cfg.get("timeout_seconds", cfg.get("podcast_timeout_seconds", 300)))
    retry_attempts = max(1, int(cfg.get("retry_attempts", 3)))
    retry_delay = max(0.0, float(cfg.get("retry_delay_seconds", 5)))
    retryable_errors = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.HTTPError,
    )

    for attempt in range(1, retry_attempts + 1):
        try:
            await stream_to_file(audio_url, temp_path, headers, timeout)
            return temp_path
        except retryable_errors as exc:
            for partial_path in (temp_path, temp_path.with_suffix(temp_path.suffix + ".part")):
                if partial_path.exists():
                    partial_path.unlink()
            if attempt >= retry_attempts:
                raise RuntimeError(
                    f"播客音频下载中断，已自动重试 {retry_attempts} 次仍失败。"
                    "通常是网络波动、音频源限流，或该单集音频链接短时间失效。"
                    f"原始错误：{type(exc).__name__}: {exc}"
                ) from exc
            print(
                f"RETRY {video.video_id} 播客音频下载中断，"
                f"第 {attempt}/{retry_attempts} 次失败，{retry_delay:g} 秒后自动重试"
            )
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)

    return temp_path


def youtube_language_preferences(video: CreatorVideo, config: Dict[str, Any]) -> List[str]:
    cfg = youtube_config(config)
    raw_languages = cfg.get("subtitle_languages") or []
    source_language = str(video.raw.get("source_language") or video.raw.get("creator_language") or "").lower()
    if "中文" in source_language or source_language in {"zh", "zh-cn", "zh-hans"}:
        preferred = ["zh-Hans", "zh-CN", "zh", "en", "en-US", "en-GB"]
    elif "英文" in source_language or source_language in {"en", "en-us", "en-gb", "english"}:
        preferred = ["en", "en-US", "en-GB", "zh-Hans", "zh-CN", "zh"]
    else:
        preferred = []
    merged = [*preferred, *[str(item) for item in raw_languages]]
    return list(dict.fromkeys([item for item in merged if item]))


def select_youtube_caption(info: Dict[str, Any], languages: List[str]) -> Tuple[str, Dict[str, Any]]:
    pools = [
        info.get("subtitles") if isinstance(info.get("subtitles"), dict) else {},
        info.get("automatic_captions") if isinstance(info.get("automatic_captions"), dict) else {},
    ]
    for pool in pools:
        for language in languages:
            formats = pool.get(language)
            if not isinstance(formats, list):
                continue
            preferred = [item for item in formats if isinstance(item, dict) and item.get("url") and item.get("ext") == "vtt"]
            if preferred:
                return language, preferred[0]
    for pool in pools:
        for language, formats in pool.items():
            if not isinstance(formats, list):
                continue
            for item in formats:
                if isinstance(item, dict) and item.get("url") and item.get("ext") == "vtt":
                    return str(language), item
    return "", {}


def strip_vtt_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt_transcript(text: str) -> str:
    lines = text.replace("\ufeff", "").splitlines()
    cues: List[str] = []
    current_time = ""
    current_text: List[str] = []

    def flush() -> None:
        nonlocal current_time, current_text
        content = " ".join(strip_vtt_tags(line) for line in current_text if strip_vtt_tags(line)).strip()
        if content:
            cues.append(f"{current_time} {content}".strip())
        current_time = ""
        current_text = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            if current_text:
                flush()
            continue
        if "-->" in line:
            if current_text:
                flush()
            start, end = line.split("-->", 1)
            current_time = f"[{start.strip().split()[0]} - {end.strip().split()[0]}]"
            continue
        if re.fullmatch(r"\d+", line):
            continue
        current_text.append(line)
    if current_text:
        flush()

    deduped: List[str] = []
    last_text = ""
    for cue in cues:
        cue_text = re.sub(r"^\[[^\]]+\]\s*", "", cue).strip()
        if cue_text and cue_text != last_text:
            deduped.append(cue)
            last_text = cue_text
    return "\n".join(deduped).strip()


async def fetch_youtube_subtitle(video: CreatorVideo, info: Dict[str, Any], config: Dict[str, Any]) -> Tuple[str, str]:
    language, caption = select_youtube_caption(info, youtube_language_preferences(video, config))
    if not caption:
        return "", ""
    url = str(caption.get("url") or "")
    if not url:
        return "", ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    transcript = parse_vtt_transcript(response.text)
    return transcript, language


def download_youtube_audio_sync(video: CreatorVideo, config: Dict[str, Any], temp_dir: Path) -> Path:
    yt_dlp = import_yt_dlp()
    target_template = str(temp_dir / f"{video.video_id}.%(ext)s")
    before = set(temp_dir.glob(f"{video.video_id}.*"))
    with yt_dlp.YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "outtmpl": target_template,
            "noplaylist": True,
            "socket_timeout": float(youtube_config(config)["audio_timeout_seconds"]),
        }
    ) as ydl:
        ydl.download([video.source_url])
    after = set(temp_dir.glob(f"{video.video_id}.*"))
    created = [path for path in after - before if path.exists() and not path.name.endswith(".part")]
    if not created:
        created = [path for path in after if path.exists() and not path.name.endswith(".part")]
    if not created:
        raise RuntimeError("YouTube 音频下载失败：没有找到下载后的音频文件")
    return max(created, key=lambda path: path.stat().st_size)


def extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
            if Path(candidate).exists():
                ffmpeg = candidate
                break
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install it first, e.g. `brew install ffmpeg`.")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def transcribe_audio(audio_path: Path, cfg: Dict[str, Any]) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed. Run `bash local_tools/setup_obsidian_sync.sh`.") from exc

    model_name = str(cfg.get("model", "small"))
    device = str(cfg.get("device", "cpu"))
    compute_type = str(cfg.get("compute_type", "int8"))
    cache_key = (model_name, device, compute_type)
    with WHISPER_MODEL_LOCK:
        model = WHISPER_MODEL_CACHE.get(cache_key)
        if model is None:
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            WHISPER_MODEL_CACHE[cache_key] = model
        language = str(cfg.get("language", "zh")).strip()
        language_arg = None if language.lower() in {"", "auto", "none"} else language
        segments, _info = model.transcribe(
            str(audio_path),
            language=language_arg,
            vad_filter=bool(cfg.get("vad_filter", True)),
        )

        lines: List[str] = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                lines.append(f"[{format_seconds(segment.start)} - {format_seconds(segment.end)}] {text}")

    transcript = "\n".join(lines).strip()
    if not transcript:
        raise RuntimeError("Transcription returned empty text")
    return transcript


def summary_config_for_video(video: CreatorVideo, config: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(config.get("summary", {}) or {})
    platform_configs = config.get("summary_by_platform") or {}
    if isinstance(platform_configs, dict):
        override = platform_configs.get(video.platform)
        if isinstance(override, dict):
            base.update(override)
    if video.platform == "xiaoyuzhou":
        podcast_cfg = config.get("podcast_summary")
        if isinstance(podcast_cfg, dict):
            base.update(podcast_cfg)
    return base


def summary_prompt_path(video: CreatorVideo, cfg: Dict[str, Any]) -> Path:
    prompts = cfg.get("prompts") or cfg.get("prompt_by_platform") or {}
    if isinstance(prompts, dict):
        platform_prompt = prompts.get(video.platform)
        if platform_prompt:
            return project_path(str(platform_prompt))
    if video.platform == "xiaoyuzhou":
        return project_path(str(cfg.get("podcast_prompt", "local_tools/obsidian_sync/prompts/podcast_summarize.md")))
    if video.platform == "weibo":
        return project_path(str(cfg.get("weibo_prompt", "local_tools/obsidian_sync/prompts/weibo_summarize.md")))
    if video.platform == "x":
        return project_path(str(cfg.get("x_prompt", cfg.get("weibo_prompt", "local_tools/obsidian_sync/prompts/weibo_summarize.md"))))
    if video.platform == "wechat":
        return project_path(str(cfg.get("wechat_prompt", "local_tools/obsidian_sync/prompts/wechat_summarize.md")))
    return project_path(str(cfg.get("prompt", "local_tools/obsidian_sync/prompts/summarize.md")))


def foreign_summary_prompt_path(cfg: Dict[str, Any]) -> Path:
    return project_path(str(cfg.get("foreign_prompt", "local_tools/obsidian_sync/prompts/foreign_summarize.md")))


def likely_non_chinese_content(video: CreatorVideo, transcript: str) -> bool:
    language = str(video.raw.get("source_language") or video.raw.get("creator_language") or "").strip().lower()
    if language in {"中文", "chinese", "zh", "zh-cn", "zh-hans", "zh-hant"} or "中文" in language:
        return False
    if language in {"英文", "english", "en", "en-us", "en-gb", "日文", "日语", "japanese", "ja", "韩文", "韩语", "korean", "ko"}:
        return True
    sample = f"{video.title}\n{transcript[:4000]}"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    hangul = len(re.findall(r"[\uac00-\ud7af]", sample))
    if kana + hangul >= 20:
        return True
    if latin >= 80 and cjk / max(1, latin + cjk) < 0.18:
        return True
    return False


def should_localize_to_chinese(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> bool:
    output_language = str(video.raw.get("output_language") or cfg.get("output_language") or "中文").strip().lower()
    if output_language not in {"中文", "zh", "zh-cn", "chinese"} and "中文" not in output_language:
        return False
    return video.platform in {"x", "youtube"} and likely_non_chinese_content(video, transcript)


def summary_user_content(video: CreatorVideo, transcript: str) -> str:
    published = format_date(video.create_time)
    if video.platform == "weibo":
        return (
            "微博元数据：\n"
            f"- 作者：{video.creator_name}\n"
            f"- 标题/首句：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "微博原文：\n"
            f"{transcript}"
        )
    if video.platform == "x":
        return (
            "X/Twitter 推文元数据：\n"
            f"- 作者：{video.creator_name}\n"
            f"- 标题/首句：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "推文原文：\n"
            f"{transcript}"
        )
    if video.platform == "wechat":
        description = str(video.raw.get("description_text") or "").strip()
        return (
            "公众号文章元数据：\n"
            f"- 公众号：{video.creator_name}\n"
            f"- 文章标题：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "摘要/导语：\n"
            f"{description or '无'}\n\n"
            "文章正文：\n"
            f"{transcript}"
        )
    if video.platform == "xiaoyuzhou":
        description = str(video.raw.get("description_text") or "").strip()
        return (
            "播客单集元数据：\n"
            f"- 节目：{video.creator_name}\n"
            f"- 单集标题：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "节目简介/Shownotes：\n"
            f"{description or '无'}\n\n"
            "逐字稿：\n"
            f"{transcript}"
        )
    if video.platform == "youtube":
        description = str(video.raw.get("description") or video.raw.get("description_text") or "").strip()
        transcript_source = str(video.raw.get("transcript_source") or "").strip() or "unknown"
        subtitle_language = str(video.raw.get("subtitle_language") or "").strip() or "auto"
        source_language = str(video.raw.get("source_language") or video.raw.get("creator_language") or "").strip() or "auto"
        return (
            "YouTube 视频元数据：\n"
            f"- 频道：{video.creator_name}\n"
            f"- 标题：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n"
            f"- 原始语言：{source_language}\n"
            f"- 逐字稿来源：{transcript_source}\n"
            f"- 字幕语言：{subtitle_language}\n\n"
            "视频简介：\n"
            f"{description[:3000] or '无'}\n\n"
            "原文逐字稿：\n"
            f"{transcript}"
        )
    if video.platform == "xiaohongshu":
        return (
            "小红书笔记元数据：\n"
            f"- 作者：{video.creator_name}\n"
            f"- 标题：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "原文/逐字稿：\n"
            f"{transcript}"
        )
    return (
        "视频元数据：\n"
        f"- 博主：{video.creator_name}\n"
        f"- 标题/描述：{video.title}\n"
        f"- 链接：{video.source_url}\n"
        f"- 发布时间：{published}\n\n"
        "逐字稿：\n"
        f"{transcript}"
    )


def split_text_by_chars(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current).strip())
            current = []
            current_len = 0
        if line_len > limit:
            for index in range(0, len(line), limit):
                part = line[index:index + limit].strip()
                if part:
                    chunks.append(part)
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def ai_provider_defaults(provider: str) -> Dict[str, str]:
    normalized = provider.lower().strip()
    if normalized in {"glm", "zhipu", "bigmodel"}:
        return {
            "label": "GLM",
            "api_key_env": "GLM_API_KEY",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "model": "glm-5-turbo",
        }
    if normalized == "deepseek":
        return {
            "label": "DeepSeek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        }
    return {
        "label": provider or "AI",
        "api_key_env": "AI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }


def chat_completion(cfg: Dict[str, Any], messages: List[Dict[str, str]], *, max_tokens: Optional[int] = None) -> str:
    provider = str(cfg.get("provider", "deepseek")).lower().strip()
    defaults = ai_provider_defaults(provider)
    api_key_env = str(cfg.get("api_key_env") or defaults["api_key_env"])
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing {api_key_env}. Put it in local_tools/obsidian_sync/.env.")
    base_url = str(cfg.get("base_url") or defaults["base_url"]).rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    model = str(cfg.get("model") or defaults["model"])
    model_aliases = {
        "deepseek-v4": "deepseek-v4-pro",
        "GLM-5.2": "glm-5.2",
    }
    model = model_aliases.get(model, model)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 5000)),
    }
    thinking = cfg.get("thinking")
    if thinking:
        payload["thinking"] = {"type": str(thinking)}
    if str(thinking or "disabled") == "disabled":
        payload["temperature"] = float(cfg.get("temperature", 0.2))

    with httpx.Client(timeout=httpx.Timeout(180.0)) as client:
        response = client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 400:
            detail = response.text.strip().replace(api_key, "[REDACTED]")
            try:
                error_data = response.json()
                error = error_data.get("error") if isinstance(error_data, dict) else None
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    if message:
                        detail = message
            except ValueError:
                pass
            detail = detail[:800] or response.reason_phrase
            raise RuntimeError(f"{defaults['label']} API {response.status_code}: {detail}")
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected {defaults['label']} response: {data}") from exc

    return str(content).strip()


def deepseek_chat_completion(cfg: Dict[str, Any], messages: List[Dict[str, str]], *, max_tokens: Optional[int] = None) -> str:
    return chat_completion(cfg, messages, max_tokens=max_tokens)


def chunk_podcast_transcript(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> Optional[str]:
    if video.platform != "xiaoyuzhou":
        return None
    chunk_chars = int(cfg.get("chunk_chars", 45000))
    chunks = split_text_by_chars(transcript, chunk_chars)
    if len(chunks) <= 1:
        return None
    max_chunks = int(cfg.get("max_chunks", 24))
    chunks = chunks[:max_chunks]
    chunk_prompt = (
        "你是播客长内容整理助手。请把这一段逐字稿整理成中间纪要，只保留后续整合需要的信息。\n"
        "要求：\n"
        "- 保留时间线、主题切换、核心论点、关键例子、可引用表达。\n"
        "- 不要写最终文章，不要编造信息。\n"
        "- 输出 Markdown 项目符号，控制在 1200-1800 字。"
    )
    summaries: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        user_content = (
            f"节目：{video.creator_name}\n"
            f"单集：{video.title}\n"
            f"分块：{index}/{len(chunks)}\n\n"
            f"{chunk}"
        )
        summary = chat_completion(
            cfg,
            [
                {"role": "system", "content": chunk_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=int(cfg.get("chunk_max_tokens", 2500)),
        )
        summaries.append(f"### 分块 {index}/{len(chunks)}\n\n{summary}")
    return "\n\n".join(summaries)


def call_ai_summary(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> str:
    prompt_path = foreign_summary_prompt_path(cfg) if should_localize_to_chinese(video, transcript, cfg) else summary_prompt_path(video, cfg)
    system_prompt = prompt_path.read_text(encoding="utf-8")
    chunked = chunk_podcast_transcript(video, transcript, cfg)
    if chunked:
        published = format_date(video.create_time)
        description = str(video.raw.get("description_text") or "").strip()
        user_content = (
            "播客单集元数据：\n"
            f"- 节目：{video.creator_name}\n"
            f"- 单集标题：{video.title}\n"
            f"- 链接：{video.source_url}\n"
            f"- 发布时间：{published}\n\n"
            "节目简介/Shownotes：\n"
            f"{description or '无'}\n\n"
            "下面是完整逐字稿按顺序分块整理后的中间纪要。请基于这些纪要生成最终 Markdown，不要误认为只有局部内容：\n\n"
            f"{chunked}"
        )
    else:
        user_content = summary_user_content(video, transcript)

    return chat_completion(
        cfg,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )


def call_deepseek_summary(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> str:
    return call_ai_summary(video, transcript, cfg)


def build_frontmatter(video: CreatorVideo, status: str, *, summary: str = "", body: str = "") -> str:
    tags = assign_tags(platform=video.platform, title=video.title, summary=summary, body=body)
    tag_lines = "\n".join(f"  - {tag}" for tag in tags)
    if video.platform == "weibo":
        id_field = "post_id"
        author_field = "author"
        author_key_field = "author_key"
    elif video.platform == "x":
        id_field = "tweet_id"
        author_field = "author"
        author_key_field = "author_key"
    elif video.platform == "wechat":
        id_field = "article_id"
        author_field = "account"
        author_key_field = "account_key"
    elif video.platform == "xiaoyuzhou":
        id_field = "episode_id"
        author_field = "podcast"
        author_key_field = "podcast_key"
    elif video.platform == "youtube":
        id_field = "youtube_id"
        author_field = "channel"
        author_key_field = "channel_key"
    else:
        id_field = "video_id"
        author_field = "creator"
        author_key_field = "creator_key"
    extra_lines = ""
    source_language = str(video.raw.get("source_language") or video.raw.get("creator_language") or "").strip()
    output_language = str(video.raw.get("output_language") or "").strip()
    transcript_source = str(video.raw.get("transcript_source") or "").strip()
    if source_language:
        extra_lines += f"source_language: {format_frontmatter_value(source_language)}\n"
    if output_language:
        extra_lines += f"output_language: {format_frontmatter_value(output_language)}\n"
    if transcript_source:
        extra_lines += f"transcript_source: {format_frontmatter_value(transcript_source)}\n"
    return (
        "---\n"
        f"source: {format_frontmatter_value(video.platform)}\n"
        f"{author_field}: {format_frontmatter_value(video.creator_name)}\n"
        f"{author_key_field}: {format_frontmatter_value(video.creator_key)}\n"
        f"{id_field}: {format_frontmatter_value(video.video_id)}\n"
        f"url: {format_frontmatter_value(video.source_url)}\n"
        f"published_at: {format_frontmatter_value(format_date(video.create_time))}\n"
        f"processed_at: {format_frontmatter_value(utc_now())}\n"
        f"sync_status: {format_frontmatter_value(status)}\n"
        f"{extra_lines}"
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
    )


def build_douyin_markdown(video: CreatorVideo, summary: str, transcript: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=transcript)
    title = video.title if video.title != video.video_id else f"抖音视频 {video.video_id}"
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原视频]({video.source_url})\n\n",
    ]
    if summary.strip():
        parts.append(f"{summary.strip()}\n\n")
    if include_transcript:
        parts.append("## 逐字稿\n\n")
        parts.append(f"{transcript.strip()}\n")
    if status == "raw":
        parts.append("\n## 笔记\n\n<!-- Claudian: 在此处生成结构化总结 -->\n")
    return "".join(parts)


def build_weibo_markdown(video: CreatorVideo, summary: str, original_text: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=original_text)
    title = video.title if video.title != video.video_id else f"微博 {format_date(video.create_time)}"
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原微博]({video.source_url})\n\n",
    ]
    if include_transcript:
        parts.extend(["## 原文\n\n", f"{original_text.strip()}\n\n"])
    summary = summary.strip()
    if summary:
        parts.append(f"{summary}\n\n")
    parts.append("## 笔记\n\n")
    if status == "raw":
        parts.append("<!-- Claudian: 在此处生成要点提炼 -->\n\n")
    else:
        parts.append("<!-- 在这里补充自己的理解、链接或后续追踪。 -->\n\n")
    parts.append("## 相关链接\n\n")
    parts.append(f"- 原微博：{video.source_url}\n")
    return "".join(parts)


def build_x_markdown(video: CreatorVideo, summary: str, original_text: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=original_text)
    title = video.title if video.title != video.video_id else f"X 推文 {format_date(video.create_time)}"
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原推文]({video.source_url})\n\n",
    ]
    if include_transcript:
        parts.extend(["## 原文\n\n", f"{original_text.strip()}\n\n"])
    summary = summary.strip()
    if summary:
        parts.append(f"{summary}\n\n")
    parts.append("## 笔记\n\n")
    if status == "raw":
        parts.append("<!-- Claudian: 在此处生成要点提炼 -->\n\n")
    else:
        parts.append("<!-- 在这里补充自己的理解、链接或后续追踪。 -->\n\n")
    parts.append("## 相关链接\n\n")
    parts.append(f"- 原推文：{video.source_url}\n")
    return "".join(parts)


def build_wechat_markdown(video: CreatorVideo, summary: str, original_text: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=original_text)
    title = video.title if video.title != video.video_id else f"公众号文章 {format_date(video.create_time)}"
    description = str(video.raw.get("description_text") or "").strip()
    description_section = f"## 导语\n\n{description}\n\n" if description else ""
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原文链接]({video.source_url})\n\n",
        description_section,
    ]
    summary = summary.strip()
    if summary:
        parts.append(f"{summary}\n\n")
    if include_transcript:
        parts.append("## 原文\n\n")
        parts.append(f"{original_text.strip()}\n\n")
    parts.append("## 笔记\n\n")
    if status == "raw":
        parts.append("<!-- Claudian: 在此处生成结构化总结 -->\n\n")
    else:
        parts.append("<!-- 在这里补充自己的理解、链接或后续追踪。 -->\n\n")
    parts.append("## 相关链接\n\n")
    parts.append(f"- 原文：{video.source_url}\n")
    return "".join(parts)


def build_podcast_markdown(video: CreatorVideo, summary: str, transcript: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=transcript)
    title = video.title if video.title != video.video_id else f"播客单集 {format_date(video.create_time)}"
    description = str(video.raw.get("description_text") or "").strip()
    shownotes_section = f"## Shownotes\n\n{description}\n\n" if description else ""
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原单集]({video.source_url})\n\n",
        shownotes_section,
    ]
    if summary.strip():
        parts.append(f"{summary.strip()}\n\n")
    if include_transcript:
        parts.append("## 逐字稿\n\n")
        parts.append(f"{transcript.strip()}\n")
    if status == "raw":
        parts.append("\n## 笔记\n\n<!-- Claudian: 在此处生成结构化总结 -->\n")
    return "".join(parts)


def build_youtube_markdown(video: CreatorVideo, summary: str, transcript: str, status: str, include_transcript: bool = True) -> str:
    frontmatter = build_frontmatter(video, status, summary=summary, body=transcript)
    title = video.title if video.title != video.video_id else f"YouTube 视频 {format_date(video.create_time)}"
    description = str(video.raw.get("description") or video.raw.get("description_text") or "").strip()
    description_section = f"## 视频简介\n\n{description[:5000]}\n\n" if description else ""
    transcript_source = str(video.raw.get("transcript_source") or "").strip()
    source_language = str(video.raw.get("source_language") or video.raw.get("creator_language") or "").strip()
    meta_lines = []
    if source_language:
        meta_lines.append(f"- 原始语言：{source_language}")
    if transcript_source:
        meta_lines.append(f"- 逐字稿来源：{transcript_source}")
    if video.raw.get("subtitle_language"):
        meta_lines.append(f"- 字幕语言：{video.raw.get('subtitle_language')}")
    meta_section = f"## 元信息\n\n{chr(10).join(meta_lines)}\n\n" if meta_lines else ""
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原视频]({video.source_url})\n\n",
        meta_section,
        description_section,
    ]
    summary = summary.strip()
    if summary:
        parts.append(f"{summary}\n\n")
    if include_transcript:
        parts.append("## 原文逐字稿\n\n")
        parts.append(f"{transcript.strip()}\n")
    if status == "raw":
        parts.append("\n## 笔记\n\n<!-- Claudian: 在此处生成中文结构化总结 -->\n")
    return "".join(parts)


def build_xiaohongshu_markdown(video: CreatorVideo, summary: str, transcript: str, status: str, include_transcript: bool = True) -> str:
    title = video.title if video.title != video.video_id else f"小红书笔记 {format_date(video.create_time)}"
    original_text = str(video.raw.get("original_text") or "").strip()
    image_paths = [str(item) for item in (video.raw.get("image_local_paths") or []) if str(item).strip()]
    video_transcript = transcript.strip()
    frontmatter = build_frontmatter(video, status, summary=summary, body=f"{original_text}\n\n{video_transcript}")
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原笔记]({video.source_url})\n\n",
    ]
    if include_transcript and original_text:
        parts.extend(["## 原文\n\n", f"{original_text}\n\n"])
    if image_paths:
        parts.append("## 图片\n\n")
        for path in image_paths:
            parts.append(f"![]({path})\n\n")
    if video_transcript and video_transcript != original_text:
        parts.extend(["## 视频逐字稿\n\n", f"{video_transcript}\n\n"])
    summary = summary.strip()
    if summary:
        parts.append(f"{summary}\n\n")
    parts.append("## 笔记\n\n")
    parts.append("<!-- 在这里补充自己的理解、可复用表达或二创角度。 -->\n\n")
    parts.append("## 相关链接\n\n")
    parts.append(f"- 原笔记：{video.source_url}\n")
    return "".join(parts)


def build_markdown(video: CreatorVideo, summary: str, transcript: str, status: str, include_transcript: bool = True) -> str:
    if video.platform == "weibo":
        return build_weibo_markdown(video, summary, transcript, status, include_transcript)
    if video.platform == "x":
        return build_x_markdown(video, summary, transcript, status, include_transcript)
    if video.platform == "wechat":
        return build_wechat_markdown(video, summary, transcript, status, include_transcript)
    if video.platform == "xiaoyuzhou":
        return build_podcast_markdown(video, summary, transcript, status, include_transcript)
    if video.platform == "youtube":
        return build_youtube_markdown(video, summary, transcript, status, include_transcript)
    if video.platform == "xiaohongshu":
        return build_xiaohongshu_markdown(video, summary, transcript, status, include_transcript)
    return build_douyin_markdown(video, summary, transcript, status, include_transcript)


def output_path_for_video(base_dir: Path, video: CreatorVideo, conn: Optional[sqlite3.Connection] = None) -> Path:
    date_prefix = format_date(video.create_time)
    title = short_title_for_filename(video.title, video.video_id)
    directory = base_dir / safe_filename(video.creator_name, 40)
    stem = safe_filename(f"{title}-{video.creator_name}-{date_prefix}", 120)
    candidate = directory / f"{stem}.md"
    if conn is None:
        return candidate

    current_row = conn.execute("SELECT markdown_path FROM videos WHERE video_id = ?", (video.video_id,)).fetchone()
    current_path = Path(str(current_row[0])) if current_row and current_row[0] else None
    if not candidate.exists() or (current_path and candidate == current_path):
        return candidate

    for index in range(2, 100):
        fallback = directory / f"{stem}-{index}.md"
        if not fallback.exists() or (current_path and fallback == current_path):
            return fallback
    return directory / f"{stem}-{video.video_id[-6:]}.md"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        temp_name = handle.name
    Path(temp_name).replace(path)


def retention_config(config: Dict[str, Any]) -> Dict[str, bool]:
    defaults = {
        "keep_video": False,
        "keep_audio": False,
        "save_transcript_txt": False,
        "save_source_raw": False,
        "include_transcript_in_markdown": True,
    }
    raw = config.get("retention")
    if isinstance(raw, dict):
        for key in defaults:
            if key in raw:
                defaults[key] = bool(raw.get(key))
    return defaults


def sidecar_path(markdown_path: Path, label: str, suffix: str) -> Path:
    return markdown_path.with_name(f"{markdown_path.stem}.{label}{suffix}")


def retain_processing_outputs(
    *,
    video: CreatorVideo,
    markdown_path: Path,
    transcript: str,
    source_media_path: Optional[Path],
    audio_path: Optional[Path],
    retention: Dict[str, bool],
) -> set[Path]:
    retained: set[Path] = set()
    if retention.get("save_transcript_txt"):
        transcript_path = sidecar_path(markdown_path, "transcript", ".txt")
        atomic_write(transcript_path, transcript.strip() + "\n")
        retained.add(transcript_path)
    if retention.get("save_source_raw"):
        source_path = sidecar_path(markdown_path, "source", ".json")
        atomic_write(source_path, json.dumps(video.raw, ensure_ascii=False, indent=2, default=str) + "\n")
        retained.add(source_path)
    if retention.get("keep_video") and video.platform in {"douyin", "xiaohongshu"} and source_media_path and source_media_path.exists():
        target = sidecar_path(markdown_path, "video", source_media_path.suffix or ".mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_media_path), str(target))
        retained.add(target)
    if retention.get("keep_audio"):
        if source_media_path and source_media_path.exists() and video.platform in {"xiaoyuzhou", "youtube"}:
            target = sidecar_path(markdown_path, "source-audio", source_media_path.suffix or ".audio")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_media_path), str(target))
            retained.add(target)
        if audio_path and audio_path.exists():
            target = sidecar_path(markdown_path, "audio", audio_path.suffix or ".wav")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio_path), str(target))
            retained.add(target)
    return retained


async def download_xiaohongshu_images(video: CreatorVideo, config: Dict[str, Any], markdown_path: Path) -> list[str]:
    urls = [str(item) for item in (video.raw.get("image_urls") or []) if str(item).startswith("http")]
    if not urls:
        return []
    asset_dir = markdown_path.with_name(f"{markdown_path.stem}.assets")
    asset_dir.mkdir(parents=True, exist_ok=True)
    headers = xiaohongshu_headers(config, video.source_url)
    timeout = httpx.Timeout(float(config.get("download", {}).get("timeout_seconds", 120)), connect=10.0)
    saved: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, url in enumerate(urls, start=1):
            suffix_match = re.search(r"\.(jpg|jpeg|png|webp|heic)(?:\?|$)", url, re.IGNORECASE)
            suffix = f".{suffix_match.group(1).lower()}" if suffix_match else ".jpg"
            target = asset_dir / f"image-{index:02d}{suffix}"
            try:
                response = await client.get(url, headers=headers, follow_redirects=True)
                response.raise_for_status()
                target.write_bytes(response.content)
                saved.append(str(Path(asset_dir.name) / target.name))
            except Exception as exc:  # noqa: BLE001
                print(f"WARN xiaohongshu image download failed {video.video_id} {url}: {type(exc).__name__}: {exc}")
    return saved


async def download_xiaohongshu_video_to_temp(video: CreatorVideo, config: Dict[str, Any], temp_dir: Path) -> Path:
    video_url = str(video.raw.get("video_url") or "").strip()
    if not video_url:
        raise RuntimeError("小红书视频链接为空")
    target = temp_dir / f"{video.video_id}.mp4"
    headers = xiaohongshu_headers(config, video.source_url)
    await stream_to_file(video_url, target, headers, float(config.get("download", {}).get("timeout_seconds", 120)))
    return target


def configured_concurrency(config: Dict[str, Any], cli_value: Optional[int]) -> int:
    if cli_value:
        value = int(cli_value)
    else:
        sync_cfg = config.get("sync") if isinstance(config.get("sync"), dict) else {}
        value = int(sync_cfg.get("max_concurrency", 2))
    return max(1, min(2, value))


def parse_video_id_filter(values: Optional[List[str]]) -> set[str]:
    if not values:
        return set()
    video_ids: set[str] = set()
    for value in values:
        for item in str(value or "").split(","):
            item = item.strip()
            if item:
                video_ids.add(item)
    return video_ids


async def process_video(
    crawler: Any,
    conn: sqlite3.Connection,
    video: CreatorVideo,
    config: Dict[str, Any],
    output_base: Path,
    skip_summary: bool,
    no_cleanup: bool,
    run_id: Optional[str] = None,
) -> str:
    work_dir = project_path(str(config.get("work_dir", "local_tools/obsidian_sync/work")))
    media_dir = work_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    retention = retention_config(config)
    include_transcript = retention.get("include_transcript_in_markdown", True)
    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None

    try:
        print(f"PROCESS {video.creator_name} {video.video_id} {video.title}")
        if video.platform in {"weibo", "wechat", "x"}:
            source_label = {"weibo": "微博", "wechat": "公众号", "x": "X 推文"}.get(video.platform, "文本")
            if run_id:
                update_run(conn, run_id, current_stage=f"读取{source_label}正文", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", f"读取{source_label}正文")
            transcript = str(video.raw.get("original_text") or "").strip()
            if not transcript and video.platform == "wechat" and "mp.weixin.qq.com" in video.source_url:
                fetched = await fetch_wechat_article(
                    {
                        "key": video.creator_key,
                        "name": video.creator_name,
                        "tags": video.tags,
                    },
                    video.source_url,
                    config,
                )
                video.raw.update(fetched.raw)
                video.title = fetched.title or video.title
                video.create_time = fetched.create_time or video.create_time
                transcript = str(video.raw.get("original_text") or "").strip()
            if not transcript:
                raise RuntimeError(f"{source_label}正文为空")

            if skip_summary:
                summary = ""
                status = "raw"
            else:
                try:
                    if run_id:
                        update_run(conn, run_id, current_stage="AI 总结", current_creator=video.creator_name, current_video_id=video.video_id)
                        upsert_run_item(conn, run_id, video, "running", "AI 总结")
                    summary = await asyncio.to_thread(call_ai_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 - keep original text even if summary fails.
                    summary = f"## 要点\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"

            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status, include_transcript)
            markdown_path = output_path_for_video(output_base, video, conn)
            atomic_write(markdown_path, markdown)
            retain_processing_outputs(
                video=video,
                markdown_path=markdown_path,
                transcript=transcript,
                source_media_path=None,
                audio_path=None,
                retention=retention,
            )
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else f"AI 总结失败，但{source_label}原文已写入 Markdown"
                upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
            print(f"WROTE {markdown_path}")
            return status

        if video.platform == "xiaohongshu":
            if run_id:
                update_run(conn, run_id, current_stage="读取小红书笔记", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "读取小红书笔记")
            video = await build_platform_registry().get("xiaohongshu").hydrate(
                video,
                PlatformRunContext(config=config, conn=conn, run_id=run_id),
            )
            note_text = str(video.raw.get("original_text") or "").strip()
            transcript = note_text
            if video.raw.get("video_url"):
                if run_id:
                    update_run(conn, run_id, current_stage="下载小红书视频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "下载小红书视频")
                video_path = await download_xiaohongshu_video_to_temp(video, config, media_dir)
                if run_id:
                    update_run(conn, run_id, current_stage="提取音频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "提取音频")
                audio_path = media_dir / f"{video.video_id}.wav"
                await asyncio.to_thread(extract_audio, video_path, audio_path)
                if run_id:
                    update_run(conn, run_id, current_stage="转录小红书视频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "转录小红书视频")
                video_transcript = await asyncio.to_thread(transcribe_audio, audio_path, config.get("transcribe", {}))
                transcript = "\n\n".join(part for part in [note_text, video_transcript] if part.strip())
            if not transcript.strip():
                raise RuntimeError("小红书原文/逐字稿为空")

            markdown_path = output_path_for_video(output_base, video, conn)
            image_paths = await download_xiaohongshu_images(video, config, markdown_path)
            video.raw["image_local_paths"] = image_paths
            if skip_summary:
                summary = ""
                status = "raw"
            else:
                try:
                    if run_id:
                        update_run(conn, run_id, current_stage="AI 总结", current_creator=video.creator_name, current_video_id=video.video_id)
                        upsert_run_item(conn, run_id, video, "running", "AI 总结")
                    summary = await asyncio.to_thread(call_ai_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001
                    summary = f"## 要点\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"
            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status, include_transcript)
            atomic_write(markdown_path, markdown)
            retain_processing_outputs(
                video=video,
                markdown_path=markdown_path,
                transcript=transcript,
                source_media_path=video_path,
                audio_path=audio_path,
                retention=retention,
            )
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else "AI 总结失败，但小红书原文/逐字稿已写入 Markdown"
                upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
            print(f"WROTE {markdown_path}")
            return status

        if video.platform == "xiaoyuzhou":
            if run_id:
                update_run(conn, run_id, current_stage="下载播客音频", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "下载播客音频")
            download_cfg = dict(config.get("download", {}) or {})
            podcast_download = config.get("podcast_download")
            if isinstance(podcast_download, dict):
                download_cfg.update(podcast_download)
            video_path = await download_audio_to_temp(video, download_cfg, media_dir)

            if run_id:
                update_run(conn, run_id, current_stage="标准化音频", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "标准化音频")
            audio_path = media_dir / f"{video.video_id}.wav"
            await asyncio.to_thread(extract_audio, video_path, audio_path)

            if run_id:
                update_run(conn, run_id, current_stage="转录播客", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "转录播客")
            transcribe_cfg = dict(config.get("transcribe", {}) or {})
            podcast_transcribe = config.get("podcast_transcribe")
            if isinstance(podcast_transcribe, dict):
                transcribe_cfg.update(podcast_transcribe)
            else:
                transcribe_cfg["language"] = "auto"
            transcript = await asyncio.to_thread(transcribe_audio, audio_path, transcribe_cfg)

            if skip_summary:
                summary = ""
                status = "raw"
            else:
                try:
                    if run_id:
                        update_run(conn, run_id, current_stage="AI 整理长播客", current_creator=video.creator_name, current_video_id=video.video_id)
                        upsert_run_item(conn, run_id, video, "running", "AI 整理长播客")
                    summary = await asyncio.to_thread(call_ai_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                    summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"

            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status, include_transcript)
            markdown_path = output_path_for_video(output_base, video, conn)
            atomic_write(markdown_path, markdown)
            retain_processing_outputs(
                video=video,
                markdown_path=markdown_path,
                transcript=transcript,
                source_media_path=video_path,
                audio_path=audio_path,
                retention=retention,
            )
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else "AI 总结失败，但播客逐字稿已写入 Markdown"
                upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
            print(f"WROTE {markdown_path}")
            return status

        if video.platform == "youtube":
            if run_id:
                update_run(conn, run_id, current_stage="读取 YouTube 详情", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "读取 YouTube 详情")
            info = await asyncio.to_thread(ytdlp_extract_info, video.source_url, {"noplaylist": True})
            if isinstance(info, dict):
                video.raw.update(info)
                video.title = compact_title(str(info.get("title") or video.title), video.video_id)
                timestamp = info.get("timestamp") or info.get("release_timestamp") or video.create_time
                try:
                    video.create_time = int(timestamp) if timestamp is not None else video.create_time
                except (TypeError, ValueError):
                    pass

            transcript = ""
            subtitle_language = ""
            if run_id:
                update_run(conn, run_id, current_stage="读取 YouTube 字幕", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "读取 YouTube 字幕")
            try:
                transcript, subtitle_language = await fetch_youtube_subtitle(video, info, config)
            except Exception as exc:  # noqa: BLE001 - subtitles are best-effort; audio fallback follows.
                print(f"WARN youtube subtitle failed {video.video_id}: {type(exc).__name__}: {exc}")
                transcript = ""
            if transcript:
                video.raw["transcript_source"] = "youtube_subtitle"
                video.raw["subtitle_language"] = subtitle_language

            if not transcript:
                if run_id:
                    update_run(conn, run_id, current_stage="下载 YouTube 音频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "下载 YouTube 音频")
                video_path = await asyncio.to_thread(download_youtube_audio_sync, video, config, media_dir)
                if run_id:
                    update_run(conn, run_id, current_stage="标准化音频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "标准化音频")
                audio_path = media_dir / f"{video.video_id}.wav"
                await asyncio.to_thread(extract_audio, video_path, audio_path)
                transcribe_cfg = dict(config.get("transcribe", {}) or {})
                transcribe_cfg.update(config.get("youtube_transcribe") or {})
                if str(video.raw.get("source_language") or "").lower() not in {"中文", "zh", "zh-cn", "zh-hans"}:
                    transcribe_cfg["language"] = transcribe_cfg.get("language") or "auto"
                    if str(transcribe_cfg.get("language", "")).lower() == "zh":
                        transcribe_cfg["language"] = "auto"
                if run_id:
                    update_run(conn, run_id, current_stage="转录 YouTube 音频", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "转录 YouTube 音频")
                transcript = await asyncio.to_thread(transcribe_audio, audio_path, transcribe_cfg)
                video.raw["transcript_source"] = "whisper_audio"

            if skip_summary:
                summary = ""
                status = "raw"
            else:
                try:
                    if run_id:
                        update_run(conn, run_id, current_stage="AI 中文整理", current_creator=video.creator_name, current_video_id=video.video_id)
                        upsert_run_item(conn, run_id, video, "running", "AI 中文整理")
                    summary = await asyncio.to_thread(call_ai_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                    summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"

            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status, include_transcript)
            markdown_path = output_path_for_video(output_base, video, conn)
            atomic_write(markdown_path, markdown)
            retain_processing_outputs(
                video=video,
                markdown_path=markdown_path,
                transcript=transcript,
                source_media_path=video_path,
                audio_path=audio_path,
                retention=retention,
            )
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else "AI 总结失败，但原文逐字稿已写入 Markdown"
                upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
            print(f"WROTE {markdown_path}")
            return status

        transcript_mode = transcript_mode_for_video(video)
        if run_id:
            update_run(conn, run_id, current_stage="下载视频", current_creator=video.creator_name, current_video_id=video.video_id)
            upsert_run_item(conn, run_id, video, "running", "下载视频")
        video_path = await download_video_to_temp(crawler, video, config.get("download", {}), media_dir)

        transcript = ""
        if transcript_mode in {"subtitle_ocr", "subtitle_ocr_fallback_audio"}:
            if run_id:
                update_run(conn, run_id, current_stage="字幕 OCR", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "字幕 OCR")
            try:
                transcript = await asyncio.to_thread(transcribe_subtitles_from_video, video_path, config)
            except Exception as exc:  # noqa: BLE001 - optional fallback mode keeps processing.
                if transcript_mode == "subtitle_ocr":
                    raise
                print(f"FALLBACK {video.video_id} 字幕 OCR 失败，改用音频转录：{type(exc).__name__}: {exc}")

        if not transcript:
            if run_id:
                update_run(conn, run_id, current_stage="提取音频", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "提取音频")
            audio_path = media_dir / f"{video.video_id}.wav"
            await asyncio.to_thread(extract_audio, video_path, audio_path)
            if run_id:
                update_run(conn, run_id, current_stage="转录音频", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "转录音频")
            transcript = await asyncio.to_thread(transcribe_audio, audio_path, config.get("transcribe", {}))

        if skip_summary:
            summary = ""
            status = "raw"
        else:
            try:
                if run_id:
                    update_run(conn, run_id, current_stage="AI 总结", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "AI 总结")
                summary = await asyncio.to_thread(call_ai_summary, video, transcript, summary_config_for_video(video, config))
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                status = "summary_error"

        if run_id:
            update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
            upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
        markdown = build_markdown(video, summary, transcript, status, include_transcript)
        markdown_path = output_path_for_video(output_base, video, conn)
        atomic_write(markdown_path, markdown)
        retain_processing_outputs(
            video=video,
            markdown_path=markdown_path,
            transcript=transcript,
            source_media_path=video_path,
            audio_path=audio_path,
            retention=retention,
        )
        is_success = status in ("ok", "raw")
        mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
        if run_id:
            item_status = "success" if is_success else "failed"
            item_error = None if is_success else "AI 总结失败，但逐字稿已写入 Markdown"
            upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
        print(f"WROTE {markdown_path}")
        return status

    except Exception as exc:  # noqa: BLE001 - record failure and continue.
        message = f"{type(exc).__name__}: {exc}"
        mark_result(conn, video, "error", None, message, 0)
        if run_id:
            upsert_run_item(conn, run_id, video, "failed", "失败", message)
        print(f"ERROR {video.video_id} {message}")
        return "error"

    finally:
        cleanup = bool(config.get("download", {}).get("cleanup_media", True)) and not no_cleanup
        if cleanup:
            for path in [video_path, audio_path]:
                if path and path.exists():
                    path.unlink()


async def sync(args: argparse.Namespace) -> int:
    config_path = project_path(str(args.config))
    config = load_yaml(config_path)
    load_env_file(project_path(str(config.get("env_file", "local_tools/obsidian_sync/.env"))))

    from crawlers.hybrid.hybrid_crawler import HybridCrawler

    state_path = project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_path))
    ensure_state(conn)
    run_id = str(args.run_id or default_run_id())
    selected_video_ids = parse_video_id_filter(args.video_id)

    vault_path = Path(str(config["vault_path"])).expanduser()
    creators = [creator for creator in config.get("creators", []) if creator.get("enabled", True)]
    selected_creator_keys = parse_video_id_filter(args.creators)
    if args.creator:
        selected_creator_keys.add(str(args.creator))
    if selected_creator_keys:
        creators = [creator for creator in creators if str(creator.get("key")) in selected_creator_keys]
    skipped_platform_creators = [
        creator for creator in creators
        if creator_platform(creator) not in RUNNABLE_PLATFORMS
    ]
    for creator in skipped_platform_creators:
        print(
            "SKIP unsupported platform "
            f"{creator_platform(creator)}: {creator.get('name') or creator.get('key') or creator.get('url')}"
        )
    creators = [
        creator for creator in creators
        if creator_platform(creator) in RUNNABLE_PLATFORMS
    ]

    if not creators:
        print("No enabled runnable creators. Current runnable platforms: douyin, weibo, x, xiaoyuzhou, wechat, youtube.")
        return 0

    final_output_path = (
        output_base_for_platform(vault_path, config, creator_platform(creators[0]))
        if len(creators) == 1
        else vault_path
    )
    creator_filter = args.creator or (",".join(sorted(selected_creator_keys)) if selected_creator_keys else None)
    start_run(conn, run_id, creator_filter, len(creators))
    print(f"RUN {run_id}")
    max_concurrency = configured_concurrency(config, args.concurrency)
    print(f"CONCURRENCY {max_concurrency}")
    crawler = HybridCrawler()
    total_seen = 0
    total_processed = 0
    had_errors = False
    paused = False
    recent_cutoff = recent_cutoff_timestamp(args)
    if recent_cutoff:
        print(f"RECENT_DAYS {int(args.recent_days)} cutoff={datetime.fromtimestamp(recent_cutoff, timezone.utc).isoformat(timespec='seconds')}")

    try:
        total_creators = len(creators)
        for creator_index, creator in enumerate(creators, start=1):
            if pause_requested(args):
                paused = True
                update_run(conn, run_id, current_stage="已暂停，等待继续", current_creator="", current_video_id="")
                print("PAUSE requested before next creator")
                break
            if not creator.get("key") or not creator.get("url"):
                print(f"SKIP invalid creator entry: {creator}")
                continue

            creator_name = creator.get("name") or creator.get("key")
            platform = creator_platform(creator)
            output_base = output_base_for_platform(vault_path, config, platform)
            prerequisite_error = creator_prerequisite_error(config, creator)
            if prerequisite_error:
                had_errors = True
                print(f"SKIP {creator_name} {prerequisite_error}")
                update_run(conn, run_id, current_stage="跳过未登录来源", current_creator=str(creator_name), current_video_id="", error=prerequisite_error)
                error_video = source_error_video(creator, platform, run_id, prerequisite_error)
                upsert_run_item(conn, run_id, error_video, "failed", "来源登录态缺失", prerequisite_error)
                continue
            try:
                apply_cookie_for_creator(config, creator)
            except Exception as exc:  # noqa: BLE001 - isolate one bad source.
                message = f"{type(exc).__name__}: {exc}"
                had_errors = True
                print(f"ERROR source {creator_name} {message}")
                update_run(conn, run_id, current_stage="来源准备失败", current_creator=str(creator_name), current_video_id="", error=message)
                error_video = source_error_video(creator, platform, run_id, message)
                upsert_run_item(conn, run_id, error_video, "failed", "来源准备失败", message)
                continue
            update_run(conn, run_id, current_stage="嗅探内容", current_creator=str(creator_name), current_video_id="")
            print(f"CREATOR {creator_index}/{total_creators} {creator_name}")
            print(f"OUTPUT {platform} {output_base}")
            print(f"FETCH {creator_name}")
            scan_limit = int(args.limit or 0) if platform in {"weibo", "x", "xiaoyuzhou", "wechat", "youtube"} and not args.full_history else 0
            try:
                videos = await fetch_creator_videos(crawler, creator, config, scan_limit, args.full_history, conn, run_id)
            except Exception as exc:  # noqa: BLE001 - one source must not break the full run.
                message = f"{type(exc).__name__}: {exc}"
                had_errors = True
                print(f"ERROR source {creator_name} {message}")
                update_run(conn, run_id, current_stage="嗅探失败，继续下一个来源", current_creator=str(creator_name), current_video_id="", error=message)
                error_video = source_error_video(creator, platform, run_id, message)
                upsert_run_item(conn, run_id, error_video, "failed", "嗅探失败", message)
                continue
            if pause_requested(args):
                paused = True
                update_run(conn, run_id, current_stage="已暂停，等待继续", current_creator=str(creator_name), current_video_id="")
                print(f"PAUSE requested after sniffing {creator_name}")
                break
            total_seen += len(videos)
            update_run(conn, run_id, detected_count=total_seen)
            if platform == "weibo":
                item_label = "posts"
            elif platform == "x":
                item_label = "tweets"
            elif platform == "xiaoyuzhou":
                item_label = "episodes"
            elif platform == "wechat":
                item_label = "articles"
            else:
                item_label = "videos"
            print(f"FOUND {len(videos)} {item_label}")

            candidates: List[CreatorVideo] = []
            for video in videos:
                if selected_video_ids and video.video_id not in selected_video_ids:
                    continue
                mark_seen(conn, video)
                if recent_cutoff and video.create_time and int(video.create_time) < recent_cutoff:
                    upsert_run_item(
                        conn,
                        run_id,
                        video,
                        "skipped",
                        "超出每日更新窗口",
                        f"发布时间早于最近 {int(args.recent_days)} 天，本轮跳过",
                    )
                    continue
                status = get_status(conn, video.video_id)
                if status == "ok" and not args.force:
                    upsert_run_item(conn, run_id, video, "skipped", "已存在", "之前已成功处理，本轮跳过")
                    continue
                if status == "filtered" and creator_promotion_filter_enabled(creator, config, args) and not args.force:
                    upsert_run_item(conn, run_id, video, "skipped", "已过滤", "之前已判定为低价值内容，本轮跳过")
                    continue
                if creator_promotion_filter_enabled(creator, config, args):
                    filter_reason = promotion_filter_reason(video, config)
                    if filter_reason:
                        mark_result(conn, video, "filtered", None, filter_reason, 0)
                        upsert_run_item(conn, run_id, video, "skipped", "已过滤低价值内容", filter_reason)
                        continue
                candidates.append(video)
                upsert_run_item(conn, run_id, video, "pending", "等待处理")

            if args.limit:
                limited_ids = {video.video_id for video in candidates[: int(args.limit)]}
                for video in candidates:
                    if video.video_id not in limited_ids:
                        upsert_run_item(conn, run_id, video, "skipped", "超出数量限制", "受本轮数量限制跳过")
                candidates = candidates[: int(args.limit)]

            print(f"CANDIDATES {creator.get('key')} count={len(candidates)}")

            if args.dry_run:
                for video_index, video in enumerate(candidates, start=1):
                    if pause_requested(args):
                        paused = True
                        update_run(conn, run_id, current_stage="已暂停，等待继续", current_creator=video.creator_name, current_video_id="")
                        print(f"PAUSE requested during dry run {video.creator_name}")
                        break
                    update_run(conn, run_id, current_stage="处理视频", current_creator=video.creator_name, current_video_id=video.video_id)
                    print(f"PROGRESS {video_index}/{len(candidates)} {video.video_id}")
                    upsert_run_item(conn, run_id, video, "dry_run", "候选", "只嗅探候选内容，未下载、未转录、未写入")
                    print(f"DRY {video.creator_name} {video.video_id} {video.title} {video.source_url}")
                if paused:
                    break
                continue

            delay = float(config.get("fetch", {}).get("delay_seconds", 2))
            sync_cfg = config.get("sync") if isinstance(config.get("sync"), dict) else {}
            stagger = float(sync_cfg.get("stagger_seconds", min(delay, 1.0)))

            async def run_candidate(video_index: int, video: CreatorVideo) -> str:
                update_run(
                    conn,
                    run_id,
                    current_stage="处理视频",
                    current_creator=video.creator_name,
                    current_video_id=video.video_id,
                )
                print(f"PROGRESS {video_index}/{len(candidates)} {video.video_id}")
                return await process_video(
                    crawler=crawler,
                    conn=conn,
                    video=video,
                    config=config,
                    output_base=output_base,
                    skip_summary=args.skip_summary or not args.with_ai_summary,
                    no_cleanup=args.no_cleanup,
                    run_id=run_id,
                )

            active: set[asyncio.Task[str]] = set()
            next_index = 0
            while next_index < len(candidates) or active:
                while next_index < len(candidates) and len(active) < max_concurrency and not pause_requested(args):
                    video = candidates[next_index]
                    active.add(asyncio.create_task(run_candidate(next_index + 1, video)))
                    next_index += 1
                    if stagger > 0 and next_index < len(candidates) and len(active) < max_concurrency:
                        await asyncio.sleep(stagger)
                if pause_requested(args) and next_index < len(candidates):
                    paused = True
                    update_run(
                        conn,
                        run_id,
                        current_stage="正在暂停，等待已开始的内容完成",
                        current_creator=str(creator_name),
                        current_video_id="",
                    )
                    print(f"PAUSE requested; wait active={len(active)} remaining={len(candidates) - next_index}")
                if not active:
                    break
                done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        result_status = task.result()
                    except Exception as exc:  # noqa: BLE001 - keep the rest of the run alive.
                        print(f"ERROR worker_task {type(exc).__name__}: {exc}")
                        result_status = "error"
                    if result_status not in ("ok", "raw"):
                        had_errors = True
                    total_processed += 1
                if paused and not active:
                    break

            if paused:
                break

        update_run_counts(conn, run_id)
        final_status = "paused" if paused else ("done_with_errors" if had_errors else "done")
        final_stage = "已暂停，点击继续后会跳过已完成内容" if paused else "完成"
        update_run(conn, run_id, status=final_status, current_stage=final_stage, output_path=final_output_path, ended=True)
        if paused:
            print(f"PAUSED seen={total_seen} processed={total_processed} output={vault_path}")
            return 0
        print(f"DONE seen={total_seen} processed={total_processed} output={vault_path}")
        return 0
    except Exception as exc:
        update_run(conn, run_id, status="failed", current_stage="失败", error=f"{type(exc).__name__}: {exc}", ended=True)
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Douyin creator videos into Obsidian Markdown notes.")
    parser.add_argument("--config", default="local_tools/obsidian_sync/creators.yaml", help="YAML config path.")
    parser.add_argument("--creator", default=None, help="Only sync one creator key.")
    parser.add_argument("--creators", action="append", default=None, help="Only sync selected creator keys. Repeat or pass comma-separated keys.")
    parser.add_argument("--video-id", action="append", default=None, help="Only process selected video id. Repeat or pass comma-separated ids.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N new videos per creator.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and list candidates without downloading.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos already marked ok.")
    parser.add_argument("--skip-summary", action="store_true",
                        help="Write raw notes without AI summary.")
    parser.add_argument("--with-ai-summary", dest="with_ai_summary", action="store_true", default=True,
                        help="Call the configured AI model for summaries. This is the default dashboard behaviour.")
    parser.add_argument("--with-deepseek", dest="with_ai_summary", action="store_true",
                        help="Deprecated alias for --with-ai-summary.")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary video/audio files for debugging.")
    parser.add_argument("--run-id", default=None, help="Stable run id for dashboard tracking.")
    parser.add_argument("--concurrency", type=int, default=None, help="Parallel video pipelines per creator. Clamped to 1-2.")
    parser.add_argument("--full-history", action="store_true", help="Scan deep history for platforms that support it, e.g. Weibo and podcast RSS.")
    parser.add_argument("--recent-days", type=int, default=0, help="Only process items published within the last N days. Older seen items are skipped for this run.")
    parser.add_argument("--no-content-filter", action="store_true", help="Disable pre-download filters for sponsored, promotional, live-announcement, and obvious non-speaking content.")
    parser.add_argument("--pause-file", default="", help="Stop launching new work when this file exists, then mark the run paused.")
    parser.add_argument("--allow-concurrent", action="store_true", help="Allow this sync process to run while another sync.py process is active.")
    return parser.parse_args()


def acquire_sync_lock(args: argparse.Namespace) -> bool:
    global SYNC_LOCK_HANDLE
    if args.allow_concurrent:
        return True
    try:
        config = load_yaml(project_path(args.config))
        work_dir = project_path(str(config.get("work_dir", "local_tools/obsidian_sync/work")))
    except Exception:
        work_dir = PROJECT_ROOT / "local_tools/obsidian_sync/work"
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / "sync.lock"
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"SKIP another sync process is already running: lock={lock_path}", flush=True)
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\nstarted_at={utc_now()}\ncommand={' '.join(sys.argv)}\n")
    handle.flush()
    SYNC_LOCK_HANDLE = handle
    return True


def main() -> int:
    args = parse_args()
    if not acquire_sync_lock(args):
        return 0
    try:
        return asyncio.run(sync(args))
    except Exception as exc:  # noqa: BLE001 - dashboard should show the concise user-facing failure.
        if os.environ.get("OBSIDIAN_SYNC_DEBUG"):
            raise
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
