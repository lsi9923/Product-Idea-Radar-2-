from __future__ import annotations

import csv
import json
import tomllib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from idea_radar import cli, runner
from idea_radar.config import load_config
from idea_radar.models import RawItem, ScoredIdea
from idea_radar.storage import Store
from idea_radar.evidence import clean_final_url, resolve_item_purchase_evidence


PUBLISHED_AT = "2026-07-10T09:00:00+00:00"
CATEGORY_SEEDS = ["gadgets", "home", "kitchen", "beauty", "pet", "fitness"]
LIVE_ACCEPTANCE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live_acceptance_30.json"
LIVE_ACCEPTANCE_FIXTURE = json.loads(LIVE_ACCEPTANCE_FIXTURE_PATH.read_text(encoding="utf-8"))
LIVE_ACCEPTANCE_ROWS = LIVE_ACCEPTANCE_FIXTURE["rows"]
LIVE_THREADS_ROW = next(row for row in LIVE_ACCEPTANCE_ROWS if row["item"]["source"] == "threads_social" and row["item"]["author"] == "moel.pick")


def social_item(
    source: str,
    platform: str,
    title: str,
    *,
    category_seed: str,
    score: float = 6.4,
    discovery_method: str = "public_web",
    published_at: str | None = PUBLISHED_AT,
    extra_raw: dict[str, object] | None = None,
) -> RawItem:
    final_url = f"https://fixture.example.test/final/{title.lower().replace(' ', '-')}"
    raw = {
        "discovery_method": discovery_method,
        "evidence_type": "canonical_purchase_evidence",
        "provisional": discovery_method == "public_web",
        "freshness_unknown": published_at is None,
        "category_seed": category_seed,
        "fixture_score": score,
        "physical_signal": {"kind": "fixture_text", "value": "physical portable product"},
        "public_engagement": {"views": 8100, "likes": 47, "replies": 8, "reposts": 3},
        "public_only": True,
        "is_live_real_time": False,
        "purchase_evidence": [
            {
                "id": f"pe_{source}_{title.lower().replace(' ', '_')}",
                "kind": "canonical_purchase_evidence",
                "raw_url": "https://link.coupang.com/a/deterministic-fixture",
                "provenance": {
                    "source": "deterministic_social_fixture",
                    "public_only": True,
                    "canonical_product_page": True,
                    "fixture_note": "deterministic test fixture; not live-captured evidence",
                },
                "affiliate": {"network": "coupang", "disclosed": True},
                "resolver": {"status": "resolved", "final_url": final_url, "fixture": True},
                "dedupe_key": f"final_url:{final_url}",
            }
        ],
        "primary_purchase_evidence_id": f"pe_{source}_{title.lower().replace(' ', '_')}",
        "dedupe_key": f"final_url:{final_url}",
        "conformant": True,
    }
    if discovery_method == "public_web":
        raw["fallback_reason"] = "missing_official_credentials"
    if extra_raw:
        raw.update(extra_raw)
    return RawItem(
        id=f"{source}:{title.lower().replace(' ', '-')}",
        source=source,
        platform=platform,
        url=f"https://example.com/{platform}/{title.lower().replace(' ', '-')}",
        title=title,
        text=f"Brand {title.split()[0]} describes a smart portable device and tool with clear text metadata evidence.",
        published_at=published_at,
        raw=raw,
    )


def write_config(path: Path, sources: dict[str, dict[str, object]], *, threshold: float = 4.0) -> None:
    path.write_text(
        json.dumps(
            {
                "scoring": {"threshold": threshold},
                "social": {"category_seeds": CATEGORY_SEEDS},
                "sources": sources,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

def item_from_fixture(row: dict[str, object]) -> RawItem:
    item = row["item"]  # type: ignore[index]
    assert isinstance(item, dict)
    return RawItem(
        id=str(item["id"]),
        source=str(item["source"]),
        platform=str(item["platform"]),
        url=str(item["url"]),
        title=str(item["title"]),
        text=str(item.get("text") or ""),
        author=item.get("author") if isinstance(item.get("author"), str) else None,
        published_at=item.get("published_at") if isinstance(item.get("published_at"), str) else None,
        media_urls=list(item.get("media_urls") or []),
        metrics=dict(item.get("metrics") or {}),
        tags=list(item.get("tags") or []),
        raw=dict(item.get("raw") or {}),
        fetched_at=str(item.get("fetched_at") or PUBLISHED_AT),
    )


def scored_from_fixture(row: dict[str, object]) -> ScoredIdea:
    scored_row = row
    item = item_from_fixture(scored_row)
    return ScoredIdea(
        item=item,
        is_product_idea=bool(scored_row.get("is_product_idea", True)),
        category=str(scored_row.get("category") or "gadgets"),
        summary_ko=str(scored_row.get("summary_ko") or item.title),
        novelty_score=float(scored_row.get("novelty_score") or 6.0),
        virality_score=float(scored_row.get("virality_score") or 6.0),
        market_score=float(scored_row.get("market_score") or 6.0),
        total_score=float(scored_row.get("total_score") or 6.0),
        duplicate_key=str(scored_row.get("duplicate_key") or item.raw.get("dedupe_key") or item.id),
        reason=str(scored_row.get("reason") or "captured live fixture"),
        matched_terms=list(scored_row.get("matched_terms") or []),
    )


def live_acceptance_ideas() -> list[ScoredIdea]:
    return [scored_from_fixture(row) for row in LIVE_ACCEPTANCE_ROWS]


class FakeSocialCollector:
    platform = ""
    source = ""

    def __init__(self, fetcher: object, config: dict[str, object], *, limit: int) -> None:
        self.config = config
        self.limit = limit

    def collect(self) -> list[RawItem]:
        mode = self.config.get("fixture_mode", "ok")
        if mode == "raise":
            raise RuntimeError(f"simulated {self.source} collector failure")
        if mode == "zero_quality":
            return [
                RawItem(
                    id=f"{self.source}:chatter",
                    source=self.source,
                    platform=self.platform,
                    url=f"https://example.com/{self.platform}/chatter",
                    title="gm legends what are you shipping this week",
                    text="drop it in the replies",
                    published_at=PUBLISHED_AT,
                    raw={"category_seed": self.config.get("category_seed", "gadgets"), "fixture_score": 7.0},
                )
            ]
        method = self._discovery_method()
        return [
            social_item(
                self.source,
                self.platform,
                str(self.config.get("fixture_title", f"{self.platform} product")),
                category_seed=str(self.config.get("category_seed", "gadgets")),
                score=float(self.config.get("fixture_score", 6.2)),
                discovery_method=method,
                published_at=self.config.get("published_at", PUBLISHED_AT),  # type: ignore[arg-type]
                extra_raw=self.config.get("extra_raw"),  # type: ignore[arg-type]
            )
        ][: self.limit]

    def _discovery_method(self) -> str:
        if self.platform == "x" and os.environ.get("X_BEARER_TOKEN"):
            return "official_api"
        if self.platform == "instagram" and os.environ.get("INSTAGRAM_ACCESS_TOKEN") and os.environ.get("INSTAGRAM_IG_USER_ID"):
            return "official_api"
        return "public_web"


class FakeXCollector(FakeSocialCollector):
    platform = "x"
    source = "x_social"


class FakeInstagramCollector(FakeSocialCollector):
    platform = "instagram"
    source = "instagram_social"


class FakeRedditCollector:
    def __init__(self, fetcher: object, config: dict[str, object], *, limit: int) -> None:
        self.config = config

    def collect(self) -> list[RawItem]:
        return [
            RawItem(
                id="reddit:desk-lamp",
                source="reddit",
                platform="reddit",
                url="https://example.com/reddit/desk-lamp",
                title="Sol desk lamp",
                text="Sol makes a compact home desk lamp with a portable charger base.",
                published_at=PUBLISHED_AT,
                raw={
                    "fixture_score": 5.6,
                    "public_engagement": {"views": 1200, "likes": 31, "replies": 5, "reposts": 2},
                    "public_only": True,
                    "purchase_evidence": [
                        {
                            "id": "pe_reddit_desk_lamp_fixture",
                            "kind": "public_product_page",
                            "raw_url": "https://example.com/products/sol-desk-lamp",
                            "resolver": {"status": "resolved", "final_url": "https://example.com/products/sol-desk-lamp", "fixture": True},
                            "dedupe_key": "final_url:https://example.com/products/sol-desk-lamp",
                            "provenance": {"source": "reddit_fixture", "public_only": True},
                        }
                    ],
                },
            )
        ]


def fake_score_item(item: RawItem, *, threshold: float = 4.7) -> ScoredIdea:
    total = float(item.raw.get("fixture_score", 6.0))
    category = str(item.raw.get("category_seed", "general"))
    return ScoredIdea(
        item=item,
        is_product_idea=total >= threshold,
        category=category,
        summary_ko=f"fixture summary for {item.title}",
        novelty_score=round(total, 2),
        virality_score=round(total, 2),
        market_score=round(total, 2),
        total_score=round(total, 2),
        duplicate_key=item.id,
        reason=f"fixture score {total} >= threshold {threshold}",
        matched_terms=[category],
    )


def strict_live_item(title: str, *, source: str = "threads_social", platform: str = "threads", published_at: str | None = PUBLISHED_AT, lane: str = "account") -> RawItem:
    captured = item_from_fixture(LIVE_THREADS_ROW)
    raw = dict(captured.raw)
    purchase = dict(raw["purchase_evidence"][0])
    resolver = dict(purchase["resolver"])
    final_url = str(resolver["final_url"])
    raw_url = str(purchase["raw_url"])
    purchase["resolver"] = resolver
    raw["purchase_evidence"] = [purchase]
    raw.update(
        {
            "category_seed": "gadgets",
            "fixture_score": 6.4,
            "is_live_real_time": True,
            "live_acquisition": True,
            "provisional": False,
            "discovery_method": "public_web_live",
            "discovery_lane": lane,
            "collector_public_read_proof": {
                "platform": platform,
                "public_only": True,
                "method": "GET",
                "surface": "post",
                "url": captured.url,
                "fetch_ok": True,
            },
            "safety_conformant": True,
            "source_evidence_policy_passed": True,
        }
    )
    item = RawItem(
        id=f"{source}:live:{title.lower().replace(' ', '-')}",
        source=source,
        platform=platform,
        url=captured.url,
        title=title,
        text=captured.text,
        author=captured.author,
        published_at=published_at,
        media_urls=list(captured.media_urls),
        metrics=dict(captured.metrics),
        tags=list(captured.tags),
        raw=raw,
        fetched_at=captured.fetched_at,
    )
    resolved = resolve_item_purchase_evidence(item, {"resolver": {"max_redirects": 1}})
    assert resolved is not None
    raw = resolved.raw
    purchase = raw["purchase_evidence"][0]
    clean = clean_final_url(final_url)
    assert raw["original_post_url"] == captured.raw["original_post_url"]
    assert purchase["raw_url"] == raw_url
    assert raw["schema_version"] == 2
    assert raw["primary_purchase_evidence_id"] == purchase["id"]
    assert raw["resolved_purchase_url"] == clean
    assert raw["dedupe_key"] == f"final_url:{clean}"
    assert purchase["dedupe_key"] == raw["dedupe_key"]
    assert purchase["resolver"]["final_url"] == clean
    assert raw["dedupe_key_namespace"] == "final_url"
    assert raw["purchase_resolver_status"] in {"pre_resolved", "resolved", "resolved_http_restricted"}
    assert raw["public_only"] is True
    assert raw["conformant"] is True
    assert raw["source_evidence_policy_passed"] is True
    assert raw["is_live_real_time"] is True
    assert raw["is_fixture"] is False
    assert raw["is_cache_fallback"] is False
    assert "cache_source" not in raw
    assert not raw.get("fixture")
    assert not raw.get("provisional")
    return resolved


class FakeThreadsCollector(FakeSocialCollector):
    platform = "threads"
    source = "threads_social"

    def __init__(self, fetcher: object, config: dict[str, object], *, limit: int) -> None:
        super().__init__(fetcher, config, limit=limit)
        self.last_run_diagnostics = dict(config.get("last_run_diagnostics", {}))


class SocialSmokeTests(unittest.TestCase):
    def run_fixture_crawl(
        self,
        tmp: Path,
        sources: dict[str, dict[str, object]],
        *,
        env: dict[str, str] | None = None,
        threshold: float = 4.0,
    ) -> runner.CrawlResult:
        config_path = tmp / "sources.json"
        write_config(config_path, sources, threshold=threshold)
        out_dir = tmp / "out"
        db_path = tmp / "ideas.sqlite"
        collectors = {
            "threads_social": FakeThreadsCollector,
            "x_social": FakeXCollector,
            "instagram_social": FakeInstagramCollector,
            "reddit": FakeRedditCollector,
        }
        social_env = {
            "X_BEARER_TOKEN": "",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "INSTAGRAM_IG_USER_ID": "",
        }
        social_env.update(env or {})
        with patch.dict(os.environ, social_env, clear=False), patch.dict(runner.COLLECTORS, collectors, clear=False), patch.object(runner, "score_item", fake_score_item):
            return runner.run_crawl(
                runner.CrawlOptions(
                    config=str(config_path),
                    sources=",".join(sources),
                    limit_per_source=5,
                    top=10,
                    threshold=None,
                    out=str(out_dir),
                    db=str(db_path),
                )
            )

    def test_fixture_e2e_social_sources_outputs_and_effective_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            result = self.run_fixture_crawl(
                tmp,
                {
                    "x_social": {"enabled": True, "category_seed": "gadgets", "fixture_title": "AeroClip pocket fan", "fixture_score": 5.4},
                    "instagram_social": {"enabled": True, "category_seed": "pet", "fixture_title": "PawPure automatic feeder", "fixture_score": 5.6},
                    "reddit": {"enabled": True},
                },
                env={"X_BEARER_TOKEN": "fake-token"},
                threshold=4.0,
            )

            raw_by_source = {item.source: item for item in result.raw_items}
            configured_sources = {"x_social", "instagram_social", "reddit"}
            self.assertEqual(configured_sources, set(result.sources))
            self.assertEqual({"x_social", "reddit"}, set(raw_by_source))
            self.assertEqual(set(), {idea.item.source for idea in result.ideas})
            self.assertEqual(2, len(result.raw_items))
            self.assertEqual(0, len(result.ideas))
            self.assertIn("instagram_social", result.rejection_summary)
            self.assertTrue(any(key.startswith("quality:") for key in result.rejection_summary["instagram_social"]))
            self.assertEqual("official_api", raw_by_source["x_social"].raw["discovery_method"])
            self.assertEqual("x", raw_by_source["x_social"].platform)
            self.assertEqual("canonical_purchase_evidence", raw_by_source["x_social"].raw["evidence_type"])
            self.assertIn("purchase_evidence", raw_by_source["x_social"].raw)
            self.assertIs(False, raw_by_source["x_social"].raw["is_live_real_time"])
            self.assertEqual("gadgets", raw_by_source["x_social"].raw["category_seed"])
            x_purchase = raw_by_source["x_social"].raw["purchase_evidence"][0]
            self.assertEqual({"kind": "fixture_text", "value": "physical portable product"}, raw_by_source["x_social"].raw["physical_signal"])
            self.assertEqual({"views": 8100, "likes": 47, "replies": 8, "reposts": 3}, raw_by_source["x_social"].raw["public_engagement"])
            self.assertEqual("https://link.coupang.com/a/deterministic-fixture", x_purchase["raw_url"])
            self.assertEqual("resolved", x_purchase["resolver"]["status"])
            self.assertTrue(str(x_purchase["dedupe_key"]).startswith("final_url:"))

            reddit_raw = raw_by_source["reddit"].raw
            reddit_purchase = reddit_raw["purchase_evidence"][0]
            self.assertEqual({"views": 1200, "likes": 31, "replies": 5, "reposts": 2}, reddit_raw["public_engagement"])
            self.assertEqual("public_product_page", reddit_purchase["kind"])
            self.assertEqual("https://example.com/products/sol-desk-lamp", reddit_purchase["resolver"]["final_url"])
            self.assertEqual("final_url:https://example.com/products/sol-desk-lamp", reddit_purchase["dedupe_key"])

            ideas_path = Path(result.outputs["ideas_json"])
            ideas = json.loads(ideas_path.read_text(encoding="utf-8"))
            raw_rows = [json.loads(line) for line in Path(result.outputs["raw"]).read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(2, len(raw_rows))
            self.assertEqual({"x_social", "reddit"}, {row["source"] for row in raw_rows})
            self.assertEqual([], ideas)
            with Path(result.outputs["ideas_csv"]).open("r", encoding="utf-8-sig", newline="") as f:
                csv_titles = {row["title"] for row in csv.DictReader(f)}
            digest = Path(result.outputs["digest"]).read_text(encoding="utf-8")
            self.assertEqual(set(), csv_titles)
            self.assertNotIn("AeroClip pocket fan", digest)
            self.assertNotIn("PawPure automatic feeder", digest)
            self.assertNotIn("Sol desk lamp", digest)

    def test_instagram_official_credentials_select_official_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            result = self.run_fixture_crawl(
                Path(tmp_name),
                {"instagram_social": {"enabled": True, "category_seed": "beauty", "fixture_title": "GlowSet skincare fridge"}},
                env={"INSTAGRAM_ACCESS_TOKEN": "fake-token", "INSTAGRAM_IG_USER_ID": "17841400000000000"},
            )

            self.assertEqual("official_api", result.raw_items[0].raw["discovery_method"])
            self.assertEqual("instagram_social", result.raw_items[0].source)
            self.assertEqual("instagram", result.raw_items[0].platform)
            raw = result.raw_items[0].raw
            purchase = raw["purchase_evidence"][0]
            self.assertEqual({"kind": "fixture_text", "value": "physical portable product"}, raw["physical_signal"])
            self.assertEqual({"views": 8100, "likes": 47, "replies": 8, "reposts": 3}, raw["public_engagement"])
            self.assertEqual("canonical_purchase_evidence", raw["evidence_type"])
            self.assertEqual("https://link.coupang.com/a/deterministic-fixture", purchase["raw_url"])
            self.assertEqual("resolved", purchase["resolver"]["status"])
            self.assertEqual(purchase["dedupe_key"], raw["dedupe_key"])

    def test_public_web_provisional_evidence_is_retained_in_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            result = self.run_fixture_crawl(
                Path(tmp_name),
                {
                    "instagram_social": {
                        "enabled": True,
                        "category_seed": "home",
                        "fixture_title": "BreezeBox smart air fan",
                        "published_at": None,
                    }
                },
                env={},
            )

            self.assertEqual([], result.raw_items)
            self.assertEqual([], result.ideas)
            self.assertIn("instagram_social", result.rejection_summary)
            self.assertTrue(any(key.startswith("quality:") for key in result.rejection_summary["instagram_social"]))
            ideas = json.loads(Path(result.outputs["ideas_json"]).read_text(encoding="utf-8"))
            raw_jsonl = Path(result.outputs["raw"]).read_text(encoding="utf-8")
            self.assertEqual([], ideas)
            self.assertEqual("", raw_jsonl)

    def test_exception_and_zero_accepted_social_paths_reuse_recent_cache_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            db_path = tmp / "ideas.sqlite"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            recent_x = strict_live_item(
                "CacheFresh smart bottle",
                source="x_social",
                platform="x",
                published_at="2026-07-09T08:00:00+00:00",
            )
            recent_x.raw["category_seed"] = "fitness"
            recent_x.raw["fixture_score"] = 6.8
            recent_x.fetched_at = (now - timedelta(hours=2)).isoformat()
            recent_ig = strict_live_item(
                "CacheFresh pet camera",
                source="instagram_social",
                platform="instagram",
                published_at="2026-07-09T07:30:00+00:00",
            )
            recent_ig.raw["category_seed"] = "pet"
            recent_ig.raw["fixture_score"] = 6.7
            recent_ig.fetched_at = (now - timedelta(hours=1)).isoformat()
            old_x = strict_live_item(
                "CacheOld kitchen sensor",
                source="x_social",
                platform="x",
                published_at="2026-07-08T06:00:00+00:00",
            )
            old_x.raw["category_seed"] = "kitchen"
            old_x.raw["fixture_score"] = 7.0
            old_x.fetched_at = (now - timedelta(hours=25, minutes=1)).isoformat()
            blocked_x = social_item(
                "x_social",
                "x",
                "JavaScript is disabled in this browser",
                category_seed="gadgets",
                published_at=None,
            )
            blocked_x.text = "Please switch to a supported browser to continue using x.com."
            blocked_x.fetched_at = (now - timedelta(minutes=30)).isoformat()
            store = Store(db_path)
            try:
                store.upsert_items([recent_x, recent_ig, old_x, blocked_x])
            finally:
                store.close()

            config_path = tmp / "sources.json"
            write_config(
                config_path,
                {
                    "x_social": {"enabled": True, "category_seed": "fitness", "fixture_mode": "raise"},
                    "instagram_social": {"enabled": True, "category_seed": "pet", "fixture_mode": "zero_quality"},
                },
            )
            collectors = {"x_social": FakeXCollector, "instagram_social": FakeInstagramCollector}
            progress: list[str] = []
            with patch.dict(runner.COLLECTORS, collectors, clear=False), patch.object(runner, "score_item", fake_score_item):
                result = runner.run_crawl(
                    runner.CrawlOptions(
                        config=str(config_path),
                        sources="x_social,instagram_social",
                        limit_per_source=5,
                        top=10,
                        threshold=None,
                        out=str(tmp / "out"),
                        db=str(db_path),
                    ),
                    on_progress=progress.append,
                )

            titles = {item.title for item in result.raw_items}
            self.assertIn("CacheFresh smart bottle", titles)
            self.assertIn("CacheFresh pet camera", titles)
            self.assertNotIn("CacheOld kitchen sensor", titles)
            self.assertNotIn("JavaScript is disabled in this browser", titles)
            reused = {item.title: item for item in result.raw_items}
            self.assertEqual("2026-07-09T08:00:00+00:00", reused["CacheFresh smart bottle"].published_at)
            self.assertEqual("2026-07-09T07:30:00+00:00", reused["CacheFresh pet camera"].published_at)
            self.assertEqual("cache", reused["CacheFresh smart bottle"].raw["cache_source"])
            self.assertEqual("cache", reused["CacheFresh pet camera"].raw["cache_source"])
            self.assertIn("cache_reused_at", reused["CacheFresh smart bottle"].raw)
            self.assertIn("x_social", result.errors)
            self.assertIn("RuntimeError: simulated x_social collector failure", result.errors["x_social"])
            self.assertTrue(any("x_social" in line and "simulated x_social collector failure" in line for line in progress))
            self.assertTrue(any("instagram_social" in line and "zero_quality_accepted" in line for line in progress))
            self.assertIn("cache_reused_at", reused["CacheFresh pet camera"].raw)
            self.assertEqual("RuntimeError: simulated x_social collector failure", reused["CacheFresh smart bottle"].raw["fallback_reason"])
            self.assertEqual("zero_quality_accepted", reused["CacheFresh pet camera"].raw["fallback_reason"])
            self.assertIs(False, reused["CacheFresh smart bottle"].raw["is_live_real_time"])
            self.assertIs(False, reused["CacheFresh pet camera"].raw["is_live_real_time"])
            self.assertIs(False, reused["CacheFresh smart bottle"].raw["conformant"])
            self.assertIs(False, reused["CacheFresh pet camera"].raw["conformant"])
            self.assertIs(False, reused["CacheFresh smart bottle"].raw["source_evidence_policy_passed"])
            self.assertIs(False, reused["CacheFresh pet camera"].raw["source_evidence_policy_passed"])
            self.assertIs(True, reused["CacheFresh smart bottle"].raw["provisional"])
            self.assertIs(True, reused["CacheFresh pet camera"].raw["provisional"])
            self.assertTrue(reused["CacheFresh smart bottle"].raw["purchase_evidence"])
            self.assertTrue(reused["CacheFresh pet camera"].raw["purchase_evidence"])

    def test_include_rejected_does_not_leak_unverified_social_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config_path = tmp / "sources.json"
            write_config(
                config_path,
                {"x_social": {"enabled": True, "category_seed": "gadgets", "fixture_mode": "zero_quality"}},
            )
            collectors = {"x_social": FakeXCollector}
            with patch.dict(runner.COLLECTORS, collectors, clear=False), patch.object(runner, "score_item", fake_score_item):
                result = runner.run_crawl(
                    runner.CrawlOptions(
                        config=str(config_path),
                        sources="x_social",
                        limit_per_source=5,
                        top=10,
                        threshold=None,
                        out=str(tmp / "out"),
                        db=str(tmp / "ideas.sqlite"),
                        include_rejected=True,
                    )
                )

            titles = {idea.item.title for idea in result.ideas}
            self.assertNotIn("gm legends what are you shipping this week", titles)


    def test_live_acceptance_separates_threads_attempts_from_verified_lane_yields(self) -> None:
        safe_trace = {
            "events": [
                {
                    "action": "navigate",
                    "allowed": True,
                    "method": "GET",
                    "reason": "public_threads_read",
                    "surface": "post",
                    "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                }
            ]
        }
        live_item = strict_live_item("LaneAccount gadget", lane="account")
        idea = fake_score_item(live_item, threshold=4.0)

        live = runner._live_acceptance_metadata(
            [idea],
            {},
            {
                "threads_social": {
                    "lane_attempt_counts": {"account": 2, "keyword": 1, "related_post": 1},
                    "browser_safety_trace": safe_trace,
                }
            },
        )
        self.assertEqual({"account": 1, "keyword": 0, "related_post": 0}, live["threads_verified_lane_counts"])
        self.assertEqual({"account": 2, "keyword": 1, "related_post": 1}, live["threads_lane_attempt_counts"])

        with tempfile.TemporaryDirectory() as tmp_name:
            args = type(
                "Args",
                (),
                {
                    "out": tmp_name,
                    "top": 30,
                    "live_acceptance_summary": None,
                    "zero_write_trace": None,
                    "min_verified_real_time": 1,
                    "require_threads_lanes": True,
                },
            )()
            result = type(
                "Result",
                (),
                {
                    "ideas": [idea],
                    "live_acceptance": live,
                    "rejection_summary": {},
                    "outputs": {},
                    "db": str(Path(tmp_name) / "ideas.sqlite"),
                    "collector_diagnostics": {"threads_social": {"browser_safety_trace": safe_trace}},
                    "raw_items": [live_item],
                },
            )()
            summary, exit_code = cli._write_live_acceptance_summary(args, result, {})
            self.assertEqual(0, exit_code)
            self.assertEqual("completed", summary["status"])
            self.assertEqual({"account": 1, "keyword": 0, "related_post": 0}, summary["threads_verified_lane_counts"])
            self.assertEqual({"account": 2, "keyword": 1, "related_post": 1}, summary["threads_lane_attempt_counts"])

    def test_acceptance_gate_blocks_on_aggregate_min_and_lane_attempts_only(self) -> None:
        options = runner.CrawlOptions(
            min_verified_real_time=30,
            require_threads_lanes=True,
        )

        blocked = runner._acceptance_gate(
            options,
            {
                "verified_real_time_count": 29,
                "threads_verified_lane_counts": {"account": 29, "keyword": 0, "related_post": 0},
                "threads_lane_attempt_counts": {"account": 1, "keyword": 0, "related_post": 0},
            },
        )
        self.assertEqual("blocked", blocked["status"])
        self.assertIn("verified_real_time_count 29 < min_verified_real_time 30", blocked["failures"])
        self.assertIn("missing Threads lane attempts: keyword, related_post", blocked["failures"])
        self.assertNotIn("verified yield", "\n".join(blocked["failures"]))

        completed = runner._acceptance_gate(
            options,
            {
                "verified_real_time_count": 30,
                "threads_verified_lane_counts": {"account": 30, "keyword": 0, "related_post": 0},
                "threads_lane_attempt_counts": {"account": 1, "keyword": 1, "related_post": 1},
            },
        )
        self.assertEqual({"status": "completed", "required": {"min_verified_real_time": 30, "require_threads_lanes": True}, "failures": [], "missing_threads_lane_attempts": []}, completed)

    def test_runner_captures_threads_collector_diagnostics_by_source_alias(self) -> None:
        safe_trace = {
            "events": [
                {
                    "action": "navigate",
                    "allowed": True,
                    "method": "GET",
                    "reason": "public_threads_read",
                    "surface": "post",
                    "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp_name:
            result = self.run_fixture_crawl(
                Path(tmp_name),
                {
                    "threads_social": {
                        "enabled": True,
                        "category_seed": "gadgets",
                        "fixture_title": "TraceCaptured gadget",
                        "last_run_diagnostics": {
                            "lane_attempt_counts": {"account": 1, "keyword": 1, "related_post": 1},
                            "browser_safety_trace": safe_trace,
                            "session": {"isolated": True, "profile": "temporary"},
                        },
                        "extra_raw": {
                            "is_live_real_time": True,
                            "live_acquisition": True,
                            "provisional": False,
                            "discovery_lane": "account",
                        },
                    }
                },
            )

        diagnostics = result.collector_diagnostics or {}
        self.assertIn("threads_social", diagnostics)
        self.assertEqual({"account": 1, "keyword": 1, "related_post": 1}, result.live_acceptance["threads_lane_attempt_counts"])  # type: ignore[index]
        self.assertEqual(safe_trace, diagnostics["threads_social"]["browser_safety_trace"])  # type: ignore[index]

    def test_threads_zero_write_trace_requires_actual_collector_evidence(self) -> None:
        missing_summary, missing_failures = cli._threads_zero_write_trace(type("Result", (), {"raw_items": []})())
        self.assertEqual("missing_browser_safety_trace", missing_summary["status"])
        self.assertIn("missing collector-level Threads browser_safety_trace evidence", missing_failures)

        safe_item = social_item(
            "threads_social",
            "threads",
            "TraceSafe desk gadget",
            category_seed="gadgets",
            extra_raw={
                "is_live_real_time": True,
                "live_acquisition": True,
                "browser_safety_trace": {
                    "conformant": True,
                    "events": [
                        {
                            "action": "navigate",
                            "allowed": True,
                            "method": "GET",
                            "reason": "public_threads_read",
                            "surface": "post",
                            "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                        }
                    ],
                }
            },
        )
        safe_summary, safe_failures = cli._threads_zero_write_trace(type("Result", (), {"raw_items": [safe_item]})())
        self.assertEqual("diagnostic_legacy_row_trace_only", safe_summary["status"])
        self.assertIn("missing collector-level Threads browser_safety_trace evidence", safe_failures)
        collector_summary, collector_failures = cli._threads_zero_write_trace(
            type(
                "Result",
                (),
                {
                    "raw_items": [safe_item],
                    "collector_diagnostics": {"threads_social": {"browser_safety_trace": safe_item.raw["browser_safety_trace"]}},
                },
            )()
        )
        self.assertEqual("zero_public_writes_observed", collector_summary["status"])
        self.assertEqual([], collector_failures)
        self.assertEqual(1, len(collector_summary["events"]))
        self.assertIn("collector_diagnostics", collector_summary["evidence"])

        missing_collector_summary, missing_collector_failures = cli._threads_zero_write_trace(
            type(
                "Result",
                (),
                {
                    "raw_items": [safe_item],
                    "collector_diagnostics": {"threads_social": {}},
                },
            )()
        )
        self.assertEqual("missing_browser_safety_trace", missing_collector_summary["status"])
        self.assertIn("missing collector-level Threads browser_safety_trace evidence", missing_collector_failures)

        compact_item = social_item(
            "threads_social",
            "threads",
            "TraceCompact desk gadget",
            category_seed="gadgets",
            extra_raw={
                "is_live_real_time": True,
                "live_acquisition": True,
                "browser_safety_trace": {
                    "allowed_events": 2,
                    "blocked_events": 0,
                    "public_write_actions": 0,
                    "private_content_actions": 0,
                },
            },
        )
        compact_summary, compact_failures = cli._threads_zero_write_trace(
            type("Result", (), {"raw_items": [compact_item], "collector_diagnostics": {"threads_social": {}}})()
        )
        self.assertEqual("missing_browser_safety_trace", compact_summary["status"])
        self.assertIn("missing collector-level Threads browser_safety_trace evidence", compact_failures)


        unsafe_item = social_item(
            "threads_social",
            "threads",
            "TraceUnsafe desk gadget",
            category_seed="gadgets",
            extra_raw={
                "is_live_real_time": True,
                "live_acquisition": True,
                "browser_safety_trace": {
                    "conformant": False,
                    "events": [
                        {
                            "action": "request",
                            "allowed": False,
                            "method": "POST",
                            "reason": "blocked_non_read_method",
                            "surface": "unknown",
                            "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                        }
                    ],
                }
            },
        )
        unsafe_summary, unsafe_failures = cli._threads_zero_write_trace(type("Result", (), {"raw_items": [unsafe_item]})())
        self.assertEqual("diagnostic_legacy_row_trace_only", unsafe_summary["status"])
        self.assertIn("missing collector-level Threads browser_safety_trace evidence", unsafe_failures)
        self.assertEqual(1, unsafe_summary["public_write_actions"])
        unsafe_collector_summary, unsafe_collector_failures = cli._threads_zero_write_trace(
            type(
                "Result",
                (),
                {
                    "raw_items": [],
                    "collector_diagnostics": {"threads_social": {"browser_safety_trace": unsafe_item.raw["browser_safety_trace"]}},
                },
            )()
        )
        self.assertEqual("unsafe_threads_browser_event_observed", unsafe_collector_summary["status"])
        self.assertIn("unsafe Threads browser_safety_trace event observed", unsafe_collector_failures)


    def test_headless_acceptance_writes_grounded_thirty_row_verified_artifacts(self) -> None:
        safe_trace = {
            "conformant": True,
            "public_only": True,
            "events": [
                {
                    "action": "navigate",
                    "allowed": True,
                    "method": "GET",
                    "reason": "public_threads_read",
                    "surface": "post",
                    "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                }
            ],
        }
        ideas = live_acceptance_ideas()
        live = runner._live_acceptance_metadata(
            ideas,
            {},
            {"threads_social": {"lane_attempt_counts": {"account": 11, "keyword": 10, "related_post": 1}, "browser_safety_trace": safe_trace}},
        )
        self.assertEqual(30, live["verified_real_time_count"])
        self.assertEqual({"threads_social": 21, "producthunt": 4, "kickstarter": 5}, live["real_time_source_counts"])
        self.assertEqual({"account": 21, "keyword": 0, "related_post": 0}, live["threads_verified_lane_counts"])
        self.assertEqual({"account": 11, "keyword": 10, "related_post": 1}, live["threads_lane_attempt_counts"])
        dedupe_keys = [idea.item.raw["dedupe_key"] for idea in ideas]
        self.assertEqual(30, len(set(dedupe_keys)))
        self.assertTrue(all(str(key).startswith("final_url:") for key in dedupe_keys))

        moel = next(idea for idea in ideas if idea.item.url == "https://www.threads.net/@moel.pick/post/DarhfjSE24O")
        moel_purchase = moel.item.raw["purchase_evidence"][0]
        self.assertEqual("https://www.threads.net/@moel.pick/post/DarhfjSE24O", moel.item.raw["original_post_url"])
        self.assertEqual("https://link.coupang.com/a/fjMZjJ4Wnk", moel_purchase["raw_url"])
        self.assertEqual("https://coupang.com/vp/products/7806683392?itemId=21159396070", moel_purchase["resolver"]["final_url"])
        self.assertEqual("final_url:https://coupang.com/vp/products/7806683392?itemId=21159396070", moel.item.raw["dedupe_key"])
        self.assertEqual(moel.item.raw["dedupe_key"], moel_purchase["dedupe_key"])
        self.assertEqual("same_author_affiliate_followup", moel_purchase["kind"])
        self.assertEqual("threads_public_ssr", moel_purchase["provenance"]["source"])
        self.assertEqual("moel.pick", moel_purchase["provenance"]["author"])
        self.assertIs(True, moel_purchase["provenance"]["public_only"])
        self.assertIs(True, moel.item.raw["affiliate"]["same_author"])
        self.assertIs(True, moel.item.raw["affiliate"]["present"])
        self.assertGreater(float(moel.item.metrics["views"]), 0)
        self.assertGreaterEqual(float(moel.item.metrics["likes"]), 0)
        self.assertEqual(2, moel.item.raw["schema_version"])
        self.assertEqual("resolved_http_restricted", moel_purchase["resolver"]["status"])
        self.assertEqual("resolved_http_restricted", moel.item.raw["purchase_resolver_status"])
        self.assertEqual("final_url", moel.item.raw["dedupe_key_namespace"])
        self.assertIs(True, moel.item.raw["physical_signal"])
        trace = moel.item.raw["browser_safety_trace"]
        self.assertIs(True, trace["conformant"])
        self.assertGreater(int(trace["event_count"]), 0)
        self.assertIs(True, moel.item.raw["source_evidence_policy_passed"])
        self.assertIs(False, moel.item.raw["is_fixture"])
        self.assertIs(False, moel.item.raw["is_cache_fallback"])
        self.assertNotIn("cache_source", moel.item.raw)
        self.assertFalse(moel.item.raw.get("fixture"))
        self.assertFalse(moel.item.raw.get("provisional"))

        producthunt = next(idea for idea in ideas if idea.item.source == "producthunt")
        producthunt_purchase = producthunt.item.raw["purchase_evidence"][0]
        self.assertEqual(clean_final_url(producthunt.item.url), producthunt_purchase["raw_url"])
        self.assertEqual(clean_final_url(producthunt.item.url), producthunt_purchase["resolver"]["final_url"])
        self.assertEqual(producthunt.item.raw["dedupe_key"], producthunt_purchase["dedupe_key"])
        self.assertEqual("producthunt_product_page", producthunt_purchase["provenance"]["source"])
        self.assertIs(True, producthunt_purchase["provenance"]["canonical_product_page"])

        kickstarter = next(idea for idea in ideas if idea.item.source == "kickstarter")
        kickstarter_purchase = kickstarter.item.raw["purchase_evidence"][0]
        self.assertEqual(clean_final_url(kickstarter.item.url), kickstarter_purchase["raw_url"])
        self.assertEqual(clean_final_url(kickstarter.item.url), kickstarter_purchase["resolver"]["final_url"])
        self.assertEqual(kickstarter.item.raw["dedupe_key"], kickstarter_purchase["dedupe_key"])
        self.assertEqual("kickstarter_product_page", kickstarter_purchase["provenance"]["source"])
        self.assertIs(True, kickstarter_purchase["provenance"]["canonical_product_page"])

        with tempfile.TemporaryDirectory() as tmp_name:
            out = Path(tmp_name)
            legacy_ideas = out / "ideas.json"
            legacy_csv = out / "ideas.csv"
            legacy_ideas.write_text("legacy ideas", encoding="utf-8")
            legacy_csv.write_text("legacy csv", encoding="utf-8")
            args = type(
                "Args",
                (),
                {
                    "out": str(out),
                    "top": 30,
                    "live_acceptance_summary": None,
                    "zero_write_trace": None,
                    "min_verified_real_time": 30,
                    "require_threads_lanes": True,
                },
            )()
            result = type(
                "Result",
                (),
                {
                    "ideas": ideas,
                    "live_acceptance": live,
                    "rejection_summary": {},
                    "outputs": {"ideas_json": str(legacy_ideas), "ideas_csv": str(legacy_csv)},
                    "db": str(out / "ideas.sqlite"),
                    "collector_diagnostics": {
                        "threads_social": {
                            "browser_safety_trace": safe_trace,
                            "session": {"mode": "anonymous", "live_profile_used_directly": False, "service_workers": "blocked"},
                        }
                    },
                    "raw_items": [idea.item for idea in ideas],
                },
            )()

            summary, exit_code = cli._write_live_acceptance_summary(args, result, {})

            self.assertEqual(0, exit_code)
            self.assertEqual("completed", summary["status"])
            self.assertEqual(30, summary["verified_real_time_count"])
            self.assertEqual({"account": 11, "keyword": 10, "related_post": 1}, summary["threads_lane_attempt_counts"])
            self.assertEqual(0, summary["fixture_cache_non_live_rows_excluded"])
            self.assertEqual(str(out / "threads_safety_trace.json"), summary["artifacts"]["threads_safety_trace"])
            self.assertEqual("legacy ideas", legacy_ideas.read_text(encoding="utf-8"))
            self.assertEqual("legacy csv", legacy_csv.read_text(encoding="utf-8"))

            verified = json.loads((out / "verified.json").read_text(encoding="utf-8"))
            self.assertEqual(30, len(verified))
            with (out / "verified.csv").open("r", encoding="utf-8-sig", newline="") as f:
                self.assertEqual(30, sum(1 for _ in csv.DictReader(f)))
            digest = (out / "digest.md").read_text(encoding="utf-8")
            self.assertEqual(30, digest.count("## "))
            self.assertNotIn("example.com/live", json.dumps(verified, ensure_ascii=False))
            self.assertNotIn("link.example.test", json.dumps(verified, ensure_ascii=False))
            trace = json.loads((out / "threads_safety_trace.json").read_text(encoding="utf-8"))
            self.assertEqual("playwright", trace["browser_engine"])
            self.assertEqual("anonymous", trace["session_mode"])
            self.assertIs(False, trace["live_profile_used_directly"])
            self.assertEqual("blocked", trace["service_workers"])
            self.assertIs(True, trace["public_only"])
            self.assertIs(True, trace["conformant"])
            self.assertEqual([], trace["public_write_attempts"])
            self.assertEqual([], trace["private_content_attempts"])

    def test_headless_acceptance_blocks_missing_or_unsafe_threads_trace(self) -> None:
        live_item = strict_live_item("Trace gate gadget", lane="account")
        idea = fake_score_item(live_item, threshold=4.0)
        live = runner._live_acceptance_metadata(
            [idea],
            {},
            {"threads_social": {"lane_attempt_counts": {"account": 1, "keyword": 1, "related_post": 1}}},
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            args = type(
                "Args",
                (),
                {
                    "out": tmp_name,
                    "top": 30,
                    "live_acceptance_summary": None,
                    "zero_write_trace": None,
                    "min_verified_real_time": 1,
                    "require_threads_lanes": True,
                },
            )()
            result = type(
                "Result",
                (),
                {
                    "ideas": [idea],
                    "live_acceptance": live,
                    "rejection_summary": {},
                    "outputs": {},
                    "db": str(Path(tmp_name) / "ideas.sqlite"),
                    "collector_diagnostics": {"threads_social": {}},
                    "raw_items": [live_item],
                },
            )()
            summary, exit_code = cli._write_live_acceptance_summary(args, result, {})
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", summary["status"])
            self.assertIn("missing collector-level Threads browser_safety_trace evidence", summary["failures"])

            unsafe_trace = {
                "events": [
                    {
                        "action": "request",
                        "allowed": False,
                        "method": "POST",
                        "reason": "blocked_non_read_method",
                        "surface": "unknown",
                        "url": "https://www.threads.com/@moel.pick/post/DarhfjSE24O",
                    }
                ]
            }
            result.collector_diagnostics = {"threads_social": {"browser_safety_trace": unsafe_trace}}
            summary, exit_code = cli._write_live_acceptance_summary(args, result, {})
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked", summary["status"])
            self.assertIn("unsafe Threads browser_safety_trace event observed", summary["failures"])
            self.assertEqual(1, summary["verified_real_time_count"])
            self.assertEqual([], [failure for failure in summary["failures"] if "verified yield" in failure])
            self.assertNotIn("missing Threads lane attempts", "\n".join(summary["failures"]))

    def test_frozen_relative_config_resolves_under_meipass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            bundled_config = tmp / "config" / "sources.json"
            bundled_config.parent.mkdir(parents=True)
            write_config(
                bundled_config,
                {"threads_social": {"enabled": True, "seed_accounts": [{"username": "bundled.seed", "public_only": True}]}},
                threshold=8.5,
            )
            old_frozen = getattr(sys, "frozen", None)
            old_meipass = getattr(sys, "_MEIPASS", None)
            had_frozen = hasattr(sys, "frozen")
            had_meipass = hasattr(sys, "_MEIPASS")
            old_cwd = Path.cwd()
            try:
                sys.frozen = True  # type: ignore[attr-defined]
                sys._MEIPASS = str(tmp)  # type: ignore[attr-defined]
                with tempfile.TemporaryDirectory() as cwd_name:
                    try:
                        os.chdir(cwd_name)
                        config = load_config("config/sources.json")
                    finally:
                        os.chdir(old_cwd)
                self.assertEqual(8.5, config["scoring"]["threshold"])
                self.assertIn("threads_social", config["_source_names"])
                self.assertEqual("bundled.seed", config["sources"]["threads_social"]["seed_accounts"][0]["username"])
            finally:
                os.chdir(old_cwd)
                if had_frozen:
                    sys.frozen = old_frozen  # type: ignore[attr-defined]
                else:
                    delattr(sys, "frozen")
                if had_meipass:
                    sys._MEIPASS = old_meipass  # type: ignore[attr-defined]
                else:
                    delattr(sys, "_MEIPASS")

    def test_installed_layout_relative_config_resolves_from_setuptools_data_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name, tempfile.TemporaryDirectory() as cwd_name:
            tmp = Path(tmp_name)
            installed_config = tmp / "share" / "product_idea_radar" / "config" / "sources.json"
            installed_config.parent.mkdir(parents=True)
            write_config(
                installed_config,
                {"threads_social": {"enabled": True, "seed_accounts": [{"username": "installed.seed", "public_only": True}]}},
                threshold=9.1,
            )
            pyproject = tomllib.loads(Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8"))
            self.assertEqual(["config/sources.json"], pyproject["tool"]["setuptools"]["data-files"]["share/product_idea_radar/config"])

            old_prefix = sys.prefix
            old_cwd = Path.cwd()
            try:
                sys.prefix = str(tmp)
                os.chdir(cwd_name)
                self.assertFalse(Path("config/sources.json").exists())
                config = load_config("config/sources.json")
                self.assertEqual(9.1, config["scoring"]["threshold"])
                self.assertEqual("installed.seed", config["sources"]["threads_social"]["seed_accounts"][0]["username"])
                with self.assertRaises(FileNotFoundError):
                    load_config(str(Path(cwd_name) / "missing.json"))
            finally:
                sys.prefix = old_prefix
                os.chdir(old_cwd)

if __name__ == "__main__":
    unittest.main()
