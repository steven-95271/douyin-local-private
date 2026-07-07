#!/usr/bin/env python3
"""
Daily health check for the local Obsidian content sync.

The check is intentionally conservative:
- it verifies whether today's scheduled all-source sync ran;
- it detects sources that were not scanned;
- it separates retryable transient failures from login/manual failures;
- it can retry only transient failures once, then writes a local report and
  optionally sends a concise Telegram message through Hermes.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "creators.yaml"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
RUNNABLE_PLATFORMS = {"douyin", "weibo", "x", "xiaoyuzhou", "wechat", "youtube"}
PLATFORM_LABELS = {
    "douyin": "抖音",
    "weibo": "微博",
    "x": "X",
    "xiaoyuzhou": "小宇宙",
    "wechat": "公众号",
    "xiaohongshu": "小红书",
    "youtube": "YouTube",
}
AUTH_MARKERS = (
    "Cookie 已失效",
    "Cookie/token 已失效",
    "登录态",
    "未登录",
    "Cookie 缺失",
    "token 缺失",
    "不被服务端认可",
)
RETRYABLE_MARKERS = (
    "RemoteProtocolError",
    "peer closed connection",
    "Timeout",
    "timeout",
    "timed out",
    "ReadTimeout",
    "ConnectionError",
    "APIConnectionError",
    "HTTP状态错误: 504",
    "HTTP 504",
    "Temporary failure",
    "Connection reset",
)
SUMMARY_MARKERS = ("AI 总结失败", "summary failed")
MANUAL_MARKERS = (
    "没有从公众号文章里解析到正文",
    "没有解析到正文",
    "文章不可公开",
    "链接是公开的",
    "not found",
    "404",
)


@dataclass
class Creator:
    key: str
    name: str
    platform: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def normalize_platform(value: Any, url: str = "") -> str:
    raw = str(value or "").strip().lower()
    text = str(url or "").lower()
    if raw:
        return raw
    if "weibo.com" in text or "weibo.cn" in text:
        return "weibo"
    if "x.com" in text or "twitter.com" in text:
        return "x"
    if "xiaoyuzhoufm.com" in text:
        return "xiaoyuzhou"
    if "mp.weixin.qq.com" in text or "weixin.qq.com" in text:
        return "wechat"
    if "xiaohongshu.com" in text:
        return "xiaohongshu"
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    return "douyin"


def source_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform or "未知平台")


def enabled_runnable_creators(config: Dict[str, Any]) -> list[Creator]:
    creators: list[Creator] = []
    for raw in config.get("creators") or []:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        platform = normalize_platform(raw.get("platform"), str(raw.get("url") or ""))
        if platform not in RUNNABLE_PLATFORMS:
            continue
        creators.append(Creator(key=key, name=str(raw.get("name") or key), platform=platform))
    return creators


def configured_state_path(config: Dict[str, Any]) -> Path:
    return project_path(str(config.get("state_db", "local_tools/obsidian_sync/state.sqlite")))


def configured_work_dir(config: Dict[str, Any]) -> Path:
    return project_path(str(config.get("work_dir", "local_tools/obsidian_sync/work")))


def health_config(config: Dict[str, Any]) -> Dict[str, Any]:
    daily_report = config.get("daily_report") if isinstance(config.get("daily_report"), dict) else {}
    daily_hermes = daily_report.get("hermes") if isinstance(daily_report.get("hermes"), dict) else {}
    current = config.get("health_check") if isinstance(config.get("health_check"), dict) else {}
    merged: Dict[str, Any] = {
        "output_subdir": "Douyin/自检",
        "auto_retry": True,
        "retry_limit_per_source": 20,
        "retry_recent_days": 3,
        "max_retry_items": 8,
        "hermes": dict(daily_hermes),
    }
    for key, value in current.items():
        if key == "hermes" and isinstance(value, dict):
            hermes = dict(merged.get("hermes") or {})
            hermes.update(value)
            merged["hermes"] = hermes
        else:
            merged[key] = value
    return merged


def parse_local_date(value: Optional[str]) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.now(LOCAL_TZ).date()


def local_day_range(target: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target, time.min, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end = (datetime.combine(target, time.min, tzinfo=LOCAL_TZ) + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def compact_error(value: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def classify_failure(error: str, video_id: str = "") -> Dict[str, Any]:
    text = str(error or "")
    if any(marker in text for marker in AUTH_MARKERS):
        return {
            "category": "auth",
            "retryable": False,
            "label": "登录态失效",
            "advice": "需要重新登录并导入 Cookie/token 后再重爬。",
        }
    if any(marker in text for marker in SUMMARY_MARKERS):
        return {
            "category": "summary",
            "retryable": not video_id.startswith("source_error"),
            "label": "AI 总结失败",
            "advice": "原文通常已写入，稍后自动重试总结即可。",
        }
    if any(marker in text for marker in MANUAL_MARKERS):
        return {
            "category": "manual",
            "retryable": False,
            "label": "需要人工确认",
            "advice": "可能是文章权限、链接失效或平台返回异常，需要手动看一眼。",
        }
    if any(marker.lower() in text.lower() for marker in RETRYABLE_MARKERS):
        return {
            "category": "transient",
            "retryable": True,
            "label": "网络/平台临时中断",
            "advice": "适合自动重试一次。",
        }
    return {
        "category": "unknown",
        "retryable": False,
        "label": "未知失败",
        "advice": "先保留现场，不自动重试，避免重复消耗资源。",
    }


def latest_primary_run(conn: sqlite3.Connection, target: date, expected_creators: int) -> Dict[str, Any]:
    start, end = local_day_range(target)
    pattern = f"run_{target.strftime('%Y%m%d')}_%"
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM runs
        WHERE started_at >= ?
          AND started_at < ?
          AND run_id LIKE ?
          AND COALESCE(creator_filter, '') = ''
        ORDER BY total_creators DESC, started_at ASC
        LIMIT 5
        """,
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), pattern),
    ).fetchall()
    if not rows:
        return {}
    preferred = None
    for row in rows:
        data = dict(row)
        if int(data.get("total_creators") or 0) >= max(1, min(expected_creators, expected_creators // 2)):
            preferred = data
            break
    return preferred or dict(rows[0])


def run_items(conn: sqlite3.Connection, run_id: str) -> list[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT run_id, video_id, creator_key, creator_name, title, source_url,
               status, stage, error, markdown_path, updated_at
        FROM run_items
        WHERE run_id = ?
        ORDER BY updated_at ASC, video_id ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def read_text_if_exists(path: Path, tail_chars: int = 800_000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-tail_chars:]


def log_candidates_for_run(config: Dict[str, Any], run_id: str) -> tuple[set[str], list[str]]:
    log_paths = [
        Path.home() / "Library" / "Logs" / "douyin-local-private" / "content_sync.log",
        configured_work_dir(config) / "logs" / "dashboard_sync.log",
    ]
    scanned: set[str] = set()
    lines: list[str] = []
    for path in log_paths:
        text = read_text_if_exists(path)
        if not text or f"RUN {run_id}" not in text:
            continue
        source_lines = text.splitlines()
        starts = [i for i, raw in enumerate(source_lines) if raw.strip() == f"RUN {run_id}"]
        if not starts:
            continue
        start = starts[-1]
        end = len(source_lines)
        for idx in range(start + 1, len(source_lines)):
            raw = source_lines[idx].strip()
            if raw.startswith("RUN ") and raw != f"RUN {run_id}":
                end = idx
                break
            if raw.startswith("[") and ("CONTENT SYNC DONE" in raw or "RETRY_FAILED" in raw):
                end = idx + 1
                break
        segment = source_lines[start:end]
        lines.extend(segment)
        for raw in segment:
            match = re.match(r"^CANDIDATES\s+(\S+)\s+count=", raw.strip())
            if match:
                scanned.add(match.group(1))
    return scanned, lines


def status_counts(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def retry_jobs_for_items(items: list[Dict[str, Any]], max_items: int) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    retryable: list[Dict[str, Any]] = []
    manual: list[Dict[str, Any]] = []
    for item in items:
        failure = classify_failure(str(item.get("error") or ""), str(item.get("video_id") or ""))
        enriched = {**item, "failure": failure}
        if failure["retryable"]:
            retryable.append(enriched)
        else:
            manual.append(enriched)
    retryable = retryable[: max(0, max_items)]
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in retryable:
        creator_key = str(item.get("creator_key") or "").strip()
        if not creator_key:
            manual.append({**item, "failure": {**item["failure"], "retryable": False, "advice": "缺少内容源 key，无法自动重试。"}})
            continue
        job = grouped.setdefault(
            creator_key,
            {
                "creator_key": creator_key,
                "creator_name": str(item.get("creator_name") or creator_key),
                "video_ids": [],
                "source_retry": False,
                "items": [],
            },
        )
        video_id = str(item.get("video_id") or "").strip()
        if video_id.startswith("source_error"):
            job["source_retry"] = True
        elif video_id:
            job["video_ids"].append(video_id)
        job["items"].append(item)
    return list(grouped.values()), manual


def run_retry_jobs(
    config_path: Path,
    config: Dict[str, Any],
    jobs: list[Dict[str, Any]],
    *,
    dry_run: bool,
) -> list[Dict[str, Any]]:
    cfg = health_config(config)
    log_dir = Path.home() / "Library" / "Logs" / "douyin-local-private"
    log_dir.mkdir(parents=True, exist_ok=True)
    retry_log = log_dir / "health_retry.log"
    results: list[Dict[str, Any]] = []
    if not jobs:
        return results
    if dry_run:
        return [{**job, "status": "dry_run", "returncode": 0} for job in jobs]
    for index, job in enumerate(jobs, start=1):
        run_id = f"health_retry_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{index}"
        cmd = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "local_tools" / "obsidian_sync" / "sync.py"),
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--creator",
            str(job["creator_key"]),
            "--limit",
            str(max(int(cfg.get("retry_limit_per_source", 20)), len(job.get("video_ids") or []), 1)),
            "--with-ai-summary",
            "--force",
        ]
        if job.get("source_retry"):
            cmd.extend(["--recent-days", str(int(cfg.get("retry_recent_days", 3)))])
        else:
            ids = sorted({str(value) for value in job.get("video_ids") or [] if value})
            if ids:
                cmd.extend(["--video-id", ",".join(ids)])
        started = utc_now()
        with retry_log.open("a", encoding="utf-8") as log:
            log.write(f"\n[{started}] HEALTH_RETRY {run_id} {' '.join(cmd)}\n")
            log.flush()
            completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log, text=True)
        results.append(
            {
                **job,
                "run_id": run_id,
                "status": "done" if completed.returncode == 0 else "failed",
                "returncode": completed.returncode,
                "started_at": started,
                "ended_at": utc_now(),
            }
        )
    return results


def run_status_by_id(config: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    state_path = configured_state_path(config)
    if not run_id or not state_path.exists():
        return {}
    with sqlite3.connect(str(state_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else {}


def failed_run_items_by_run_id(config: Dict[str, Any], run_id: str) -> list[Dict[str, Any]]:
    state_path = configured_state_path(config)
    if not run_id or not state_path.exists():
        return []
    with sqlite3.connect(str(state_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_id, video_id, creator_key, creator_name, title, source_url,
                   status, stage, error, markdown_path, updated_at
            FROM run_items
            WHERE run_id = ? AND status = 'failed'
            ORDER BY updated_at ASC
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def video_statuses(config: Dict[str, Any], video_ids: list[str]) -> Dict[str, Dict[str, Any]]:
    if not video_ids:
        return {}
    state_path = configured_state_path(config)
    if not state_path.exists():
        return {}
    placeholders = ",".join("?" for _ in video_ids)
    with sqlite3.connect(str(state_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT video_id, status, error, markdown_path, updated_at
            FROM videos
            WHERE video_id IN ({placeholders})
            """,
            video_ids,
        ).fetchall()
    return {str(row["video_id"]): dict(row) for row in rows}


def health_status_from_findings(
    *,
    primary_run: Dict[str, Any],
    missing_sources: list[Creator],
    failed_items: list[Dict[str, Any]],
    manual_items: list[Dict[str, Any]],
    retry_results: list[Dict[str, Any]],
) -> str:
    if not primary_run:
        return "bad"
    run_status = str(primary_run.get("status") or "")
    if run_status in {"failed", "stopped", "paused", "unknown"}:
        return "bad"
    if missing_sources:
        return "bad"
    if manual_items:
        return "bad"
    if failed_items:
        return "warn"
    if retry_results and any(result.get("returncode") for result in retry_results):
        return "warn"
    if run_status == "done_with_errors":
        return "warn"
    return "ok"


def status_label(status: str) -> str:
    return {
        "ok": "正常",
        "warn": "有可重试问题",
        "bad": "需要处理",
    }.get(status, status or "未知")


def write_health_report(config: Dict[str, Any], target: date, report: Dict[str, Any]) -> Path:
    cfg = health_config(config)
    vault = Path(str(config["vault_path"])).expanduser()
    base = vault / str(cfg.get("output_subdir", "Douyin/自检"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"内容同步自检-{target.isoformat()}.md"
    primary = report.get("primary_run") or {}
    lines = [
        "---",
        "source: local_content_sync",
        "report_type: health_check",
        f"date: {target.isoformat()}",
        f"generated_at: {report.get('checked_at', utc_now())}",
        f"status: {report.get('status')}",
        "tags:",
        "  - 内容同步",
        "  - 自检",
        "---",
        "",
        f"# 内容同步自检 {target.isoformat()}",
        "",
        f"状态：{status_label(str(report.get('status') or ''))}",
        "",
        "## 今日任务",
        "",
    ]
    if primary:
        lines.extend(
            [
                f"- 运行编号：`{primary.get('run_id')}`",
                f"- 运行状态：{primary.get('status')}",
                f"- 开始时间：{primary.get('started_at') or '-'}",
                f"- 结束时间：{primary.get('ended_at') or '-'}",
                f"- 嗅探总数：{primary.get('detected_count', 0)}",
                f"- 本轮计划：{primary.get('planned_count', 0)}",
                f"- 成功：{primary.get('success_count', 0)}",
                f"- 失败：{primary.get('failed_count', 0)}",
                f"- 跳过：{primary.get('skipped_count', 0)}",
            ]
        )
    else:
        lines.append("- 没有找到今天 00:00 的全量同步任务。")
    lines.extend(["", "## 来源覆盖", ""])
    lines.append(f"- 启用可抓取来源：{report.get('source_total', 0)}")
    lines.append(f"- 已扫描/尝试来源：{report.get('source_attempted', 0)}")
    missing = report.get("missing_sources") or []
    if missing:
        lines.append("- 漏扫来源：")
        for item in missing:
            lines.append(f"  - {item.get('name')}（{source_label(item.get('platform', ''))}）")
    else:
        lines.append("- 漏扫来源：无")
    lines.extend(["", "## 失败处理", ""])
    failed = report.get("failed_items") or []
    if not failed:
        lines.append("没有残留失败项。")
    else:
        lines.append(f"失败项：{len(failed)}")
        for item in failed[:20]:
            failure = item.get("failure") or {}
            lines.append(
                f"- {item.get('creator_name')} / {item.get('title') or item.get('video_id')}："
                f"{failure.get('label')}，{compact_error(str(item.get('error') or ''))}"
            )
    retry_results = report.get("retry_results") or []
    if retry_results:
        lines.extend(["", "## 自动重试", ""])
        for result in retry_results:
            lines.append(
                f"- {result.get('creator_name')}：{len(result.get('items') or [])} 条，"
                f"运行 `{result.get('run_id')}`，返回码 {result.get('returncode')}"
            )
    lines.extend(["", "## 建议", ""])
    actions = report.get("actions") or []
    if actions:
        for action in actions:
            lines.append(f"- {action}")
    else:
        lines.append("- 今天无需处理。")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def telegram_text(report: Dict[str, Any], report_path: Path) -> str:
    primary = report.get("primary_run") or {}
    status = status_label(str(report.get("status") or ""))
    lines = [
        f"内容同步自检 {report.get('date')}",
        "",
        f"状态：{status}",
    ]
    if primary:
        lines.append(
            f"任务：{primary.get('status')}，成功 {primary.get('success_count', 0)}，"
            f"失败 {primary.get('failed_count', 0)}，跳过 {primary.get('skipped_count', 0)}"
        )
    else:
        lines.append("任务：今天没有找到自动同步任务")
    lines.append(f"来源：已扫 {report.get('source_attempted', 0)}/{report.get('source_total', 0)}")
    failed = report.get("failed_items") or []
    if failed:
        lines.append(f"失败项：{len(failed)}")
        for item in failed[:5]:
            failure = item.get("failure") or {}
            lines.append(f"- {item.get('creator_name')}：{failure.get('label')} / {item.get('title') or item.get('video_id')}")
    retry_results = report.get("retry_results") or []
    if retry_results:
        lines.append(f"自动重试：{sum(len(item.get('items') or []) for item in retry_results)} 条")
    actions = report.get("actions") or []
    if actions:
        lines.append("")
        lines.append("需要处理：")
        for action in actions[:6]:
            lines.append(f"- {action}")
    lines.append("")
    lines.append(f"完整报告：{report_path}")
    return "\n".join(lines)


def send_with_hermes(config: Dict[str, Any], message: str, *, dry_run: bool) -> int:
    cfg = health_config(config)
    hermes_cfg = cfg.get("hermes") if isinstance(cfg.get("hermes"), dict) else {}
    if not hermes_cfg.get("enabled", True):
        print("HERMES disabled")
        return 0
    bot_username = str(hermes_cfg.get("bot_username") or "@Steven_Secretary_bot").strip()
    prompt = (
        f"请通过 Telegram 机器人 {bot_username} 把下面这份内容同步自检报告发送给我。"
        "只发送正文，不需要解释。"
        f"\n\n{message}"
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
    completed = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=int(hermes_cfg.get("timeout_seconds", 120)),
    )
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    print(f"HERMES EXIT {completed.returncode}")
    return completed.returncode


def build_actions(
    *,
    primary_run: Dict[str, Any],
    missing_sources: list[Creator],
    failed_items: list[Dict[str, Any]],
    manual_items: list[Dict[str, Any]],
    retry_results: list[Dict[str, Any]],
) -> list[str]:
    actions: list[str] = []
    if not primary_run:
        actions.append("今天没有找到自动同步任务，请确认 Mac 在 00:00 时开机联网，或手动运行同步。")
    elif str(primary_run.get("status") or "") in {"failed", "stopped", "paused", "unknown"}:
        actions.append(f"今日任务状态为 {primary_run.get('status')}，建议打开 Dashboard 查看运行记录。")
    if missing_sources:
        names = "、".join(f"{item.name}({source_label(item.platform)})" for item in missing_sources[:8])
        actions.append(f"有 {len(missing_sources)} 个来源本轮没有扫到：{names}。")
    auth_items = [item for item in manual_items if (item.get("failure") or {}).get("category") == "auth"]
    if auth_items:
        names = "、".join(sorted({str(item.get("creator_name") or item.get("creator_key")) for item in auth_items})[:8])
        actions.append(f"登录态问题：{names}，需要重新登录/导入 Cookie 后重爬。")
    other_manual = [item for item in manual_items if (item.get("failure") or {}).get("category") != "auth"]
    if other_manual:
        actions.append(f"有 {len(other_manual)} 条失败需要人工确认，主要是文章权限、正文解析或未知错误。")
    retry_failed = [
        result
        for result in retry_results
        if result.get("returncode")
        or int(((result.get("run_status") if isinstance(result.get("run_status"), dict) else {}) or {}).get("failed_count") or 0) > 0
    ]
    if retry_failed:
        actions.append(f"自动重试后仍有 {len(retry_failed)} 组失败，需要在 Dashboard 里看失败项。")
    if failed_items and not retry_results and not manual_items:
        actions.append("存在可重试失败项，但本次没有开启自动重试。")
    return actions


def run_check(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = project_path(str(args.config))
    config = load_yaml(config_path)
    cfg = health_config(config)
    target = parse_local_date(args.date)
    creators = enabled_runnable_creators(config)
    creator_by_key = {creator.key: creator for creator in creators}
    state_path = configured_state_path(config)
    if not state_path.exists():
        primary_run: Dict[str, Any] = {}
        items: list[Dict[str, Any]] = []
    else:
        with sqlite3.connect(str(state_path)) as conn:
            primary_run = latest_primary_run(conn, target, len(creators))
            items = run_items(conn, str(primary_run.get("run_id") or "")) if primary_run else []
    scanned_from_log, log_lines = log_candidates_for_run(config, str(primary_run.get("run_id") or "")) if primary_run else (set(), [])
    attempted_keys = set(scanned_from_log)
    attempted_keys.update(str(item.get("creator_key") or "") for item in items if item.get("creator_key"))
    missing_sources = [creator for creator in creators if creator.key not in attempted_keys]
    failed_items = [item for item in items if str(item.get("status") or "") == "failed"]
    classified_failed = []
    for item in failed_items:
        classified_failed.append(
            {
                **item,
                "failure": classify_failure(str(item.get("error") or ""), str(item.get("video_id") or "")),
            }
        )
    auto_retry = cfg.get("auto_retry", True) and not args.no_auto_retry
    retry_jobs, manual_items = retry_jobs_for_items(classified_failed, int(cfg.get("max_retry_items", 8)))
    retry_results: list[Dict[str, Any]] = []
    if auto_retry and retry_jobs and str(primary_run.get("status") or "") in {"done", "done_with_errors"}:
        retry_results = run_retry_jobs(config_path, config, retry_jobs, dry_run=args.dry_run)
        retried_ids: list[str] = []
        for job in retry_jobs:
            retried_ids.extend(str(value) for value in job.get("video_ids") or [])
        latest_statuses = video_statuses(config, sorted(set(retried_ids)))
        for result in retry_results:
            retry_run = run_status_by_id(config, str(result.get("run_id") or ""))
            result["run_status"] = retry_run
            result["latest_video_statuses"] = {
                video_id: latest_statuses.get(video_id, {})
                for video_id in sorted(set(str(value) for value in result.get("video_ids") or []))
            }
    resolved_ids: set[str] = set()
    resolved_source_creators: set[str] = set()
    post_retry_failed: list[Dict[str, Any]] = []
    for result in retry_results:
        for video_id, status in (result.get("latest_video_statuses") or {}).items():
            if str(status.get("status") or "") in {"ok", "raw", "filtered"}:
                resolved_ids.add(str(video_id))
        retry_run = result.get("run_status") if isinstance(result.get("run_status"), dict) else {}
        if result.get("source_retry") and int(retry_run.get("failed_count") or 0) == 0 and str(retry_run.get("status") or "") in {"done", "done_with_errors"}:
            resolved_source_creators.add(str(result.get("creator_key") or ""))
        if int(retry_run.get("failed_count") or 0) > 0:
            for retry_item in failed_run_items_by_run_id(config, str(result.get("run_id") or "")):
                failure = classify_failure(str(retry_item.get("error") or ""), str(retry_item.get("video_id") or ""))
                post_retry_failed.append({**retry_item, "failure": failure})
    remaining_failed: list[Dict[str, Any]] = []
    resolved_items: list[Dict[str, Any]] = []
    for item in classified_failed:
        video_id = str(item.get("video_id") or "")
        creator_key = str(item.get("creator_key") or "")
        resolved = video_id in resolved_ids or (video_id.startswith("source_error") and creator_key in resolved_source_creators)
        if resolved:
            resolved_items.append({**item, "resolved_after_retry": True})
        else:
            remaining_failed.append(item)
    for item in post_retry_failed:
        failure = item.get("failure") or {}
        if not failure.get("retryable"):
            manual_items.append(item)
    actions = build_actions(
        primary_run=primary_run,
        missing_sources=missing_sources,
        failed_items=remaining_failed,
        manual_items=manual_items,
        retry_results=retry_results,
    )
    report: Dict[str, Any] = {
        "date": target.isoformat(),
        "checked_at": utc_now(),
        "primary_run": primary_run,
        "source_total": len(creators),
        "source_attempted": len(attempted_keys & set(creator_by_key)),
        "missing_sources": [
            {"key": item.key, "name": item.name, "platform": item.platform}
            for item in missing_sources
        ],
        "status_counts": status_counts(items),
        "failed_items": remaining_failed,
        "resolved_items": resolved_items,
        "manual_items": manual_items,
        "retry_jobs": retry_jobs,
        "retry_results": retry_results,
        "actions": actions,
        "log_excerpt": log_lines[-80:],
    }
    report["status"] = health_status_from_findings(
        primary_run=primary_run,
        missing_sources=missing_sources,
        failed_items=remaining_failed,
        manual_items=manual_items,
        retry_results=retry_results,
    )
    report_path = write_health_report(config, target, report)
    report["report_path"] = str(report_path)
    work_dir = configured_work_dir(config)
    work_dir.mkdir(parents=True, exist_ok=True)
    status_path = work_dir / "health_status.json"
    status_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if not args.no_hermes:
        try:
            report["telegram_returncode"] = send_with_hermes(config, telegram_text(report, report_path), dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - health report should still be written.
            report["telegram_error"] = f"{type(exc).__name__}: {exc}"
            print(f"HERMES ERROR {report['telegram_error']}", file=sys.stderr)
        status_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether the daily content sync ran cleanly.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--date", default=None, help="Local date in YYYY-MM-DD. Defaults to today in Asia/Shanghai.")
    parser.add_argument("--no-auto-retry", action="store_true", help="Only report failures; do not retry transient failures.")
    parser.add_argument("--no-hermes", action="store_true", help="Do not send Telegram notification through Hermes.")
    parser.add_argument("--dry-run", action="store_true", help="Do not actually retry or send Telegram; print intended actions.")
    return parser.parse_args()


def main() -> int:
    report = run_check(parse_args())
    print(f"HEALTH_STATUS {report['status']}")
    print(f"HEALTH_REPORT {report['report_path']}")
    primary = report.get("primary_run") or {}
    if primary:
        print(
            "HEALTH_RUN "
            f"{primary.get('run_id')} status={primary.get('status')} "
            f"success={primary.get('success_count', 0)} failed={primary.get('failed_count', 0)}"
        )
    print(
        "HEALTH_SOURCES "
        f"attempted={report.get('source_attempted', 0)} total={report.get('source_total', 0)} "
        f"missing={len(report.get('missing_sources') or [])}"
    )
    print(
        "HEALTH_FAILURES "
        f"failed={len(report.get('failed_items') or [])} "
        f"manual={len(report.get('manual_items') or [])} "
        f"retry_jobs={len(report.get('retry_jobs') or [])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
