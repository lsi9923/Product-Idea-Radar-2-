from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from idea_radar.collectors.reddit import RedditCollector
from idea_radar.fetcher import FetchResult
from idea_radar.models import RawItem
from idea_radar.quality import partition_quality_items, quality_reasons
from idea_radar.scoring import score_item


NOW = datetime(2026, 7, 10, tzinfo=timezone.utc)


def raw(
    title: str,
    text: str = "",
    *,
    source: str | None = None,
    platform: str = "kickstarter",
    published_at: str | None = "2026-07-01T00:00:00+00:00",
    author: str | None = None,
    raw_fields: dict[str, object] | None = None,
) -> RawItem:
    return RawItem(
        id=f"{source or platform}:test",
        source=source or platform,
        platform=platform,
        url=f"https://example.com/{platform}/test",
        title=title,
        text=text,
        author=author,
        published_at=published_at,
        raw=raw_fields or {},
    )


def purchase_evidence(
    *,
    final_url: str = "https://www.coupang.com/vp/products/123456789",
    author: str = "moel.pick",
) -> dict[str, object]:
    return {
        "physical_signal": {"kind": "product_name", "value": "Nimbus ChillPod desk fan"},
        "public_engagement": {"views": 8100, "likes": 47, "replies": 8, "reposts": 3},
        "public_only": True,
        "is_live_real_time": False,
        "purchase_evidence": [
            {
                "id": "pe_threads_moel_pick_coupang_1",
                "kind": "same_author_affiliate_followup",
                "raw_url": "https://link.coupang.com/a/deterministic-fixture",
                "provenance": {
                    "source": "threads_fixture",
                    "author": author,
                    "public_only": True,
                    "post_url": "https://www.threads.com/@moel.pick/post/reply-fixture",
                    "reply_post_id": "reply-fixture",
                    "root_post_url": "https://www.threads.com/@moel.pick/post/root-fixture",
                    "fixture_note": "deterministic test fixture; not live-captured evidence",
                },
                "affiliate": {"network": "coupang", "disclosed": True, "same_author": True},
                "resolver": {"status": "resolved", "final_url": final_url, "fixture": True},
                "dedupe_key": f"final_url:{final_url}",
            }
        ],
        "primary_purchase_evidence_id": "pe_threads_moel_pick_coupang_1",
        "dedupe_key": f"final_url:{final_url}",
        "conformant": True,
    }


class QualityGateTests(unittest.TestCase):
    def test_rejects_old_reddit_items(self) -> None:
        item = raw("Marvel led fan", "submitted by /u/example", platform="reddit", published_at="2023-06-03T23:48:01+00:00")
        self.assertTrue(any("오래된" in reason for reason in quality_reasons(item, now=NOW)))

    def test_rejects_creative_kickstarter_campaign(self) -> None:
        item = raw(
            "Grimdark Empire. Miniature STL Files",
            "Colonial Modular Troops for Grimdark Games.",
            platform="kickstarter",
        )
        self.assertTrue(quality_reasons(item, now=NOW))
        self.assertFalse(score_item(item, threshold=4.0).is_product_idea)

    def test_rejects_kickstarter_game_platform(self) -> None:
        item = raw(
            "Ludika: virtual tabletop board gaming platform & community",
            "A web-hosted virtual tabletop platform tailored for board games.",
            platform="kickstarter",
        )
        self.assertTrue(any("킥스타터 비제품" in reason for reason in quality_reasons(item, now=NOW)))

    def test_accepts_concrete_kickstarter_product(self) -> None:
        item = raw(
            "ZERA mini - The True Card-Sized Cooling Fan",
            "13m/s windspeed, intuitive display, ultralight portable cooling fan.",
            platform="kickstarter",
        )
        kept, rejected = partition_quality_items([item], now=NOW)
        self.assertEqual([item], kept)
        self.assertEqual([], rejected)

    def test_rejects_social_chatter(self) -> None:
        item = raw(
            "gm legends! what are you shipping this week? drop it in the replies",
            platform="x",
            published_at="2026-07-08T11:15:37.000Z",
        )
        self.assertTrue(any("소셜" in reason for reason in quality_reasons(item, now=NOW)))

    def test_social_product_requires_public_metrics_and_purchase_evidence(self) -> None:
        accepted = raw(
            "Nimbus ChillPod desk fan",
            "moel.pick shows a compact portable cooling fan for desks with quiet all-day airflow.",
            source="threads_social",
            platform="threads",
            published_at="2026-07-09T10:15:00+00:00",
            author="moel.pick",
            raw_fields=purchase_evidence(),
        )
        missing_purchase = raw(
            "Nimbus ChillPod desk fan",
            "Nimbus shows a compact portable cooling fan for desks with quiet all-day airflow.",
            source="threads_social",
            platform="threads",
            published_at="2026-07-09T10:15:00+00:00",
            raw_fields={"public_engagement": {"views": 8100, "likes": 47, "replies": 8, "reposts": 3}, "public_only": True},
        )
        missing_metrics = raw(
            "Nimbus ChillPod desk fan",
            "Nimbus shows a compact portable cooling fan for desks with quiet all-day airflow.",
            source="threads_social",
            platform="threads",
            published_at="2026-07-09T10:15:00+00:00",
            raw_fields={"purchase_evidence": purchase_evidence()["purchase_evidence"], "public_only": True},
        )

        kept, rejected = partition_quality_items([accepted, missing_purchase, missing_metrics], now=NOW)

        self.assertEqual([accepted], kept)
        self.assertEqual([missing_purchase, missing_metrics], [row.item for row in rejected])
        self.assertTrue(score_item(accepted, threshold=5.5).is_product_idea)

    def test_rejects_malformed_non_public_and_author_mismatched_social_evidence(self) -> None:
        base = purchase_evidence()
        cases: list[tuple[str, dict[str, object], str]] = []

        non_public = dict(base)
        non_public["public_only"] = False
        non_public["requires_login"] = True
        cases.append(("Login-only ChillPod desk fan", non_public, "소셜 공개 출처 아님"))

        unresolved = dict(base)
        unresolved["purchase_evidence"] = [dict(base["purchase_evidence"][0])]
        unresolved["purchase_evidence"][0]["resolver"] = {"status": "unresolved"}
        unresolved["purchase_evidence"][0].pop("dedupe_key", None)
        cases.append(("Unresolved ChillPod desk fan", unresolved, "구매 증거 리졸버 실패"))

        same_author_false = dict(base)
        same_author_false["purchase_evidence"] = [dict(base["purchase_evidence"][0])]
        same_author_false["purchase_evidence"][0]["affiliate"] = {"network": "coupang", "disclosed": True, "same_author": False}
        cases.append(("Cross-author ChillPod desk fan", same_author_false, "동일 작성자"))

        author_mismatch = dict(base)
        author_mismatch["purchase_evidence"] = [dict(base["purchase_evidence"][0])]
        author_mismatch["purchase_evidence"][0]["provenance"] = dict(base["purchase_evidence"][0]["provenance"])
        author_mismatch["purchase_evidence"][0]["provenance"]["author"] = "other.creator"
        cases.append(("Author mismatch ChillPod desk fan", author_mismatch, "동일 작성자"))

        for title, fields, expected_reason in cases:
            with self.subTest(title=title):
                item = raw(
                    title,
                    "moel.pick shows a compact portable cooling fan for desks with quiet all-day airflow.",
                    source="threads_social",
                    platform="threads",
                    published_at="2026-07-09T10:15:00+00:00",
                    author="moel.pick",
                    raw_fields=fields,
                )
                if expected_reason == "동일 작성자":
                    item.author = "moel.pick"

                kept, rejected = partition_quality_items([item], now=NOW)

                self.assertEqual([], kept)
                self.assertEqual([item], [row.item for row in rejected])
                self.assertTrue(any(expected_reason in reason for reason in rejected[0].reasons))

    def test_rejects_social_memes_news_and_chatter_without_product_evidence(self) -> None:
        cases = [
            raw("When the gadget launch is just vibes", "meme template about founders", source="x_social", platform="x"),
            raw("Kitchen startup funding roundup", "News: investors backed several companies today.", source="x_social", platform="x"),
            raw("gm legends! what are you shipping this week?", "drop it in the replies", source="x_social", platform="x"),
        ]

        for item in cases:
            with self.subTest(title=item.title):
                reasons = quality_reasons(item, now=NOW)
                self.assertTrue(reasons)

    def test_rejects_social_platform_block_and_index_pages(self) -> None:
        cases = [
            raw(
                "JavaScript is disabled in this browser",
                "Please switch to a supported browser to continue using x.com. Something went wrong, but don't fret.",
                source="x_social",
                platform="x",
                published_at=None,
                raw_fields={"provisional": True, "freshness_unknown": True, "category_seed": "gadgets"},
            ),
            raw(
                "Gadgets • 46 million reels on Instagram",
                "See reels about gadgets from people around the world.",
                source="instagram_social",
                platform="instagram",
                published_at=None,
                raw_fields={"provisional": True, "freshness_unknown": True, "category_seed": "gadgets"},
            ),
            raw(
                "Instagram",
                "Instagram 계정을 만들거나 Instagram에 로그인하여 관심사를 공유해보세요.",
                source="instagram_social",
                platform="instagram",
                published_at=None,
                raw_fields={"provisional": True, "freshness_unknown": True, "category_seed": "home"},
            ),
        ]

        for item in cases:
            with self.subTest(title=item.title):
                self.assertTrue(any("플랫폼" in reason for reason in quality_reasons(item, now=NOW)))

    def test_rejects_social_promos_ads_and_events_without_canonical_purchase_evidence(self) -> None:
        cases = [
            raw(
                "Ad: PawPal automatic pet feeder",
                "PawPal demonstrates a smart pet feeder with portion control and a sealed food container.",
                source="instagram_social",
                platform="instagram",
                raw_fields={"public_only": True, "public_engagement": {"views": 2000, "likes": 42, "replies": 5, "reposts": 2}},
            ),
            raw(
                "Sponsored demo: LumaPan self-cleaning cookware",
                "LumaPan is showing its nonstick self-cleaning kitchen pan at the creator booth.",
                source="x_social",
                platform="x",
                raw_fields={"public_only": True, "public_engagement": {"views": 2200, "likes": 40, "replies": 4, "reposts": 1}},
            ),
            raw(
                "Event launch: FlexForm massage pillow",
                "FlexForm announced a portable neck massage pillow with adjustable heat at today's expo.",
                source="threads_social",
                platform="threads",
                raw_fields={"public_only": True, "public_engagement": {"views": 2100, "likes": 41, "replies": 6, "reposts": 3}},
            ),
        ]

        kept, rejected = partition_quality_items(cases, now=NOW)

        self.assertEqual([], kept)
        self.assertEqual(cases, [row.item for row in rejected])

    def test_rejects_provisional_social_item_without_public_purchase_provenance(self) -> None:
        item = raw(
            "GlowGrip smart phone case",
            "GlowGrip describes a protective phone case with integrated stand and safety light.",
            source="instagram_social",
            platform="instagram",
            published_at=None,
            raw_fields={
                "discovery_method": "public_web",
                "evidence_type": "text_metadata",
                "provisional": True,
                "freshness_unknown": True,
                "category_seed": "gadgets",
                "fallback_reason": "missing_official_credentials",
                "public_engagement": {"views": 1200, "likes": 30, "replies": 3, "reposts": 1},
            },
        )

        kept, rejected = partition_quality_items([item], now=NOW)

        self.assertEqual([], kept)
        self.assertEqual([item], [row.item for row in rejected])


@dataclass
class FakeFetcher:
    urls: list[str]

    def fetch(self, url: str):
        self.urls.append(url)
        xml = """<?xml version='1.0'?><rss><channel><item><title>Smart desk lamp</title><link>https://reddit.com/r/gadgets/comments/1/test</link><description>submitted by /u/test</description><pubDate>Fri, 10 Jul 2026 00:00:00 GMT</pubDate><guid>t3_test</guid></item></channel></rss>"""
        return FetchResult(url=url, ok=True, content=xml, stderr="", returncode=0)


class RedditCollectorTests(unittest.TestCase):
    def test_reddit_collector_uses_explicit_rss_sort(self) -> None:
        fetcher = FakeFetcher(urls=[])
        collector = RedditCollector(fetcher, {"subreddits": ["gadgets"], "sort": "new", "limit_per_subreddit": 2}, limit=2)  # type: ignore[arg-type]
        items = collector.collect()
        self.assertEqual(1, len(items))
        self.assertIn("/r/gadgets/new/.rss?limit=2", fetcher.urls[0])


if __name__ == "__main__":
    unittest.main()
