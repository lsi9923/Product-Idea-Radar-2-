from __future__ import annotations

import inspect
import copy

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .collectors import COLLECTORS
from .config import load_config
from .dedupe import dedupe_ideas
from .evidence import clean_final_url, resolve_item_purchase_evidence
from .digest import write_outputs
from .fetcher import InsaneSearchFetcher
from .models import RawItem, ScoredIdea
from .quality import is_social_commerce_source, partition_quality_items, rejection_summary
from .scoring import effective_score_threshold, score_item
from .storage import Store

ProgressCallback = Callable[[str], None]


@dataclass(slots=True)
class CrawlOptions:
    config: str = "config/sources.json"
    sources: str | list[str] | None = None
    limit_per_source: int = 20
    top: int = 30
    threshold: float | None = None
    out: str = "runs/latest"
    db: str = "data/ideas.sqlite"
    insane_search_dir: str | None = None
    timeout: int = 40
    trace_fetch: bool = False
    include_rejected: bool = False
    min_verified_real_time: int = 0
    require_threads_lanes: bool = False


@dataclass(slots=True)
class CrawlResult:
    sources: list[str]
    raw_items: list[RawItem]
    ideas: list[ScoredIdea]
    outputs: dict[str, str]
    errors: dict[str, str]
    insane_search_dir: str
    db: str = "data/ideas.sqlite"
    live_acceptance: dict[str, Any] | None = None
    rejection_summary: dict[str, Any] | None = None
    collector_diagnostics: dict[str, object] = field(default_factory=dict)


def source_names(value: str | list[str] | None, config: dict[str, Any]) -> list[str]:
    if isinstance(value, list):
        return [name.strip().lower() for name in value if name.strip()]
    if value is None:
        return ["producthunt", "kickstarter", "reddit", "x_social", "instagram_social", "threads_social"]
    if value.strip().lower() == "all":
        names = list(config.get("_source_names") or config.get("sources", {}).keys())
        if "threads_social" not in names:
            names.append("threads_social")
        return names
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _collector_name(source_name: str) -> str:
    if source_name == "x_social":
        return "x"
    if source_name == "instagram_social":
        return "instagram"
    if source_name == "threads_social":
        return "threads"
    return source_name


def _collector_type(source_name: str) -> type | None:
    return COLLECTORS.get(source_name) or COLLECTORS.get(_collector_name(source_name))


def _platform_for_source(source_name: str) -> str:
    if source_name == "x_social":
        return "x"
    if source_name == "instagram_social":
        return "instagram"
    if source_name == "threads_social":
        return "threads"
    return source_name


def _source_config(config: dict[str, Any], source_name: str) -> dict[str, Any]:
    sources = config.get("sources", {})
    base_name = _collector_name(source_name)
    source_config = dict(sources.get(base_name, {}))
    alias_config = sources.get(source_name, {})
    source_config.update(alias_config)
    if source_name in {"x_social", "instagram_social", "threads_social"} and "enabled" not in alias_config:
        source_config["enabled"] = True
    return source_config


def _normalize_source(items: list[RawItem], source_name: str) -> list[RawItem]:
    return [replace(item, source=source_name) for item in items]


def _reuse_cached_social_items(
    store: Store,
    *,
    source_name: str,
    source_config: dict[str, Any],
    reason: str,
    limit: int,
) -> list[RawItem]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    hours = int(source_config.get("cache_fallback_hours", 24) or 24)
    platform = _platform_for_source(source_name)
    cached = store.recent_social_items(source=source_name, platform=platform, hours=hours, limit=limit)
    original_rows = [
        item
        for item in cached
        if not _is_fixture_cache_or_degraded(item.raw if isinstance(item.raw, dict) else {})
    ]
    kept, _ = partition_quality_items(original_rows, source_config=source_config)
    reused: list[RawItem] = []
    for item in kept[:limit]:
        raw = dict(item.raw)
        raw.update(
            {
                "provisional": True,
                "freshness_unknown": item.published_at is None,
                "discovery_method": "cache_fallback",
                "evidence_type": "cached_text_metadata",
                "cache_source": "cache",
                "cache_item_id": item.id,
                "cache_reused_at": now,
                "fallback_reason": reason,
                "is_live_real_time": False,
                "conformant": False,
                "source_evidence_policy_passed": False,
            }
        )
        reused.append(replace(item, id=f"{item.id}:cache:{now}", raw=raw))
    return reused


def run_crawl(
    options: CrawlOptions,
    *,
    on_progress: ProgressCallback | None = None,
) -> CrawlResult:
    def log(message: str) -> None:
        if on_progress:
            on_progress(message)

    config = load_config(options.config)
    names = source_names(options.sources, config)
    fetcher = InsaneSearchFetcher(
        skill_dir=options.insane_search_dir,
        timeout=options.timeout,
        trace=options.trace_fetch,
    )
    log(f"insane-search: {fetcher.skill_dir}")

    raw_items: list[RawItem] = []
    scoring_items: list[RawItem] = []
    errors: dict[str, str] = {}
    rejected_by_source: dict[str, dict[str, int]] = {}
    collector_diagnostics: dict[str, object] = {}
    quality_now = None
    store = Store(options.db)
    try:
        for source_name in names:
            source_config = _source_config(config, source_name)
            if not source_config.get("enabled", True):
                log(f"[skip] {source_name} (비활성)")
                continue
            collector_type = _collector_type(source_name)
            if collector_type is None:
                errors[source_name] = "unknown source"
                log(f"[error] {source_name}: 알 수 없는 소스")
                continue
            log(f"[수집] {source_name} ...")
            accepted: list[RawItem] = []
            collected: list[RawItem] = []
            rejected = []
            fallback_reason = ""
            collector = None
            try:
                collector = collector_type(fetcher, source_config, limit=options.limit_per_source)
                collected = _normalize_source(collector.collect(), source_name)
                accepted, rejected = partition_quality_items(collected, source_config=source_config, now=quality_now)
                accepted = accepted[: options.limit_per_source]
            except Exception as exc:
                fallback_reason = f"{type(exc).__name__}: {exc}"
                errors[source_name] = fallback_reason
                log(f"[오류] {source_name}: {exc}")
            finally:
                if collector is not None and hasattr(collector, "last_run_diagnostics"):
                    collector_diagnostics[source_name] = copy.deepcopy(getattr(collector, "last_run_diagnostics"))

            if (
                source_name == "threads_social"
                or is_social_commerce_source(source_name, _platform_for_source(source_name))
            ) and not accepted:
                reason = fallback_reason or "zero_quality_accepted"
                cached = _reuse_cached_social_items(
                    store,
                    source_name=source_name,
                    source_config=source_config,
                    reason=reason,
                    limit=options.limit_per_source,
                )
                if cached:
                    accepted = cached
                    log(f"[캐시] {source_name}: 최근 {len(cached)}건 재사용 ({reason})")
                else:
                    log(f"[캐시] {source_name}: 재사용 가능한 최근 캐시 없음 ({reason})")

            resolved: list[RawItem] = []
            diagnostic_items: list[RawItem] = []
            purchase_rejected = 0
            for item in accepted:
                resolved_item = resolve_item_purchase_evidence(item, source_config)
                if resolved_item is None:
                    purchase_rejected += 1
                    diagnostic_items.append(_diagnostic_purchase_rejection(item, "purchase_evidence_unresolved"))
                    continue
                diagnostic_items.append(resolved_item)
                if not _is_verified_real_time_item(resolved_item):
                    purchase_rejected += 1
                    continue
                resolved.append(resolved_item)
            accepted = resolved
            if purchase_rejected:
                rejected_by_source.setdefault(source_name, {})["purchase_evidence_unresolved"] = purchase_rejected

            raw_items.extend(diagnostic_items)
            scoring_items.extend(
                item
                for item in accepted
                if not _is_fixture_cache_or_degraded(item.raw if isinstance(item.raw, dict) else {})
            )
            filter_note = ""
            if rejected:
                rejected_by_source[source_name] = {
                    **rejected_by_source.get(source_name, {}),
                    **{f"quality:{reason}": count for reason, count in _rejection_counts(rejected).items()},
                }
                summary = rejection_summary(rejected)
                filter_note = f" (품질 필터 {len(rejected)}건"
                if summary:
                    filter_note += f": {summary}"
                filter_note += ")"
            log(f"[완료] {source_name}: {len(accepted)}건 채택 / {len(collected)}건 수집{filter_note}")

        threshold = (
            options.threshold
            if options.threshold is not None
            else float(config.get("scoring", {}).get("threshold", 4.7))
        )
        strict_candidates = [item for item in scoring_items if _is_verified_real_time_item(item)]
        log(f"[점수] {len(strict_candidates)}건 분석 중 (기준 {threshold}) ...")
        scored = [
            replace(
                _score_item_for_source(item, threshold=threshold, source_config=_source_config(config, item.source)),
                is_product_idea=True,
            )
            for item in strict_candidates
        ]
        ideas = dedupe_ideas(scored)
        live_acceptance = _live_acceptance_metadata(ideas, rejected_by_source, collector_diagnostics)
        live_acceptance["acceptance"] = _acceptance_gate(options, live_acceptance)
        live_acceptance["collector_diagnostics"] = _redact_metadata(collector_diagnostics)
        acceptance_status = str((live_acceptance.get("acceptance") or {}).get("status") or "")
        publish_ideas = ideas if acceptance_status == "completed" else []
        publish_item_ids = {idea.item.id for idea in publish_ideas}
        publish_raw_items = [item for item in raw_items if item.id in publish_item_ids]
        log(f"[정리] 검증 아이디어 {len(ideas)}건 / 게시 {len(publish_ideas)}건")

        if publish_ideas:
            store.upsert_items(publish_raw_items)
            store.upsert_ideas(publish_ideas)
    finally:
        store.close()

    outputs = write_outputs(raw_items, publish_ideas, options.out, top=options.top, live_acceptance=live_acceptance)
    log(f"[저장] {Path(options.out).resolve()}")

    return CrawlResult(
        sources=names,
        raw_items=raw_items,
        ideas=ideas,
        outputs=outputs,
        errors=errors,
        insane_search_dir=str(fetcher.skill_dir),
        live_acceptance=live_acceptance,
        rejection_summary=rejected_by_source,
        collector_diagnostics=collector_diagnostics,
        db=options.db,
    )


def _score_item_for_source(
    item: RawItem,
    *,
    threshold: float,
    source_config: dict[str, Any],
) -> ScoredIdea:
    effective_threshold = effective_score_threshold(threshold, source_config=source_config, item=item)
    parameters = inspect.signature(score_item).parameters
    if "source_config" in parameters:
        return score_item(item, threshold=effective_threshold, source_config=source_config)
    return score_item(item, threshold=effective_threshold)


def _diagnostic_purchase_rejection(item: RawItem, reason: str) -> RawItem:
    raw = dict(item.raw if isinstance(item.raw, dict) else {})
    reasons = raw.get("rejection_reasons")
    if not isinstance(reasons, list):
        reasons = []
    raw["purchase_evidence_rejected"] = True
    raw["resolved_for_scoring"] = False
    raw["rejection_reasons"] = [*reasons, reason]
    return replace(item, raw=raw)


def _rejection_counts(rejected: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rejected:
        for reason in getattr(row, "reasons", ()):
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts

def _live_acceptance_metadata(
    ideas: list[ScoredIdea],
    rejected_by_source: dict[str, dict[str, int]],
    collector_diagnostics: dict[str, object] | None = None,
) -> dict[str, Any]:
    source_counts: dict[str, int] = {}
    real_time_source_counts: dict[str, int] = {}
    threads_verified_lane_counts = {"account": 0, "keyword": 0, "related_post": 0}
    threads_lane_attempt_counts = {"account": 0, "keyword": 0, "related_post": 0}
    verified_real_time_count = 0

    threads_diagnostics = (collector_diagnostics or {}).get("threads_social")
    if isinstance(threads_diagnostics, dict):
        lane_attempt_counts = threads_diagnostics.get("lane_attempt_counts")
        if isinstance(lane_attempt_counts, dict):
            for lane in threads_lane_attempt_counts:
                threads_lane_attempt_counts[lane] = int(lane_attempt_counts.get(lane) or 0)

    for idea in ideas:
        item = idea.item
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
        if not _is_verified_real_time_item(item):
            continue
        verified_real_time_count += 1
        real_time_source_counts[item.source] = real_time_source_counts.get(item.source, 0) + 1
        if item.source == "threads_social" or item.platform == "threads":
            lane = str((item.raw or {}).get("discovery_lane") or "")
            if lane in threads_verified_lane_counts:
                threads_verified_lane_counts[lane] += 1

    return {
        "verified_real_time_count": verified_real_time_count,
        "threads_lane_counts": threads_verified_lane_counts,
        "threads_verified_lane_counts": threads_verified_lane_counts,
        "threads_lane_attempt_counts": threads_lane_attempt_counts,
        "source_counts": source_counts,
        "real_time_source_counts": real_time_source_counts,
        "rejection_summary": rejected_by_source,
    }


def _acceptance_gate(options: CrawlOptions, live_acceptance: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    verified_real_time_count = int(live_acceptance.get("verified_real_time_count") or 0)
    lane_attempt_counts = live_acceptance.get("threads_lane_attempt_counts")
    if not isinstance(lane_attempt_counts, dict):
        lane_attempt_counts = {}
    missing_lanes = [
        lane
        for lane in ["account", "keyword", "related_post"]
        if int(lane_attempt_counts.get(lane) or 0) <= 0
    ]
    if verified_real_time_count < options.min_verified_real_time:
        failures.append(f"verified_real_time_count {verified_real_time_count} < min_verified_real_time {options.min_verified_real_time}")
    if options.require_threads_lanes and missing_lanes:
        failures.append("missing Threads lane attempts: " + ", ".join(missing_lanes))
    return {
        "status": "completed" if not failures else "blocked",
        "required": {
            "min_verified_real_time": int(options.min_verified_real_time),
            "require_threads_lanes": bool(options.require_threads_lanes),
        },
        "failures": failures,
        "missing_threads_lane_attempts": missing_lanes,
    }


def _redact_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ("token", "secret", "password", "cookie", "authorization", "api_key")):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact_metadata(item)
        return redacted
    if isinstance(value, list):
        return [_redact_metadata(item) for item in value]
    return value


def _is_verified_real_time_item(item: RawItem) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    if raw.get("is_live_real_time") is not True:
        return False
    if _is_fixture_cache_or_degraded(raw):
        return False
    evidence = raw.get("purchase_evidence")
    if raw.get("public_only") is not True:
        return False
    if raw.get("conformant") is not True:
        return False
    if raw.get("source_evidence_policy_passed") is not True:
        return False
    top_dedupe_key = raw.get("dedupe_key")
    if not (isinstance(top_dedupe_key, str) and top_dedupe_key.startswith("final_url:")):
        return False
    if not isinstance(evidence, list):
        return False
    return any(_is_successful_canonical_purchase_evidence(row, item=item, raw=raw, top_dedupe_key=top_dedupe_key) for row in evidence)




def _is_fixture_cache_or_degraded(raw: dict[str, Any]) -> bool:
    if raw.get("fixture") or raw.get("is_fixture") or raw.get("provisional") or raw.get("degraded") or raw.get("cache_source") or raw.get("is_cache_fallback"):
        return True
    lowered_fields = {
        str(raw.get("acquisition_mode") or "").lower(),
        str(raw.get("discovery_method") or "").lower(),
        str(raw.get("evidence_type") or "").lower(),
    }
    return bool(lowered_fields & {"cache", "cached", "fixture", "degraded", "fallback", "cached_text_metadata"})


def _is_successful_canonical_purchase_evidence(row: object, *, item: RawItem, raw: dict[str, Any], top_dedupe_key: str) -> bool:
    if not isinstance(row, dict):
        return False
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    if row.get("fixture") is True:
        return False
    status = str(resolver.get("status") or row.get("resolver_status") or "").lower()
    if resolver.get("fixture") is True:
        return False
    if status not in {"resolved", "pre_resolved", "resolved_http_restricted"}:
        return False
    final_url = clean_final_url(str(row.get("final_url") or resolver.get("final_url") or ""))
    if not final_url:
        return False
    dedupe_key = row.get("dedupe_key")
    if not (isinstance(dedupe_key, str) and dedupe_key.startswith("final_url:")):
        return False
    dedupe_final_url = clean_final_url(dedupe_key.removeprefix("final_url:"))
    top_final_url = clean_final_url(top_dedupe_key.removeprefix("final_url:"))
    if not dedupe_final_url or not top_final_url or dedupe_final_url != top_final_url or final_url != dedupe_final_url:
        return False
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    if provenance.get("fixture") is True or "fixture" in str(provenance.get("source") or "").lower() or provenance.get("fixture_note"):
        return False
    canonical = (
        row.get("canonical") is True
        or row.get("canonical_purchase_evidence") is True
        or provenance.get("canonical_product_page") is True
        or row.get("kind") in {"product_page", "canonical_purchase_evidence"}
        or (row.get("kind") == "same_author_affiliate_followup" and _same_author_followup_verified(item, raw, row))
        or (
            row.get("kind") == "purchase_url"
            and _collector_public_read_proof_passed(raw, platform=item.platform)
            and raw.get("source_evidence_policy_passed") is True
        )
    )
    if not canonical:
        return False
    if provenance and provenance.get("public_only") is not True and raw.get("public_only") is not True:
        return False
    return True

def _same_author_followup_verified(item: RawItem, raw: dict[str, Any], row: dict[str, Any]) -> bool:
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    item_author = _canonical_author(item.author)
    reply_author = _canonical_author(provenance.get("reply_author") or row.get("reply_author") or provenance.get("author") or row.get("author"))
    source_author = _canonical_author(provenance.get("author") or row.get("author") or item.author)
    if not item_author or reply_author != item_author or source_author != item_author:
        return False
    return _has_reply_binding(item, row, provenance) or _collector_same_author_reply_proof_passed(item, raw, provenance)


def _has_reply_binding(item: RawItem, row: dict[str, Any], provenance: dict[str, Any]) -> bool:
    reply_values = [
        row.get("reply_url"),
        row.get("reply_id"),
        row.get("reply_post_id"),
        row.get("post_url"),
        row.get("captured_reply_url"),
        row.get("captured_reply_id"),
        provenance.get("reply_url"),
        provenance.get("reply_id"),
        provenance.get("reply_post_id"),
        provenance.get("post_url"),
        provenance.get("captured_reply_url"),
        provenance.get("captured_reply_id"),
    ]
    root_values = [
        row.get("root_url"),
        row.get("root_id"),
        row.get("root_post_url"),
        row.get("root_post_id"),
        provenance.get("root_url"),
        provenance.get("root_id"),
        provenance.get("root_post_url"),
        provenance.get("root_post_id"),
    ]
    has_reply = any(isinstance(value, str) and value.strip() for value in reply_values)
    has_root = any(isinstance(value, str) and value.strip() for value in root_values)
    return has_reply and (has_root or _url_matches_item(item.url, reply_values))


def _collector_same_author_reply_proof_passed(item: RawItem, raw: dict[str, Any], provenance: dict[str, Any]) -> bool:
    proof = raw.get("collector_public_read_proof")
    if not _collector_public_read_proof_passed(raw, platform=item.platform) or not isinstance(proof, dict):
        return False
    item_author = _canonical_author(item.author)
    proof_author = _canonical_author(proof.get("author") or proof.get("item_author") or provenance.get("author"))
    proof_reply_author = _canonical_author(proof.get("reply_author") or provenance.get("reply_author") or provenance.get("author"))
    if proof_author != item_author or proof_reply_author != item_author:
        return False
    return any(
        isinstance(proof.get(key), str) and proof.get(key).strip()
        for key in ("reply_url", "reply_id", "captured_reply_url", "captured_reply_id", "root_url", "root_id", "root_post_url", "root_post_id")
    )


def _url_matches_item(item_url: str, values: list[Any]) -> bool:
    normalized_item_url = str(item_url or "").strip().rstrip("/")
    if not normalized_item_url:
        return False
    return any(isinstance(value, str) and value.strip().rstrip("/") == normalized_item_url for value in values)


def _canonical_author(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("@").lower()


def _collector_public_read_proof_passed(raw: dict[str, Any], *, platform: str) -> bool:
    proof = raw.get("collector_public_read_proof")
    if not isinstance(proof, dict):
        return False
    if proof.get("public_only") is not True:
        return False
    method = str(proof.get("method") or proof.get("http_method") or "").upper()
    if method not in {"GET", "HEAD"}:
        return False
    if proof.get("fetch_ok") is False:
        return False
    return str(proof.get("platform") or "").lower() == platform.lower()

