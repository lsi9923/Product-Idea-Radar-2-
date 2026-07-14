from __future__ import annotations

import csv
import json
import sys
import types
import tempfile
import unittest
from pathlib import Path

from idea_radar.dedupe import dedupe_ideas
from idea_radar.digest import write_outputs
from idea_radar.gui import DEFAULT_ON_SOURCES, SOURCE_LABELS, _evidence_lines, _latest_verified_ideas, load_ideas_from_json
from idea_radar.models import RawItem, ScoredIdea


FINAL_URL = "https://www.coupang.com/vp/products/123456789?itemId=987654321"
RAW_AFFILIATE_URL = "https://link.coupang.com/a/moelpick-fixture"
POST_URL = "https://www.threads.net/@moel.pick/post/Cfixture"
IMAGE_URL = "https://scontent.fixture.test/moel-pick-fan.jpg"
THREADS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "threads_moel_pick_DarhfjSE24O.json"
THREADS_LIVE_FIXTURE = json.loads(THREADS_FIXTURE_PATH.read_text(encoding="utf-8"))
LIVE_ORIGINAL_POST_URL = THREADS_LIVE_FIXTURE["captured_source"]["original_url"]
LIVE_POST_URL = THREADS_LIVE_FIXTURE["captured_source"]["normalized_public_url"]
LIVE_ROOT = THREADS_LIVE_FIXTURE["root"]
LIVE_REPLY = THREADS_LIVE_FIXTURE["same_author_reply"]
LIVE_EVIDENCE = THREADS_LIVE_FIXTURE["evidence"]
LIVE_RESOLVER_MAPPING = THREADS_LIVE_FIXTURE["resolver_mapping"]
LIVE_MEDIA_URLS = LIVE_ROOT["media_urls"]
LIVE_IMAGE_URL = LIVE_MEDIA_URLS[0]
LIVE_RAW_AFFILIATE_URL = LIVE_EVIDENCE["raw_purchase_url"]
LIVE_FINAL_URL = THREADS_LIVE_FIXTURE["resolved_purchase"]["final_url"]
LIVE_DEDUPE_KEY = THREADS_LIVE_FIXTURE["resolved_purchase"]["dedupe_key"]
LIVE_PRODUCT_TITLE = LIVE_ROOT["product_name"]
LIVE_METRICS = LIVE_ROOT["metrics"]
LIVE_TIMESTAMP = LIVE_ROOT["timestamp"]
LIVE_AFFILIATE_DISCLOSURE = LIVE_EVIDENCE["affiliate_disclosure_text"]


def evidence(final_url: str = FINAL_URL, *, evidence_id: str = "pe_threads_moel_pick_coupang_1") -> dict[str, object]:
    return {
        "id": evidence_id,
        "kind": "same_author_affiliate_followup",
        "raw_url": RAW_AFFILIATE_URL,
        "raw_value": None,
        "provenance": {
            "source": "threads_fixture",
            "author": "moel.pick",
            "public_only": True,
            "fixture_note": "deterministic test fixture; not live-captured evidence",
        },
        "affiliate": {"network": "coupang", "disclosed": True, "same_author": True},
        "resolver": {"status": "resolved", "final_url": final_url, "fixture": True},
        "dedupe_key": f"final_url:{final_url}",
    }


def raw_threads_item(
    title: str = "MoelPick ChillPod desk fan",
    *,
    published_at: str = "2026-07-10T09:00:00+00:00",
    final_url: str = FINAL_URL,
    score: float = 6.8,
) -> RawItem:
    return RawItem(
        id=f"threads:{published_at}",
        source="threads_social",
        platform="threads",
        url=POST_URL,
        title=title,
        text="moel.pick demonstrates a physical portable desk cooling fan and links the same-author Coupang affiliate follow-up.",
        author="moel.pick",
        published_at=published_at,
        media_urls=[IMAGE_URL],
        metrics={"views": 8100, "likes": 47, "replies": 8, "reposts": 3},
        raw={
            "product_name": title,
            "product_image_url": IMAGE_URL,
            "original_post_url": POST_URL,
            "physical_signal": {"kind": "media_and_text", "value": "portable desk cooling fan"},
            "public_engagement": {"views": 8100, "likes": 47, "replies": 8, "reposts": 3},
            "purchase_evidence": [evidence(final_url)],
            "primary_purchase_evidence_id": "pe_threads_moel_pick_coupang_1",
            "public_only": True,
            "discovery_lane": "account",
            "discovery_method": "public_web_fixture",
            "affiliate_disclosure": "same-author Coupang affiliate follow-up",
            "conformant": True,
            "is_live_real_time": False,
            "dedupe_key": f"final_url:{final_url}",
            "fixture_score": score,
        },
    )


def scored(item: RawItem, *, score: float = 6.8, accepted: bool = True) -> ScoredIdea:
    return ScoredIdea(
        item=item,
        is_product_idea=accepted,
        category="gadgets",
        summary_ko=f"fixture summary for {item.title}",
        novelty_score=score,
        virality_score=score,
        market_score=score,
        total_score=score,
        duplicate_key=str(item.raw.get("dedupe_key", item.id)),
        reason="fixture score",
        matched_terms=["fan", "coupang"],
    )


class DummyFetcher:
    def fetch(self, url: str):  # pragma: no cover - Threads fixtures must not use live fetches in these tests.
        raise AssertionError(f"unexpected live fetch: {url}")


class DeterministicRedirectOpener:
    def __call__(self, request, timeout):
        class Response:
            code = 200
            status = 200
            headers = {}

            def geturl(self) -> str:
                return LIVE_FINAL_URL

        self.request_url = request.full_url
        return Response()
def strict_live_threads_item(
    title: str = LIVE_PRODUCT_TITLE,
    *,
    published_at: str = LIVE_TIMESTAMP,
    final_url: str = LIVE_FINAL_URL,
    url: str = LIVE_POST_URL,
    views: int = 8100,
    likes: int = 47,
    replies: int = 8,
    reposts: int = 3,
    score: float = 6.8,
) -> RawItem:
    from idea_radar.evidence import clean_final_url, resolve_item_purchase_evidence

    item = raw_threads_item(title=title, published_at=published_at, final_url=final_url, score=score)
    item.url = url
    item.metrics = {"views": views, "likes": likes, "replies": replies, "reposts": reposts}
    raw = dict(item.raw)
    raw.update(
        {
            "schema_version": 2,
            "public_engagement": dict(item.metrics),
            "product_image_url": LIVE_IMAGE_URL,
            "product_name": LIVE_PRODUCT_TITLE,
            "original_post_url": LIVE_ORIGINAL_POST_URL,
            "affiliate_disclosure": LIVE_AFFILIATE_DISCLOSURE,
            "discovery_method": "public_web_live",
            "collector_public_read_proof": True,
            "safety_conformant": True,
            "source_evidence_policy_passed": True,
            "is_live_real_time": True,
            "live_acquisition": True,
            "provisional": False,
            "fixture": False,
        }
    )
    raw["purchase_evidence"] = [
        {
            "id": "pe_threads_moel_pick_coupang_1",
            "kind": "same_author_affiliate_followup",
            "raw_url": LIVE_RAW_AFFILIATE_URL,
            "raw_value": None,
            "provenance": {
                "source": "threads_public_ssr",
                "author": LIVE_ROOT["author"],
                "public_only": True,
                "root_post_url": LIVE_POST_URL,
                "post_url": LIVE_REPLY["url"],
            },
            "affiliate": {"network": "coupang", "disclosed": True, "same_author": True},
            "resolver": {"status": "pre_resolved", "final_url": final_url},
            "dedupe_key": f"final_url:{final_url}",
        }
    ]
    item.raw = raw
    resolved = resolve_item_purchase_evidence(item, {"resolver": {"max_redirects": 1}})
    assert resolved is not None
    clean = clean_final_url(final_url)
    purchase = resolved.raw["purchase_evidence"][0]
    assert resolved.raw["schema_version"] == 2
    assert resolved.raw["primary_purchase_evidence_id"] == purchase["id"]
    assert resolved.raw["dedupe_key"] == f"final_url:{clean}"
    assert purchase["dedupe_key"] == resolved.raw["dedupe_key"]
    assert purchase["resolver"]["final_url"] == clean
    assert resolved.raw["dedupe_key_namespace"] == "final_url"
    assert resolved.raw["purchase_resolver_status"] in {"pre_resolved", "resolved", "resolved_http_restricted"}
    assert resolved.raw["public_only"] is True
    assert resolved.raw["conformant"] is True
    assert resolved.raw["source_evidence_policy_passed"] is True
    assert resolved.raw["is_live_real_time"] is True
    assert resolved.raw["is_fixture"] is False
    assert resolved.raw["is_cache_fallback"] is False
    assert "cache_source" not in resolved.raw
    assert not resolved.raw.get("provisional")
    return resolved



class ThreadsCollectorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        from idea_radar.collectors.threads import ThreadsCollector

        self.collector_type = ThreadsCollector

    def collect(self, fixtures: list[dict[str, object]], **config: object) -> list[RawItem]:
        collector = self.collector_type(
            DummyFetcher(),
            {
                "enabled": True,
                "fixture_mode": True,
                "accounts": ["moel.pick"],
                "keywords": ["portable desk fan"],
                "related_posts": [POST_URL],
                "fixtures": fixtures,
                "resolver_fixtures": {RAW_AFFILIATE_URL: FINAL_URL},
                **config,
            },
            limit=20,
        )
        return collector.collect()

    def moel_fixture(self, *, lane: str = "account", links: list[str] | None = None, author: str = "moel.pick", **extra: object) -> dict[str, object]:
        row: dict[str, object] = {
            "lane": lane,
            "post_url": POST_URL,
            "author": author,
            "caption": "MoelPick ChillPod desk fan is a physical portable cooling fan for desks.",
            "media_urls": [IMAGE_URL],
            "product_name": "MoelPick ChillPod desk fan",
            "metrics": {"views": 8100, "likes": 47, "replies": 8, "reposts": 3},
            "purchase_followups": [
                {
                    "author": author,
                    "url": link,
                    "kind": "same_author_affiliate_followup",
                    "affiliate": {"network": "coupang", "disclosed": True, "same_author": True},
                }
                for link in (links or [RAW_AFFILIATE_URL])
            ],
            "public_only": True,
            "is_live_real_time": False,
            "fixture_note": "deterministic test fixture; not live-captured evidence",
        }
        row.update(extra)
        return row

    def test_account_keyword_related_fixture_lanes_emit_moel_pick_canonical_rows(self) -> None:
        items = self.collect([self.moel_fixture(lane="account"), self.moel_fixture(lane="keyword"), self.moel_fixture(lane="related")])

        self.assertEqual(["account", "keyword", "related_post"], [item.raw["discovery_lane"] for item in items])
        for item in items:
            self.assertEqual("threads_social", item.source)
            self.assertEqual("threads", item.platform)
            self.assertEqual("moel.pick", item.author)
            self.assertEqual(POST_URL, item.url)
            self.assertEqual([IMAGE_URL], item.media_urls)
            self.assertEqual("MoelPick ChillPod desk fan", item.raw["product_name"])
            self.assertEqual({"views": 8100, "likes": 47, "replies": 8, "reposts": 3}, item.metrics)
            self.assertIs(False, item.raw["is_live_real_time"])
            self.assertIs(True, item.raw["public_only"])
            self.assertIs(False, item.raw["conformant"])
            purchase = item.raw["purchase_evidence"]
            self.assertEqual(1, len(purchase))
            self.assertEqual("same_author_affiliate_followup", purchase[0]["kind"])
            self.assertEqual(RAW_AFFILIATE_URL, purchase[0]["raw_url"])
            self.assertEqual(FINAL_URL, purchase[0]["resolver"]["final_url"])
            self.assertEqual(f"final_url:{FINAL_URL}", purchase[0]["dedupe_key"])

    def test_rejects_non_author_purchase_link_and_safety_cases(self) -> None:
        fixtures = [
            self.moel_fixture(purchase_followups=[{"author": "other.creator", "url": RAW_AFFILIATE_URL, "kind": "same_author_affiliate_followup", "affiliate": {"network": "coupang", "disclosed": True, "same_author": False}}]),
            self.moel_fixture(metrics={"likes": 47, "replies": 8, "reposts": 3}),
            self.moel_fixture(purchase_followups=[]),
            self.moel_fixture(caption="MoelPick launches a workflow automation dashboard app", product_name="MoelPick SaaS dashboard"),
            self.moel_fixture(public_only=False, requires_login=True),
        ]

        self.assertEqual([], self.collect(fixtures))

    def test_multi_link_splitting_and_resolver_timeout(self) -> None:
        second_raw = "https://link.coupang.com/a/second-fixture"
        second_final = "https://www.coupang.com/vp/products/222222222"
        split_items = self.collect(
            [self.moel_fixture(links=[RAW_AFFILIATE_URL, second_raw])],
            resolver_fixtures={RAW_AFFILIATE_URL: FINAL_URL, second_raw: second_final},
        )

        self.assertEqual(2, len(split_items))
        self.assertEqual({f"final_url:{FINAL_URL}", f"final_url:{second_final}"}, {item.raw["dedupe_key"] for item in split_items})
        self.assertEqual(2, len({item.raw["primary_purchase_evidence_id"] for item in split_items}))
        self.assertEqual(2, len({item.id for item in split_items}))

        timeout_items = self.collect([self.moel_fixture()], resolver_fixtures={RAW_AFFILIATE_URL: {"status": "timeout"}})
        self.assertEqual([], timeout_items)


class ThreadsStructuredSsrParserTests(unittest.TestCase):
    def setUp(self) -> None:
        from idea_radar.collectors.threads import ThreadsCollector

        self.collector = ThreadsCollector(
            DummyFetcher(),
            {"enabled": True, "resolver_fixtures": {}},
            limit=10,
        )

    def html(self, *, reply_author: str = "moel.pick", reply_url: str | None = None) -> str:
        reply_url = reply_url or LIVE_RAW_AFFILIATE_URL
        root_post = {
            "post": {
                "pk": LIVE_ROOT["code"],
                "code": LIVE_ROOT["code"],
                "user": {"username": LIVE_ROOT["author"]},
                "caption": {"text": LIVE_ROOT["caption"]},
                "taken_at": LIVE_ROOT["epoch"],
                "like_count": LIVE_METRICS["likes"],
                "text_post_app_info": {"direct_reply_count": LIVE_METRICS["replies"], "share_info": {"repost_count": LIVE_METRICS["reposts"]}},
                "impression_count": LIVE_METRICS["views"],
                "image_versions2": {"candidates": [{"url": LIVE_IMAGE_URL}]},
            }
        }
        reply_post = {
            "post": {
                "pk": LIVE_REPLY["post_id"],
                "code": LIVE_REPLY["code"],
                "user": {"username": reply_author},
                "caption": {"text": LIVE_REPLY["text"]},
                "taken_at": LIVE_ROOT["epoch"] + 1,
                "like_count": 1,
                "text_post_app_info": {
                    "is_reply": True,
                    "root_post_author": {"username": LIVE_ROOT["author"]},
                    "root_post_code": LIVE_ROOT["code"],
                    "text": LIVE_REPLY["text"].replace(LIVE_RAW_AFFILIATE_URL, reply_url),
                    "direct_reply_count": 0,
                    "share_info": {"repost_count": 0},
                    "link_preview_attachment": {"url": reply_url, "title": LIVE_PRODUCT_TITLE},
                },
                "impression_count": 10,
            }
        }
        unrelated_post = {
            "post": {
                "pk": "3",
                "code": "Darhunrelated",
                "user": {"username": "other.creator"},
                "caption": {"text": "unrelated recommendation https://link.coupang.com/a/not-this-one"},
                "text_post_app_info": {
                    "is_reply": True,
                    "root_post_author": {"username": LIVE_ROOT["author"]},
                    "link_preview_attachment": {"url": "https://link.coupang.com/a/not-this-one", "title": "Wrong title"},
                },
            }
        }
        related_root = {
            "post": {
                "pk": "4",
                "code": "DarhRelated",
                "user": {"username": LIVE_ROOT["author"]},
                "caption": {"text": "Related SSR post must not become direct permalink target."},
                "taken_at": LIVE_ROOT["epoch"] + 2,
                "like_count": 1,
                "text_post_app_info": {"direct_reply_count": 0, "share_info": {"repost_count": 0}},
                "impression_count": 2,
            }
        }
        payload = {"require": [{"thread_items": [root_post]}, {"thread_items": [reply_post]}, {"thread_items": [unrelated_post]}, {"thread_items": [related_root]}]}
        return f'<html><body><script type="application/json">{json.dumps(payload)}</script><a href="/@moel.pick/post/{LIVE_ROOT["code"]}">post</a></body></html>'

    def test_structured_ssr_extracts_direct_root_same_author_reply_and_unresolved_purchase(self) -> None:
        self.assertEqual("https://www.threads.com/@moel.pick/post/DarhfjSE24O", LIVE_ORIGINAL_POST_URL)
        posts = self.collector._posts_from_html(LIVE_ORIGINAL_POST_URL, self.html(), lane="related_post")
        self.assertEqual(1, len(posts))
        self.assertEqual(LIVE_ROOT["code"], posts[0]["code"])
        self.assertEqual(LIVE_ROOT["author"], posts[0]["author"])
        self.assertEqual(LIVE_POST_URL, posts[0]["url"])
        self.assertEqual(LIVE_MEDIA_URLS, posts[0]["media_urls"])
        self.assertEqual(LIVE_PRODUCT_TITLE, posts[0]["product_name"])
        self.assertEqual(LIVE_METRICS, posts[0]["engagement"])
        self.assertEqual(LIVE_TIMESTAMP, posts[0]["published_at"])
        self.assertEqual(LIVE_REPLY["url"], posts[0]["replies"][0]["url"])
        self.assertIn(LIVE_REPLY["code"], posts[0]["replies"][0]["url"])

        items = self.collector._items_from_post(posts[0], lane="related_post", is_live=True, trace=None)
        self.assertEqual(1, len(items))
        self.assertEqual(LIVE_PRODUCT_TITLE, items[0].title)
        self.assertEqual(LIVE_ROOT["author"], items[0].author)
        self.assertEqual(LIVE_POST_URL, items[0].url)
        self.assertEqual(LIVE_METRICS, items[0].metrics)
        self.assertIn(LIVE_AFFILIATE_DISCLOSURE, items[0].text)
        self.assertIs(False, items[0].raw["conformant"])
        self.assertIsNone(items[0].raw["dedupe_key"])
        self.assertIs(True, items[0].raw["affiliate_disclosure"])
        purchase = items[0].raw["purchase_evidence"][0]
        self.assertEqual(LIVE_RAW_AFFILIATE_URL, purchase["raw_url"])
        self.assertEqual(LIVE_PRODUCT_TITLE, purchase["product_name"])
        self.assertEqual("not_attempted", purchase["resolver"]["status"])
        self.assertNotIn("final_url", purchase["resolver"])
        self.assertIsNone(purchase["dedupe_key"])
        self.assertIs(True, purchase["affiliate"]["same_author"])
        self.assertIs(True, purchase["affiliate"]["present"])
        self.assertEqual("threads_public_ssr", purchase["provenance"]["source"])
        self.assertEqual(LIVE_ROOT["author"], purchase["provenance"]["author"])
        self.assertEqual(LIVE_REPLY["url"], purchase["provenance"]["post_url"])
        self.assertEqual(LIVE_POST_URL, purchase["provenance"]["root_post_url"])
        self.assertEqual(LIVE_REPLY["post_id"], purchase["provenance"]["reply_post_id"])

    def test_structured_ssr_excludes_unrelated_author_reply_purchase(self) -> None:
        posts = self.collector._posts_from_html(LIVE_POST_URL, self.html(reply_author="other.creator"), lane="related_post")
        items = self.collector._items_from_post(posts[0], lane="related_post", is_live=True, trace=None)
        self.assertEqual([], items)

    def test_resolver_promotes_raw_live_purchase_evidence_to_final_url_conformance(self) -> None:
        from idea_radar.evidence import resolve_item_purchase_evidence
        from idea_radar import runner

        post = self.collector._posts_from_html(LIVE_ORIGINAL_POST_URL, self.html(), lane="related_post")[0]
        item = self.collector._items_from_post(post, lane="related_post", is_live=True, trace=None)[0]
        opener = DeterministicRedirectOpener()

        resolved = resolve_item_purchase_evidence(item, {"resolver": {"max_redirects": 1}}, opener=opener)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual({LIVE_RAW_AFFILIATE_URL: LIVE_FINAL_URL}, LIVE_RESOLVER_MAPPING)
        self.assertEqual(LIVE_RAW_AFFILIATE_URL, opener.request_url)
        purchase = resolved.raw["purchase_evidence"][0]
        self.assertEqual("resolved", purchase["resolver"]["status"])
        self.assertEqual(LIVE_DEDUPE_KEY, purchase["dedupe_key"])
        self.assertEqual(LIVE_DEDUPE_KEY, resolved.raw["dedupe_key"])
        self.assertEqual(LIVE_FINAL_URL, resolved.raw["resolved_purchase_url"])
        self.assertEqual(purchase["id"], resolved.raw["primary_purchase_evidence_id"])
        self.assertIs(True, resolved.raw["conformant"])
        self.assertIs(True, resolved.raw["source_evidence_policy_passed"])
        self.assertEqual(2, resolved.raw["schema_version"])
        self.assertEqual("final_url", resolved.raw["dedupe_key_namespace"])
        self.assertEqual("resolved", resolved.raw["purchase_resolver_status"])
        self.assertEqual([LIVE_FINAL_URL], resolved.raw["purchase_urls"])
        self.assertFalse(resolved.raw["is_fixture"])
        self.assertFalse(resolved.raw["is_cache_fallback"])
        self.assertTrue(resolved.raw["is_live_real_time"])
        self.assertEqual([], resolved.raw["rejection_reasons"])
        bad_suffix = raw_threads_item(final_url="https://")
        bad_resolved = resolve_item_purchase_evidence(bad_suffix, {"resolver": {"max_redirects": 1}})
        self.assertIsNotNone(bad_resolved)
        assert bad_resolved is not None
        self.assertFalse(runner._is_verified_real_time_item(bad_resolved))
        self.assertIn("purchase_evidence_noncanonical", bad_resolved.raw["rejection_reasons"])

        not_public = strict_live_threads_item("Private proof gadget")
        not_public.raw["public_only"] = False
        self.assertFalse(runner._is_verified_real_time_item(not_public))

        requires_login = strict_live_threads_item("Login proof gadget")
        requires_login.raw["requires_login"] = True
        requires_login.raw["public_only"] = False
        self.assertFalse(runner._is_verified_real_time_item(requires_login))

        unresolved = raw_threads_item()
        unresolved.raw["purchase_evidence"][0]["resolver"] = {"status": "timeout", "final_url": FINAL_URL}
        unresolved_result = resolve_item_purchase_evidence(unresolved, {"resolver": {"max_redirects": 1}})
        self.assertIsNotNone(unresolved_result)
        assert unresolved_result is not None
        self.assertFalse(runner._is_verified_real_time_item(unresolved_result))
        self.assertIn("purchase_evidence_noncanonical", unresolved_result.raw["rejection_reasons"])

        missing_final = strict_live_threads_item("Missing final gadget")
        missing_final.raw["purchase_evidence"][0]["resolver"].pop("final_url", None)
        self.assertFalse(runner._is_verified_real_time_item(missing_final))

        dedupe_mismatch = strict_live_threads_item("Dedupe mismatch gadget")
        dedupe_mismatch.raw["purchase_evidence"][0]["dedupe_key"] = "final_url:https://example.com/other-product"
        self.assertFalse(runner._is_verified_real_time_item(dedupe_mismatch))

    def test_safety_trace_classifies_passive_assets_telemetry_and_mutations(self) -> None:
        from idea_radar.collectors.threads import BrowserSafetyTrace, threads_browser_safety_decision

        asset = threads_browser_safety_decision("https://scontent.cdninstagram.com/v/t51.2885-15/image.jpg", method="GET")
        telemetry = threads_browser_safety_decision("https://www.threads.net/ajax/qm", method="POST")
        mutation = threads_browser_safety_decision("https://www.threads.net/@moel.pick/post/DarhfjSE24O/like", method="POST")
        self.assertEqual((True, False, "passive_asset"), (asset["allowed"], asset["unsafe"], asset["surface"]))
        self.assertEqual((False, False, "blocked_telemetry"), (telemetry["allowed"], telemetry["unsafe"], telemetry["reason"]))
        self.assertEqual((False, True), (mutation["allowed"], mutation["unsafe"]))

        trace = BrowserSafetyTrace()
        trace.record(url=asset["url"], method="GET")
        trace.record(url=telemetry["url"], method="POST")
        self.assertTrue(trace.to_dict()["conformant"])
        trace.record(url=mutation["url"], method="POST")
        self.assertFalse(trace.to_dict()["conformant"])

    def test_live_trace_diagnostics_are_full_but_rows_are_compact_and_redacted(self) -> None:
        from idea_radar.collectors.threads import BrowserSafetyTrace

        trace = BrowserSafetyTrace()
        trace.record(url="https://www.threads.net/search?q=%EC%B6%94%EC%B2%9C&fbclid=signed", method="GET", action="navigate")
        trace.record(url="https://www.threads.net/@moel.pick/post/DarhfjSE24O/like?sig=secret", method="POST", action="request")

        full = trace.to_dict()
        compact = trace.compact_dict()

        self.assertIn("events", full)
        self.assertNotIn("?", full["events"][0]["url"])
        self.assertNotIn("events", compact)
        self.assertEqual(full["event_count"], compact["event_count"])
        self.assertEqual(full["unsafe_events"], compact["unsafe_events"])
        self.assertFalse(compact["conformant"])
        self.assertTrue(compact["full_trace_in_collector_diagnostics"])

    def test_live_collection_attempts_all_lanes_after_early_product_yield(self) -> None:
        from idea_radar.collectors.threads import ThreadsCollector

        class FakePage:
            def __init__(self) -> None:
                self.visited: list[str] = []

            def goto(self, url: str, wait_until: str) -> None:
                self.visited.append(url)

            def content(self) -> str:
                return "<html></html>"

        class FakeContext:
            def __init__(self) -> None:
                self.page = FakePage()

            def set_default_timeout(self, timeout: int) -> None:
                self.timeout = timeout

            def add_init_script(self, script: str) -> None:
                self.script = script

            def route(self, pattern: str, handler) -> None:
                self.route_handler = handler

            def new_page(self) -> FakePage:
                return self.page

            def close(self) -> None:
                self.closed = True

        class FakeBrowser:
            def __init__(self) -> None:
                self.context = FakeContext()

            def new_context(self, **kwargs):
                self.context_kwargs = kwargs
                return self.context

            def close(self) -> None:
                self.closed = True

        class FakeChromium:
            def __init__(self) -> None:
                self.browser = FakeBrowser()

            def launch(self, **kwargs):
                self.launch_kwargs = kwargs
                return self.browser

        class FakePlaywright:
            def __init__(self) -> None:
                self.chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        fake_sync = FakePlaywright()
        old_playwright = sys.modules.get("playwright")
        old_sync_api = sys.modules.get("playwright.sync_api")
        sys.modules["playwright"] = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")
        class FakePlaywrightError(Exception):
            pass

        class FakePlaywrightTimeoutError(FakePlaywrightError):
            pass

        sync_api.Error = FakePlaywrightError
        sync_api.TimeoutError = FakePlaywrightTimeoutError
        sync_api.sync_playwright = lambda: fake_sync
        sys.modules["playwright.sync_api"] = sync_api
        try:
            links = [f"https://link.coupang.com/a/live-{index}" for index in range(3)]
            resolver = {link: f"https://www.coupang.com/vp/products/{index}" for index, link in enumerate(links)}
            collector = ThreadsCollector(
                DummyFetcher(),
                {
                    "enabled": True,
                    "accounts": ["moel.pick"],
                    "korean_queries": ["가젯 추천"],
                    "related_post_urls": [LIVE_POST_URL],
                    "live": {"enabled": True, "headless": True, "related_post_urls": [LIVE_POST_URL], "timeout_ms": 100},
                    "resolver_fixtures": resolver,
                },
                limit=1,
            )
            collector._discover_post_urls_from_html = lambda url, html: [LIVE_POST_URL] if url.endswith("@moel.pick") else []
            collector._posts_from_html = lambda url, html, lane: [
                {
                    "lane": lane,
                    "url": LIVE_POST_URL,
                    "author": "moel.pick",
                    "caption": "Physical portable gadget stand with same author purchase links.",
                    "media_urls": LIVE_MEDIA_URLS,
                    "product_name": LIVE_PRODUCT_TITLE,
                    "metrics": {"views": 9400, "likes": 62, "replies": 9, "reposts": 1},
                    "purchase_followups": [
                        {"author": "moel.pick", "url": link, "kind": "same_author_affiliate_followup", "affiliate": {"network": "coupang", "disclosed": True, "same_author": True}}
                        for link in links
                    ],
                    "public_only": True,
                }
            ]

            items = collector.collect()
        finally:
            if old_playwright is None:
                sys.modules.pop("playwright", None)
            else:
                sys.modules["playwright"] = old_playwright
            if old_sync_api is None:
                sys.modules.pop("playwright.sync_api", None)
            else:
                sys.modules["playwright.sync_api"] = old_sync_api

        self.assertEqual(3, len(items))
        self.assertEqual({"account": 1, "keyword": 1, "related_post": 1}, collector.last_run_diagnostics["lane_attempt_counts"])
        self.assertEqual("anonymous", collector.last_run_diagnostics["session"]["mode"])
        self.assertFalse(collector.last_run_diagnostics["session"]["live_profile_used_directly"])
        self.assertEqual("blocked", collector.last_run_diagnostics["session"]["service_workers"])
        self.assertIn("events", collector.last_run_diagnostics["browser_safety_trace"])
        self.assertNotIn("events", items[0].raw["browser_safety_trace"])
        self.assertTrue(items[0].raw["browser_safety_trace"]["full_trace_in_collector_diagnostics"])

    def test_storage_state_is_copied_into_disposable_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            source = root / "state.json"
            source.write_text('{"cookies": []}', encoding="utf-8")
            collector = self.collector.__class__(DummyFetcher(), {"enabled": True}, limit=1)

            copied = collector._copied_storage_state({"browser": {"storage_state_file": str(source)}}, root / "tmp")

            self.assertIsNotNone(copied)
            assert copied is not None
            self.assertNotEqual(source, copied)
            self.assertEqual(source.read_text(encoding="utf-8"), copied.read_text(encoding="utf-8"))

    def test_copied_profile_helper_uses_disposable_copy_and_excludes_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            source = root / "source-profile"
            source.mkdir()
            (source / "Default").mkdir()
            (source / "Default" / "Cookies").write_text("cookie db", encoding="utf-8")
            (source / "Cache").mkdir()
            (source / "Cache" / "entry").write_text("cache", encoding="utf-8")
            (source / "History").write_text("history", encoding="utf-8")
            (source / "SingletonLock").write_text("lock", encoding="utf-8")
            collector = self.collector.__class__(DummyFetcher(), {"enabled": True}, limit=1)

            copied = collector._copy_browser_profile({"session_mode": "copied_profile", "source_profile_dir": str(source)}, root / "tmp")

            self.assertIsNotNone(copied)
            assert copied is not None
            self.assertNotEqual(source.resolve(), copied.resolve())
            self.assertFalse(source.resolve() in copied.resolve().parents)
            self.assertTrue((copied / "Default" / "Cookies").exists())
            self.assertFalse((copied / "Cache").exists())
            self.assertFalse((copied / "History").exists())
            self.assertFalse((copied / "SingletonLock").exists())

    def test_default_config_threads_seed_breadth_matches_public_inventory(self) -> None:
        config = json.loads(Path(__file__).resolve().parents[1].joinpath("config", "sources.json").read_text(encoding="utf-8"))
        threads = config["sources"]["threads"]

        self.assertEqual(
            ["moel.pick", "smart.pick_", "item_market.k", "homlit_pick", "it_goodgiraffe", "1year3year", "nostalsokei", "tkasiddl_99", "iguan_a9305", "pogeun__shop", "health60s"],
            threads["accounts"],
        )
        self.assertEqual(
            ["신박한 상품", "쿠팡 추천템", "생활용품 추천", "주방템 추천", "가성비템", "아이디어 상품", "선물 추천", "반려동물 용품", "캠핑용품", "정리 수납템"],
            threads["korean_queries"],
        )
        self.assertEqual(["https://www.threads.com/@moel.pick/post/DarhfjSE24O"], threads["related_post_urls"])
        self.assertNotIn("fixture_items", threads)
        self.assertNotIn("example.com", json.dumps(threads, ensure_ascii=False))
        browser = threads["live"]["browser"]
        self.assertEqual("playwright", browser["engine"])
        self.assertEqual("anonymous", browser["session_mode"])
        self.assertEqual("block", browser["service_workers"])
        self.assertEqual("msedge", self.collector._browser_config({"browser": {"browser_channel": "msedge"}})["channel"])

    def test_bounded_discovery_dedupes_public_permalinks_from_ssr_and_links(self) -> None:
        discovered = self.collector._discover_post_urls_from_html("https://www.threads.net/@moel.pick", self.html())
        self.assertEqual(["https://www.threads.net/@moel.pick/post/DarhfjSE24O", "https://www.threads.net/@moel.pick/post/DarhRelated"], discovered)

class ThreadsOutputAndGuiContractTests(unittest.TestCase):
    def test_final_url_dedupe_uses_newest_representative_without_source_url_fallback(self) -> None:
        older = scored(strict_live_threads_item(published_at="2026-07-09T09:00:00+00:00", score=9.5), score=9.5)
        newer = scored(strict_live_threads_item(published_at="2026-07-10T09:00:00+00:00", score=5.5), score=5.5)

        representatives = dedupe_ideas([older, newer])

        self.assertEqual([newer.item.id], [idea.item.id for idea in representatives])
        self.assertEqual(2, newer.item.raw["dedupe_group_size"])
        self.assertTrue(newer.duplicate_key.startswith("final_url:"))
        self.assertNotEqual(f"source_url:{POST_URL}", newer.duplicate_key)

    def test_gui_defaults_detail_old_loader_and_latest_thirty_contract(self) -> None:
        self.assertIn("threads_social", DEFAULT_ON_SOURCES)
        self.assertIn("Threads", SOURCE_LABELS["threads_social"])

        ideas = [
            scored(
                strict_live_threads_item(
                    published_at=f"2026-07-{day:02d}T09:00:00+00:00",
                    final_url=f"https://www.coupang.com/vp/products/{day:09d}",
                    url=f"{LIVE_POST_URL}?day={day}",
                    views=9000 + day,
                    likes=40 + day,
                    replies=8,
                    reposts=3,
                    score=float(day % 10),
                ),
                score=float(day % 10),
            )
            for day in range(1, 32)
        ]
        high_score_fixture = scored(raw_threads_item(title="Fixture high score", published_at="2026-07-31T10:00:00+00:00", score=99.0), score=99.0)
        cache_row = scored(strict_live_threads_item("Cached high score", published_at="2026-07-31T11:00:00+00:00", score=100.0), score=100.0)
        cache_row.item.raw["cache_source"] = "cache"
        same_time_low_score_more_engagement = scored(
            strict_live_threads_item(
                "Same time higher engagement",
                published_at="2026-07-31T09:00:00+00:00",
                final_url="https://www.coupang.com/vp/products/999999991",
                url=f"{LIVE_POST_URL}?variant=engagement",
                views=99999,
                likes=1,
                replies=1,
                reposts=1,
                score=1.0,
            ),
            score=1.0,
        )
        same_time_high_score_less_engagement = scored(
            strict_live_threads_item(
                "Same time lower engagement",
                published_at="2026-07-31T09:00:00+00:00",
                final_url="https://www.coupang.com/vp/products/999999992",
                url=f"{LIVE_POST_URL}?variant=score",
                views=1,
                likes=1,
                replies=1,
                reposts=1,
                score=99.0,
            ),
            score=99.0,
        )
        ideas.extend([high_score_fixture, cache_row, same_time_high_score_less_engagement, same_time_low_score_more_engagement])
        latest = _latest_verified_ideas(ideas)

        self.assertEqual(30, len(latest))
        titles = [idea.item.title for idea in latest]
        high_engagement_index = titles.index("Same time higher engagement")
        low_engagement_index = titles.index("Same time lower engagement")
        self.assertLess(high_engagement_index, titles.index(LIVE_PRODUCT_TITLE))
        self.assertLess(high_engagement_index, low_engagement_index)
        self.assertLess(latest[high_engagement_index].total_score, latest[low_engagement_index].total_score)
        self.assertNotIn(high_score_fixture, latest)
        self.assertNotIn(cache_row, latest)
        self.assertTrue(all(idea.item.raw["schema_version"] == 2 for idea in latest))
        self.assertTrue(all(idea.item.raw["is_live_real_time"] for idea in latest))
        self.assertTrue(all(not idea.item.raw.get("cache_source") for idea in latest))
        self.assertTrue(all(not idea.item.raw.get("is_fixture") for idea in latest))

        self.assertEqual(LIVE_AFFILIATE_DISCLOSURE, latest[high_engagement_index].item.raw["affiliate_disclosure"])
        lines = "\n".join(_evidence_lines(latest[high_engagement_index]))
        for expected in [
            f"Product image: {LIVE_IMAGE_URL}",
            f"Product name: {LIVE_PRODUCT_TITLE}",
            f"Original post URL: {LIVE_ORIGINAL_POST_URL}",
            "Purchase URL: https://coupang.com/vp/products/999999991",
            "Views: 99999",
            "Likes: 1",
            "Replies: 1",
            "Reposts: 1",
            "Affiliate disclosure: coupang (disclosed=True)",
            "Discovery lane: account",
            "Conformance: True",
            "dedupe=final_url:https://coupang.com/vp/products/999999991",
        ]:
            self.assertIn(expected, lines)

        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "ideas.json"
            old = scored(raw_threads_item()).to_dict()
            old["legacy_extra"] = "ignored"
            old["item"].pop("metrics")
            old["item"].pop("media_urls")
            old["item"]["future_extra"] = "ignored"
            path.write_text(json.dumps([old], ensure_ascii=False), encoding="utf-8")

            loaded = load_ideas_from_json(path)

        self.assertEqual(1, len(loaded))
        self.assertEqual({}, loaded[0].item.metrics)
        self.assertEqual([], loaded[0].item.media_urls)

    def test_all_export_is_uncapped_but_digest_and_gui_are_latest_thirty(self) -> None:
        ideas = [
            scored(
                strict_live_threads_item(
                    published_at=f"2026-07-{((day - 1) % 28) + 1:02d}T{(day - 1) // 28:02d}:00:00+00:00",
                    final_url=f"https://www.coupang.com/vp/products/{day:09d}",
                    url=f"{LIVE_POST_URL}?export={day}",
                    score=6.0,
                ),
                score=6.0,
            )
            for day in range(1, 36)
        ]
        fixture = scored(raw_threads_item(title="Export fixture diagnostic", published_at="2026-07-31T11:00:00+00:00", score=99.0), score=99.0)
        cached = scored(strict_live_threads_item("Export cached diagnostic", published_at="2026-07-31T12:00:00+00:00", score=98.0), score=98.0)
        cached.item.raw["cache_source"] = "cache"
        ideas = list(reversed([*ideas, fixture, cached]))

        with tempfile.TemporaryDirectory() as tmp_name:
            outputs = write_outputs([idea.item for idea in ideas], _latest_verified_ideas(ideas, limit=35), tmp_name, top=30)
            ideas_json = json.loads(Path(outputs["ideas_json"]).read_text(encoding="utf-8"))
            with Path(outputs["ideas_csv"]).open("r", encoding="utf-8-sig", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            digest = Path(outputs["digest"]).read_text(encoding="utf-8")

        self.assertEqual(35, len(ideas_json))
        self.assertEqual(35, len(csv_rows))
        self.assertEqual(30, digest.count("## "))
        self.assertNotIn("Export fixture diagnostic", digest)
        self.assertNotIn("Export cached diagnostic", digest)
        self.assertEqual(30, len(_latest_verified_ideas(ideas)))
        self.assertNotIn(fixture, _latest_verified_ideas(ideas))
        self.assertNotIn(cached, _latest_verified_ideas(ideas))


if __name__ == "__main__":
    unittest.main()
