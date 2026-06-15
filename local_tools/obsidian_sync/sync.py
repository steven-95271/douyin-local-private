#!/usr/bin/env python3
"""
Sync selected Douyin creators into an Obsidian vault.

The MVP intentionally runs as a manual/cron-style command. It keeps durable
state in SQLite and deletes temporary media after Markdown is written.
"""

from __future__ import annotations

import argparse
import asyncio
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
from dataclasses import dataclass
from datetime import datetime, timezone
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


VIDEO_TYPES = {0, 4, 51, 55, 58, 61}
RUNNABLE_PLATFORMS = {"douyin", "weibo", "xiaoyuzhou", "wechat"}
WHISPER_MODEL_CACHE: Dict[Tuple[str, str, str], Any] = {}
WHISPER_MODEL_LOCK = threading.Lock()


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


def infer_platform_from_url(url: str) -> str:
    text = str(url or "").lower()
    if "mp.weixin.qq.com" in text or "weixin.qq.com" in text:
        return "wechat"
    if "xiaoyuzhoufm.com" in text or "feed.xyzfm.space" in text or "podcast.xyz" in text:
        return "xiaoyuzhou"
    if "weibo.com" in text or "weibo.cn" in text:
        return "weibo"
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
        "xiaoyuzhou": "Podcast/小宇宙",
        "wechat": "WeChat/公众号",
        "youtube": "YouTube/视频博主",
        "bilibili": "Bilibili/视频博主",
        "tiktok": "TikTok/视频博主",
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


def apply_cookie_for_creator(config: Dict[str, Any], creator: Optional[Dict[str, Any]] = None) -> None:
    apply_runtime_cookies(read_secret_file(cookie_profile_path(config, creator)), None)


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

    return CreatorVideo(
        creator_key=str(creator["key"]),
        creator_name=str(creator.get("name") or creator["key"]),
        video_id=video_id,
        source_url=f"https://www.douyin.com/video/{video_id}",
        title=title,
        create_time=create_time,
        raw=item,
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


def clean_wechat_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" -_|")


def extract_wechat_biz_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    return str(query.get("__biz", [""])[0]).strip()


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

    url = str(creator.get("url") or "").strip()
    if "mp.weixin.qq.com" not in url:
        raise RuntimeError("公众号暂不支持直接按主页回溯历史。请填写单篇文章 URL，或在内容源里补 RSS URL。")
    video = await fetch_wechat_article(creator, url, config)
    if conn and run_id:
        update_run(conn, run_id, current_stage="嗅探公众号文章", detected_count=1)
    return [video]


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
    if platform == "weibo":
        return await fetch_weibo_posts(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "xiaoyuzhou":
        return await fetch_xiaoyuzhou_episodes(creator, config, scan_limit, full_history, conn, run_id)
    if platform == "wechat":
        return await fetch_wechat_articles(creator, config, scan_limit, full_history, conn, run_id)

    fetch_cfg = config.get("fetch", {})
    sec_user_id = creator.get("sec_user_id")
    if not sec_user_id:
        sec_user_id = await crawler.DouyinWebCrawler.get_sec_user_id(str(creator["url"]))

    count = int(fetch_cfg.get("count_per_page", 20))
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
    if video.platform == "wechat":
        return project_path(str(cfg.get("wechat_prompt", "local_tools/obsidian_sync/prompts/wechat_summarize.md")))
    return project_path(str(cfg.get("prompt", "local_tools/obsidian_sync/prompts/summarize.md")))


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


def deepseek_chat_completion(cfg: Dict[str, Any], messages: List[Dict[str, str]], *, max_tokens: Optional[int] = None) -> str:
    api_key_env = str(cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing {api_key_env}. Put it in local_tools/obsidian_sync/.env.")
    base_url = str(cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    model = str(cfg.get("model", "deepseek-v4-flash"))
    model_aliases = {
        "deepseek-v4": "deepseek-v4-pro",
    }
    model = model_aliases.get(model, model)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": int(max_tokens or cfg.get("max_tokens", 5000)),
        "thinking": {"type": str(cfg.get("thinking", "disabled"))},
    }
    if str(cfg.get("thinking", "disabled")) == "disabled":
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
            raise RuntimeError(f"DeepSeek API {response.status_code}: {detail}")
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected DeepSeek response: {data}") from exc

    return str(content).strip()


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
        summary = deepseek_chat_completion(
            cfg,
            [
                {"role": "system", "content": chunk_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=int(cfg.get("chunk_max_tokens", 2500)),
        )
        summaries.append(f"### 分块 {index}/{len(chunks)}\n\n{summary}")
    return "\n\n".join(summaries)


def call_deepseek_summary(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> str:
    prompt_path = summary_prompt_path(video, cfg)
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

    return deepseek_chat_completion(
        cfg,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )


def build_frontmatter(video: CreatorVideo, status: str) -> str:
    tag_lines = "\n".join(f"  - {tag}" for tag in video.tags)
    if video.platform == "weibo":
        id_field = "post_id"
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
    else:
        id_field = "video_id"
        author_field = "creator"
        author_key_field = "creator_key"
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
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
    )


def build_douyin_markdown(video: CreatorVideo, summary: str, transcript: str, status: str) -> str:
    frontmatter = build_frontmatter(video, status)
    title = video.title if video.title != video.video_id else f"抖音视频 {video.video_id}"
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原视频]({video.source_url})\n\n",
    ]
    if summary.strip():
        parts.append(f"{summary.strip()}\n\n")
    parts.append("## 逐字稿\n\n")
    parts.append(f"{transcript.strip()}\n")
    if status == "raw":
        parts.append("\n## 笔记\n\n<!-- Claudian: 在此处生成结构化总结 -->\n")
    return "".join(parts)


def build_weibo_markdown(video: CreatorVideo, summary: str, original_text: str, status: str) -> str:
    frontmatter = build_frontmatter(video, status)
    title = video.title if video.title != video.video_id else f"微博 {format_date(video.create_time)}"
    parts = [
        frontmatter,
        f"# {title}\n\n",
        f"[原微博]({video.source_url})\n\n",
        "## 原文\n\n",
        f"{original_text.strip()}\n\n",
    ]
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


def build_wechat_markdown(video: CreatorVideo, summary: str, original_text: str, status: str) -> str:
    frontmatter = build_frontmatter(video, status)
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


def build_podcast_markdown(video: CreatorVideo, summary: str, transcript: str, status: str) -> str:
    frontmatter = build_frontmatter(video, status)
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
    parts.append("## 逐字稿\n\n")
    parts.append(f"{transcript.strip()}\n")
    if status == "raw":
        parts.append("\n## 笔记\n\n<!-- Claudian: 在此处生成结构化总结 -->\n")
    return "".join(parts)


def build_markdown(video: CreatorVideo, summary: str, transcript: str, status: str) -> str:
    if video.platform == "weibo":
        return build_weibo_markdown(video, summary, transcript, status)
    if video.platform == "wechat":
        return build_wechat_markdown(video, summary, transcript, status)
    if video.platform == "xiaoyuzhou":
        return build_podcast_markdown(video, summary, transcript, status)
    return build_douyin_markdown(video, summary, transcript, status)


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
    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None

    try:
        print(f"PROCESS {video.creator_name} {video.video_id} {video.title}")
        if video.platform in {"weibo", "wechat"}:
            source_label = "微博" if video.platform == "weibo" else "公众号"
            if run_id:
                update_run(conn, run_id, current_stage=f"读取{source_label}正文", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", f"读取{source_label}正文")
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
                    summary = await asyncio.to_thread(call_deepseek_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 - keep original text even if summary fails.
                    summary = f"## 要点\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"

            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status)
            markdown_path = output_path_for_video(output_base, video, conn)
            atomic_write(markdown_path, markdown)
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else f"AI 总结失败，但{source_label}原文已写入 Markdown"
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
                    summary = await asyncio.to_thread(call_deepseek_summary, video, transcript, summary_config_for_video(video, config))
                    status = "ok"
                except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                    summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                    status = "summary_error"

            if run_id:
                update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
                upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
            markdown = build_markdown(video, summary, transcript, status)
            markdown_path = output_path_for_video(output_base, video, conn)
            atomic_write(markdown_path, markdown)
            is_success = status in ("ok", "raw")
            mark_result(conn, video, status, markdown_path, None if is_success else "summary failed", len(transcript))
            if run_id:
                item_status = "success" if is_success else "failed"
                item_error = None if is_success else "AI 总结失败，但播客逐字稿已写入 Markdown"
                upsert_run_item(conn, run_id, video, item_status, "完成", item_error, markdown_path)
            print(f"WROTE {markdown_path}")
            return status

        if run_id:
            update_run(conn, run_id, current_stage="下载视频", current_creator=video.creator_name, current_video_id=video.video_id)
            upsert_run_item(conn, run_id, video, "running", "下载视频")
        video_path = await download_video_to_temp(crawler, video, config.get("download", {}), media_dir)
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
                summary = await asyncio.to_thread(call_deepseek_summary, video, transcript, summary_config_for_video(video, config))
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                status = "summary_error"

        if run_id:
            update_run(conn, run_id, current_stage="写入 Markdown", current_creator=video.creator_name, current_video_id=video.video_id)
            upsert_run_item(conn, run_id, video, "running", "写入 Markdown")
        markdown = build_markdown(video, summary, transcript, status)
        markdown_path = output_path_for_video(output_base, video, conn)
        atomic_write(markdown_path, markdown)
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

    apply_cookie_for_creator(config)

    from crawlers.hybrid.hybrid_crawler import HybridCrawler

    state_path = project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_path))
    ensure_state(conn)
    run_id = str(args.run_id or default_run_id())
    selected_video_ids = parse_video_id_filter(args.video_id)

    vault_path = Path(str(config["vault_path"])).expanduser()
    creators = [creator for creator in config.get("creators", []) if creator.get("enabled", True)]
    if args.creator:
        creators = [creator for creator in creators if str(creator.get("key")) == args.creator]
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
        print("No enabled runnable creators. Current runnable platforms: douyin, weibo, xiaoyuzhou, wechat.")
        return 0

    final_output_path = (
        output_base_for_platform(vault_path, config, creator_platform(creators[0]))
        if len(creators) == 1
        else vault_path
    )
    start_run(conn, run_id, args.creator, len(creators))
    print(f"RUN {run_id}")
    max_concurrency = configured_concurrency(config, args.concurrency)
    print(f"CONCURRENCY {max_concurrency}")
    crawler = HybridCrawler()
    total_seen = 0
    total_processed = 0
    had_errors = False

    try:
        total_creators = len(creators)
        for creator_index, creator in enumerate(creators, start=1):
            if not creator.get("key") or not creator.get("url"):
                print(f"SKIP invalid creator entry: {creator}")
                continue

            creator_name = creator.get("name") or creator.get("key")
            platform = creator_platform(creator)
            output_base = output_base_for_platform(vault_path, config, platform)
            apply_cookie_for_creator(config, creator)
            update_run(conn, run_id, current_stage="嗅探内容", current_creator=str(creator_name), current_video_id="")
            print(f"CREATOR {creator_index}/{total_creators} {creator_name}")
            print(f"OUTPUT {platform} {output_base}")
            print(f"FETCH {creator_name}")
            scan_limit = int(args.limit or 0) if platform in {"weibo", "xiaoyuzhou", "wechat"} and not args.full_history else 0
            videos = await fetch_creator_videos(crawler, creator, config, scan_limit, args.full_history, conn, run_id)
            total_seen += len(videos)
            update_run(conn, run_id, detected_count=total_seen)
            if platform == "weibo":
                item_label = "posts"
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
                status = get_status(conn, video.video_id)
                if status == "ok" and not args.force:
                    upsert_run_item(conn, run_id, video, "skipped", "已存在", "之前已成功处理，本轮跳过")
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
                    update_run(conn, run_id, current_stage="处理视频", current_creator=video.creator_name, current_video_id=video.video_id)
                    print(f"PROGRESS {video_index}/{len(candidates)} {video.video_id}")
                    upsert_run_item(conn, run_id, video, "dry_run", "候选", "只嗅探候选内容，未下载、未转录、未写入")
                    print(f"DRY {video.creator_name} {video.video_id} {video.title} {video.source_url}")
                continue

            semaphore = asyncio.Semaphore(max_concurrency)
            delay = float(config.get("fetch", {}).get("delay_seconds", 2))
            sync_cfg = config.get("sync") if isinstance(config.get("sync"), dict) else {}
            stagger = float(sync_cfg.get("stagger_seconds", min(delay, 1.0)))

            async def run_candidate(video_index: int, video: CreatorVideo) -> str:
                async with semaphore:
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
                        skip_summary=not args.with_deepseek,
                        no_cleanup=args.no_cleanup,
                        run_id=run_id,
                    )

            tasks = []
            for video_index, video in enumerate(candidates, start=1):
                tasks.append(asyncio.create_task(run_candidate(video_index, video)))
                if stagger > 0 and video_index < len(candidates):
                    await asyncio.sleep(stagger)

            for result_status in await asyncio.gather(*tasks):
                if result_status not in ("ok", "raw"):
                    had_errors = True
                total_processed += 1

        update_run_counts(conn, run_id)
        final_status = "done_with_errors" if had_errors else "done"
        update_run(conn, run_id, status=final_status, current_stage="完成", output_path=final_output_path, ended=True)
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
    parser.add_argument("--video-id", action="append", default=None, help="Only process selected video id. Repeat or pass comma-separated ids.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N new videos per creator.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and list candidates without downloading.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos already marked ok.")
    parser.add_argument("--skip-summary", action="store_true", default=True,
                        help="(Default) Write raw notes without AI summary; intended for Claudian in Obsidian.")
    parser.add_argument("--with-deepseek", action="store_true",
                        help="Call DeepSeek API for summaries (legacy behaviour).")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary video/audio files for debugging.")
    parser.add_argument("--run-id", default=None, help="Stable run id for dashboard tracking.")
    parser.add_argument("--concurrency", type=int, default=None, help="Parallel video pipelines per creator. Clamped to 1-2.")
    parser.add_argument("--full-history", action="store_true", help="Scan deep history for platforms that support it, e.g. Weibo and podcast RSS.")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(sync(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
