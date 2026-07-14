from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import RawItem, ScoredIdea


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_items (
              id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              platform TEXT NOT NULL,
              url TEXT NOT NULL,
              title TEXT NOT NULL,
              text TEXT NOT NULL,
              author TEXT,
              published_at TEXT,
              media_json TEXT NOT NULL,
              metrics_json TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              fetched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_platform ON raw_items(platform);
            CREATE INDEX IF NOT EXISTS idx_raw_url ON raw_items(url);

            CREATE TABLE IF NOT EXISTS scored_ideas (
              item_id TEXT PRIMARY KEY REFERENCES raw_items(id),
              is_product_idea INTEGER NOT NULL,
              category TEXT NOT NULL,
              summary_ko TEXT NOT NULL,
              novelty_score REAL NOT NULL,
              virality_score REAL NOT NULL,
              market_score REAL NOT NULL,
              total_score REAL NOT NULL,
              duplicate_key TEXT NOT NULL,
              reason TEXT NOT NULL,
              matched_terms_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ideas_total ON scored_ideas(total_score DESC);
            """
        )
        self.conn.commit()


    def recent_social_items(
        self,
        *,
        source: str,
        platform: str,
        hours: int = 24,
        limit: int = 20,
    ) -> list[RawItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = self.conn.execute(
            """
            SELECT id, source, platform, url, title, text, author, published_at,
                   media_json, metrics_json, tags_json, raw_json, fetched_at
            FROM raw_items
            WHERE source = ? AND platform = ?
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            (source, platform, max(limit * 4, limit)),
        ).fetchall()
        items: list[RawItem] = []
        for row in rows:
            fetched_at = _parse_utc(row[12])
            if fetched_at is None or fetched_at < cutoff:
                continue
            items.append(
                RawItem(
                    id=row[0],
                    source=row[1],
                    platform=row[2],
                    url=row[3],
                    title=row[4],
                    text=row[5],
                    author=row[6],
                    published_at=row[7],
                    media_urls=_json_list(row[8]),
                    metrics=_json_dict(row[9]),
                    tags=_json_list(row[10]),
                    raw=_json_dict(row[11]),
                    fetched_at=row[12],
                )
            )
            if len(items) >= limit:
                break
        return items

    def upsert_items(self, items: list[RawItem]) -> None:
        rows = [
            (
                item.id,
                item.source,
                item.platform,
                item.url,
                item.title,
                item.text,
                item.author,
                item.published_at,
                json.dumps(item.media_urls, ensure_ascii=False),
                json.dumps(item.metrics, ensure_ascii=False),
                json.dumps(item.tags, ensure_ascii=False),
                json.dumps(item.raw, ensure_ascii=False),
                item.fetched_at,
            )
            for item in items
        ]
        self.conn.executemany(
            """
            INSERT INTO raw_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              source=excluded.source,
              platform=excluded.platform,
              url=excluded.url,
              title=excluded.title,
              text=excluded.text,
              author=excluded.author,
              published_at=excluded.published_at,
              media_json=excluded.media_json,
              metrics_json=excluded.metrics_json,
              tags_json=excluded.tags_json,
              raw_json=excluded.raw_json,
              fetched_at=excluded.fetched_at
            """,
            rows,
        )
        self.conn.commit()

    def upsert_ideas(self, ideas: list[ScoredIdea]) -> None:
        self.upsert_items([idea.item for idea in ideas])
        rows = [
            (
                idea.item.id,
                int(idea.is_product_idea),
                idea.category,
                idea.summary_ko,
                idea.novelty_score,
                idea.virality_score,
                idea.market_score,
                idea.total_score,
                idea.duplicate_key,
                idea.reason,
                json.dumps(idea.matched_terms, ensure_ascii=False),
            )
            for idea in ideas
        ]
        self.conn.executemany(
            """
            INSERT INTO scored_ideas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
              is_product_idea=excluded.is_product_idea,
              category=excluded.category,
              summary_ko=excluded.summary_ko,
              novelty_score=excluded.novelty_score,
              virality_score=excluded.virality_score,
              market_score=excluded.market_score,
              total_score=excluded.total_score,
              duplicate_key=excluded.duplicate_key,
              reason=excluded.reason,
              matched_terms_json=excluded.matched_terms_json
            """,
            rows,
        )
        self.conn.commit()


def _json_list(value: str) -> list:
    data = json.loads(value or "[]")
    return data if isinstance(data, list) else []


def _json_dict(value: str) -> dict:
    data = json.loads(value or "{}")
    return data if isinstance(data, dict) else {}


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
