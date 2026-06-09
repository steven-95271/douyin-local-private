#!/usr/bin/env python3
"""
Sync selected Douyin creators into an Obsidian vault.

The MVP intentionally runs as a manual/cron-style command. It keeps durable
state in SQLite and deletes temporary media after Markdown is written.
"""

from __future__ import annotations

import argparse
import asyncio
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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    text = re.sub(r"#\S+", "", text or "")
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
    text = re.sub(r"#\S+", "", text or "")
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
    )


async def fetch_creator_videos(crawler: Any, creator: Dict[str, Any], fetch_cfg: Dict[str, Any]) -> List[CreatorVideo]:
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


def extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
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
        segments, _info = model.transcribe(
            str(audio_path),
            language=str(cfg.get("language", "zh")),
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


def call_deepseek_summary(video: CreatorVideo, transcript: str, cfg: Dict[str, Any]) -> str:
    api_key_env = str(cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing {api_key_env}. Put it in local_tools/obsidian_sync/.env.")

    prompt_path = project_path(str(cfg.get("prompt", "local_tools/obsidian_sync/prompts/summarize.md")))
    system_prompt = prompt_path.read_text(encoding="utf-8")
    base_url = str(cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    user_content = (
        "视频元数据：\n"
        f"- 博主：{video.creator_name}\n"
        f"- 标题/描述：{video.title}\n"
        f"- 链接：{video.source_url}\n"
        f"- 发布时间：{format_date(video.create_time)}\n\n"
        "逐字稿：\n"
        f"{transcript}"
    )

    payload: Dict[str, Any] = {
        "model": str(cfg.get("model", "deepseek-v4-flash")),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "max_tokens": int(cfg.get("max_tokens", 5000)),
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
        response.raise_for_status()
        data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected DeepSeek response: {data}") from exc

    return str(content).strip()


def build_markdown(video: CreatorVideo, summary: str, transcript: str, status: str) -> str:
    tag_lines = "\n".join(f"  - {tag}" for tag in video.tags)
    frontmatter = (
        "---\n"
        "source: douyin\n"
        f"creator: {format_frontmatter_value(video.creator_name)}\n"
        f"creator_key: {format_frontmatter_value(video.creator_key)}\n"
        f"video_id: {format_frontmatter_value(video.video_id)}\n"
        f"url: {format_frontmatter_value(video.source_url)}\n"
        f"published_at: {format_frontmatter_value(format_date(video.create_time))}\n"
        f"processed_at: {format_frontmatter_value(utc_now())}\n"
        f"sync_status: {format_frontmatter_value(status)}\n"
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
    )
    title = video.title if video.title != video.video_id else f"抖音视频 {video.video_id}"
    return (
        frontmatter +
        f"# {title}\n\n"
        f"[原视频]({video.source_url})\n\n"
        f"{summary.strip()}\n\n"
        "## 逐字稿\n\n"
        f"{transcript.strip()}\n"
    )


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
            summary = "## 摘要\n\n未调用 AI 总结，本文件只包含逐字稿。"
            status = "ok"
        else:
            try:
                if run_id:
                    update_run(conn, run_id, current_stage="AI 总结", current_creator=video.creator_name, current_video_id=video.video_id)
                    upsert_run_item(conn, run_id, video, "running", "AI 总结")
                summary = await asyncio.to_thread(call_deepseek_summary, video, transcript, config.get("summary", {}))
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
        mark_result(conn, video, status, markdown_path, None if status == "ok" else "summary failed", len(transcript))
        if run_id:
            item_status = "success" if status == "ok" else "failed"
            item_error = None if status == "ok" else "AI 总结失败，但逐字稿已写入 Markdown"
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

    vault_path = Path(str(config["vault_path"])).expanduser()
    output_base = vault_path / str(config.get("output_subdir", "Douyin/口播博主"))
    creators = [creator for creator in config.get("creators", []) if creator.get("enabled", True)]
    if args.creator:
        creators = [creator for creator in creators if str(creator.get("key")) == args.creator]

    if not creators:
        print("No enabled creators. Edit local_tools/obsidian_sync/creators.yaml first.")
        return 0

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
            apply_cookie_for_creator(config, creator)
            update_run(conn, run_id, current_stage="嗅探视频", current_creator=str(creator_name), current_video_id="")
            print(f"CREATOR {creator_index}/{total_creators} {creator_name}")
            print(f"FETCH {creator_name}")
            videos = await fetch_creator_videos(crawler, creator, config.get("fetch", {}))
            total_seen += len(videos)
            update_run(conn, run_id, detected_count=total_seen)
            print(f"FOUND {len(videos)} videos")

            candidates: List[CreatorVideo] = []
            for video in videos:
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
                    upsert_run_item(conn, run_id, video, "dry_run", "Dry run", "只演练，不下载和写入")
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
                        skip_summary=args.skip_summary,
                        no_cleanup=args.no_cleanup,
                        run_id=run_id,
                    )

            tasks = []
            for video_index, video in enumerate(candidates, start=1):
                tasks.append(asyncio.create_task(run_candidate(video_index, video)))
                if stagger > 0 and video_index < len(candidates):
                    await asyncio.sleep(stagger)

            for result_status in await asyncio.gather(*tasks):
                if result_status != "ok":
                    had_errors = True
                total_processed += 1

        update_run_counts(conn, run_id)
        final_status = "done_with_errors" if had_errors else "done"
        update_run(conn, run_id, status=final_status, current_stage="完成", output_path=output_base, ended=True)
        print(f"DONE seen={total_seen} processed={total_processed} output={output_base}")
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
    parser.add_argument("--limit", type=int, default=0, help="Process at most N new videos per creator.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and list candidates without downloading.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos already marked ok.")
    parser.add_argument("--skip-summary", action="store_true", help="Write transcript-only notes without DeepSeek summary.")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary video/audio files for debugging.")
    parser.add_argument("--run-id", default=None, help="Stable run id for dashboard tracking.")
    parser.add_argument("--concurrency", type=int, default=None, help="Parallel video pipelines per creator. Clamped to 1-2.")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(sync(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
