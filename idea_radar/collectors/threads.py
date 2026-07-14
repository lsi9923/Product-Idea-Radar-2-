from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import RawItem
from ..utils import clean_text, parse_compact_number, short_hash, stable_id
from .base import Collector

_ALLOWED_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
_META_CDN_SUFFIXES = ("fbcdn.net", "cdninstagram.com")
_TELEMETRY_RE = re.compile(r"/(?:ajax/qm|logging|log|falco|error-reporting|collector)(?:/|$)", re.IGNORECASE)
_MUTATION_RE = re.compile(
    r"/(?:i/)?(?:create|compose|composer|direct|dm|messages|settings|privacy|activity|notifications|login|oauth|accounts|follow|like|unlike|reply|replies|comment|comments|save|unsave|share|reshare)(?:/|$)",
    re.IGNORECASE,
)
_PUBLIC_POST_RE = re.compile(r"^/@[A-Za-z0-9_.]+/(?:post|posts)/[A-Za-z0-9_-]+/?$", re.IGNORECASE)
_PUBLIC_ACCOUNT_RE = re.compile(r"^/@[A-Za-z0-9_.]+/?$", re.IGNORECASE)
_PUBLIC_SEARCH_RE = re.compile(r"^/search/?$", re.IGNORECASE)
_PUBLIC_SHORT_POST_RE = re.compile(r"^/t/[A-Za-z0-9_-]+/?$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\])}>'\"]+", re.IGNORECASE)
_PHYSICAL_TERMS = {
    "appliance",
    "bag",
    "bottle",
    "case",
    "charger",
    "cookware",
    "device",
    "fan",
    "feeder",
    "gadget",
    "kit",
    "lamp",
    "maker",
    "organizer",
    "robot",
    "sensor",
    "stand",
    "tool",
    "wearable",
    "가젯",
    "기기",
    "도구",
    "로봇",
    "스마트",
}
_SOFTWARE_TERMS = {"app", "dashboard", "digital", "platform", "saas", "service", "software", "workflow"}
_PROFILE_SKIP_NAMES = {
    "browsermetrics",
    "cache",
    "code cache",
    "crashpad",
    "crash reports",
    "dawncache",
    "downloads",
    "gpucache",
    "grshadercache",
    "media cache",
    "optimization hints",
    "safe browsing",
    "shadercache",
}
_PROFILE_SKIP_PREFIXES = ("lock", "singleton", "crashpad", "debug")
_PROFILE_SKIP_SUFFIXES = (".lock", ".tmp", ".log", "-journal")


@dataclass(slots=True)
class BrowserSafetyTrace:
    events: list[dict[str, Any]] = field(default_factory=list)
    conformant: bool = True

    def record(self, *, url: str, method: str = "GET", action: str = "request") -> dict[str, Any]:
        decision = threads_browser_safety_decision(url, method=method)
        event = {"action": action, **decision}
        if action == "navigate" and event.get("allowed") is not True:
            event["unsafe"] = True
        self.events.append(event)
        if _is_unsafe_trace_event(event):
            self.conformant = False
        return event

    def compact_dict(self) -> dict[str, Any]:
        full = self.to_dict()
        return {
            key: value
            for key, value in full.items()
            if key != "events"
        } | {
            "full_trace_ref": "collector.last_run_diagnostics.browser_safety_trace",
            "full_trace_in_collector_diagnostics": True,
        }

    def to_dict(self) -> dict[str, Any]:
        unsafe_events = [_redact_trace_event(event) for event in self.events if _is_unsafe_trace_event(event)]
        return {
            "conformant": self.conformant,
            "events": [_redact_trace_event(event) for event in self.events],
            "event_count": len(self.events),
            "allowed_count": sum(1 for event in self.events if event.get("allowed") is True),
            "blocked_count": sum(1 for event in self.events if event.get("allowed") is not True),
            "unsafe_count": len(unsafe_events),
            "unsafe_events": unsafe_events,
        }


def _redact_trace_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if not parts.scheme or not parts.netloc:
        return str(url or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def _redact_trace_event(event: dict[str, Any]) -> dict[str, Any]:
    out = {
        key: value
        for key, value in event.items()
        if key in {"action", "allowed", "unsafe", "reason", "surface", "method", "url"}
    }
    if "url" in out:
        out["url"] = _redact_trace_url(str(out["url"]))
    return out


def normalize_threads_public_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower() or "https"
    host = parts.netloc.lower()
    if host == "threads.net":
        host = "www.threads.net"
    elif host == "threads.com":
        host = "www.threads.com"
    path = re.sub(r"/+$", "", parts.path or "/") or "/"
    query_pairs = []
    if _PUBLIC_SEARCH_RE.match(path):
        query_pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=False) if key == "q"]
    query = "&".join(f"{key}={quote_plus(value)}" for key, value in query_pairs)
    return urlunsplit((scheme, host, path, query, ""))


def threads_browser_safety_decision(url: str, *, method: str = "GET") -> dict[str, Any]:
    raw = (url or "").strip()
    parts = urlsplit(raw)
    method_upper = (method or "GET").upper()
    host = parts.netloc.lower()
    path = parts.path or "/"
    normalized = normalize_threads_public_url(raw) if host in _ALLOWED_HOSTS or not host else raw
    allowed = True
    unsafe = False
    reason = "public_threads_read"
    surface = "unknown"

    if parts.scheme not in {"https", "http"}:
        allowed = False
        unsafe = True
        reason = "blocked_non_http_surface"
    elif _is_passive_meta_cdn_host(host):
        surface = "passive_asset"
        reason = "passive_meta_cdn_asset"
        allowed = method_upper in {"GET", "HEAD"}
        unsafe = not allowed
        if not allowed:
            reason = "blocked_non_read_asset_method"
    elif host not in _ALLOWED_HOSTS:
        allowed = False
        unsafe = False
        reason = "blocked_non_threads_host"
    elif _TELEMETRY_RE.search(path):
        allowed = False
        unsafe = False
        reason = "blocked_telemetry"
        surface = "telemetry"
    elif method_upper not in {"GET", "HEAD"}:
        allowed = False
        unsafe = bool(_MUTATION_RE.search(path))
        reason = "blocked_non_read_method" if unsafe else "blocked_background_non_read"
    elif _MUTATION_RE.search(path):
        allowed = False
        unsafe = True
        reason = "blocked_private_or_mutation_surface"
    elif _PUBLIC_ACCOUNT_RE.match(path):
        surface = "account"
    elif _PUBLIC_SEARCH_RE.match(path) and parse_qsl(parts.query):
        surface = "search"
    elif _PUBLIC_POST_RE.match(path) or _PUBLIC_SHORT_POST_RE.match(path):
        surface = "post"
    else:
        allowed = False
        unsafe = False
        reason = "blocked_non_public_threads_surface"

    return {"allowed": allowed, "unsafe": unsafe, "reason": reason, "surface": surface, "method": method_upper, "url": normalized}


def _is_unsafe_trace_event(event: dict[str, Any]) -> bool:
    if "unsafe" in event:
        return event.get("unsafe") is True
    return (
        event.get("method") not in {"GET", "HEAD"}
        or event.get("reason") in {"blocked_non_read_method", "blocked_private_or_mutation_surface"}
        or (event.get("action") == "navigate" and event.get("allowed") is not True)
    )


def is_threads_public_read_url(url: str) -> bool:
    return bool(threads_browser_safety_decision(url).get("allowed"))


class ThreadsCollector(Collector):
    name = "threads"

    def collect(self) -> list[RawItem]:
        target = max(3, self.fetch_limit(multiplier=2))
        if self._fixture_collection_enabled():
            return self._collect_fixture_items(target)
        if self._live_config().get("enabled"):
            return self._collect_live_items(target)
        return []

    def _collect_fixture_items(self, target: int) -> list[RawItem]:
        out: list[RawItem] = []
        for lane in ("account", "keyword", "related_post"):
            for post in self._fixture_posts_for_lane(lane):
                if len(out) >= target:
                    return out
                out.extend(self._items_from_post(post, lane=_normalize_lane(lane), is_live=False, trace=None))
                if len(out) >= target:
                    return out[:target]
        return out[:target]

    def _collect_live_items(self, target: int) -> list[RawItem]:
        trace = BrowserSafetyTrace()
        lane_attempt_counts = {"account": 0, "keyword": 0, "related_post": 0}
        session_metadata: dict[str, Any] = {}
        html_rows = self._acquire_live_public_html(target=target, trace=trace, lane_attempt_counts=lane_attempt_counts, session_metadata=session_metadata)
        out: list[RawItem] = []
        for row in html_rows:
            if len(out) >= target:
                break
            for post in self._posts_from_html(row["url"], row["html"], lane=row["lane"]):
                if len(out) >= target:
                    break
                out.extend(self._items_from_post(post, lane=row["lane"], is_live=True, trace=trace))
        self.last_run_diagnostics = {
            "lane_attempt_counts": lane_attempt_counts,
            "browser_safety_trace": trace.to_dict(),
            "session": session_metadata,
        }
        if not trace.conformant:
            raise RuntimeError("Threads live safety trace recorded a blocked surface")
        return out[:target]

    def _fixture_posts_for_lane(self, lane: str) -> list[dict[str, Any]]:
        fixtures = self.config.get("fixture_items") or self.config.get("fixtures") or []
        return [post for post in fixtures if isinstance(post, dict) and _normalize_lane(post.get("lane") or "account") == lane]

    def _items_from_post(
        self,
        post: dict[str, Any],
        *,
        lane: str,
        is_live: bool,
        trace: BrowserSafetyTrace | None,
    ) -> list[RawItem]:
        url = normalize_threads_public_url(str(post.get("url") or post.get("post_url") or ""))
        safety = threads_browser_safety_decision(url)
        if not safety["allowed"]:
            return []
        if post.get("public_only") is not True or post.get("requires_login"):
            return []
        author = str(post.get("author") or post.get("username") or "").lstrip("@") or None
        text_parts = [post.get("text"), post.get("caption")]
        same_author_replies = []
        for reply in list(post.get("replies") or []) + list(post.get("purchase_followups") or []):
            if not isinstance(reply, dict):
                continue
            reply_author = str(reply.get("author") or reply.get("username") or "").lstrip("@")
            if author and reply_author and reply_author.lower() == author.lower():
                same_author_replies.append(reply)
                text_parts.append(reply.get("text") or reply.get("caption"))
        text = clean_text(" ".join(str(part) for part in text_parts if part), max_len=900)
        product_name = clean_text(post.get("product_name") or post.get("product") or _guess_product_name(text), max_len=120)
        media_urls = [str(value) for value in post.get("media_urls") or post.get("images") or [] if value]
        if not media_urls:
            return []
        product_image = str(post.get("product_image") or post.get("product_image_url") or media_urls[0])
        engagement = _engagement_from(post)
        if not engagement:
            return []
        evidences = self._purchase_evidence_from_post(post, replies=same_author_replies, product_name=product_name)
        if _has_software_signal(text, product_name):
            return []
        physical_signal = _has_physical_signal(text, product_name) or bool(evidences)
        if not physical_signal:
            return []
        out: list[RawItem] = []
        for evidence_index, evidence in enumerate(evidences):
            if not evidence.get("dedupe_key") and not is_live:
                continue
            primary_id = str(evidence["id"])
            row_evidences = [evidence]
            normalized_lane = _normalize_lane(lane)
            affiliate = evidence.get("affiliate") if isinstance(evidence.get("affiliate"), dict) else {}
            safety_conformant = bool(safety["allowed"] and (trace is None or trace.conformant))
            collector_public_read_proof = {
                "platform": "threads",
                "public_only": True,
                "method": "GET",
                "url": url,
                "surface": safety.get("surface"),
                "safety_reason": safety.get("reason"),
                "browser_safety_trace_conformant": True if trace is None else trace.conformant,
            }
            raw = {
                "discovery_lane": normalized_lane,
                "original_post_url": url,
                "threads_post_id": post.get("post_id") or _post_id_from_url(url),
                "discovery_method": "fixture" if not is_live else "public_web_live",
                "evidence_type": "public_social_purchase_evidence",
                "purchase_evidence": row_evidences,
                "primary_purchase_evidence_id": primary_id,
                "dedupe_key": evidence["dedupe_key"] if evidence.get("dedupe_key") else None,
                "engagement": engagement,
                "public_engagement": engagement,
                "affiliate": affiliate,
                "affiliate_disclosure": affiliate.get("disclosed") if "disclosed" in affiliate else affiliate.get("present"),
                "product_name": product_name,
                "product_image": product_image,
                "public_only": True,
                "collector_public_read_proof": collector_public_read_proof,
                "safety_conformant": safety_conformant,
                "source_evidence_policy_passed": True,
                "conformant": False,
                "is_live_real_time": bool(is_live),
                "live_acquisition": bool(is_live),
                "browser_session_used": bool(is_live),
                "browser_session_mode": self._session_mode(self._live_config()) if is_live else "none",
                "fixture": not is_live,
                "physical_signal": physical_signal,
                "provenance": evidence.get("provenance") or {},
                "browser_safety_trace": trace.compact_dict() if trace else {"conformant": True, "event_count": 0, "unsafe_count": 0, "unsafe_events": []},
            }
            title = clean_text(product_name or text, max_len=110)
            out.append(
                RawItem(
                    id=f"{stable_id(self.name, url, title)}:purchase:{evidence_index}:{short_hash(primary_id)}",
                    source="threads_social",
                    platform="threads",
                    url=url,
                    title=title,
                    text=text,
                    author=author,
                    published_at=post.get("published_at") or post.get("timestamp"),
                    media_urls=media_urls,
                    metrics=engagement,
                    tags=["threads", normalized_lane, "physical-product"],
                    raw=raw,
                )
            )
        return out

    def _purchase_evidence_from_post(
        self,
        post: dict[str, Any],
        *,
        replies: list[dict[str, Any]],
        product_name: str,
    ) -> list[dict[str, Any]]:
        raw_evidences: list[Any] = []
        raw_evidences.extend(value for value in post.get("purchase_evidence") or [] if _is_same_author_evidence(value, author=str(post.get("author") or post.get("username") or "")))
        for key in ("purchase_links", "links"):
            for value in post.get(key) or []:
                if isinstance(value, dict) and value.get("same_author") is True:
                    raw_evidences.append(value)
        for reply in replies:
            explicit_reply_evidence = list(reply.get("purchase_evidence") or []) + list(reply.get("purchase_links") or [])
            if not explicit_reply_evidence and reply.get("url") and urlsplit(str(reply.get("url"))).netloc.lower() not in _ALLOWED_HOSTS:
                explicit_reply_evidence.append(reply)
            raw_evidences.extend(explicit_reply_evidence)
            if not explicit_reply_evidence:
                raw_evidences.extend(_unwrap_threads_redirect(link) for link in _URL_RE.findall(str(reply.get("text") or "")))

        evidences: list[dict[str, Any]] = []
        for index, value in enumerate(raw_evidences):
            evidence = _normalize_purchase_evidence(value, index=index, post_url=str(post.get("url") or post.get("post_url") or ""), product_name=product_name, resolver_fixtures=self._resolver_fixtures())
            if evidence:
                evidences.append(evidence)
        return evidences

    def _posts_from_html(self, url: str, html_text: str, *, lane: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html_text or "", "html.parser")
        groups = _thread_groups_from_html(soup)
        structured_posts = [post for group in groups for post in group]
        page_surface = str(threads_browser_safety_decision(url).get("surface") or "")
        wanted_code = _post_id_from_url(url) if page_surface == "post" else ""
        posts: list[dict[str, Any]] = []
        for group in groups:
            root = _public_post_from_thread_group(group, lane=lane, page_url=url, wanted_code=wanted_code, page_text=soup.get_text(" ", strip=True), reply_pool=structured_posts)
            if root:
                posts.append(root)
        if posts:
            return _dedupe_posts(posts)
        if page_surface == "post" and groups:
            return []

        text = clean_text(" ".join(part for part in [self._meta(soup, "og:title"), self._meta(soup, "og:description"), soup.get_text(" ", strip=True)] if part), max_len=900)
        image = self._meta(soup, "og:image")
        links = [_unwrap_threads_redirect(href) for href in (tag.get("href") for tag in soup.find_all("a")) if isinstance(href, str) and href.startswith("http")]
        return [
            {
                "lane": lane,
                "url": url,
                "text": text,
                "caption": text,
                "product_name": _guess_product_name(text),
                "product_image": image,
                "media_urls": [image] if image else [],
                "purchase_links": [link for link in links if urlsplit(link).netloc.lower() not in _ALLOWED_HOSTS],
                "engagement": {},
                "public_only": True,
            }
        ]

    def _discover_post_urls_from_html(self, url: str, html_text: str) -> list[str]:
        soup = BeautifulSoup(html_text or "", "html.parser")
        urls: list[str] = []
        for post in self._posts_from_html(url, html_text, lane="account"):
            post_url = str(post.get("url") or "")
            if threads_browser_safety_decision(post_url).get("surface") == "post":
                urls.append(post_url)
        for href in (tag.get("href") for tag in soup.find_all("a")):
            if not isinstance(href, str):
                continue
            candidate = href if href.startswith("http") else urlunsplit(("https", "www.threads.net", href, "", ""))
            if threads_browser_safety_decision(candidate).get("surface") == "post":
                urls.append(normalize_threads_public_url(candidate))
        return _dedupe_strings(urls)

    def _acquire_live_public_html(
        self,
        *,
        target: int,
        trace: BrowserSafetyTrace,
        lane_attempt_counts: dict[str, int],
        session_metadata: dict[str, Any],
    ) -> list[dict[str, str]]:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required for live Threads acquisition; fixture collection does not import it") from exc

        live = self._live_config()
        browser_config = self._browser_config(live)
        urls: list[tuple[str, str]] = []
        for account in live.get("accounts") or self.config.get("accounts") or self.config.get("account") or []:
            urls.append(("account", f"https://www.threads.net/@{str(account).lstrip('@')}"))
        search_url = str(live.get("search_url") or "https://www.threads.net/search?q={query}")
        for query in live.get("korean_queries") or live.get("queries") or live.get("keyword") or live.get("keywords") or self.config.get("korean_queries") or self.config.get("queries") or self.config.get("keyword") or self.config.get("keywords") or []:
            urls.append(("keyword", search_url.format(query=quote_plus(str(query)))))
        for post_url in live.get("related_post_urls") or live.get("related_posts") or self.config.get("related_post_urls") or self.config.get("related_posts") or self.config.get("related") or []:
            urls.append(("related_post", str(post_url)))

        out: list[dict[str, str]] = []
        acquisition_diagnostics: list[dict[str, Any]] = []
        session_metadata["acquisition_diagnostics"] = acquisition_diagnostics
        session_metadata["isolated_lane_diagnostics"] = acquisition_diagnostics
        per_seed_limit = max(1, int(live.get("per_seed_permalink_limit") or target))
        if browser_config.get("trace_zero_write", True) is not True:
            raise RuntimeError("Threads live browser config requires trace_zero_write=true")
        disposable_root = self._disposable_profile_root(browser_config)
        with tempfile.TemporaryDirectory(prefix="threads-safe-", dir=str(disposable_root) if disposable_root else None) as tmpdir:
            tmp_path = Path(tmpdir)
            session_mode = self._session_mode(live)
            storage_state = self._copied_storage_state(live, tmp_path) if session_mode == "storage_state" else None
            profile_copy = self._copy_browser_profile(live, tmp_path) if session_mode == "copied_profile" else None
            if session_mode == "storage_state" and storage_state is None:
                raise RuntimeError("Threads storage_state mode requires storage_state_file")
            session_metadata.update(
                {
                    "mode": session_mode,
                    "live_profile_used_directly": False,
                    "service_workers": "blocked",
                    "public_only": True,
                    "storage_state_imported": bool(storage_state),
                    "profile_copied": bool(profile_copy),
                    "disposable_data": True,
                    "trace_zero_write": True,
                    "disposable_profile_root": str(disposable_root) if disposable_root else None,
                }
            )
            with sync_playwright() as playwright:
                browser = None
                context = None
                try:
                    launch_kwargs = self._browser_launch_kwargs(browser_config, live)
                    if session_mode == "copied_profile":
                        if profile_copy is None:
                            raise RuntimeError("Threads copied_profile mode requires a copied disposable profile")
                        context = playwright.chromium.launch_persistent_context(
                            str(profile_copy),
                            service_workers="block",
                            permissions=[],
                            **launch_kwargs,
                        )
                    else:
                        browser = playwright.chromium.launch(**launch_kwargs)
                        context = browser.new_context(
                            storage_state=str(storage_state) if storage_state else None,
                            service_workers="block",
                            permissions=[],
                        )
                    context.set_default_timeout(int(live.get("timeout_ms", 15_000)))
                    context.add_init_script(
                        """
                        (() => {
                          const block = event => { event.preventDefault(); event.stopImmediatePropagation(); return false; };
                          window.addEventListener('submit', block, true);
                          window.addEventListener('beforeinput', event => {
                            const target = event.target;
                            if (target && (target.isContentEditable || ['INPUT','TEXTAREA','SELECT'].includes(target.tagName))) block(event);
                          }, true);
                          navigator.serviceWorker && navigator.serviceWorker.getRegistrations && navigator.serviceWorker.getRegistrations().then(rs => rs.forEach(r => r.unregister()));
                        })();
                        """
                    )
                    context.route("**/*", lambda route, request: _guarded_route(route, request, trace))
                    page = context.new_page()
                    seen_pages: set[str] = set()
                    seen_posts: set[str] = set()
                    for lane, raw_url in urls:
                        lane_attempt_counts[lane] = int(lane_attempt_counts.get(lane, 0)) + 1
                        decision = trace.record(url=raw_url, method="GET", action="navigate")
                        if not decision["allowed"]:
                            raise RuntimeError(f"Blocked unsafe Threads URL: {decision['reason']} {decision['url']}")
                        try:
                            page = _open_threads_page_if_needed(context, page)
                            page.goto(decision["url"], wait_until="domcontentloaded")
                            seed_html = page.content()
                        except (PlaywrightTimeoutError, PlaywrightError) as exc:
                            if _is_expected_playwright_acquisition_error(exc, timeout_error_type=PlaywrightTimeoutError, playwright_error_type=PlaywrightError):
                                _record_live_acquisition_diagnostic(acquisition_diagnostics, lane=lane, stage="seed", url=decision["url"], error=exc)
                                continue
                            raise
                        seed_post_urls = self._discover_post_urls_from_html(decision["url"], seed_html)
                        if decision["surface"] == "post":
                            seed_post_urls.insert(0, decision["url"])
                        discovered = _dedupe_strings(seed_post_urls)[:per_seed_limit]
                        for post_url in discovered:
                            if len(out) >= target:
                                break
                            if post_url in seen_pages:
                                continue
                            seen_pages.add(post_url)
                            post_decision = trace.record(url=post_url, method="GET", action="navigate")
                            if not post_decision["allowed"] or post_decision["surface"] != "post":
                                continue
                            try:
                                page = _open_threads_page_if_needed(context, page)
                                page.goto(post_decision["url"], wait_until="domcontentloaded")
                                html = page.content()
                            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                                if _is_expected_playwright_acquisition_error(exc, timeout_error_type=PlaywrightTimeoutError, playwright_error_type=PlaywrightError):
                                    _record_live_acquisition_diagnostic(acquisition_diagnostics, lane=lane, stage="post", url=post_decision["url"], error=exc)
                                    continue
                                raise
                            posts = self._posts_from_html(post_decision["url"], html, lane=lane)
                            root_codes = [str(post.get("code") or post.get("post_id") or "") for post in posts]
                            if root_codes and root_codes[0] in seen_posts:
                                continue
                            if root_codes:
                                seen_posts.add(root_codes[0])
                            out.append({"lane": lane, "url": post_decision["url"], "html": html})
                finally:
                    if context is not None:
                        context.close()
                    if browser is not None:
                        browser.close()
        return out

    def _copied_storage_state(self, live: dict[str, Any], tmp_path: Path) -> Path | None:
        source = self._browser_config(live).get("storage_state_file") or live.get("storage_state")
        if not source:
            return None
        src = Path(str(source)).expanduser()
        if not src.is_file():
            raise RuntimeError(f"Threads storage_state file is not readable: {src}")
        tmp_path.mkdir(parents=True, exist_ok=True)
        dst = tmp_path / "storage-state.json"
        shutil.copyfile(src, dst)
        return dst

    def _copy_browser_profile(self, live: dict[str, Any], tmp_path: Path) -> Path | None:
        source = self._browser_config(live).get("source_profile_dir")
        if not source:
            return None
        src = Path(str(source)).expanduser().resolve()
        dst = (tmp_path / "copied-profile").resolve()
        if not src.is_dir():
            raise RuntimeError(f"Threads source_profile_dir is not readable: {src}")
        if src == dst or src in dst.parents:
            raise RuntimeError("Threads copied_profile source must not be a disposable target")
        tmp_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=self._profile_copy_ignore)
        return dst

    @staticmethod
    def _profile_copy_ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            lowered = name.lower()
            if (
                lowered in _PROFILE_SKIP_NAMES
                or lowered.startswith(_PROFILE_SKIP_PREFIXES)
                or lowered.endswith(_PROFILE_SKIP_SUFFIXES)
                or lowered in {"history", "downloads", "visited links"}
            ):
                ignored.add(name)
        return ignored

    def _session_mode(self, live: dict[str, Any]) -> str:
        browser_config = self._browser_config(live)
        mode = str(browser_config.get("session_mode") or "").strip().lower()
        if not mode:
            if browser_config.get("source_profile_dir"):
                mode = "copied_profile"
            elif browser_config.get("storage_state_file") or live.get("storage_state"):
                mode = "storage_state"
            else:
                mode = "anonymous"
        if mode not in {"anonymous", "storage_state", "copied_profile"}:
            raise RuntimeError(f"Unsupported Threads session_mode: {mode}")
        return mode

    @staticmethod
    def _browser_config(live: dict[str, Any]) -> dict[str, Any]:
        nested = live.get("browser") if isinstance(live.get("browser"), dict) else {}
        out = dict(live)
        out.update(nested)
        aliases = {
            "storage_state": "storage_state_file",
            "storage_state_path": "storage_state_file",
            "profile_dir": "source_profile_dir",
            "user_data_dir": "source_profile_dir",
            "browser_path": "executable_path",
            "browser_executable_path": "executable_path",
            "app_browser_path": "executable_path",
            "app_managed_browser_path": "executable_path",
            "browser_channel": "channel",
            "managed_browser_path": "executable_path",
        }
        for old, new in aliases.items():
            if old in out and new not in out:
                out[new] = out[old]
        return out

    @staticmethod
    def _browser_launch_kwargs(browser_config: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"headless": bool(browser_config.get("headless", live.get("headless", True)))}
        executable_path = browser_config.get("executable_path")
        channel = browser_config.get("channel")
        if executable_path:
            kwargs["executable_path"] = str(executable_path)
        elif channel:
            kwargs["channel"] = str(channel)
        return kwargs
    @staticmethod
    def _disposable_profile_root(browser_config: dict[str, Any]) -> Path | None:
        raw_root = browser_config.get("disposable_profile_root")
        if not raw_root:
            return None
        expanded = os.path.expandvars(str(raw_root)).strip()
        if not expanded:
            return None
        root = Path(expanded).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root


    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return str(tag.get("content", "")) if tag else ""

    def _live_config(self) -> dict[str, Any]:
        value = self.config.get("live")
        return value if isinstance(value, dict) else {}

    def _fixture_collection_enabled(self) -> bool:
        mode = self.config.get("fixture_mode")
        diagnostic_mode = str(self.config.get("diagnostic_mode") or self.config.get("mode") or "").strip().lower()
        return mode is True or diagnostic_mode in {"fixture_only", "fixtures_only", "diagnostic_fixture_only"}


    def _resolver_fixtures(self) -> dict[str, Any]:
        value = self.config.get("resolver_fixtures")
        return value if isinstance(value, dict) else {}

def _thread_groups_from_html(soup: BeautifulSoup) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        groups.extend(_thread_groups_from_json(payload))
    return groups


def _thread_groups_from_json(value: Any) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    if isinstance(value, dict):
        thread_items = value.get("thread_items")
        if isinstance(thread_items, list):
            posts = [_post_dict_from_any(item) for item in thread_items]
            group = [post for post in posts if post]
            if group:
                groups.append(group)
            return groups
        post = _post_dict_from_any(value)
        if post:
            groups.append([post])
            return groups
        for child in value.values():
            groups.extend(_thread_groups_from_json(child))
    elif isinstance(value, list):
        for child in value:
            groups.extend(_thread_groups_from_json(child))
    return groups


def _post_dict_from_any(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if _looks_like_threads_post(value):
        return value
    for key in ("post", "thread", "node", "item"):
        child = value.get(key)
        if isinstance(child, dict):
            found = _post_dict_from_any(child)
            if found:
                return found
    return None


def _looks_like_threads_post(value: dict[str, Any]) -> bool:
    return bool(value.get("pk") and value.get("code") and isinstance(value.get("user"), dict))


def _public_post_from_thread_group(
    group: list[dict[str, Any]],
    *,
    lane: str,
    page_url: str,
    wanted_code: str,
    page_text: str,
    reply_pool: list[dict[str, Any]],
) -> dict[str, Any] | None:
    root = next((post for post in group if str(post.get("code") or "") == wanted_code), None) if wanted_code else None
    if wanted_code and root is None:
        return None
    root = root or (group[0] if group else None)
    if not root:
        return None
    if not wanted_code and _is_reply_post(root):
        return None
    author = _username_from_post(root)
    code = str(root.get("code") or "")
    if not code or not author:
        return None
    text = _text_from_post(root)
    metrics = _metrics_from_structured_post(root, page_text=page_text)
    media_urls = _media_urls_from_post(root)
    replies: list[dict[str, Any]] = []
    reply_product_name = ""
    root_ids = {str(value) for value in (root.get("code"), root.get("pk"), root.get("id")) if value not in (None, "")}
    for reply in reply_pool:
        if reply is root or not _is_same_author_reply_to_root(
            reply,
            root_author=author,
            root_ids=root_ids,
            allow_implicit=any(reply is member for member in group) or bool(wanted_code and code == wanted_code),
        ):
            continue
        reply_text = _text_from_post(reply)
        reply_permalink = _post_permalink(reply, fallback_author=author)
        preview_title = _link_preview_title(reply)
        links = _purchase_links_from_structured_post(reply)
        reply_product_name = reply_product_name or preview_title
        replies.append(
            {
                "author": _username_from_post(reply),
                "text": reply_text,
                "caption": reply_text,
                "url": reply_permalink,
                "purchase_links": [
                    {
                        "url": link,
                        "kind": "same_author_affiliate_followup",
                        "affiliate": {"same_author": True, "present": True},
                        "product_name": preview_title,
                        "provenance": {
                            "source": "threads_public_ssr",
                            "author": _username_from_post(reply),
                            "post_url": reply_permalink,
                            "root_post_url": _post_permalink(root, fallback_author=author),
                            "reply_post_id": str(reply.get("pk") or ""),
                        },
                    }
                    for link in links
                    if urlsplit(link).netloc.lower() not in _ALLOWED_HOSTS
                ],
            }
        )
    post_url = _post_permalink(root, fallback_author=author) or normalize_threads_public_url(page_url)
    return {
        "lane": lane,
        "url": post_url,
        "post_url": post_url,
        "post_id": code,
        "code": code,
        "author": author,
        "username": author,
        "text": text,
        "caption": text,
        "published_at": _timestamp_from_post(root),
        "media_urls": media_urls,
        "product_image": media_urls[0] if media_urls else "",
        "product_name": reply_product_name or _guess_product_name(text),
        "engagement": metrics,
        "replies": replies,
        "public_only": True,
    }


def _username_from_post(post: dict[str, Any]) -> str:
    user = post.get("user") if isinstance(post.get("user"), dict) else {}
    return str(user.get("username") or user.get("pk") or "").lstrip("@")


def _text_from_post(post: dict[str, Any]) -> str:
    caption = post.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    text_info = post.get("text_post_app_info") if isinstance(post.get("text_post_app_info"), dict) else {}
    fragments = [caption, post.get("text"), text_info.get("text")]
    for key in ("text_fragments", "fragments", "text_entities"):
        fragments.extend(_text_fragments_from_any(text_info.get(key)))
    return clean_text(" ".join(str(value) for value in fragments if value), max_len=900)


def _text_fragments_from_any(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("text", "plaintext", "value", "string"):
            raw = value.get(key)
            if isinstance(raw, str):
                out.append(raw)
        for key in ("fragments", "children", "ranges"):
            out.extend(_text_fragments_from_any(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for child in value:
            out.extend(_text_fragments_from_any(child))
        return out
    return []


def _is_reply_post(post: dict[str, Any]) -> bool:
    text_info = post.get("text_post_app_info") if isinstance(post.get("text_post_app_info"), dict) else {}
    return text_info.get("is_reply") is True

def _is_same_author_reply_to_root(
    post: dict[str, Any],
    *,
    root_author: str,
    root_ids: set[str],
    allow_implicit: bool = False,
) -> bool:
    author = _username_from_post(post)
    text_info = post.get("text_post_app_info") if isinstance(post.get("text_post_app_info"), dict) else {}
    root_post_author = text_info.get("root_post_author") if isinstance(text_info.get("root_post_author"), dict) else {}
    root_username = str(root_post_author.get("username") or "").lstrip("@")
    if not (
        author
        and root_author
        and author.lower() == root_author.lower()
        and text_info.get("is_reply") is True
        and root_username.lower() == root_author.lower()
    ):
        return False
    explicit_root_ids = {
        str(value)
        for value in (
            text_info.get("root_post_id"),
            text_info.get("root_post_pk"),
            text_info.get("root_post_code"),
            text_info.get("reply_to_post_id"),
            text_info.get("reply_to_post_pk"),
            text_info.get("reply_to_post_code"),
            post.get("root_post_id"),
            post.get("root_post_pk"),
            post.get("root_post_code"),
        )
        if value not in (None, "")
    }
    if explicit_root_ids:
        return bool(explicit_root_ids.intersection(root_ids))
    return allow_implicit


def _link_preview_attachment(post: dict[str, Any]) -> dict[str, Any]:
    text_info = post.get("text_post_app_info") if isinstance(post.get("text_post_app_info"), dict) else {}
    for value in (post.get("link_preview_attachment"), text_info.get("link_preview_attachment")):
        if isinstance(value, dict):
            return value
    return {}


def _link_preview_title(post: dict[str, Any]) -> str:
    preview = _link_preview_attachment(post)
    return clean_text(str(preview.get("title") or ""), max_len=120)


def _purchase_links_from_structured_post(post: dict[str, Any]) -> list[str]:
    links = [_unwrap_threads_redirect(link) for link in _URL_RE.findall(_text_from_post(post))]
    preview = _link_preview_attachment(post)
    for key in ("url", "href", "link_url", "redirect_url", "original_url", "destination_url"):
        value = preview.get(key)
        if isinstance(value, str) and value.startswith("http"):
            links.append(_unwrap_threads_redirect(value))
    return _dedupe_strings(links)


def _timestamp_from_post(post: dict[str, Any]) -> str:
    for key in ("taken_at", "taken_at_timestamp", "created_at", "timestamp"):
        value = post.get(key)
        if value:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
            if isinstance(value, str) and value.isdigit():
                return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
            return str(value)
    return ""


def _post_permalink(post: dict[str, Any], *, fallback_author: str) -> str:
    code = str(post.get("code") or "")
    author = _username_from_post(post) or fallback_author
    return normalize_threads_public_url(f"https://www.threads.net/@{author}/post/{code}") if code and author else ""


def _media_urls_from_post(post: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for media in [post, *(post.get("carousel_media") if isinstance(post.get("carousel_media"), list) else [])]:
        if not isinstance(media, dict):
            continue
        image_versions = media.get("image_versions2") if isinstance(media.get("image_versions2"), dict) else {}
        for candidate in image_versions.get("candidates") or []:
            if isinstance(candidate, dict) and candidate.get("url"):
                urls.append(str(candidate["url"]))
                break
    return _dedupe_strings(urls)


def _metrics_from_structured_post(post: dict[str, Any], *, page_text: str) -> dict[str, float]:
    text_info = post.get("text_post_app_info") if isinstance(post.get("text_post_app_info"), dict) else {}
    candidates = [post, text_info, text_info.get("share_info") if isinstance(text_info.get("share_info"), dict) else {}]
    aliases = {
        "views": ("view_count", "views", "impression_count", "public_view_count", "play_count"),
        "likes": ("like_count", "likes"),
        "replies": ("direct_reply_count", "reply_count", "replies"),
        "reposts": ("repost_count", "reshare_count", "reposts"),
    }
    metrics: dict[str, float] = {}
    for canonical, keys in aliases.items():
        for source in candidates:
            if not isinstance(source, dict):
                continue
            for key in keys:
                value = source.get(key)
                if key in source and value not in (None, ""):
                    metrics[canonical] = parse_compact_number(value)
                    break
            if canonical in metrics:
                break
    if "views" not in metrics:
        match = re.search(r"\b([0-9][0-9.,KMkm]*)\s+views\b", page_text or "", re.IGNORECASE)
        if match:
            metrics["views"] = parse_compact_number(match.group(1))
    return metrics if set(metrics) == {"views", "likes", "replies", "reposts"} else {}


def _unwrap_threads_redirect(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if parts.netloc.lower() == "l.threads.com":
        for key, value in parse_qsl(parts.query, keep_blank_values=False):
            if key == "u" and value:
                return unquote(value)
    return url


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out

def _is_passive_meta_cdn_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].strip(".").lower()
    if not normalized:
        return False
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in _META_CDN_SUFFIXES)


def _dedupe_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for post in posts:
        key = str(post.get("code") or post.get("post_id") or post.get("url") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(post)
    return out

def _record_live_acquisition_diagnostic(
    diagnostics: list[dict[str, Any]],
    *,
    lane: str,
    stage: str,
    url: str,
    error: BaseException,
) -> None:
    diagnostics.append(
        {
            "lane": _normalize_lane(lane),
            "stage": stage,
            "url": _redact_trace_url(url),
            "error_type": type(error).__name__,
            "error": clean_text(str(error).splitlines()[0] if str(error) else type(error).__name__, max_len=180),
            "isolated": True,
        }
    )


def _is_expected_playwright_acquisition_error(
    error: BaseException,
    *,
    timeout_error_type: type[BaseException],
    playwright_error_type: type[BaseException],
) -> bool:
    if isinstance(error, timeout_error_type):
        return True
    if not isinstance(error, playwright_error_type):
        return False
    message = str(error).lower()
    return "closed" in message and ("page" in message or "context" in message or "browser" in message or "target" in message)


def _open_threads_page_if_needed(context: Any, page: Any) -> Any:
    try:
        if page is not None and not page.is_closed():
            return page
    except AttributeError:
        return page
    return context.new_page()

def _guarded_route(route: Any, request: Any, trace: BrowserSafetyTrace) -> None:
    decision = trace.record(url=request.url, method=request.method, action="request")
    resource_type = getattr(request, "resource_type", "")
    if not decision["allowed"] or resource_type in {"websocket", "eventsource"}:
        route.abort()
        return
    route.continue_()


def _engagement_from(post: dict[str, Any]) -> dict[str, float]:
    source = (
        post.get("engagement")
        if isinstance(post.get("engagement"), dict)
        else post.get("metrics")
        if isinstance(post.get("metrics"), dict)
        else post.get("public_engagement")
        if isinstance(post.get("public_engagement"), dict)
        else post
    )
    aliases = {
        "views": ("views", "view_count"),
        "likes": ("likes", "like_count"),
        "replies": ("replies", "reply_count"),
        "reposts": ("reposts", "repost_count"),
    }
    metrics: dict[str, float] = {}
    for canonical, keys in aliases.items():
        for key in keys:
            if key in source:
                metrics[canonical] = parse_compact_number(source.get(key))
                break
    if set(metrics) != set(aliases):
        return {}
    return metrics


def _normalize_purchase_evidence(
    value: Any,
    *,
    index: int,
    post_url: str,
    product_name: str,
    resolver_fixtures: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(value, str):
        data: dict[str, Any] = {"url": value, "kind": "purchase_url"}
    elif isinstance(value, dict):
        data = dict(value)
    else:
        return None
    raw_value = clean_text(_unwrap_threads_redirect(str(data.get("url") or data.get("value") or data.get("raw_url") or "")))
    if not raw_value:
        return None
    resolver = data.get("resolver") if isinstance(data.get("resolver"), dict) else {}
    fixture_resolution = resolver_fixtures.get(raw_value)
    if isinstance(fixture_resolution, str):
        resolver = {"status": "resolved", "final_url": fixture_resolution, "fixture": True}
        data["final_url"] = fixture_resolution
    elif isinstance(fixture_resolution, dict):
        status = str(fixture_resolution.get("status") or "").lower()
        if status in {"timeout", "error", "failed", "unresolved"}:
            return None
        resolver = dict(fixture_resolution)
        if fixture_resolution.get("final_url"):
            data["final_url"] = fixture_resolution["final_url"]
    final_url = clean_text(_unwrap_threads_redirect(str(data.get("final_url") or data.get("resolved_url") or resolver.get("final_url") or "")))
    normalized_final = final_url
    kind = str(data.get("kind") or ("purchase_url" if raw_value.startswith("http") else "purchase_value"))
    evidence_id = str(
        data.get("id")
        or stable_id(
            "threads_purchase",
            f"{post_url.rstrip('/')}/purchase/{index}/{short_hash(raw_value)}",
        )
    )
    affiliate = data.get("affiliate") if isinstance(data.get("affiliate"), dict) else {}
    if not resolver:
        resolver = {"status": "fixture_final_url" if normalized_final else "not_attempted"}
    if normalized_final:
        resolver.setdefault("final_url", normalized_final)
    provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    provenance = {"platform": "threads", "post_url": normalize_threads_public_url(post_url), **provenance}
    evidence_product_name = clean_text(str(data.get("product_name") or product_name), max_len=120)
    return {
        "id": evidence_id,
        "kind": kind,
        "raw_url": raw_value if raw_value.startswith("http") else "",
        "raw_value": raw_value,
        "provenance": provenance,
        "affiliate": affiliate,
        "resolver": resolver,
        "dedupe_key": f"final_url:{normalized_final}" if normalized_final else None,
        "product_name": evidence_product_name,
    }


def _post_id_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _guess_product_name(text: str) -> str:
    cleaned = clean_text(text, max_len=140)
    if not cleaned:
        return ""
    for sep in (" — ", " - ", ":", "|", "\n"):
        if sep in cleaned:
            candidate = clean_text(cleaned.split(sep, 1)[0], max_len=80)
            if len(candidate) >= 4:
                return candidate
    return clean_text(cleaned, max_len=80)



def _is_same_author_evidence(value: Any, *, author: str) -> bool:
    if not isinstance(value, dict):
        return False
    affiliate = value.get("affiliate") if isinstance(value.get("affiliate"), dict) else {}
    if affiliate.get("same_author") is True or value.get("same_author") is True:
        return True
    provenance = value.get("provenance") if isinstance(value.get("provenance"), dict) else {}
    evidence_author = str(provenance.get("author") or value.get("author") or "").lstrip("@")
    return bool(author and evidence_author and evidence_author.lower() == author.lstrip("@").lower())

def _normalize_lane(value: Any) -> str:
    lane = str(value or "").strip().lower()
    if lane in {"accounts", "account"}:
        return "account"
    if lane in {"queries", "query", "keyword", "keywords"}:
        return "keyword"
    if lane in {"related_posts", "related_post", "related"}:
        return "related_post"
    return lane or "account"

def _has_physical_signal(text: str, product_name: str) -> bool:
    lowered = f"{text} {product_name}".lower()
    return any(term in lowered for term in _PHYSICAL_TERMS)


def _has_software_signal(text: str, product_name: str) -> bool:
    lowered = f"{text} {product_name}".lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in _SOFTWARE_TERMS)
