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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

settings: Optional[argparse.Namespace] = None
worker_process: Optional[subprocess.Popen] = None
worker_log: Optional[Path] = None


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


async def fetch_douyin_creator_profile(url: str, cookie: str) -> Dict[str, Any]:
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler
    from local_tools.batch_download import apply_runtime_cookies

    apply_runtime_cookies(cookie or None, None)
    crawler = DouyinWebCrawler()
    sec_user_id = extract_sec_user_id_from_url(url) or await crawler.get_sec_user_id(url)
    response = await crawler.fetch_user_post_videos(str(sec_user_id), 0, 1)
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    items = data.get("aweme_list") or data.get("aweme_list_v2") or data.get("items") or []
    name = ""
    if items and isinstance(items[0], dict):
        name = extract_author_name_from_item(items[0])
    return {"sec_user_id": sec_user_id, "name": name}


def resolve_creator_from_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_url = str(payload.get("url", "")).strip()
    if not raw_url:
        raise ValueError("url is required")
    url = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
    parsed = urlparse(url)
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
    if not name:
        detail = "；".join(errors[-2:])
        if detail:
            raise ValueError(f"没有获取到博主昵称。内部原因：{detail}")
        raise ValueError("没有获取到博主昵称。请确认主页 URL 正确，并先用 Chrome 插件导入抖音 Cookie。")

    key = pinyin_initials(name)
    if not key:
        raise ValueError(f"无法根据博主名称生成 Key: {name}")
    key = unique_creator_key(key, final_url, data)

    return {
        "key": key,
        "name": name,
        "url": final_url,
        "sec_user_id": profile.get("sec_user_id", ""),
        "enabled": True,
        "tags": ["douyin", "口播"],
    }


def public_config(data: Dict[str, Any]) -> Dict[str, Any]:
    creators = data.get("creators") or []
    if not isinstance(creators, list):
        creators = []
    return {
        "vault_path": data.get("vault_path", ""),
        "output_subdir": data.get("output_subdir", ""),
        "fetch": data.get("fetch", {}),
        "summary": {
            "model": (data.get("summary") or {}).get("model", ""),
        },
        "creators": creators,
    }


def status_payload() -> Dict[str, Any]:
    data = load_config()
    cookie_file = project_path(str(data.get("douyin_cookie_file", "local_tools/douyin_cookie.txt")))
    env_file = project_path(str(data.get("env_file", "local_tools/obsidian_sync/.env")))
    api_key_env = str((data.get("summary") or {}).get("api_key_env", "DEEPSEEK_API_KEY"))
    vault_path = Path(str(data.get("vault_path", ""))).expanduser()
    output_dir = vault_path / str(data.get("output_subdir", ""))
    return {
        "time": utc_now(),
        "config_path": str(get_config_path()),
        "cookie": cookie_status(cookie_file),
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
            errors.append(line)
    return {
        "warning_count": len(warnings),
        "error_count": len(errors),
        "warnings": warnings[-8:],
        "errors": errors[-8:],
    }


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
            }
            continue
        if not current:
            continue
        line = raw.strip()
        lower = line.lower()
        if line.startswith("DONE "):
            current["status"] = "done"
            seen = re.search(r"seen=(\d+)", line)
            processed = re.search(r"processed=(\d+)", line)
            output = re.search(r"output=(.*)$", line)
            current["seen"] = int(seen.group(1)) if seen else None
            current["processed"] = int(processed.group(1)) if processed else None
            current["output"] = output.group(1) if output else ""
        elif line.startswith("ERROR ") or "traceback" in lower or "exception" in lower or "failed" in lower or "失败" in line:
            current["status"] = "error"
            current["errors"].append(line)
        elif "warning" in lower or "warn " in lower:
            current["warnings"].append(line)
        elif line.startswith("WROTE "):
            current["wrote"].append(line.replace("WROTE ", "", 1))
    if current:
        runs.append(current)
    return runs[-12:]


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
    history = parse_run_history(text)
    if history and not running and history[-1]["status"] == "running":
        history[-1]["status"] = "unknown"
    output_base = output_base_path(data)
    return {
        "running": running,
        "returncode": code,
        "pid": worker_process.pid if running and worker_process else None,
        "log_path": str(worker_log) if worker_log else None,
        "log_tail": log_text,
        "analysis": analysis,
        "history": history,
        "output_path": str(output_base),
        "recent_files": recent_markdown_files(output_base),
    }


def normalize_creator(raw: Dict[str, Any]) -> Dict[str, Any]:
    key = str(raw.get("key", "")).strip()
    name = str(raw.get("name", "")).strip()
    url = str(raw.get("url", "")).strip()
    if not key:
        raise ValueError("Creator key is required")
    if not name:
        raise ValueError(f"Creator name is required for {key}")
    if not url:
        raise ValueError(f"Creator URL is required for {key}")
    tags = raw.get("tags") or ["douyin", "口播"]
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not isinstance(tags, list):
        tags = ["douyin", "口播"]
    creator = {
        "key": key,
        "name": name,
        "url": url,
        "enabled": bool(raw.get("enabled", True)),
        "tags": [str(tag) for tag in tags],
    }
    sec_user_id = str(raw.get("sec_user_id", "")).strip()
    if sec_user_id:
        creator["sec_user_id"] = sec_user_id
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
        data["creators"] = [normalize_creator(item) for item in creators if isinstance(item, dict)]
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


def start_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    global worker_process, worker_log
    if worker_process and worker_process.poll() is None:
        raise RuntimeError("A sync process is already running")

    data = load_config()
    work_dir = project_path(str(data.get("work_dir", "local_tools/obsidian_sync/work")))
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_log = log_dir / "dashboard_sync.log"

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "local_tools" / "obsidian_sync" / "sync.py"),
        "--config",
        str(get_config_path()),
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
        log.write(f"\n[{utc_now()}] START {' '.join(cmd)}\n")
        log.flush()
        worker_process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log)
    return worker_status()


def stop_sync() -> Dict[str, Any]:
    if worker_process and worker_process.poll() is None:
        worker_process.terminate()
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
