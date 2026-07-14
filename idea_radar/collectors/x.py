from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from ..models import RawItem
from ..utils import canonical_url, clean_text, parse_compact_number, stable_id
from .base import Collector

_STATUS_RE = re.compile(r"/(?P<handle>[A-Za-z0-9_]{1,32})/status(?:es)?/(?P<id>\d+)")
_TWITTER_EPOCH_MS = 1_288_834_974_657
_PRODUCT_QUERY = "product OR launch OR gadget OR tool OR device OR accessory"

class XOfficialApiExternalError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics



class XCollector(Collector):
    name = "x"

    def collect(self) -> list[RawItem]:
        handles = self.config.get("handles", [])
        target = self.fetch_limit(multiplier=2)
        per_handle = max(1, min(target, int(self.config.get("limit_per_handle", self.limit))))
        out: list[RawItem] = []
        for handle in handles:
            if len(out) >= target:
                break
            remaining = target - len(out)
            out.extend(self._collect_handle(str(handle).lstrip("@"), min(per_handle, remaining)))
        return out[:target]

    def _collect_handle(self, handle: str, limit: int) -> list[RawItem]:
        profile_url = f"https://x.com/{handle}"
        profile = self.fetcher.fetch(profile_url)
        if not profile.ok:
            return []
        ids = self._status_ids(profile.content, handle=handle)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._max_age_days())
        items: list[RawItem] = []
        for tweet_id in ids:
            if len(items) >= limit:
                break
            created_at = self._tweet_created_at(tweet_id)
            if created_at is not None and created_at < cutoff:
                continue
            url = f"https://x.com/{handle}/status/{tweet_id}"
            result = self.fetcher.fetch(url)
            item = self._parse_tweet_result(result.content, url=url, handle=handle, tweet_id=tweet_id)
            if item:
                items.append(item)
        return items

    @staticmethod
    def _status_ids(html_text: str, *, handle: str | None = None) -> list[str]:
        wanted = handle.lower() if handle else None
        seen: set[str] = set()
        ids: list[str] = []
        for match in _STATUS_RE.finditer(html_text or ""):
            matched_handle = match.group("handle").lower()
            if wanted and matched_handle != wanted:
                continue
            tweet_id = match.group("id")
            if tweet_id not in seen:
                seen.add(tweet_id)
                ids.append(tweet_id)
        return ids

    @staticmethod
    def _tweet_created_at(tweet_id: str) -> datetime | None:
        try:
            timestamp_ms = (int(tweet_id) >> 22) + _TWITTER_EPOCH_MS
        except ValueError:
            return None
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

    def _max_age_days(self) -> int:
        try:
            return max(1, int(self.config.get("max_age_days", 30)))
        except (TypeError, ValueError):
            return 30

    def _parse_tweet_result(self, content: str, *, url: str, handle: str, tweet_id: str) -> RawItem | None:
        data: dict[str, Any] = {}
        try:
            loaded = json.loads(content)
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}

        text = clean_text(data.get("text") if data else "")
        media_urls: list[str] = []
        metrics: dict[str, float] = {}
        author = handle
        published = None

        if data:
            user = data.get("user") or {}
            if isinstance(user, dict):
                screen_name = str(user.get("screen_name") or "").lstrip("@")
                if screen_name and screen_name.lower() != handle.lower():
                    return None
                author = screen_name or user.get("name") or handle
            published = data.get("created_at") or data.get("createdAt")
            for key in ("favorite_count", "retweet_count", "reply_count", "quote_count", "view_count"):
                if key in data:
                    metrics[key] = parse_compact_number(data.get(key))
            for media in data.get("mediaDetails") or data.get("media_details") or []:
                if isinstance(media, dict):
                    media_url = media.get("media_url_https") or media.get("media_url") or media.get("url")
                    if media_url:
                        media_urls.append(media_url)
        else:
            soup = BeautifulSoup(content or "", "html.parser")
            text = clean_text(soup.get_text(" ", strip=True), max_len=500)

        if not text:
            return None
        title = clean_text(text, max_len=110)
        item = RawItem(
            id=stable_id(self.name, url, title),
            source=self.name,
            platform="x",
            url=url,
            title=title,
            text=text,
            author=author,
            published_at=published,
            media_urls=media_urls,
            metrics=metrics,
            tags=[f"@{handle}"],
            raw={"tweet_id": tweet_id},
        )
        _normalize_social_purchase_fields(item, text=text, source_url=url, engagement=metrics)
        return item


class XSocialCollector(XCollector):
    name = "x_social"

    def collect(self) -> list[RawItem]:
        target = self.fetch_limit(multiplier=2)
        if self._has_official_credentials():
            try:
                items = self._collect_official(target)
            except XOfficialApiExternalError as exc:
                self.last_run_diagnostics = {"x_official_api": exc.diagnostics}
                return self._collect_public(target, fallback_reason="official_api_external_failure")[:target]
            if items:
                return items[:target]
            self.last_run_diagnostics = {"x_official_api": {"status": "empty"}}
            return self._collect_public(target, fallback_reason="official_api_empty")[:target]
        return self._collect_public(target, fallback_reason="missing_official_credentials")[:target]

    def _has_official_credentials(self) -> bool:
        return bool(os.environ.get("X_BEARER_TOKEN") or isinstance(self._official_config().get("fixture_items"), list))

    def _collect_official(self, target: int) -> list[RawItem]:
        fixture_items = self._official_config().get("fixture_items")
        if isinstance(fixture_items, list):
            return self._parse_official_payload({"data": fixture_items}, target=target, category_seed="fixture")

        bearer = os.environ.get("X_BEARER_TOKEN", "")
        endpoint = str(self._official_config().get("endpoint") or "https://api.x.com/2/tweets/search/recent")
        out: list[RawItem] = []
        for seed in self._category_seeds():
            if len(out) >= target:
                break
            params = urlencode(
                {
                    "query": f"({seed}) ({_PRODUCT_QUERY}) -is:retweet lang:en",
                    "max_results": str(max(10, min(100, target - len(out)))),
                    "tweet.fields": "created_at,public_metrics,entities,attachments",
                    "expansions": "author_id",
                    "user.fields": "username,name",
                }
            )
            request = Request(f"{endpoint}?{params}", headers={"Authorization": f"Bearer {bearer}"})
            payload = self._read_official_payload(request)
            out.extend(self._parse_official_payload(payload, target=target - len(out), category_seed=seed))
        return out[:target]

    @staticmethod
    def _read_official_payload(request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            diagnostics = {
                "status": "http_error",
                "http_status": exc.code,
                "reason": exc.reason,
                "url": getattr(exc, "url", request.full_url),
            }
            if exc.code in {401, 403}:
                raise RuntimeError(f"X official API authentication failed with HTTP {exc.code}: {exc.reason}") from exc
            if 400 <= exc.code < 500 and exc.code not in {408, 409, 425, 429}:
                raise RuntimeError(f"X official API request/configuration failed with HTTP {exc.code}: {exc.reason}") from exc
            raise XOfficialApiExternalError("X official API external HTTP failure", diagnostics=diagnostics) from exc
        except (TimeoutError, URLError) as exc:
            diagnostics = {
                "status": "network_error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "url": request.full_url,
            }
            raise XOfficialApiExternalError("X official API network failure", diagnostics=diagnostics) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("X official API returned non-object JSON payload")
        return payload

    def _parse_official_payload(self, payload: dict[str, Any], *, target: int, category_seed: str) -> list[RawItem]:
        users = {
            str(user.get("id")): user
            for user in (payload.get("includes", {}).get("users", []) if isinstance(payload.get("includes"), dict) else [])
            if isinstance(user, dict)
        }
        out: list[RawItem] = []
        for tweet in payload.get("data") or []:
            if len(out) >= target or not isinstance(tweet, dict):
                break
            text = clean_text(tweet.get("text"), max_len=500)
            tweet_id = str(tweet.get("id") or "")
            if not text or not tweet_id:
                continue
            user = users.get(str(tweet.get("author_id")), {})
            username = str(user.get("username") or tweet.get("username") or tweet.get("author") or "").lstrip("@")
            url = str(tweet.get("url") or f"https://x.com/{username or 'i'}/status/{tweet_id}")
            metrics = {
                key: parse_compact_number(value)
                for key, value in (tweet.get("public_metrics") or tweet.get("metrics") or {}).items()
            }
            item = RawItem(
                id=stable_id(self.name, url, text),
                source=self.name,
                platform="x",
                url=url,
                title=clean_text(text, max_len=110),
                text=text,
                author=username or None,
                published_at=tweet.get("created_at") or tweet.get("createdAt"),
                metrics=metrics,
                tags=[category_seed],
                raw={
                    "tweet_id": tweet_id,
                    "discovery_method": "official_api",
                    "evidence_type": "text_metadata",
                    "provisional": False,
                    "freshness_unknown": False,
                    "category_seed": category_seed,
                },
            )
            _normalize_social_purchase_fields(item, text=text, source_url=url, engagement=metrics)
            out.append(item)
        return out

    def _collect_public(self, target: int, *, fallback_reason: str) -> list[RawItem]:
        out: list[RawItem] = []
        search_url = str(self._public_config().get("search_url") or "https://x.com/search?q={query}&f=live")
        for seed in self._category_seeds():
            if len(out) >= target:
                break
            query = quote_plus(f"{seed} product")
            url = search_url.format(query=query, seed=quote_plus(seed))
            try:
                result = self.fetcher.fetch(url)
            except Exception:
                continue
            if not result.ok and not result.content:
                continue
            ids = self._status_ids(result.content)
            if ids:
                for handle, tweet_id in self._status_matches(result.content):
                    if len(out) >= target:
                        break
                    item = self._public_tweet_item(handle=handle, tweet_id=tweet_id, category_seed=seed, fallback_reason=fallback_reason)
                    if item:
                        out.append(item)
            elif len(out) < target:
                item = self._public_search_item(url, result.content, category_seed=seed, fallback_reason=fallback_reason)
                if item:
                    out.append(item)
        return out[:target]

    def _public_tweet_item(
        self, *, handle: str, tweet_id: str, category_seed: str, fallback_reason: str
    ) -> RawItem | None:
        url = f"https://x.com/{handle}/status/{tweet_id}"
        try:
            result = self.fetcher.fetch(url)
        except Exception:
            return None
        item = self._parse_tweet_result(result.content, url=url, handle=handle, tweet_id=tweet_id)
        if not item:
            return None
        item.id = stable_id(self.name, item.url, item.title)
        item.source = self.name
        item.platform = "x"
        item.tags = list(dict.fromkeys([*item.tags, category_seed]))
        item.raw.update(self._public_raw(category_seed=category_seed, fallback_reason=fallback_reason))
        _normalize_social_purchase_fields(item, text=item.text, source_url=item.url, engagement=item.metrics)
        return item

    def _public_search_item(self, url: str, content: str, *, category_seed: str, fallback_reason: str) -> RawItem | None:
        soup = BeautifulSoup(content or "", "html.parser")
        text = clean_text(soup.get_text(" ", strip=True), max_len=500)
        if not text:
            return None
        title = clean_text(text, max_len=110)
        item = RawItem(
            id=stable_id(self.name, url, title),
            source=self.name,
            platform="x",
            url=url,
            title=title,
            text=text,
            tags=[category_seed],
            raw=self._public_raw(category_seed=category_seed, fallback_reason=fallback_reason),
        )
        _normalize_social_purchase_fields(item, text=text, source_url=url, engagement={})
        return item

    @staticmethod
    def _status_matches(html_text: str) -> list[tuple[str, str]]:
        seen: set[str] = set()
        matches: list[tuple[str, str]] = []
        for match in _STATUS_RE.finditer(html_text or ""):
            tweet_id = match.group("id")
            if tweet_id in seen:
                continue
            seen.add(tweet_id)
            matches.append((match.group("handle"), tweet_id))
        return matches

    def _public_raw(self, *, category_seed: str, fallback_reason: str) -> dict[str, Any]:
        return {
            "discovery_method": "public_web",
            "evidence_type": "text_metadata",
            "provisional": bool(self._public_config().get("provisional", True)),
            "freshness_unknown": bool(self._public_config().get("freshness_unknown", True)),
            "category_seed": category_seed,
            "fallback_reason": fallback_reason,
        }

    def _category_seeds(self) -> list[str]:
        seeds = self.config.get("category_seeds") or ["gadgets", "home", "kitchen", "beauty", "pet", "fitness"]
        return [str(seed) for seed in seeds if str(seed).strip()]

    def _official_config(self) -> dict[str, Any]:
        value = self.config.get("official_api")
        return value if isinstance(value, dict) else {}

    def _public_config(self) -> dict[str, Any]:
        value = self.config.get("public_web")
        return value if isinstance(value, dict) else {}
_X_URL_RE = re.compile(r"https?://[^\s\])}>'\"]+", re.IGNORECASE)


def _normalize_social_purchase_fields(
    item: RawItem,
    *,
    text: str,
    source_url: str,
    engagement: dict[str, float],
) -> None:
    urls = _candidate_purchase_urls(text)
    if not urls and not engagement:
        return
    item.raw.setdefault("public_only", True)
    item.raw.setdefault("is_live_real_time", bool(item.raw.get("discovery_method") == "official_api"))
    if engagement:
        item.raw["engagement"] = dict(engagement)
    evidences = [_x_purchase_evidence(url, index=index, source_url=source_url) for index, url in enumerate(urls)]
    evidences = [evidence for evidence in evidences if evidence]
    if evidences:
        item.raw["purchase_evidence"] = evidences
        item.raw["primary_purchase_evidence_id"] = evidences[0]["id"]
        item.raw["affiliate"] = evidences[0].get("affiliate") or {}
        item.raw["conformant"] = all(bool(evidence.get("dedupe_key", "").startswith("final_url:")) for evidence in evidences)


def _candidate_purchase_urls(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _X_URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?")
        host = urlsplit(url).netloc.lower()
        if not host or host.endswith("x.com") or host.endswith("twitter.com"):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _x_purchase_evidence(url: str, *, index: int, source_url: str) -> dict[str, Any]:
    final_url = canonical_url(url)
    evidence_id = stable_id("x_purchase", f"{source_url}#{index}", url)
    affiliate = {"present": _looks_affiliate_url(url)}
    return {
        "id": evidence_id,
        "kind": "purchase_url",
        "raw_url": url,
        "raw_value": url,
        "provenance": {"platform": "x", "post_url": source_url},
        "affiliate": affiliate,
        "resolver": {"status": "source_value_unresolved", "final_url": final_url},
        "dedupe_key": f"final_url:{final_url}",
    }


def _looks_affiliate_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("affiliate", "aff", "ref=", "utm_", "tag=", "coupon"))
