#!/usr/bin/env python3
"""
Local batch downloader wrapper for personal, authorized use.

The script uses this project's internal crawlers directly. It does not call the
public demo service, and it does not persist cookies back into config files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def read_secret_file(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"Cookie file does not exist: {path}")
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def project_path(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    return path if path.is_absolute() else PROJECT_ROOT / path


def apply_runtime_cookies(douyin_cookie: Optional[str], tiktok_cookie: Optional[str]) -> None:
    if douyin_cookie:
        import crawlers.douyin.web.web_crawler as douyin_web_crawler

        douyin_web_crawler.config["TokenManager"]["douyin"]["headers"]["Cookie"] = douyin_cookie

    if tiktok_cookie:
        import crawlers.tiktok.web.web_crawler as tiktok_web_crawler

        tiktok_web_crawler.config["TokenManager"]["tiktok"]["headers"]["Cookie"] = tiktok_cookie


def safe_part(value: Any, default: str = "unknown") -> str:
    text = str(value or default)
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or default


def extension_from_response(url: str, content_type: Optional[str], fallback: str) -> str:
    if content_type:
        content_type = content_type.split(";")[0].strip().lower()
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed

    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return fallback


def load_successful_urls(manifest_path: Path) -> Set[str]:
    done: Set[str] = set()
    if not manifest_path.exists():
        return done

    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "ok" and row.get("url"):
            done.add(row["url"])
    return done


def append_manifest(manifest_path: Path, row: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


async def headers_for_platform(crawler: Any, platform: str) -> dict[str, str]:
    if platform == "douyin":
        bundle = await crawler.DouyinWebCrawler.get_douyin_headers()
    elif platform == "tiktok":
        bundle = await crawler.TikTokWebCrawler.get_tiktok_headers()
    elif platform == "bilibili":
        bundle = await crawler.BilibiliWebCrawler.get_bilibili_headers()
    else:
        bundle = {"headers": {}}
    return bundle.get("headers", {})


async def stream_to_file(
    url: str,
    destination: Path,
    headers: dict[str, str],
    timeout: float,
) -> Tuple[int, Optional[str]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".part")
    bytes_written = 0
    content_type: Optional[str] = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            with tmp_path.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)

    tmp_path.replace(destination)
    return bytes_written, content_type


@dataclass
class DownloadOptions:
    output_dir: Path
    watermark: str
    include_images: bool
    timeout: float
    dry_run: bool
    skip_existing: bool


async def download_parsed_item(crawler: Any, source_url: str, data: dict[str, Any], options: DownloadOptions) -> dict[str, Any]:
    platform = safe_part(data.get("platform"))
    item_type = data.get("type")
    video_id = safe_part(data.get("video_id"))
    item_dir = options.output_dir / f"{platform}_{item_type}"

    base = {
        "url": source_url,
        "platform": platform,
        "type": item_type,
        "video_id": video_id,
        "watermark": options.watermark,
    }

    if options.dry_run:
        return {**base, "status": "ok", "path": None, "bytes": 0, "dry_run": True}

    headers = await headers_for_platform(crawler, platform)

    if item_type == "video":
        video_data = data.get("video_data") or {}
        key = "wm_video_url_HQ" if options.watermark == "keep" else "nwm_video_url_HQ"
        video_url = video_data.get(key) or video_data.get(key.replace("_HQ", ""))
        if not video_url:
            raise RuntimeError(f"No downloadable video URL in parsed data for {video_id}")

        suffix = "_watermark" if options.watermark == "keep" else ""
        destination = item_dir / f"{platform}_{video_id}{suffix}.mp4"
        if options.skip_existing and destination.exists() and destination.stat().st_size > 0:
            return {**base, "status": "ok", "path": str(destination), "bytes": destination.stat().st_size, "skipped": True}

        size, _ = await stream_to_file(video_url, destination, headers, options.timeout)
        return {**base, "status": "ok", "path": str(destination), "bytes": size}

    if item_type == "image":
        if not options.include_images:
            return {**base, "status": "skipped", "reason": "image_post"}

        image_data = data.get("image_data") or {}
        key = "watermark_image_list" if options.watermark == "keep" else "no_watermark_image_list"
        image_urls = image_data.get(key) or []
        if not image_urls:
            raise RuntimeError(f"No downloadable image URLs in parsed data for {video_id}")

        target_dir = item_dir / f"{platform}_{video_id}_images"
        written_files: list[str] = []
        total = 0
        for index, image_url in enumerate(image_urls, start=1):
            provisional = target_dir / f"{index:03d}.bin"
            size, content_type = await stream_to_file(image_url, provisional, headers, options.timeout)
            suffix = extension_from_response(image_url, content_type, ".jpg")
            final_path = provisional.with_suffix(suffix)
            if final_path != provisional:
                provisional.replace(final_path)
            total += size
            written_files.append(str(final_path))

        return {**base, "status": "ok", "path": str(target_dir), "files": written_files, "bytes": total}

    return {**base, "status": "skipped", "reason": f"unsupported_type:{item_type}"}


async def process_url(
    crawler: Any,
    url: str,
    options: DownloadOptions,
    retries: int,
    delay: float,
) -> dict[str, Any]:
    last_error: Optional[str] = None
    for attempt in range(1, retries + 2):
        try:
            data = await crawler.hybrid_parsing_single_video(url, minimal=True)
            result = await download_parsed_item(crawler, url, data, options)
            result["attempt"] = attempt
            result["finished_at"] = utc_now()
            if delay > 0:
                await asyncio.sleep(delay)
            return result
        except Exception as exc:  # noqa: BLE001 - CLI should record failures and continue.
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                await asyncio.sleep(min(30.0, 2.0 * attempt))

    return {
        "url": url,
        "status": "error",
        "error": last_error,
        "attempt": retries + 1,
        "finished_at": utc_now(),
    }


async def run(args: argparse.Namespace) -> int:
    input_path = project_path(args.input)
    output_dir = project_path(args.output)
    manifest_path = args.manifest if args.manifest.is_absolute() else output_dir / args.manifest

    douyin_cookie = read_secret_file(project_path(args.douyin_cookie_file))
    tiktok_cookie = read_secret_file(project_path(args.tiktok_cookie_file))
    apply_runtime_cookies(douyin_cookie, tiktok_cookie)

    from crawlers.hybrid.hybrid_crawler import HybridCrawler

    urls = read_lines(input_path)
    if args.limit:
        urls = urls[: args.limit]

    already_done = load_successful_urls(manifest_path) if not args.no_resume else set()
    options = DownloadOptions(
        output_dir=output_dir,
        watermark=args.watermark,
        include_images=args.include_images,
        timeout=args.timeout,
        dry_run=args.dry_run,
        skip_existing=not args.no_skip_existing,
    )

    crawler = HybridCrawler()
    semaphore = asyncio.Semaphore(args.concurrency)
    counters = {"ok": 0, "error": 0, "skipped": 0}

    async def guarded(url: str) -> None:
        if args.resume and url in already_done:
            counters["skipped"] += 1
            print(f"SKIP done: {url}")
            return

        async with semaphore:
            result = await process_url(crawler, url, options, args.retries, args.delay)
            append_manifest(manifest_path, result)
            status = result.get("status", "error")
            counters[status if status in counters else "error"] += 1
            label = status.upper()
            target = result.get("path") or result.get("reason") or result.get("error") or ""
            print(f"{label}: {url} {target}")

    await asyncio.gather(*(guarded(url) for url in urls))

    print(
        "DONE "
        f"ok={counters['ok']} "
        f"skipped={counters['skipped']} "
        f"error={counters['error']} "
        f"manifest={manifest_path}"
    )
    return 1 if counters["error"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch download Douyin/TikTok/Bilibili URLs locally. "
            "Use only for content you own or have permission to download."
        )
    )
    parser.add_argument("--input", type=Path, default=Path("local_tools/urls.txt"), help="URL list file.")
    parser.add_argument("--output", type=Path, default=Path("download/private_batch"), help="Download output directory.")
    parser.add_argument("--manifest", type=Path, default=Path("manifest.jsonl"), help="JSONL result file, relative to output unless absolute.")
    parser.add_argument("--douyin-cookie-file", type=Path, default=None, help="Optional file containing your Douyin web cookie.")
    parser.add_argument("--tiktok-cookie-file", type=Path, default=None, help="Optional file containing your TikTok web cookie.")
    parser.add_argument("--watermark", choices=["keep", "remove"], default="keep", help="Keep watermark by default; remove only for authorized content.")
    parser.add_argument("--include-images", action="store_true", help="Download image posts too. By default image posts are skipped.")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel downloads. Keep this low for personal use.")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay after each URL, in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per URL after the first attempt.")
    parser.add_argument("--timeout", type=float, default=90.0, help="HTTP timeout in seconds.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N URLs.")
    parser.add_argument("--dry-run", action="store_true", help="Parse URLs but do not download files.")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip URLs already marked ok in the manifest.")
    parser.add_argument("--no-skip-existing", action="store_true", help="Download even if the destination file already exists.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")
    if args.concurrency > 5:
        print("Warning: high concurrency can trigger rate limits. Consider 1-2 for personal use.", file=sys.stderr)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
