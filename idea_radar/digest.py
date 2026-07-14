from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import RawItem, ScoredIdea
from .utils import clean_text


EVIDENCE_RAW_FIELDS = [
    "provisional",
    "freshness_unknown",
    "evidence_type",
    "discovery_method",
    "discovery_lane",
    "category_seed",
    "cache_source",
    "cache_item_id",
    "cache_reused_at",
    "fallback_reason",
    "acquisition_mode",
    "is_live_real_time",
]


def evidence_fields(item: RawItem) -> dict[str, object]:
    raw = item.raw or {}
    return {
        field: raw.get(field)
        for field in EVIDENCE_RAW_FIELDS
        if raw.get(field) is not None and raw.get(field) != ""
    }


def evidence_summary(item: RawItem) -> str:
    fields = evidence_fields(item)
    return "; ".join(f"{field}={value}" for field, value in fields.items())


def primary_purchase_evidence(item: RawItem) -> dict[str, object]:
    evidence = (item.raw or {}).get("purchase_evidence")
    if not isinstance(evidence, list):
        return {}
    for row in evidence:
        if isinstance(row, dict):
            return row
    return {}


def _json_cell(value: object) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def purchase_url(item: RawItem) -> str:
    evidence = primary_purchase_evidence(item)
    resolver = evidence.get("resolver") if isinstance(evidence.get("resolver"), dict) else {}
    for value in [
        evidence.get("final_url"),
        resolver.get("final_url") if isinstance(resolver, dict) else None,
        evidence.get("purchase_url"),
        evidence.get("raw_url"),
        evidence.get("url"),
        evidence.get("value"),
    ]:
        if isinstance(value, str) and value:
            return value
    return ""

IDEA_CSV_FIELDNAMES = [
    "rank",
    "total_score",
    "novelty_score",
    "virality_score",
    "market_score",
    "category",
    "platform",
    "source",
    "provisional",
    "freshness_unknown",
    "evidence_type",
    "discovery_method",
    "discovery_lane",
    "category_seed",
    "cache_source",
    "cache_item_id",
    "cache_reused_at",
    "fallback_reason",
    "acquisition_mode",
    "is_live_real_time",
    "original_url",
    "purchase_url",
    "metrics",
    "affiliate",
    "conformance",
    "title",
    "summary_ko",
    "url",
    "reason",
]


def write_ideas_json(ideas: list[ScoredIdea], path: str | Path) -> None:
    Path(path).write_text(json.dumps([idea.to_dict() for idea in ideas], ensure_ascii=False, indent=2), encoding="utf-8")


def write_ideas_csv(ideas: list[ScoredIdea], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=IDEA_CSV_FIELDNAMES)
        writer.writeheader()
        for rank, idea in enumerate(ideas, start=1):
            evidence = evidence_fields(idea.item)
            purchase = primary_purchase_evidence(idea.item)
            resolver = purchase.get("resolver") if isinstance(purchase.get("resolver"), dict) else {}
            writer.writerow(
                {
                    "rank": rank,
                    "total_score": idea.total_score,
                    "novelty_score": idea.novelty_score,
                    "virality_score": idea.virality_score,
                    "market_score": idea.market_score,
                    "category": idea.category,
                    "platform": idea.item.platform,
                    "source": idea.item.source,
                    "provisional": evidence.get("provisional", ""),
                    "freshness_unknown": evidence.get("freshness_unknown", ""),
                    "evidence_type": evidence.get("evidence_type", ""),
                    "discovery_method": evidence.get("discovery_method", ""),
                    "category_seed": evidence.get("category_seed", ""),
                    "cache_source": evidence.get("cache_source", ""),
                    "cache_item_id": evidence.get("cache_item_id", ""),
                    "cache_reused_at": evidence.get("cache_reused_at", ""),
                    "fallback_reason": evidence.get("fallback_reason", ""),
                    "discovery_lane": evidence.get("discovery_lane", ""),
                    "acquisition_mode": evidence.get("acquisition_mode", ""),
                    "is_live_real_time": evidence.get("is_live_real_time", ""),
                    "original_url": purchase.get("raw_url") or purchase.get("original_url") or idea.item.url,
                    "purchase_url": purchase_url(idea.item),
                    "metrics": _json_cell(idea.item.metrics),
                    "affiliate": _json_cell(purchase.get("affiliate")),
                    "conformance": _json_cell(purchase.get("conformance") or (resolver.get("conformance") if isinstance(resolver, dict) else "")),
                    "title": idea.item.title,
                    "summary_ko": idea.summary_ko,
                    "url": idea.item.url,
                    "reason": idea.reason,
                }
            )



def write_outputs(
    items: list[RawItem],
    ideas: list[ScoredIdea],
    out_dir: str | Path,
    *,
    top: int = 30,
    live_acceptance: dict[str, object] | None = None,
) -> dict[str, str]:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    raw_path = path / "raw_items.jsonl"
    ideas_path = path / "ideas.json"
    csv_path = path / "ideas.csv"
    digest_path = path / "digest.md"
    live_acceptance_path = path / "live_acceptance_diagnostics.json"

    with raw_path.open("w", encoding="utf-8", newline="") as f:
        for item in items:
            f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    write_ideas_json(ideas, ideas_path)

    write_ideas_csv(ideas, csv_path)

    digest_path.write_text(render_digest(ideas, top=top), encoding="utf-8")
    live_acceptance_path.write_text(
        json.dumps(live_acceptance or {}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "raw": str(raw_path),
        "ideas_json": str(ideas_path),
        "ideas_csv": str(csv_path),
        "digest": str(digest_path),
        "live_acceptance_diagnostics": str(live_acceptance_path),
    }


def render_digest(ideas: list[ScoredIdea], *, top: int = 30) -> str:
    selected = ideas[:top]
    lines = ["# Product Idea Radar Digest", "", f"총 후보: {len(ideas)}개", ""]
    for rank, idea in enumerate(selected, start=1):
        item = idea.item
        lines.extend(
            [
                f"## {rank}. {clean_text(item.title, max_len=120)}",
                "",
                f"- 점수: {idea.total_score} / 10 (참신함 {idea.novelty_score}, 바이럴 {idea.virality_score}, 시장성 {idea.market_score})",
                f"- 분류: {idea.category} / 플랫폼: {item.platform} / 소스: {item.source}",
                f"- 요약: {idea.summary_ko}",
                f"- 이유: {idea.reason}",
                f"- 링크: {item.url}",
            ]
        )
        evidence = evidence_summary(item)
        if evidence:
            lines.append(f"- 증거 상태: {evidence}")
        url = purchase_url(item)
        if url:
            lines.append(f"- 구매 링크: {url}")
        purchase = primary_purchase_evidence(item)
        if purchase:
            affiliate = _json_cell(purchase.get("affiliate"))
            resolver = purchase.get("resolver") if isinstance(purchase.get("resolver"), dict) else {}
            conformance = _json_cell(purchase.get("conformance") or (resolver.get("conformance") if isinstance(resolver, dict) else ""))
            if affiliate:
                lines.append(f"- 제휴: {affiliate}")
            if conformance:
                lines.append(f"- 증거 적합성: {conformance}")
        if item.media_urls:
            lines.append(f"- 이미지/미디어: {item.media_urls[0]}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
