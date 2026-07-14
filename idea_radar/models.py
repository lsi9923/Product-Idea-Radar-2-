from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class RawItem:
    id: str
    source: str
    platform: str
    url: str
    title: str
    text: str = ""
    author: str | None = None
    published_at: str | None = None
    media_urls: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScoredIdea:
    item: RawItem
    is_product_idea: bool
    category: str
    summary_ko: str
    novelty_score: float
    virality_score: float
    market_score: float
    total_score: float
    duplicate_key: str
    reason: str
    matched_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["item"] = self.item.to_dict()
        return data
