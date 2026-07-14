from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from ..models import RawItem
from ..utils import clean_text, parse_compact_number, stable_id
from .base import Collector

_PRODUCT_HASHTAGS = ("product", "gadget", "homedesign", "kitchentools", "beautytools", "petproducts", "fitnessgear")


class InstagramCollector(Collector):
    name = "instagram"

    def collect(self) -> list[RawItem]:
        urls = self.config.get("urls", [])
        out: list[RawItem] = []
        for url in urls:
            if len(out) >= self.limit:
                break
            result = self.fetcher.fetch(str(url), selectors=["meta[property='og:title']"])
            if not result.ok and not result.content:
                continue
            item = self._parse_public_page(str(url), result.content)
            if item:
                out.append(item)
        return out

    def _parse_public_page(self, url: str, html_text: str) -> RawItem | None:
        soup = BeautifulSoup(html_text or "", "html.parser")
        og_title = self._meta(soup, "og:title") or self._meta(soup, "twitter:title")
        og_desc = self._meta(soup, "og:description") or self._meta(soup, "description")
        image = self._meta(soup, "og:image") or self._meta(soup, "twitter:image")
        title = clean_text(og_title or og_desc, max_len=120)
        text = clean_text(" ".join(x for x in [og_title, og_desc] if x))
        if not title and not text:
            return None
        return RawItem(
            id=stable_id(self.name, url, title),
            source=self.name,
            platform="instagram",
            url=url,
            title=title or clean_text(text, max_len=120),
            text=text,
            media_urls=[image] if image else [],
            raw={"public_only": True},
        )

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return str(tag.get("content", "")) if tag else ""


class InstagramSocialCollector(InstagramCollector):
    name = "instagram_social"

    def collect(self) -> list[RawItem]:
        target = self.fetch_limit(multiplier=2)
        if self._has_official_credentials():
            try:
                items = self._collect_official(target)
                if items:
                    return items[:target]
                return self._collect_public(target, fallback_reason="official_api_empty")[:target]
            except Exception:
                return self._collect_public(target, fallback_reason="official_api_error")[:target]
        return self._collect_public(target, fallback_reason="missing_official_credentials")[:target]

    def _has_official_credentials(self) -> bool:
        return bool(
            (os.environ.get("INSTAGRAM_ACCESS_TOKEN") and os.environ.get("INSTAGRAM_IG_USER_ID"))
            or isinstance(self._official_config().get("fixture_items"), list)
        )

    def _collect_official(self, target: int) -> list[RawItem]:
        fixture_items = self._official_config().get("fixture_items")
        if isinstance(fixture_items, list):
            return self._parse_media_items(fixture_items, target=target, category_seed="fixture")

        token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        user_id = os.environ.get("INSTAGRAM_IG_USER_ID", "")
        hashtag_endpoint = str(
            self._official_config().get("hashtag_search_endpoint")
            or "https://graph.facebook.com/v19.0/ig_hashtag_search"
        )
        media_template = str(
            self._official_config().get("recent_media_endpoint")
            or "https://graph.facebook.com/v19.0/{hashtag_id}/recent_media"
        )
        out: list[RawItem] = []
        for seed in self._category_seeds():
            if len(out) >= target:
                break
            hashtag_id = self._hashtag_id(hashtag_endpoint, seed, user_id=user_id, token=token)
            if not hashtag_id:
                continue
            params = urlencode(
                {
                    "user_id": user_id,
                    "fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count,username",
                    "limit": str(max(1, min(50, target - len(out)))),
                    "access_token": token,
                }
            )
            request = Request(f"{media_template.format(hashtag_id=hashtag_id)}?{params}")
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            out.extend(self._parse_media_items(payload.get("data") or [], target=target - len(out), category_seed=seed))
        return out[:target]

    def _hashtag_id(self, endpoint: str, seed: str, *, user_id: str, token: str) -> str | None:
        params = urlencode({"user_id": user_id, "q": seed, "access_token": token})
        request = Request(f"{endpoint}?{params}")
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") or []
        if not data:
            return None
        first = data[0]
        return str(first.get("id")) if isinstance(first, dict) and first.get("id") else None

    def _parse_media_items(self, media_items: list[Any], *, target: int, category_seed: str) -> list[RawItem]:
        out: list[RawItem] = []
        for media in media_items:
            if len(out) >= target or not isinstance(media, dict):
                break
            caption = clean_text(media.get("caption") or media.get("text") or media.get("title"), max_len=500)
            media_id = str(media.get("id") or "")
            url = str(media.get("permalink") or media.get("url") or "")
            if not caption or not (media_id or url):
                continue
            if not url:
                url = f"https://www.instagram.com/p/{media_id}/"
            image = media.get("media_url") or media.get("thumbnail_url") or media.get("image")
            metrics: dict[str, float] = {}
            for key in ("like_count", "comments_count"):
                if key in media:
                    metrics[key] = parse_compact_number(media.get(key))
            item = RawItem(
                id=stable_id(self.name, url, caption),
                source=self.name,
                platform="instagram",
                url=url,
                title=clean_text(caption, max_len=110),
                text=caption,
                author=media.get("username") or media.get("author"),
                published_at=media.get("timestamp") or media.get("published_at"),
                media_urls=[str(image)] if image else [],
                metrics=metrics,
                tags=[category_seed],
                raw={
                    "instagram_media_id": media_id,
                    "discovery_method": "official_api",
                    "evidence_type": "text_metadata",
                    "provisional": False,
                    "freshness_unknown": False,
                    "category_seed": category_seed,
                },
            )
            out.append(item)
        return out

    def _collect_public(self, target: int, *, fallback_reason: str) -> list[RawItem]:
        out: list[RawItem] = []
        tag_url = str(self._public_config().get("tag_url") or "https://www.instagram.com/explore/tags/{tag}/")
        for seed in self._category_seeds():
            if len(out) >= target:
                break
            tags = [seed, *_PRODUCT_HASHTAGS]
            for tag in dict.fromkeys(tags):
                if len(out) >= target:
                    break
                url = tag_url.format(tag=quote(str(tag).replace("#", "")), seed=quote(seed))
                try:
                    result = self.fetcher.fetch(url, selectors=["meta[property='og:title']", "meta[property='og:description']"])
                except Exception:
                    continue
                if not result.ok and not result.content:
                    continue
                item = self._parse_public_social_page(url, result.content, category_seed=seed, fallback_reason=fallback_reason)
                if item:
                    out.append(item)
                    break
        return out[:target]

    def _parse_public_social_page(
        self, url: str, html_text: str, *, category_seed: str, fallback_reason: str
    ) -> RawItem | None:
        item = self._parse_public_page(url, html_text)
        if not item:
            soup = BeautifulSoup(html_text or "", "html.parser")
            text = clean_text(soup.get_text(" ", strip=True), max_len=500)
            if not text:
                return None
            item = RawItem(
                id=stable_id(self.name, url, text),
                source=self.name,
                platform="instagram",
                url=url,
                title=clean_text(text, max_len=110),
                text=text,
            )
        item.id = stable_id(self.name, item.url, item.title)
        item.source = self.name
        item.platform = "instagram"
        item.tags = list(dict.fromkeys([*item.tags, category_seed]))
        item.raw.update(
            {
                "discovery_method": "public_web",
                "evidence_type": "text_metadata",
                "provisional": bool(self._public_config().get("provisional", True)),
                "freshness_unknown": bool(self._public_config().get("freshness_unknown", True)),
                "category_seed": category_seed,
                "fallback_reason": fallback_reason,
            }
        )
        return item

    def _category_seeds(self) -> list[str]:
        seeds = self.config.get("category_seeds") or ["gadgets", "home", "kitchen", "beauty", "pet", "fitness"]
        return [str(seed) for seed in seeds if str(seed).strip()]

    def _official_config(self) -> dict[str, Any]:
        value = self.config.get("official_api")
        return value if isinstance(value, dict) else {}

    def _public_config(self) -> dict[str, Any]:
        value = self.config.get("public_web")
        return value if isinstance(value, dict) else {}
