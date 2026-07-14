from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CATEGORY_SEEDS = ["gadgets", "home", "kitchen", "beauty", "pet", "fitness"]
INSTALLED_CONFIG_RELATIVE_PATH = Path("share/product_idea_radar/config/sources.json")
CANONICAL_CONFIG_PATH = Path("config/sources.json")


DEFAULT_CONFIG: dict[str, Any] = {
    "sources": {
        "producthunt": {"enabled": True, "feed_url": "https://www.producthunt.com/feed"},
        "kickstarter": {"enabled": True, "feed_url": "https://www.kickstarter.com/projects/feed.atom"},
        "reddit": {
            "enabled": True,
            "subreddits": ["INEEEEDIT", "DidntKnowIWantedThat", "gadgets", "ShutUpAndTakeMyMoney"],
            "limit_per_subreddit": 8,
        },
        "threads": {
            "enabled": True,
            "platform": "threads",
            "discovery_mode": "public_threads_live",
            "social_threshold": 5.5,
            "cache_fallback_hours": 24,
            "fixture_mode": False,
            "accounts": [
                "moel.pick",
                "smart.pick_",
                "item_market.k",
                "homlit_pick",
                "it_goodgiraffe",
                "1year3year",
                "nostalsokei",
                "tkasiddl_99",
                "iguan_a9305",
                "pogeun__shop",
                "health60s",
            ],
            "korean_queries": [
                "신박한 상품",
                "쿠팡 추천템",
                "생활용품 추천",
                "주방템 추천",
                "가성비템",
                "아이디어 상품",
                "선물 추천",
                "반려동물 용품",
                "캠핑용품",
                "정리 수납템",
            ],
            "related_post_urls": ["https://www.threads.com/@moel.pick/post/DarhfjSE24O"],
            "live": {
                "enabled": True,
                "headless": True,
                "search_url": "https://www.threads.com/search?q={query}",
                "related_post_urls": ["https://www.threads.com/@moel.pick/post/DarhfjSE24O"],
                "timeout_ms": 15000,
                "browser": {
                    "engine": "playwright",
                    "session_mode": "anonymous",
                    "service_workers": "block",
                    "trace_zero_write": True,
                    "disposable_profile_root": "%LOCALAPPDATA%/ProductIdeaRadar/threads_sessions",
                },
            },
            "resolver": {"timeout_seconds": 8, "max_redirects": 5},
            "fetch_multiplier": 2,
            "max_age_days": 30,
        },
        "x_social": {
            "enabled": True,
            "platform": "x",
            "discovery_mode": "hybrid",
            "social_threshold": 5.5,
            "cache_fallback_hours": 24,
            "category_seeds": CATEGORY_SEEDS,
            "official_api": {
                "env": ["X_BEARER_TOKEN"],
                "endpoint": "https://api.x.com/2/tweets/search/recent",
            },
            "public_web": {
                "provisional": True,
                "freshness_unknown": True,
                "search_url": "https://x.com/search?q={query}&f=live",
            },
        },
        "instagram_social": {
            "enabled": True,
            "platform": "instagram",
            "discovery_mode": "hybrid",
            "social_threshold": 5.5,
            "cache_fallback_hours": 24,
            "category_seeds": CATEGORY_SEEDS,
            "official_api": {
                "env": ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_IG_USER_ID"],
                "hashtag_search_endpoint": "https://graph.facebook.com/v19.0/ig_hashtag_search",
                "recent_media_endpoint": "https://graph.facebook.com/v19.0/{hashtag_id}/recent_media",
            },
            "public_web": {
                "provisional": True,
                "freshness_unknown": True,
                "tag_url": "https://www.instagram.com/explore/tags/{tag}/",
            },
        },
        "x": {
            "enabled": False,
            "discovery_mode": "direct_legacy",
            "handles": ["ProductHunt", "yankodesign", "GadgetFlow", "coolhunting"],
            "limit_per_handle": 4,
        },
        "instagram": {"enabled": False, "discovery_mode": "direct_legacy", "urls": []},
    },
    "scoring": {"threshold": 4.0},
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        return _default_config()

    config_path = Path(path)
    if config_path.exists():
        return _load_config_file(config_path)

    if not config_path.is_absolute() and getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            bundled_path = Path(bundle_dir) / config_path
            if bundled_path.exists():
                return _load_config_file(bundled_path)

    installed_path = _installed_config_path(config_path)
    if installed_path is not None:
        return _load_config_file(installed_path)

    raise FileNotFoundError(f"Config file not found: {config_path}")


def _installed_config_path(config_path: Path) -> Path | None:
    if config_path.is_absolute() or config_path != CANONICAL_CONFIG_PATH:
        return None
    prefix = Path(sys.prefix).resolve()
    candidate = (prefix / INSTALLED_CONFIG_RELATIVE_PATH).resolve()
    try:
        candidate.relative_to(prefix)
    except ValueError:
        return None
    if candidate.exists():
        return candidate
    return None


def _default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _load_config_file(config_path: Path) -> dict[str, Any]:
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    merged = merge_dicts(_default_config(), loaded)
    if isinstance(loaded.get("sources"), dict):
        merged["_source_names"] = list(loaded["sources"].keys())
    return merged


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = merge_dicts(base[key], value)
        else:
            base[key] = value
    return base
