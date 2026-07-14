from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

from .evidence import has_public_provenance, normalize_purchase_evidence
from .models import RawItem
from .utils import clean_text, tokenize

DEFAULT_MAX_AGE_DAYS = {
    "producthunt": 90,
    "kickstarter": 180,
    "reddit": 45,
    "x": 30,
    "instagram": 30,
}

STRONG_PHYSICAL_TERMS = {
    "appliance", "bag", "bike", "bottle", "camera", "case", "chair", "charger", "container",
    "controller", "cookware", "desk", "device", "display", "drone", "earbuds", "fan", "feeder",
    "fridge", "furniture", "gadget", "kit", "knife", "lamp", "maker", "phone", "pillow",
    "robot", "screen", "sensor", "socks", "stand", "table", "tool", "wearable",
    "가젯", "기기", "도구", "로봇", "휴대용", "스마트",
}

SOFTWARE_PRODUCT_TERMS = {
    "agent", "agents", "api", "app", "apps", "assistant", "automation", "browser", "dashboard",
    "extension", "plugin", "platform", "workflow", "saas", "software", "service", "services",
    "newsletter", "course", "template", "templates", "download", "digital",
}

OFF_TOPIC_TERMS = {
    "album", "anthology", "artbook", "book", "books", "chapter", "comic", "comics", "documentary",
    "drama", "fantasy", "fiction", "film", "folklore", "game", "games", "horror", "issue", "journal",
    "manga", "miniature", "miniatures", "movie", "mystery", "mythology", "novel", "novella", "pdf",
    "puzzle book", "romance", "rpg", "short stories", "stl", "stl files", "story", "stories",
    "storybook", "superhero", "tarot", "troops", "vampire", "zine",
}

HARD_REJECT_PHRASES = {
    "comic book", "puzzle book", "short stories", "stl file", "stl files", "tabletop rpg",
    "miniature stl", "chapter books", "special edition", "issue 1", "issue #1",
}

SOCIAL_NOISE_PHRASES = {
    "could win", "drop it in the replies", "gm legends", "giveaway", "hiring", "job", "livestream",
    "shipaton", "sponsored", "what are you shipping", "win big", "take your job",
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SOCIAL_COMMERCE_SOURCES = {"x_social", "instagram_social", "threads_social", "reddit"}
SOCIAL_COMMERCE_PLATFORMS = {"x", "instagram", "threads", "reddit"}

SOCIAL_PRODUCT_EVIDENCE_KEYS = {
    "brand", "brand_name", "manufacturer", "maker", "product", "product_name", "name", "caption",
    "description",
}

SOCIAL_CHATTER_PHRASES = {
    "breaking news", "did you see", "hot take", "just posted", "just vibes", "lol",
    "meme", "memes", "my thoughts", "news:", "reply guys", "thread", "thoughts?",
    "viral clip", "vibes",
}

SOCIAL_PLATFORM_BOILERPLATE_PHRASES = {
    "javascript is disabled in this browser",
    "supported browser to continue using x.com",
    "something went wrong, but don't fret",
    "something went wrong, but don’t fret",
    "reels on instagram",
    "posts on instagram",
    "instagram 계정을 만들거나",
    "instagram에 로그인",
}


PROMO_WITH_EVIDENCE_PHRASES = {
    "#ad", "ad:", "event", "event launch", "launch event", "promo", "promos", "promotion", "sponsored",
}
SAME_AUTHOR_FOLLOWUP_REJECTION = "동일 작성자 후속 구매 증거 불일치"
RESOLVER_FAILURE_STATUSES = {"timeout", "unresolved", "error", "http_error", "blocked", "invalid", "unsupported", "failed"}



@dataclass(frozen=True, slots=True)
class RejectedItem:
    item: RawItem
    reasons: tuple[str, ...]


def is_social_commerce_source(source: str | None, platform: str | None = None) -> bool:
    return source in SOCIAL_COMMERCE_SOURCES or platform in SOCIAL_COMMERCE_PLATFORMS


def partition_quality_items(
    items: list[RawItem],
    *,
    source_config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[list[RawItem], list[RejectedItem]]:
    kept: list[RawItem] = []
    rejected: list[RejectedItem] = []
    for item in items:
        reasons = quality_reasons(item, source_config=source_config, now=now)
        if reasons:
            rejected.append(RejectedItem(item=item, reasons=reasons))
        else:
            kept.append(item)
    return kept, rejected


def quality_reasons(
    item: RawItem,
    *,
    source_config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, ...]:
    source_config = source_config or {}
    now = _as_utc(now or datetime.now(timezone.utc))
    text = _combined_text(item)
    lowered = text.lower()
    tokens = set(tokenize(lowered))
    reasons: list[str] = []

    if len(clean_text(item.title)) < 4:
        reasons.append("제목 부족")

    max_age_days = _max_age_days(item, source_config)
    published = parse_datetime(item.published_at)
    if item.platform in {"reddit", "x", "instagram", "threads"} and published is None and not _freshness_may_be_unknown(item):
        reasons.append("소셜 발행일 없음")
    if published is not None and max_age_days is not None:
        age_days = (now - published).total_seconds() / 86_400
        if age_days > max_age_days:
            reasons.append(f"오래된 글>{max_age_days}일")
        elif age_days < -2:
            reasons.append("미래 발행일")

    physical_signal = _has_physical_signal(lowered, tokens) or (item.raw or {}).get("physical_signal") is True
    software_service_signal = _has_software_or_service_signal(lowered, tokens)
    strong_signal = physical_signal
    off_topic_hits = _matched_terms(lowered, tokens, OFF_TOPIC_TERMS)
    if software_service_signal and not physical_signal:
        reasons.append("소프트웨어/서비스/디지털 제품")
    if not physical_signal:
        reasons.append("물리 소비재 신호 부족")
    if _contains_any(lowered, HARD_REJECT_PHRASES):
        reasons.append("콘텐츠/굿즈 캠페인")
    elif off_topic_hits and not strong_signal:
        reasons.append("비제품 키워드: " + ", ".join(off_topic_hits[:4]))
    if item.platform == "kickstarter" and off_topic_hits and not physical_signal:
        reasons.append("킥스타터 비제품 캠페인")
    if item.platform == "kickstarter" and not strong_signal:
        reasons.append("킥스타터 제품 신호 부족")

    purchase_evidence = normalize_purchase_evidence(item)
    has_public = has_public_provenance(item)

    if _has_resolver_failure(purchase_evidence):
        reasons.append("구매 증거 리졸버 실패")
    if _has_invalid_same_author_followup(item, purchase_evidence):
        reasons.append(SAME_AUTHOR_FOLLOWUP_REJECTION)

    if item.platform in {"producthunt", "kickstarter"} and not _has_url_purchase_evidence(purchase_evidence):
        reasons.append("물리 제품 페이지 구매 증거 부족")
    if is_social_commerce_source(item.source, item.platform):
        if _contains_any(lowered, SOCIAL_PLATFORM_BOILERPLATE_PHRASES):
            reasons.append("소셜 플랫폼 차단/색인 페이지")
        product_identity = _has_product_identity(item, lowered)
        if _contains_any(lowered, SOCIAL_CHATTER_PHRASES) and not product_identity:
            reasons.append("소셜 잡담/뉴스/밈")
        if _contains_any(lowered, SOCIAL_NOISE_PHRASES) and not (strong_signal and product_identity):
            reasons.append("소셜 잡담/홍보")
        if not strong_signal and not product_identity:
            reasons.append("소셜 제품/브랜드 신호 부족")
        if not strong_signal and not product_identity and len(_without_urls(lowered)) < 80:
            reasons.append("얇은 소셜 글")
        if not _has_public_metrics(item):
            reasons.append("소셜 공개 참여 지표 부족")
        if not _has_url_purchase_evidence(purchase_evidence):
            reasons.append("소셜 구매/태그 증거 부족")
        if not has_public:
            reasons.append("소셜 공개 출처 아님")

    extra_rejects = source_config.get("exclude_terms") or source_config.get("excluded_terms") or []
    for term in _iter_terms(extra_rejects):
        if term in lowered:
            reasons.append(f"소스 제외어: {term}")
            break

    return tuple(dict.fromkeys(reasons))


def rejection_summary(rejected: list[RejectedItem], *, limit: int = 3) -> str:
    if not rejected:
        return ""
    counts = Counter(reason for row in rejected for reason in row.reasons)
    return ", ".join(f"{reason} {count}" for reason, count in counts.most_common(limit))


def has_strong_product_signal(text: str, tokens: set[str] | None = None) -> bool:
    tokens = tokens or set(tokenize(text))
    return _has_physical_signal(text, tokens)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return _as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _max_age_days(item: RawItem, source_config: dict[str, Any]) -> int | None:
    value = source_config.get("max_age_days")
    if value is None and isinstance(source_config.get("quality"), dict):
        value = source_config["quality"].get("max_age_days")
    if value is None:
        value = DEFAULT_MAX_AGE_DAYS.get(item.platform)
    if value in (None, False):
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS.get(item.platform)


def _combined_text(item: RawItem) -> str:
    return clean_text(" ".join(part for part in [item.title, item.text, " ".join(item.tags)] if part), max_len=2_000)


def _without_urls(text: str) -> str:
    return clean_text(_URL_RE.sub(" ", text))


def _freshness_may_be_unknown(item: RawItem) -> bool:
    return bool(item.raw.get("provisional") or item.raw.get("freshness_unknown"))


def _has_product_identity(item: RawItem, text: str) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    for key in SOCIAL_PRODUCT_EVIDENCE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and clean_text(value):
            return True
    title_tokens = [token for token in tokenize(item.title.lower()) if len(token) >= 3]
    if len(title_tokens) >= 2 and has_strong_product_signal(text) and not _contains_any(text, SOCIAL_CHATTER_PHRASES):
        return True
    return any(phrase in text for phrase in PROMO_WITH_EVIDENCE_PHRASES) and has_strong_product_signal(text)


def _has_physical_signal(text: str, tokens: set[str]) -> bool:
    return any(_contains_term(text, tokens, term) for term in STRONG_PHYSICAL_TERMS)


def _has_software_or_service_signal(text: str, tokens: set[str]) -> bool:
    return any(_contains_term(text, tokens, term) for term in SOFTWARE_PRODUCT_TERMS)


def _has_public_metrics(item: RawItem) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    metric_sources = [item.metrics, raw.get("engagement"), raw.get("public_engagement")]
    metric_keys = ("like_count", "likes", "reply_count", "replies", "comment_count", "comments", "view_count", "views", "score", "reposts", "quotes")
    return any(
        isinstance(source, dict)
        and any(isinstance(source.get(key), (int, float, str)) and str(source.get(key)).strip() != "" for key in metric_keys)
        for source in metric_sources
    )


def _has_url_purchase_evidence(purchase_evidence: list[dict[str, Any]]) -> bool:
    return any(
        (isinstance(row.get("raw_url"), str) and bool(str(row.get("raw_url") or "").strip()))
        or str(row.get("dedupe_key") or "").startswith("final_url:")
        for row in purchase_evidence
    )

def _has_resolver_failure(purchase_evidence: list[dict[str, Any]]) -> bool:
    for row in purchase_evidence:
        resolver = row.get("resolver")
        if isinstance(resolver, dict) and str(resolver.get("status") or "").lower() in RESOLVER_FAILURE_STATUSES:
            return True
    return False


def _has_invalid_same_author_followup(item: RawItem, purchase_evidence: list[dict[str, Any]]) -> bool:
    for row in purchase_evidence:
        if _claims_same_author_followup(row) and not _valid_same_author_followup(item, row):
            return True
    return False


def _valid_same_author_followup(item: RawItem, row: dict[str, Any]) -> bool:
    affiliate = row.get("affiliate") if isinstance(row.get("affiliate"), dict) else {}
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    if affiliate.get("same_author") is False:
        return False

    item_author = _canonical_author(item.author)
    if not item_author or _provenance_author_mismatched(item_author, provenance):
        return False
    if not _provenance_authors_match_item(item_author, provenance):
        return False
    return _has_reply_binding(item, row, provenance) or _collector_same_author_reply_proof_passed(item, provenance)


def _provenance_authors_match_item(item_author: str, provenance: dict[str, Any]) -> bool:
    if not item_author:
        return False
    provenance_authors = [
        _canonical_author(provenance.get(key))
        for key in ("author", "reply_author")
        if _canonical_author(provenance.get(key))
    ]
    return bool(provenance_authors) and all(author == item_author for author in provenance_authors)


def _provenance_author_mismatched(item_author: str, provenance: dict[str, Any]) -> bool:
    if not item_author:
        return False
    return any(
        bool(author) and author != item_author
        for author in (_canonical_author(provenance.get(key)) for key in ("author", "reply_author"))
    )


def _claims_same_author_followup(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "").lower()
    if "same_author" in kind and ("followup" in kind or "follow_up" in kind or "reply" in kind):
        return True
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    surface = " ".join(str(provenance.get(key) or "").lower() for key in ("surface", "lane", "capture", "source"))
    return "same_author" in surface and ("followup" in surface or "follow_up" in surface or "reply" in surface)


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
    has_reply = any(isinstance(value, str) and clean_text(value) for value in reply_values)
    has_root = any(isinstance(value, str) and clean_text(value) for value in root_values)
    return has_reply and (has_root or _url_matches_item(item.url, reply_values))


def _collector_same_author_reply_proof_passed(item: RawItem, provenance: dict[str, Any]) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    proof = raw.get("collector_public_read_proof")
    if not isinstance(proof, dict):
        return False
    item_author = _canonical_author(item.author)
    proof_author = _canonical_author(proof.get("author") or proof.get("item_author") or provenance.get("author"))
    proof_reply_author = _canonical_author(proof.get("reply_author") or provenance.get("reply_author") or provenance.get("author"))
    if not item_author or proof_author != item_author or proof_reply_author != item_author:
        return False
    if proof.get("public_only") is not True:
        return False
    method = str(proof.get("method") or proof.get("http_method") or "").upper()
    if method not in {"GET", "HEAD"}:
        return False
    return any(
        isinstance(proof.get(key), str) and clean_text(proof.get(key))
        for key in ("reply_url", "reply_id", "captured_reply_url", "captured_reply_id", "root_url", "root_id", "root_post_url", "root_post_id")
    )


def _url_matches_item(item_url: str, values: Iterable[Any]) -> bool:
    normalized_item_url = clean_text(item_url).rstrip("/")
    if not normalized_item_url:
        return False
    return any(isinstance(value, str) and clean_text(value).rstrip("/") == normalized_item_url for value in values)

def _canonical_author(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return clean_text(value).strip().lstrip("@").lower()


def _matched_terms(text: str, tokens: set[str], terms: Iterable[str]) -> list[str]:
    return sorted(term for term in terms if _contains_term(text, tokens, term))


def _contains_any(text: str, phrases: Iterable[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _contains_term(text: str, tokens: set[str], term: str) -> bool:
    term = term.lower()
    if " " in term or "-" in term or any(ord(ch) > 127 for ch in term):
        return term in text
    return term in tokens


def _iter_terms(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value.lower()
    elif isinstance(value, Iterable):
        for term in value:
            if isinstance(term, str) and term.strip():
                yield term.strip().lower()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
