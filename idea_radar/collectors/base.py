from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..fetcher import InsaneSearchFetcher
from ..models import RawItem


class Collector(ABC):
    name: str

    def __init__(self, fetcher: InsaneSearchFetcher, config: dict[str, Any], *, limit: int) -> None:
        self.fetcher = fetcher
        self.config = config
        self.limit = limit
    def fetch_limit(self, *, multiplier: int | None = None, cap: int = 200) -> int:
        configured = self.config.get("fetch_limit")
        try:
            factor = int(multiplier if multiplier is not None else self.config.get("fetch_multiplier", 3))
            value = int(configured) if configured is not None else self.limit * max(1, factor)
            return max(self.limit, min(value, cap))
        except (TypeError, ValueError):
            return self.limit

    @abstractmethod
    def collect(self) -> list[RawItem]:
        raise NotImplementedError
