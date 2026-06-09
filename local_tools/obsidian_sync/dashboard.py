#!/usr/bin/env python3
"""
Local dashboard for managing Douyin -> Obsidian sync.

The server binds to 127.0.0.1 by default and never returns secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local_tools" / "obsidian_sync" / "creators.yaml"

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
        "cookie": file_ready(cookie_file, ["paste_your", "PASTE_YOUR"]),
        "deepseek": env_status(env_file, api_key_env),
        "vault": {"path": str(vault_path), "exists": vault_path.exists()},
        "output": {"path": str(output_dir), "exists": output_dir.exists()},
        "worker": worker_status(),
    }


def worker_status() -> Dict[str, Any]:
    global worker_process
    running = bool(worker_process and worker_process.poll() is None)
    code = None if running or worker_process is None else worker_process.returncode
    log_text = ""
    if worker_log and worker_log.exists():
        text = worker_log.read_text(encoding="utf-8", errors="replace")
        log_text = text[-12000:]
    return {
        "running": running,
        "returncode": code,
        "pid": worker_process.pid if running and worker_process else None,
        "log_path": str(worker_log) if worker_log else None,
        "log_tail": log_text,
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
    return {
        "key": key,
        "name": name,
        "url": url,
        "enabled": bool(raw.get("enabled", True)),
        "tags": [str(tag) for tag in tags],
    }


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
            elif path == "/api/run":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": start_sync(payload)})
            elif path == "/api/run/stop":
                self.send_json(HTTPStatus.OK, {"ok": True, "worker": stop_sync()})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


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
