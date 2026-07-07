#!/usr/bin/env python3
"""
Generate Obsidian briefs from processed notes and optionally ask Hermes to send
a compact version to Telegram.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "creators.yaml"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
REPORT_META = {
    "weekly": {
        "label": "周报",
        "period_label": "本周",
        "title_prefix": "关注来源周报",
        "default_days": 7,
        "default_subdir": "Douyin/周报",
    },
    "daily": {
        "label": "日报",
        "period_label": "今日",
        "title_prefix": "关注来源日报",
        "default_days": 1,
        "default_subdir": "Douyin/日报",
    },
}


@dataclass
class ProcessedVideo:
    source: str
    video_id: str
    creator_key: str
    creator_name: str
    source_url: str
    title: str
    markdown_path: Path
    processed_at: str
    published_at: str
    summary: str
    points: List[str]
    keywords: str


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def default_report_range(period: str = "weekly", now: Optional[datetime] = None, days: Optional[int] = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    end = now.replace(microsecond=0)
    default_days = int(REPORT_META.get(period, REPORT_META["weekly"])["default_days"])
    start = end - timedelta(days=days or default_days)
    return start, end


def parse_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    data = {}
    for raw in text[4:end].splitlines():
        if ":" not in raw or raw.startswith(" "):
            continue
        key, value = raw.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def section(text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + next_match.start() if next_match else len(text)
    return text[start:end].strip()


def first_paragraph(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def truncate_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def bullets(text: str, limit: int = 5) -> List[str]:
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items[:limit]


def first_section(text: str, headings: List[str]) -> str:
    for heading in headings:
        value = section(text, heading)
        if value:
            return value
    return ""


def source_label(source: str) -> str:
    labels = {
        "douyin": "抖音",
        "weibo": "微博",
        "x": "X",
        "xiaoyuzhou": "小宇宙",
        "wechat": "公众号",
        "xiaohongshu": "小红书",
        "youtube": "YouTube",
        "bilibili": "Bilibili",
        "tiktok": "TikTok",
        "kuaishou": "快手",
        "tieba": "贴吧",
        "zhihu": "知乎",
    }
    return labels.get(source, source or "未知平台")


def creator_heading(video: ProcessedVideo) -> str:
    return f"{source_label(video.source)} · {video.creator_name}"


def report_config(config: Dict[str, Any], period: str) -> Dict[str, Any]:
    weekly = config.get("weekly_report") if isinstance(config.get("weekly_report"), dict) else {}
    current = config.get(f"{period}_report") if isinstance(config.get(f"{period}_report"), dict) else {}
    merged = dict(weekly)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def range_label(since: datetime, until: datetime, period: str) -> str:
    if period == "daily":
        return str(until.astimezone(LOCAL_TZ).date())
    start = since.astimezone(LOCAL_TZ).date()
    end = (until - timedelta(seconds=1)).astimezone(LOCAL_TZ).date()
    return f"{start} - {end}"


def load_processed_videos(config: Dict[str, Any], since: datetime, until: datetime) -> List[ProcessedVideo]:
    state_path = project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))
    if not state_path.exists():
        return []
    conn = sqlite3.connect(str(state_path))
    rows = conn.execute(
        """
        SELECT video_id, creator_key, creator_name, source_url, title, markdown_path, processed_at
        FROM videos
        WHERE status IN ('ok', 'summary_error')
          AND markdown_path IS NOT NULL
          AND processed_at >= ?
          AND processed_at < ?
        ORDER BY creator_name, processed_at
        """,
        (since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")),
    ).fetchall()
    conn.close()

    videos: List[ProcessedVideo] = []
    for row in rows:
        path = Path(str(row[5]))
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        summary_text = first_section(text, ["摘要", "内容摘要", "总结"])
        point_text = first_section(text, ["核心观点", "要点", "主要内容"])
        videos.append(
            ProcessedVideo(
                source=fm.get("source", ""),
                video_id=str(row[0]),
                creator_key=str(row[1]),
                creator_name=str(row[2]),
                source_url=str(row[3]),
                title=str(row[4] or path.stem),
                markdown_path=path,
                processed_at=str(row[6] or ""),
                published_at=fm.get("published_at", ""),
                summary=first_paragraph(summary_text),
                points=bullets(point_text),
                keywords=section(text, "关键词").replace("\n", " ").strip(),
            )
        )
    return videos


def group_by_creator(videos: List[ProcessedVideo]) -> Dict[str, List[ProcessedVideo]]:
    grouped: Dict[str, List[ProcessedVideo]] = {}
    for video in videos:
        grouped.setdefault(creator_heading(video), []).append(video)
    return grouped


def build_markdown(videos: List[ProcessedVideo], since: datetime, until: datetime, period: str = "weekly") -> str:
    meta = REPORT_META.get(period, REPORT_META["weekly"])
    label = str(meta["label"])
    period_label = str(meta["period_label"])
    title = f"{meta['title_prefix']} {range_label(since, until, period)}"
    grouped = group_by_creator(videos)
    total = len(videos)
    lines = [
        "---",
        "source: local_content_sync",
        f"report_type: {period}",
        f"range_start: {since.isoformat(timespec='seconds')}",
        f"range_end: {until.isoformat(timespec='seconds')}",
        f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "tags:",
        "  - 内容同步",
        f"  - {label}",
        "---",
        "",
        f"# {title}",
        "",
        f"{period_label}新增处理内容：{total} 条。",
        "",
    ]
    if not videos:
        lines.append(f"{period_label}没有新的已处理内容。")
        return "\n".join(lines).rstrip() + "\n"

    lines.extend(["## 速览", ""])
    for creator, items in grouped.items():
        lines.append(f"- {creator}：{len(items)} 条")
    lines.append("")

    for creator, items in grouped.items():
        lines.extend([f"## {creator}", ""])
        for idx, video in enumerate(items, 1):
            lines.extend([
                f"### {idx}. {video.title}",
                "",
                f"- 平台：{source_label(video.source)}",
                f"- 发布日期：{video.published_at or '未知'}",
                f"- 原文：{video.source_url}",
                f"- 笔记：[[{video.markdown_path.stem}]]",
                f"- 摘要：{video.summary or '无摘要'}",
            ])
            if video.points:
                lines.append("- 核心观点：")
                for point in video.points[:3]:
                    lines.append(f"  - {point}")
            if video.keywords:
                lines.append(f"- 关键词：{video.keywords}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_telegram_text(
    videos: List[ProcessedVideo],
    since: datetime,
    until: datetime,
    period: str = "weekly",
    config: Optional[Dict[str, Any]] = None,
) -> str:
    meta = REPORT_META.get(period, REPORT_META["weekly"])
    label = str(meta["label"])
    period_label = str(meta["period_label"])
    report_cfg = report_config(config or {}, period)
    telegram_cfg = report_cfg.get("telegram") if isinstance(report_cfg.get("telegram"), dict) else {}
    max_creators = int(telegram_cfg.get("max_creators", 8))
    max_items_per_creator = int(telegram_cfg.get("max_items_per_creator", 3))
    title_chars = int(telegram_cfg.get("title_chars", 72))
    summary_chars = int(telegram_cfg.get("summary_chars", 80))
    max_chars = int(telegram_cfg.get("max_chars", 3500))
    header = f"{meta['title_prefix']} {range_label(since, until, period)}"
    if not videos:
        return f"{header}\n\n{period_label}没有新的已处理内容。"
    lines = [header, "", f"{period_label}新增 {len(videos)} 条。"]
    grouped = sorted(group_by_creator(videos).items(), key=lambda item: len(item[1]), reverse=True)
    for creator, items in grouped[:max_creators]:
        lines.extend(["", f"{creator}：{len(items)} 条"])
        for idx, video in enumerate(items[:max_items_per_creator], 1):
            summary = video.summary or (video.points[0] if video.points else "")
            lines.append(f"{idx}. {truncate_text(video.title, title_chars)}")
            if summary:
                lines.append(f"   {truncate_text(summary, summary_chars)}")
        if len(items) > max_items_per_creator:
            lines.append(f"   另有 {len(items) - max_items_per_creator} 条见 Obsidian 完整{label}。")
    if len(grouped) > max_creators:
        lines.extend(["", f"另有 {len(grouped) - max_creators} 个来源见 Obsidian 完整{label}。"])
    text = "\n".join(lines)
    return truncate_text(text, max_chars)


def write_report(config: Dict[str, Any], markdown: str, since: datetime, until: datetime, period: str = "weekly") -> Path:
    meta = REPORT_META.get(period, REPORT_META["weekly"])
    report_cfg = report_config(config, period)
    vault = Path(str(config["vault_path"])).expanduser()
    base = vault / str(report_cfg.get("output_subdir", meta["default_subdir"]))
    base.mkdir(parents=True, exist_ok=True)
    safe_label = range_label(since, until, period).replace(" ", "")
    path = base / f"{meta['title_prefix']}-{safe_label}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def send_with_hermes(message: str, report_path: Path, config: Dict[str, Any], dry_run: bool, period: str = "weekly") -> int:
    meta = REPORT_META.get(period, REPORT_META["weekly"])
    label = str(meta["label"])
    report_cfg = report_config(config, period)
    hermes_cfg = report_cfg.get("hermes", {})
    if not hermes_cfg.get("enabled", True):
        print("HERMES disabled")
        return 0
    bot_username = str(hermes_cfg.get("bot_username") or "@Steven_Secretary_bot").strip()
    prompt = (
        f"请通过 Telegram 机器人 {bot_username} 把下面这份{label}精简内容发送给我。"
        "只发送正文，不需要解释。"
        f"\n\n完整{label}本地路径：{report_path}\n\n{message}"
    )
    if dry_run:
        print("HERMES DRY RUN")
        print(prompt)
        return 0
    cmd = ["hermes"]
    profile = hermes_cfg.get("profile")
    if profile:
        cmd.extend(["--profile", str(profile)])
    cmd.extend(["-z", prompt])
    model = hermes_cfg.get("model")
    if model:
        cmd.extend(["--model", str(model)])
    provider = hermes_cfg.get("provider")
    if provider:
        cmd.extend(["--provider", str(provider)])
    print("HERMES START")
    timeout_seconds = int(hermes_cfg.get("timeout_seconds", 120))
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"HERMES TIMEOUT {timeout_seconds}s", file=sys.stderr)
        if exc.stdout:
            print(str(exc.stdout).strip())
        if exc.stderr:
            print(str(exc.stderr).strip(), file=sys.stderr)
        return 124
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    print(f"HERMES EXIT {completed.returncode}")
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate local content briefs and optionally send with Hermes.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--period", choices=sorted(REPORT_META), default="weekly")
    parser.add_argument("--days", type=int, default=None, help="Default lookback window when --since is omitted.")
    parser.add_argument("--since", default=None, help="Inclusive date/datetime. Defaults to the period lookback.")
    parser.add_argument("--until", default=None, help="Exclusive date/datetime. Defaults to now.")
    parser.add_argument("--no-hermes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_date(value: Optional[str], fallback: datetime) -> datetime:
    if not value:
        return fallback
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main() -> int:
    args = parse_args()
    config = load_yaml(project_path(args.config))
    default_since, default_until = default_report_range(args.period, days=args.days)
    since = parse_date(args.since, default_since)
    until = parse_date(args.until, default_until)
    videos = load_processed_videos(config, since, until)
    markdown = build_markdown(videos, since, until, args.period)
    report_path = write_report(config, markdown, since, until, args.period)
    print(f"{args.period.upper()}_REPORT {report_path}")
    print(f"{args.period.upper()}_ITEMS {len(videos)}")
    if not args.no_hermes:
        text = build_telegram_text(videos, since, until, args.period, config)
        return send_with_hermes(text, report_path, config, args.dry_run, args.period)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
