from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .evidence import purchase_evidence_dedupe_keys
from .models import ScoredIdea
from .utils import normalize_key


def deterministic_idea_sort_key(idea: ScoredIdea) -> tuple[float, float, str, str, str]:
    """Newest-first ordering; public engagement only breaks equal timestamps."""
    return (
        _timestamp(idea.item.published_at or idea.item.fetched_at),
        _engagement(idea),
        normalize_key(idea.item.url, limit=16),
        normalize_key(idea.item.title, limit=8),
        idea.item.id,
    )


def dedupe_ideas(ideas: list[ScoredIdea], *, similarity: float = 0.82) -> list[ScoredIdea]:
    groups: dict[str, list[ScoredIdea]] = {}

    for idea in ideas:
        final_keys = purchase_evidence_dedupe_keys(idea.item, require_final_url=True)
        if not final_keys:
            continue
        primary_key = final_keys[0]
        group = groups.setdefault(primary_key, [])
        group.append(idea)
        for key in final_keys[1:]:
            if key in groups and groups[key] is not group:
                group.extend(groups[key])
                groups[key] = group
            else:
                groups[key] = group

    representatives: list[ScoredIdea] = []
    seen_groups: set[int] = set()
    for group_ideas in groups.values():
        group_id = id(group_ideas)
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        unique: dict[str, ScoredIdea] = {idea.item.id: idea for idea in group_ideas}
        representative = max(unique.values(), key=deterministic_idea_sort_key)
        if isinstance(representative.item.raw, dict):
            representative.item.raw["dedupe_group_size"] = len(unique)
        representatives.append(representative)

    return sorted(representatives, key=deterministic_idea_sort_key, reverse=True)




def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _engagement(idea: ScoredIdea) -> float:
    total = 0.0
    for value in idea.item.metrics.values():
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return total
