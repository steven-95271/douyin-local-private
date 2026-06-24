from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class PlatformRunContext:
    config: Dict[str, Any]
    scan_limit: int = 0
    full_history: bool = False
    conn: Optional[sqlite3.Connection] = None
    run_id: Optional[str] = None


class PlatformCrawler(Protocol):
    platform: str
    item_label: str

    async def sniff(self, creator: Dict[str, Any], context: PlatformRunContext) -> List[Any]:
        """Return candidate content items for this creator."""

    async def hydrate(self, item: Any, context: PlatformRunContext) -> Any:
        """Fetch detail/media metadata for one item before processing."""
