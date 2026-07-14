from __future__ import annotations
from urllib.parse import urlsplit


from ..feeds import parse_feed_items
from ..models import RawItem
from .base import Collector


class ProductHuntCollector(Collector):
    name = "producthunt"

    def collect(self) -> list[RawItem]:
        feed_url = self.config.get("feed_url", "https://www.producthunt.com/feed")
        if not is_producthunt_public_feed_url(str(feed_url)):
            raise RuntimeError(f"Blocked unsafe Product Hunt feed URL: {feed_url}")
        result = self.fetcher.fetch(feed_url)
        if not is_producthunt_public_feed_url(str(result.url)):
            raise RuntimeError(f"Blocked unsafe Product Hunt fetched URL: {result.url}")
        if not result.ok:
            return []
        return parse_feed_items(
            result.content,
            source=self.name,
            platform="producthunt",
            base_url="https://www.producthunt.com",
            limit=self.fetch_limit(multiplier=2),
            raw_extra={
                "discovery_method": "public_feed",
                "collector_public_read_proof": {
                    "platform": "producthunt",
                    "public_only": True,
                    "method": "GET",
                    "url": result.url,
                    "fetch_ok": result.ok,
                },
                "safety_conformant": True,
                "public_only": True,
                "is_live_real_time": True,
                "live_acquisition": True,
                "fixture": False,
                "cache": False,
            },
        )


def is_producthunt_public_feed_url(url: str) -> bool:
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return parts.scheme == "https" and host == "www.producthunt.com" and path == "/feed"
