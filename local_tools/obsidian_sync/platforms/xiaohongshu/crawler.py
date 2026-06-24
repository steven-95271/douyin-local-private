from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from ..base import PlatformRunContext


class XiaohongshuCrawler:
    platform = "xiaohongshu"
    item_label = "notes"

    def __init__(
        self,
        *,
        sniff_notes: Callable[[Dict[str, Any], Dict[str, Any], int, bool, Any, Any], Awaitable[List[Any]]],
        fetch_note: Callable[[Dict[str, Any], str, Dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self._sniff_notes = sniff_notes
        self._fetch_note = fetch_note

    async def sniff(self, creator: Dict[str, Any], context: PlatformRunContext) -> List[Any]:
        return await self._sniff_notes(
            creator,
            context.config,
            context.scan_limit,
            context.full_history,
            context.conn,
            context.run_id,
        )

    async def hydrate(self, item: Any, context: PlatformRunContext) -> Any:
        if str(item.raw.get("original_text") or "").strip() or not item.source_url:
            return item
        fetched = await self._fetch_note(
            {
                "key": item.creator_key,
                "name": item.creator_name,
                "tags": item.tags,
            },
            item.source_url,
            context.config,
        )
        item.raw.update(fetched.raw)
        item.title = fetched.title or item.title
        item.create_time = fetched.create_time or item.create_time
        return item
