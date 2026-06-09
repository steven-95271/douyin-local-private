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
import time
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
    conn.commit()


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

    model = WhisperModel(
        str(cfg.get("model", "small")),
        device=str(cfg.get("device", "cpu")),
        compute_type=str(cfg.get("compute_type", "int8")),
    )
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


def output_path_for_video(base_dir: Path, video: CreatorVideo) -> Path:
    date_prefix = format_date(video.create_time)
    filename = safe_filename(f"{video.title}-{video.creator_name}-{date_prefix}-{video.video_id}") + ".md"
    return base_dir / safe_filename(video.creator_name, 40) / filename


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        temp_name = handle.name
    Path(temp_name).replace(path)


async def process_video(
    crawler: Any,
    conn: sqlite3.Connection,
    video: CreatorVideo,
    config: Dict[str, Any],
    output_base: Path,
    skip_summary: bool,
    no_cleanup: bool,
) -> None:
    work_dir = project_path(str(config.get("work_dir", "local_tools/obsidian_sync/work")))
    media_dir = work_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None

    try:
        print(f"PROCESS {video.creator_name} {video.video_id} {video.title}")
        video_path = await download_video_to_temp(crawler, video, config.get("download", {}), media_dir)
        audio_path = media_dir / f"{video.video_id}.wav"
        extract_audio(video_path, audio_path)
        transcript = transcribe_audio(audio_path, config.get("transcribe", {}))

        if skip_summary:
            summary = "## 摘要\n\n未调用 AI 总结，本文件只包含逐字稿。"
            status = "ok"
        else:
            try:
                summary = call_deepseek_summary(video, transcript, config.get("summary", {}))
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - keep transcript even if summary fails.
                summary = f"## 摘要\n\nAI 总结失败：`{type(exc).__name__}: {exc}`"
                status = "summary_error"

        markdown = build_markdown(video, summary, transcript, status)
        markdown_path = output_path_for_video(output_base, video)
        atomic_write(markdown_path, markdown)
        mark_result(conn, video, status, markdown_path, None if status == "ok" else "summary failed", len(transcript))
        print(f"WROTE {markdown_path}")

    except Exception as exc:  # noqa: BLE001 - record failure and continue.
        message = f"{type(exc).__name__}: {exc}"
        mark_result(conn, video, "error", None, message, 0)
        print(f"ERROR {video.video_id} {message}")

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

    cookie_file = project_path(str(config.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))
    apply_runtime_cookies(read_secret_file(cookie_file), None)

    from crawlers.hybrid.hybrid_crawler import HybridCrawler

    state_path = project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_path))
    ensure_state(conn)

    vault_path = Path(str(config["vault_path"])).expanduser()
    output_base = vault_path / str(config.get("output_subdir", "Douyin/口播博主"))
    creators = [creator for creator in config.get("creators", []) if creator.get("enabled", True)]
    if args.creator:
        creators = [creator for creator in creators if str(creator.get("key")) == args.creator]

    if not creators:
        print("No enabled creators. Edit local_tools/obsidian_sync/creators.yaml first.")
        return 0

    crawler = HybridCrawler()
    total_seen = 0
    total_processed = 0

    for creator in creators:
        if not creator.get("key") or not creator.get("url"):
            print(f"SKIP invalid creator entry: {creator}")
            continue

        print(f"FETCH {creator.get('name') or creator.get('key')}")
        videos = await fetch_creator_videos(crawler, creator, config.get("fetch", {}))
        total_seen += len(videos)
        print(f"FOUND {len(videos)} videos")

        candidates: List[CreatorVideo] = []
        for video in videos:
            mark_seen(conn, video)
            status = get_status(conn, video.video_id)
            if status == "ok" and not args.force:
                continue
            candidates.append(video)

        if args.limit:
            candidates = candidates[: int(args.limit)]

        for video in candidates:
            if args.dry_run:
                print(f"DRY {video.creator_name} {video.video_id} {video.title} {video.source_url}")
                continue

            await process_video(
                crawler=crawler,
                conn=conn,
                video=video,
                config=config,
                output_base=output_base,
                skip_summary=args.skip_summary,
                no_cleanup=args.no_cleanup,
            )
            total_processed += 1
            delay = float(config.get("fetch", {}).get("delay_seconds", 2))
            if delay > 0:
                time.sleep(delay)

    conn.close()
    print(f"DONE seen={total_seen} processed={total_processed} output={output_base}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Douyin creator videos into Obsidian Markdown notes.")
    parser.add_argument("--config", default="local_tools/obsidian_sync/creators.yaml", help="YAML config path.")
    parser.add_argument("--creator", default=None, help="Only sync one creator key.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N new videos per creator.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and list candidates without downloading.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos already marked ok.")
    parser.add_argument("--skip-summary", action="store_true", help="Write transcript-only notes without DeepSeek summary.")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary video/audio files for debugging.")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(sync(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
