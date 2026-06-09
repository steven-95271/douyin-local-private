#!/usr/bin/env python3
"""
Local receiver for the Chrome extension.

The server only binds to 127.0.0.1 by default. It receives URLs collected by the
extension, appends them to a local queue, and can start batch_download.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = PROJECT_ROOT / "local_tools" / "extension_queue.txt"
DEFAULT_OUTPUT = PROJECT_ROOT / "download" / "private_batch"
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
ALLOWED_HOST_RE = re.compile(r"(douyin\.com|iesdouyin\.com|tiktok\.com|bilibili\.com|b23\.tv)", re.IGNORECASE)

settings: Optional[argparse.Namespace] = None
worker_process: Optional[subprocess.Popen] = None


@dataclass
class EnqueueRequest:
    urls: list[str]
    source: Optional[str] = None
    start: bool = False
    watermark: str = "keep"
    include_images: bool = False
    concurrency: int = 1
    delay: float = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(raw: str) -> Optional[str]:
    match = URL_PATTERN.search(str(raw).strip())
    if not match:
        return None
    url = match.group(0).rstrip(".,);]")
    if not ALLOWED_HOST_RE.search(url):
        return None
    return url


def read_queue(queue_path: Path) -> list[str]:
    if not queue_path.exists():
        return []
    return [
        line.strip()
        for line in queue_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def append_unique(queue_path: Path, urls: list[str]) -> list[str]:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set(read_queue(queue_path))
    added: list[str] = []
    with queue_path.open("a", encoding="utf-8") as handle:
        for url in urls:
            if url in existing:
                continue
            handle.write(url + "\n")
            existing.add(url)
            added.append(url)
    return added


def cookie_file_status(path: Optional[Path]) -> dict[str, Union[str, bool, None]]:
    if path is None:
        return {"configured": False, "path": None, "looks_ready": False}
    if not path.exists():
        return {"configured": True, "path": str(path), "looks_ready": False}
    text = path.read_text(encoding="utf-8").strip()
    looks_ready = bool(text and "paste_your" not in text)
    return {"configured": True, "path": str(path), "looks_ready": looks_ready}


def process_running() -> bool:
    global worker_process
    if worker_process is None:
        return False
    return worker_process.poll() is None


def clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def clamp_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def parse_enqueue_payload(payload: dict[str, object]) -> EnqueueRequest:
    raw_urls = payload.get("urls", [])
    urls = raw_urls if isinstance(raw_urls, list) else []
    watermark = payload.get("watermark", "keep")
    if watermark not in {"keep", "remove"}:
        watermark = "keep"

    return EnqueueRequest(
        urls=[str(url) for url in urls],
        source=str(payload.get("source")) if payload.get("source") else None,
        start=bool(payload.get("start", False)),
        watermark=str(watermark),
        include_images=bool(payload.get("include_images", False)),
        concurrency=clamp_int(payload.get("concurrency"), default=1, minimum=1, maximum=5),
        delay=clamp_float(payload.get("delay"), default=2.0, minimum=0.0, maximum=60.0),
    )


def start_worker(request: EnqueueRequest) -> dict[str, Union[str, int, bool, None]]:
    global worker_process
    if process_running():
        return {"started": False, "running": True, "pid": worker_process.pid if worker_process else None}

    assert settings is not None
    output_dir = Path(settings.output)
    queue_path = Path(settings.queue)
    log_dir = output_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "extension_worker.log"
    stderr_path = log_dir / "extension_worker.err.log"

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "local_tools" / "batch_download.py"),
        "--input",
        str(queue_path),
        "--output",
        str(output_dir),
        "--watermark",
        request.watermark,
        "--concurrency",
        str(request.concurrency),
        "--delay",
        str(request.delay),
    ]

    if settings.douyin_cookie_file:
        cmd.extend(["--douyin-cookie-file", str(Path(settings.douyin_cookie_file))])
    if settings.tiktok_cookie_file:
        cmd.extend(["--tiktok-cookie-file", str(Path(settings.tiktok_cookie_file))])
    if request.include_images:
        cmd.append("--include-images")

    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        stdout.write(f"\n[{utc_now()}] START {' '.join(cmd)}\n")
        stdout.flush()
        worker_process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=stdout, stderr=stderr)

    return {
        "started": True,
        "running": True,
        "pid": worker_process.pid,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def status_payload() -> dict[str, object]:
    assert settings is not None
    queue_path = Path(settings.queue)
    return {
        "ok": True,
        "time": utc_now(),
        "queue": str(queue_path),
        "queued": len(read_queue(queue_path)),
        "running": process_running(),
        "pid": worker_process.pid if process_running() and worker_process else None,
        "douyin_cookie": cookie_file_status(Path(settings.douyin_cookie_file) if settings.douyin_cookie_file else None),
        "tiktok_cookie": cookie_file_status(Path(settings.tiktok_cookie_file) if settings.tiktok_cookie_file else None),
    }


def enqueue_payload(request: EnqueueRequest) -> dict[str, object]:
    assert settings is not None
    normalized = []
    seen = set()
    for raw in request.urls:
        url = normalize_url(raw)
        if url and url not in seen:
            normalized.append(url)
            seen.add(url)

    queue_path = Path(settings.queue)
    added = append_unique(queue_path, normalized)
    worker = start_worker(request) if request.start else {"started": False, "running": process_running()}

    return {
        "ok": True,
        "received": len(request.urls),
        "accepted": len(normalized),
        "added": len(added),
        "queue": str(queue_path),
        "running": process_running(),
        "worker": worker,
        "cookie": cookie_file_status(Path(settings.douyin_cookie_file) if settings.douyin_cookie_file else None),
    }


class ExtensionHandler(BaseHTTPRequestHandler):
    server_version = "DouyinLocalExtensionServer/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (utc_now(), fmt % args))

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json(HTTPStatus.OK, status_payload())
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/enqueue":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            request = parse_enqueue_payload(payload)
            self.send_json(HTTPStatus.OK, enqueue_payload(request))
        except Exception as exc:  # noqa: BLE001 - local API should return JSON errors.
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local localhost receiver for the Chrome extension.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Keep 127.0.0.1 for local-only use.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="Queue text file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Download output directory.")
    parser.add_argument("--douyin-cookie-file", type=Path, default=PROJECT_ROOT / "local_tools" / "douyin_cookie.txt")
    parser.add_argument("--tiktok-cookie-file", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    global settings
    settings = parse_args()
    server = ThreadingHTTPServer((settings.host, settings.port), ExtensionHandler)
    print(f"Listening on http://{settings.host}:{settings.port}")
    print(f"Queue: {settings.queue}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
