from __future__ import annotations

from typing import Dict, Iterable

from .base import PlatformCrawler


class PlatformRegistry:
    def __init__(self) -> None:
        self._crawlers: Dict[str, PlatformCrawler] = {}

    def register(self, crawler: PlatformCrawler) -> None:
        self._crawlers[crawler.platform] = crawler

    def get(self, platform: str) -> PlatformCrawler:
        try:
            return self._crawlers[platform]
        except KeyError as exc:
            raise KeyError(f"Platform adapter is not registered: {platform}") from exc

    def has(self, platform: str) -> bool:
        return platform in self._crawlers

    def runnable_platforms(self) -> set[str]:
        return set(self._crawlers)

    def labels(self) -> Dict[str, str]:
        return {key: crawler.item_label for key, crawler in self._crawlers.items()}

    def extend(self, crawlers: Iterable[PlatformCrawler]) -> None:
        for crawler in crawlers:
            self.register(crawler)
