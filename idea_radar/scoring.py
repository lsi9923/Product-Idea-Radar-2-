from __future__ import annotations

import math

from .models import RawItem, ScoredIdea
from .quality import is_social_commerce_source
from .utils import clamp, normalize_key, parse_compact_number, tokenize

PRODUCT_TERMS = {
    "gadget", "device", "tool", "kit", "robot", "camera", "sensor", "wearable", "pillow",
    "fan", "cooling", "kitchen", "cookware", "knife", "bag", "bottle", "lamp", "charger",
    "container", "feeder", "fridge", "screen", "touchscreen", "desk", "home", "smart",
    "portable", "pocket", "card-sized", "self-cleaning", "modular", "massage", "safety",
    "gps", "radar", "air", "socks", "appliance", "maker", "case", "stand", "controller",
    "display", "mini", "all-terrain", "bike", "bicycle", "chair", "drone", "earbuds",
    "furniture", "phone", "table",
    "아이디어", "상품", "신박", "꿀템", "가젯", "로봇", "휴대용", "스마트", "便利", "グッズ", "ガジェット",
}

WEAK_PRODUCT_TERMS = {
    "air", "all-terrain", "card-sized", "gps", "home", "mini", "modular", "pocket", "portable",
    "radar", "safety", "smart", "아이디어", "상품", "신박", "꿀템", "휴대용", "스마트",
}

SOFTWARE_PRODUCT_TERMS = {
    "app", "apps", "agent", "agents", "ai", "api", "workflow", "automation", "platform",
    "extension", "plugin", "dashboard", "assistant", "inbox", "browser", "search", "voice",
}

NOVELTY_TERMS = {
    "first", "new", "novel", "world", "smart", "ai", "automatic", "automated", "self", "adaptive",
    "modular", "snap-on", "card-sized", "pocket-sized", "all-terrain", "zero", "portable", "mini",
    "reimagined", "reinvent", "breakthrough", "prototype", "launch", "frontier", "true",
    "최초", "신박", "새로운", "자동", "휴대용", "초소형", "스마트", "便利", "世界初",
}

PAIN_TERMS = {
    "clean", "stay", "safe", "safety", "alert", "relief", "sleep", "pain", "neck", "shoulder",
    "faster", "without", "offline", "control", "organize", "automate", "prevent", "detect",
    "reduce", "save", "comfortable", "cool", "heat", "stress", "focus", "hands-free", "easy",
}

REJECT_TERMS = {
    "comic", "comics", "book", "books", "novel", "novella", "romance", "film", "movie", "album", "tarot",
    "artbook", "anthology", "series", "issue", "zine", "game", "games", "rpg", "miniature", "miniatures",
    "stl", "files", "fiction", "chapter", "journal", "folklore", "mythology", "vampire", "horror",
    "documentary", "drama", "fantasy", "manga", "mystery", "story", "stories", "superhero", "interview",
    "newsletter", "investing", "crypto", "token", "trading", "benchmark", "seo", "brand", "course", "podcast",
}

CATEGORIES = {
    "kitchen": {"kitchen", "cook", "cookware", "pan", "knife", "food", "coffee", "bottle"},
    "home": {"home", "lamp", "desk", "chair", "clean", "sleep", "pillow", "air", "fan", "cooling"},
    "health": {"health", "massage", "relief", "sleep", "neck", "fitness", "running", "vest", "stress"},
    "outdoor": {"outdoor", "edc", "camp", "fire", "pocket", "knife", "all-terrain", "gps", "radar"},
    "pet": {"pet", "dog", "cat", "litter", "leash", "feeder"},
    "mobile": {"phone", "mobile", "macbook", "screen", "touchscreen", "charger", "case", "display"},
    "robotics": {"robot", "robotics", "camera", "auto-following", "sensor", "drone"},
    "ai_software": {"ai", "agent", "agents", "app", "api", "workflow", "automation", "assistant"},
    "productivity": {"task", "inbox", "notes", "browser", "search", "voice", "dictation"},
}

CATEGORY_KO = {
    "kitchen": "주방",
    "home": "홈/생활",
    "health": "헬스케어",
    "outdoor": "아웃도어",
    "pet": "반려동물",
    "mobile": "모바일/디바이스",
    "robotics": "로봇/하드웨어",
    "ai_software": "AI/소프트웨어",
    "productivity": "생산성",
    "general": "아이디어 상품",
}


def score_item(
    item: RawItem,
    *,
    threshold: float = 4.7,
    source_config: dict[str, object] | None = None,
) -> ScoredIdea:
    combined = " ".join([item.title, item.text, " ".join(item.tags)]).lower()
    tokens = set(tokenize(combined))

    product_hits = sorted(t for t in PRODUCT_TERMS if _contains(combined, tokens, t))
    strong_product_hits = [hit for hit in product_hits if hit not in WEAK_PRODUCT_TERMS]
    software_hits = sorted(t for t in SOFTWARE_PRODUCT_TERMS if _contains(combined, tokens, t))
    novelty_hits = sorted(t for t in NOVELTY_TERMS if _contains(combined, tokens, t))
    pain_hits = sorted(t for t in PAIN_TERMS if _contains(combined, tokens, t))
    reject_hits = sorted(t for t in REJECT_TERMS if _contains(combined, tokens, t))

    category = _category(tokens, combined)
    metrics_score = _metrics_score(item.metrics)

    source_boost = _source_boost(item)
    product_signal = min(5.5, len(strong_product_hits) * 1.35 + (len(product_hits) - len(strong_product_hits)) * 0.35 + len(software_hits) * 0.9)
    novelty_score = clamp(2.0 + len(novelty_hits) * 1.15 + (1.0 if product_hits else 0.0) + source_boost * 0.2)
    virality_score = clamp(2.0 + metrics_score + (0.6 if item.platform in {"x", "reddit"} else 0.25))
    market_score = clamp(2.6 + product_signal + len(pain_hits) * 0.85 + _category_boost(category) + source_boost)

    social_candidate = is_social_commerce_source(item.source, item.platform)
    social_identity = social_candidate and _has_social_product_identity(item, strong_product_hits)
    reject_block = bool(
        reject_hits and len(strong_product_hits) < 2 and not software_hits and not social_identity
        and not (social_candidate and strong_product_hits)
    )
    raw_penalty = _raw_evidence_penalty(item)
    penalty = raw_penalty
    if reject_hits:
        penalty += 3.0 if reject_block else 1.2
    if item.platform == "producthunt" and not strong_product_hits and not software_hits:
        penalty += 0.6
    total_score = clamp((novelty_score * 0.34) + (virality_score * 0.18) + (market_score * 0.48) - penalty)
    signal_ok = _has_product_signal(item.platform, product_hits, software_hits) or social_identity
    effective_threshold = effective_score_threshold(
        threshold,
        source_config=source_config,
        item=item,
    )
    is_product = total_score >= effective_threshold and signal_ok and not reject_block

    matched = sorted(set(product_hits + software_hits + novelty_hits + pain_hits))[:20]
    title = item.title.strip() or item.url
    desc = item.text.strip()
    summary = f"{CATEGORY_KO.get(category, '아이디어 상품')} 후보: {title}"
    if desc and desc.lower() not in title.lower():
        summary += f" — {desc[:120]}"

    reason_parts = []
    if product_hits:
        reason_parts.append(f"physical/product signals: {', '.join(product_hits[:6])}")
    if software_hits:
        reason_parts.append(f"software/product signals: {', '.join(software_hits[:6])}")
    if novelty_hits:
        reason_parts.append(f"novelty terms: {', '.join(novelty_hits[:6])}")
    if pain_hits:
        reason_parts.append(f"pain/market terms: {', '.join(pain_hits[:6])}")
    if reject_hits:
        reason_parts.append(f"penalty terms: {', '.join(reject_hits[:6])}")
    if raw_penalty:
        reason_parts.append(f"evidence penalty: -{raw_penalty:.1f}")
    reason = "; ".join(reason_parts) or "weak product signal"

    return ScoredIdea(
        item=item,
        is_product_idea=bool(is_product),
        category=category,
        summary_ko=summary,
        novelty_score=round(novelty_score, 2),
        virality_score=round(virality_score, 2),
        market_score=round(market_score, 2),
        total_score=round(total_score, 2),
        duplicate_key=normalize_key(f"{item.title} {item.text}"),
        reason=reason,
        matched_terms=matched,
    )



def effective_score_threshold(
    global_threshold: float,
    *,
    source_config: dict[str, object] | None = None,
    item: RawItem | None = None,
) -> float:
    if item is not None and not is_social_commerce_source(item.source, item.platform):
        return global_threshold
    source_config = source_config or {}
    try:
        social_threshold = float(source_config.get("social_threshold", 5.5))
    except (TypeError, ValueError):
        social_threshold = 5.5
    return max(global_threshold, social_threshold)


def _raw_evidence_penalty(item: RawItem) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    penalty = 0.0
    if raw.get("provisional"):
        penalty += 0.45
    discovery_method = str(raw.get("discovery_method") or "")
    evidence_type = str(raw.get("evidence_type") or "")
    if "public" in discovery_method or "public" in evidence_type:
        penalty += 0.35
    if raw.get("cache_source") or raw.get("cache_reused_at") or discovery_method == "cache_fallback":
        penalty += 0.65
    return penalty


def _has_social_product_identity(item: RawItem, strong_product_hits: list[str]) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    for key in ("brand", "brand_name", "manufacturer", "maker", "product", "product_name", "name"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            return True
    title_tokens = [token for token in tokenize(item.title.lower()) if len(token) >= 3]
    chatter_text = f"{item.title} {item.text}".lower()
    if any(phrase in chatter_text for phrase in ("just vibes", "meme", "news:", "what are you shipping")):
        return False
    return len(title_tokens) >= 2 and bool(strong_product_hits)

def _contains(text: str, tokens: set[str], term: str) -> bool:
    if "-" in term or " " in term or any(ord(c) > 127 for c in term):
        return term.lower() in text
    return term.lower() in tokens


def _metrics_score(metrics: dict[str, float]) -> float:
    if not metrics:
        return 0.0

    def best(*keys: str) -> float:
        values = [parse_compact_number(metrics.get(k)) for k in keys if k in metrics]
        return max(values) if values else 0.0

    likes = best("likes", "favorite_count", "score", "ups")
    shares = best("retweets", "retweet_count", "shares")
    comments = best("comments", "reply_count", "num_comments")
    views = best("views", "view_count")
    raw = likes + shares * 1.7 + comments * 1.2 + views * 0.03
    return min(5.0, math.log10(raw + 1.0) * 1.15)


def _category(tokens: set[str], text: str) -> str:
    best = ("general", 0)
    for name, terms in CATEGORIES.items():
        score = sum(1 for term in terms if _contains(text, tokens, term))
        if score > best[1]:
            best = (name, score)
    return best[0]


def _has_product_signal(platform: str, product_hits: list[str], software_hits: list[str]) -> bool:
    strong_product_hits = [hit for hit in product_hits if hit not in WEAK_PRODUCT_TERMS]
    if strong_product_hits:
        return True
    if len(product_hits) >= 2 and platform in {"kickstarter", "producthunt"}:
        return True
    non_generic_software = [hit for hit in software_hits if hit != "ai"]
    if platform == "producthunt":
        return bool(software_hits)
    if platform in {"x", "instagram", "reddit"}:
        return len(non_generic_software) >= 1 and len(software_hits) >= 2
    return bool(non_generic_software)

def _source_boost(item: RawItem) -> float:
    if item.platform == "kickstarter":
        return 0.9
    if item.platform == "producthunt":
        return 0.8
    if item.platform == "reddit":
        return 0.5
    if item.platform in {"x", "instagram"}:
        return 0.4
    return 0.0

def _category_boost(category: str) -> float:
    if category in {"kitchen", "home", "health", "mobile", "robotics", "outdoor"}:
        return 1.1
    if category in {"ai_software", "productivity"}:
        return 0.6
    return 0.0
