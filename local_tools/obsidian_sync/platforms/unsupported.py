from __future__ import annotations

from typing import Any, Dict, List

from .base import PlatformRunContext


class UnsupportedPlatformCrawler:
    def __init__(self, platform: str, label: str) -> None:
        self.platform = platform
        self.item_label = "items"
        self.label = label

    async def sniff(self, creator: Dict[str, Any], context: PlatformRunContext) -> List[Any]:
        raise RuntimeError(
            f"{self.label}抓取适配器还未接入。当前版本只先注册平台入口，"
            "等小红书新链路跑通后再逐个平台实装。"
        )

    async def hydrate(self, item: Any, context: PlatformRunContext) -> Any:
        raise RuntimeError(f"{self.label}详情适配器还未接入")
