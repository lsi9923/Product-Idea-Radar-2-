from __future__ import annotations

from ..feeds import parse_feed_items
from ..models import RawItem
from .base import Collector


class RedditCollector(Collector):
    name = "reddit"

    def collect(self) -> list[RawItem]:
        subreddits = self.config.get(
            "subreddits",
            ["INEEEEDIT", "DidntKnowIWantedThat", "gadgets", "ShutUpAndTakeMyMoney"],
        )
        target = self.fetch_limit(multiplier=3)
        per_subreddit = max(1, min(target, int(self.config.get("limit_per_subreddit", self.limit))))
        sort = str(self.config.get("sort", "new")).strip("/").lower() or "new"
        if sort not in {"hot", "new", "rising", "top"}:
            sort = "new"
        out: list[RawItem] = []
        for subreddit in subreddits:
            if len(out) >= target:
                break
            remaining = target - len(out)
            url = self._feed_url(str(subreddit), sort=sort, limit=min(per_subreddit, remaining))
            result = self.fetcher.fetch(url)
            if not result.ok:
                continue
            try:
                items = parse_feed_items(
                    result.content,
                    source=f"reddit:{subreddit}",
                    platform="reddit",
                    base_url="https://www.reddit.com",
                    limit=min(per_subreddit, remaining),
                    raw_extra={"subreddit": subreddit, "sort": sort},
                )
            except Exception:
                continue
            for item in items:
                item.source = "reddit"
                item.tags.append(f"r/{subreddit}")
            out.extend(items)
        return out[:target]

    def _feed_url(self, subreddit: str, *, sort: str, limit: int) -> str:
        if sort == "top":
            period = str(self.config.get("top_period", "month"))
            return f"https://www.reddit.com/r/{subreddit}/top/.rss?t={period}&limit={limit}"
        return f"https://www.reddit.com/r/{subreddit}/{sort}/.rss?limit={limit}"
