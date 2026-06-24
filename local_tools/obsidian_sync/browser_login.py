#!/usr/bin/env python3
"""Open a real browser window for QR/login-state based platform login."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


PLATFORMS = {
    "douyin": {
        "label": "抖音",
        "entry_url": "https://www.douyin.com/",
        "domains": ("douyin.com",),
        "login_cookie_names": {"sessionid", "sid_guard", "uid_tt", "uid_tt_ss"},
    },
    "xiaohongshu": {
        "label": "小红书",
        "entry_url": "https://www.xiaohongshu.com/",
        "domains": ("xiaohongshu.com",),
        "login_cookie_names": {"web_session"},
    },
}


def cookie_string(cookies: list[dict[str, Any]], domains: tuple[str, ...]) -> str:
    pairs: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").lstrip(".")
        if not name or not any(domain.endswith(item) for item in domains):
            continue
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def has_login_cookie(cookies: list[dict[str, Any]], domains: tuple[str, ...], names: set[str]) -> bool:
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".")
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if value and name in names and any(domain.endswith(item) for item in domains):
            return True
    return False


def xiaohongshu_page_confirms_login(page: Any) -> bool:
    try:
        state = page.evaluate(
            """() => {
                const bodyText = (document.body && document.body.innerText || '').trim();
                const login = document.querySelector('#login-btn');
                let loginVisible = false;
                if (login) {
                    const style = window.getComputedStyle(login);
                    const rect = login.getBoundingClientRect();
                    loginVisible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                }
                return {
                    rendered: bodyText.length > 20 || document.title.includes('小红书'),
                    loginVisible,
                };
            }"""
        )
        if not isinstance(state, dict) or not state.get("rendered"):
            return False
        return not bool(state.get("loginVisible"))
    except Exception:
        return False


def page_confirms_login(platform: str, page: Any) -> bool:
    if platform == "xiaohongshu":
        return xiaohongshu_page_confirms_login(page)
    return True


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="QR/browser profile login helper.")
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--cookie-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    info = PLATFORMS[args.platform]
    profile_dir = Path(args.profile_dir).expanduser()
    cookie_file = Path(args.cookie_file).expanduser()
    status_file = Path(args.status_file).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)

    write_status(
        status_file,
        {
            "platform": args.platform,
            "label": info["label"],
            "status": "starting",
            "message": f"正在打开{info['label']}登录窗口",
            "updated_at": time.time(),
        },
    )

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        write_status(
            status_file,
            {
                "platform": args.platform,
                "label": info["label"],
                "status": "missing_dependency",
                "message": "Playwright 未安装，请先运行：.venv/bin/python -m pip install -r requirements-obsidian.txt",
                "updated_at": time.time(),
            },
        )
        raise RuntimeError("Playwright is not installed") from exc

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chrome",
                **launch_kwargs,
            )
        except Exception:
            context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(3000)
        page.goto(str(info["entry_url"]), wait_until="domcontentloaded", timeout=60_000)
        write_status(
            status_file,
            {
                "platform": args.platform,
                "label": info["label"],
                "status": "waiting_login",
                "message": f"请在打开的 Chrome 窗口里完成{info['label']}扫码登录或滑块验证",
                "profile_dir": str(profile_dir),
                "updated_at": time.time(),
            },
        )

        deadline = time.time() + max(30, args.timeout)
        logged_in = False
        cookies: list[dict[str, Any]] = []
        reloaded_after_cookie = False
        while time.time() < deadline:
            cookies = context.cookies()
            cookie_present = has_login_cookie(cookies, tuple(info["domains"]), set(info["login_cookie_names"]))
            if cookie_present and args.platform == "xiaohongshu" and not reloaded_after_cookie:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                reloaded_after_cookie = True
            if cookie_present and page_confirms_login(args.platform, page):
                logged_in = True
                break
            if cookie_present:
                write_status(
                    status_file,
                    {
                        "platform": args.platform,
                        "label": info["label"],
                        "status": "waiting_page_login",
                        "message": f"检测到{info['label']} Cookie，但网页仍未确认登录；请继续完成扫码或滑块验证",
                        "profile_dir": str(profile_dir),
                        "updated_at": time.time(),
                    },
                )
            page.wait_for_timeout(2000)

        if logged_in:
            value = cookie_string(cookies, tuple(info["domains"]))
            if not value:
                raise RuntimeError("检测到登录态，但没有导出到可用 Cookie")
            cookie_file.write_text(value + "\n", encoding="utf-8")
            write_status(
                status_file,
                {
                    "platform": args.platform,
                    "label": info["label"],
                    "status": "ready",
                    "message": f"{info['label']}扫码登录完成，登录态已保存，窗口将自动关闭",
                    "profile_dir": str(profile_dir),
                    "cookie_file": str(cookie_file),
                    "cookie_count": len([item for item in value.split(";") if item.strip()]),
                    "updated_at": time.time(),
                },
            )
            page.wait_for_timeout(2500)
        else:
            write_status(
                status_file,
                {
                    "platform": args.platform,
                    "label": info["label"],
                    "status": "timeout",
                    "message": f"没有检测到{info['label']}登录态。可以重新点击扫码登录再试。",
                    "profile_dir": str(profile_dir),
                    "updated_at": time.time(),
                },
            )
            context.close()
            return 2

        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
