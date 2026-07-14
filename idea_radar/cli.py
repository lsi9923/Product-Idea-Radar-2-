from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import os
import sys
from typing import Any

from .fetcher import InsaneSearchFetcher
from .digest import render_digest, write_ideas_csv, write_ideas_json
from .dedupe import deterministic_idea_sort_key
from .runner import CrawlOptions, _is_fixture_cache_or_degraded, _is_verified_real_time_item, run_crawl


FROZEN_APP_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ProductIdeaRadar"
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _resolve_frozen_writable_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if not getattr(sys, "frozen", False) or path.is_absolute():
        return str(path)
    FROZEN_APP_ROOT.mkdir(parents=True, exist_ok=True)
    return str(FROZEN_APP_ROOT / path)


def _resolve_frozen_config_path(value: str) -> str:
    path = Path(value)
    if getattr(sys, "frozen", False) and not path.is_absolute() and value == "config/sources.json":
        return str(BUNDLE_ROOT / path)
    return str(path)


def _verified_artifact_sort_key(idea: Any) -> tuple[float, float, str, str]:
    timestamp, engagement, _score, stable_title, stable_id = deterministic_idea_sort_key(idea)
    return (timestamp, engagement, stable_title, stable_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="idea-radar", description="Public product-idea crawler using insane-search")
    sub = parser.add_subparsers(dest="cmd", required=True)

    crawl = sub.add_parser("crawl", help="Collect, score, dedupe, store, and export ideas")
    crawl.add_argument("--config", default="config/sources.json", help="JSON source config path")
    crawl.add_argument(
        "--sources",
        default="producthunt,kickstarter,reddit,x_social,instagram_social,threads_social",
        help=(
            "Comma-separated source names or 'all' (default: "
            "producthunt,kickstarter,reddit,x_social,instagram_social,threads_social). "
            "X_BEARER_TOKEN for X; INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_IG_USER_ID for "
            "Instagram. Missing credentials fall back to provisional public-web collection."
        ),
    )
    crawl.add_argument("--limit-per-source", type=int, default=20)
    crawl.add_argument("--top", type=int, default=30)
    crawl.add_argument("--threshold", type=float, default=None)
    crawl.add_argument("--out", default="runs/latest")
    crawl.add_argument("--db", default="data/ideas.sqlite")
    crawl.add_argument("--insane-search-dir", default=None)
    crawl.add_argument("--timeout", type=int, default=40)
    crawl.add_argument("--trace-fetch", action="store_true")
    crawl.add_argument("--include-rejected", action="store_true", help="Compatibility flag; verified exports still exclude rejected rows")
    crawl.add_argument("--headless-live-acceptance", action="store_true", help="Write live acceptance summary and fail when requested acceptance gates fail")
    crawl.add_argument("--min-verified-real-time", type=int, default=0, help="Minimum verified non-fixture/non-cache real-time rows required")
    crawl.add_argument("--require-threads-lanes", action="store_true", help="Require account, keyword, and related_post Threads lanes to be attempted; verified yield may be zero per lane")
    crawl.add_argument("--live-acceptance-summary", "--summary-artifact", dest="live_acceptance_summary", default=None, help="Path for live_acceptance_summary.json (default: OUT/live_acceptance_summary.json)")
    crawl.add_argument("--zero-write-trace", default=None, help="Path for zero-write trace JSON (default with headless acceptance: OUT/threads_safety_trace.json)")

    fetch = sub.add_parser("fetch-url", help="Fetch one public URL through insane-search")
    fetch.add_argument("url")
    fetch.add_argument("--insane-search-dir", default=None)
    fetch.add_argument("--timeout", type=int, default=40)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "fetch-url":
        fetcher = InsaneSearchFetcher(skill_dir=args.insane_search_dir, timeout=args.timeout, trace=True)
        result = fetcher.fetch(args.url)
        print(json.dumps({"ok": result.ok, "verdict": result.verdict, "attempts": result.attempts, "content_preview": result.content[:2000]}, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.cmd == "crawl":
        return crawl(args)
    return 2


def crawl(args: argparse.Namespace) -> int:
    out_path = _resolve_frozen_writable_path(args.out) or args.out
    db_path = _resolve_frozen_writable_path(args.db) or args.db
    if getattr(sys, "frozen", False):
        args.out = out_path
        args.db = db_path

    result = run_crawl(
        CrawlOptions(
            config=_resolve_frozen_config_path(args.config),
            sources=args.sources,
            limit_per_source=args.limit_per_source,
            top=args.top,
            threshold=args.threshold,
            out=out_path,
            db=db_path,
            insane_search_dir=args.insane_search_dir,
            timeout=args.timeout,
            trace_fetch=args.trace_fetch,
            include_rejected=args.include_rejected,
            min_verified_real_time=args.min_verified_real_time,
            require_threads_lanes=args.require_threads_lanes,
        )
    )
    summary: dict[str, Any] = {
        "sources": result.sources,
        "raw_items": len(result.raw_items),
        "ideas": len(result.ideas),
        "outputs": result.outputs,
        "db": result.db,
        "errors": result.errors,
        "insane_search_dir": result.insane_search_dir,
        "live_acceptance": result.live_acceptance or {},
        "rejection_summary": result.rejection_summary or {},
    }
    exit_code = 0
    if args.headless_live_acceptance:
        acceptance_summary, exit_code = _write_live_acceptance_summary(args, result, summary)
        summary["live_acceptance_summary"] = acceptance_summary
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def _write_live_acceptance_summary(
    args: argparse.Namespace,
    result: Any,
    crawl_summary: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.live_acceptance_summary) if args.live_acceptance_summary else out_dir / "live_acceptance_summary.json"
    zero_write_path = Path(args.zero_write_trace) if args.zero_write_trace else out_dir / "threads_safety_trace.json"
    zero_write_path.parent.mkdir(parents=True, exist_ok=True)
    zero_write_trace, trace_failures = _threads_zero_write_trace(result)
    zero_write_trace["run_id"] = out_dir.name
    zero_write_trace["output_dir"] = str(out_dir)
    zero_write_trace["db"] = result.db
    zero_write_path.write_text(json.dumps(zero_write_trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    verified_ideas = _strict_live_ideas(result)
    verified_json_path = out_dir / "verified.json"
    verified_csv_path = out_dir / "verified.csv"
    verified_digest_path = out_dir / "digest.md"
    write_ideas_json(verified_ideas, verified_json_path)
    write_ideas_csv(verified_ideas, verified_csv_path)
    verified_digest_path.write_text(render_digest(verified_ideas, top=int(getattr(args, "top", 30))), encoding="utf-8")
    verified_artifact_count = len(json.loads(verified_json_path.read_text(encoding="utf-8")))
    with verified_csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        verified_csv_count = sum(1 for _ in csv.DictReader(f))
    fixture_cache_non_live_excluded = _fixture_cache_non_live_exclusion_count(result, verified_ideas)

    live = result.live_acceptance or {}
    verified_lane_counts = live.get("threads_verified_lane_counts") or live.get("threads_lane_counts") or {}
    lane_attempt_counts = live.get("threads_lane_attempt_counts") or live.get("threads_lane_counts") or {}
    missing_lanes = [
        lane
        for lane in ["account", "keyword", "related_post"]
        if int(lane_attempt_counts.get(lane) or 0) <= 0
    ]
    strict_verified_real_time_count = len(verified_ideas)
    metadata_verified_real_time_count = int(live.get("verified_real_time_count") or 0)
    failures: list[str] = list(trace_failures)
    if strict_verified_real_time_count != metadata_verified_real_time_count:
        failures.append(
            f"strict verified artifact count {strict_verified_real_time_count} != metadata verified_real_time_count {metadata_verified_real_time_count}"
        )
    if verified_artifact_count != strict_verified_real_time_count:
        failures.append(
            f"verified.json row count {verified_artifact_count} != strict verified count {strict_verified_real_time_count}"
        )
    if verified_csv_count != strict_verified_real_time_count:
        failures.append(
            f"verified.csv row count {verified_csv_count} != strict verified count {strict_verified_real_time_count}"
        )
    if strict_verified_real_time_count < args.min_verified_real_time:
        failures.append(
            f"verified_real_time_count {strict_verified_real_time_count} < min_verified_real_time {args.min_verified_real_time}"
        )
    if args.require_threads_lanes and missing_lanes:
        failures.append("missing Threads lane attempts: " + ", ".join(missing_lanes))

    acceptance_summary = {
        "status": "completed" if not failures else "blocked",
        "required": {
            "min_verified_real_time": args.min_verified_real_time,
            "require_threads_lanes": bool(args.require_threads_lanes),
        },
        "threads_contract": "All three Threads lanes must be attempted when required; verified yield may be zero for any attempted lane.",
        "verified_real_time_count": strict_verified_real_time_count,
        "threads_lane_counts": {
            "account": int(verified_lane_counts.get("account") or 0),
            "keyword": int(verified_lane_counts.get("keyword") or 0),
            "related_post": int(verified_lane_counts.get("related_post") or 0),
        },
        "threads_verified_yield_counts": {
            "account": int(verified_lane_counts.get("account") or 0),
            "keyword": int(verified_lane_counts.get("keyword") or 0),
            "related_post": int(verified_lane_counts.get("related_post") or 0),
        },
        "threads_verified_lane_counts": {
            "account": int(verified_lane_counts.get("account") or 0),
            "keyword": int(verified_lane_counts.get("keyword") or 0),
            "related_post": int(verified_lane_counts.get("related_post") or 0),
        },
        "threads_lane_attempt_counts": {
            "account": int(lane_attempt_counts.get("account") or 0),
            "keyword": int(lane_attempt_counts.get("keyword") or 0),
            "related_post": int(lane_attempt_counts.get("related_post") or 0),
        },
        "source_counts": live.get("source_counts") or {},
        "real_time_source_counts": live.get("real_time_source_counts") or {},
        "rejection_summary": live.get("rejection_summary") or result.rejection_summary or {},
        "fixture_cache_non_live_rows_excluded": fixture_cache_non_live_excluded,
        "artifacts": {
            "verified_json": str(verified_json_path),
            "verified_csv": str(verified_csv_path),
            "digest": str(verified_digest_path),
            "threads_safety_trace": str(zero_write_path),
        },
        "zero_write_trace": str(zero_write_path),
        "failures": failures,
        "outputs": {**result.outputs, "verified_json": str(verified_json_path), "verified_csv": str(verified_csv_path), "digest": str(verified_digest_path)},
        "db": result.db,
        "crawl": dict(crawl_summary),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(acceptance_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**acceptance_summary, "path": str(summary_path)}, 0 if not failures else 1

def _strict_live_ideas(result: Any) -> list[Any]:
    return sorted(
        [idea for idea in getattr(result, "ideas", []) or [] if _is_verified_real_time_item(idea.item)],
        key=_verified_artifact_sort_key,
        reverse=True,
    )


def _fixture_cache_non_live_exclusion_count(result: Any, verified_ideas: list[Any]) -> int:
    verified_ids = {id(idea) for idea in verified_ideas}
    count = 0
    for idea in getattr(result, "ideas", []) or []:
        if id(idea) in verified_ids:
            continue
        raw = idea.item.raw if isinstance(idea.item.raw, dict) else {}
        if raw.get("is_live_real_time") is False or _is_fixture_cache_or_degraded(raw) or not _is_verified_real_time_item(idea.item):
            count += 1
    return count


def _threads_zero_write_trace(result: Any) -> tuple[dict[str, Any], list[str]]:
    collector_diagnostics = getattr(result, "collector_diagnostics", None)
    if collector_diagnostics is not None:
        threads_diagnostics = collector_diagnostics.get("threads_social") if isinstance(collector_diagnostics, dict) else None
        trace = threads_diagnostics.get("browser_safety_trace") if isinstance(threads_diagnostics, dict) else None
        if not isinstance(trace, dict) or not isinstance(trace.get("events"), list) or not trace.get("events"):
            return _missing_threads_trace("No collector-level Threads browser_safety_trace with genuine events was captured.")
        return _summarize_threads_trace(
            [trace],
            "Captured from collector_diagnostics['threads_social']['browser_safety_trace'].",
            metadata=_threads_trace_metadata(trace, threads_diagnostics),
        )

    diagnostic_trace, diagnostic_failures = _legacy_threads_trace_diagnostics(result)
    if diagnostic_trace is not None:
        return diagnostic_trace, diagnostic_failures
    return _missing_threads_trace("No collector-level live Threads browser_safety_trace with genuine events was captured.")


def _legacy_threads_trace_diagnostics(result: Any) -> tuple[dict[str, Any], list[str]] | tuple[None, list[str]]:
    traces: list[dict[str, Any]] = []
    compact_summaries: list[dict[str, Any]] = []
    for item in getattr(result, "raw_items", []) or []:
        if getattr(item, "source", "") != "threads_social" and getattr(item, "platform", "") != "threads":
            continue
        raw = getattr(item, "raw", {}) or {}
        trace = raw.get("browser_safety_trace")
        if raw.get("is_live_real_time") is not True and raw.get("live_acquisition") is not True:
            continue
        if not isinstance(trace, dict):
            continue
        events = trace.get("events")
        if isinstance(events, list) and events:
            traces.append(trace)
        else:
            compact_summaries.append(trace)

    if traces:
        summary, failures = _summarize_threads_trace(
            traces,
            "Diagnostic only: legacy collected Threads row browser_safety_trace events are not collector-level strict acceptance proof.",
            metadata=_threads_trace_metadata(traces[0], None),
        )
        summary["status"] = "diagnostic_legacy_row_trace_only"
        return summary, [*failures, "missing collector-level Threads browser_safety_trace evidence"]
    if compact_summaries:
        summary, failures = _summarize_compact_threads_traces(compact_summaries)
        return summary, [*failures, "missing collector-level Threads browser_safety_trace evidence"]
    return None, []


def _missing_threads_trace(evidence: str) -> tuple[dict[str, Any], list[str]]:
    return (
        {
            "status": "missing_browser_safety_trace",
            "mutations": [],
            "mutation_attempts": [],
            "mutation_attempt_count": 0,
            "public_write_attempts": [],
            "public_write_actions": 0,
            "private_content_actions": 0,
            "private_content_attempts": [],
            "unsafe_events": [],
            "allowed_events": 0,
            "blocked_events": 0,
            "events": [],
            "evidence": evidence,
        },
        ["missing collector-level Threads browser_safety_trace evidence"],
    )


def _summarize_threads_trace(traces: list[dict[str, Any]], evidence: str, *, metadata: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace in traces:
        for event in trace.get("events") or []:
            if not isinstance(event, dict):
                continue
            key = json.dumps(event, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            events.append(dict(event))
    return _build_threads_trace_summary(events, evidence=evidence, metadata=metadata or {})


def _threads_trace_metadata(trace: dict[str, Any], diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if diagnostics is not None:
        metadata["browser_engine"] = "playwright"
    session = diagnostics.get("session") if isinstance(diagnostics, dict) and isinstance(diagnostics.get("session"), dict) else {}
    trace_session = trace.get("session") if isinstance(trace.get("session"), dict) else {}
    for key in ["session_mode", "service_workers", "live_profile_used_directly", "public_only"]:
        value = trace.get(key)
        if value is None and trace_session:
            value = trace_session.get(key)
        if value is None and session:
            session_key = "mode" if key == "session_mode" else key
            value = session.get(session_key)
        if value is not None:
            metadata[key] = value
    if trace.get("conformant") is not None:
        metadata["conformant"] = trace.get("conformant")
    return metadata



def _summarize_compact_threads_traces(traces: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    unsafe_events = [
        event
        for trace in traces
        for event in (trace.get("unsafe_events") or [])
        if isinstance(event, dict)
    ]
    public_write_actions = sum(int(trace.get("public_write_actions") or 0) for trace in traces)
    private_content_actions = sum(int(trace.get("private_content_actions") or 0) for trace in traces)
    unsafe_count = public_write_actions + private_content_actions + len(unsafe_events)
    allowed_events = sum(int(trace.get("allowed_events") or 0) for trace in traces)
    blocked_events = sum(int(trace.get("blocked_events") or 0) for trace in traces)
    private_content_attempts = [
        event
        for trace in traces
        for event in (trace.get("private_content_attempts") or [])
        if isinstance(event, dict)
    ]
    summary = {
        "status": "zero_public_writes_observed" if unsafe_count <= 0 else "unsafe_threads_browser_event_observed",
        "mutations": unsafe_events,
        "mutation_attempts": unsafe_events,
        "mutation_attempt_count": len(unsafe_events),
        "public_write_attempts": unsafe_events,
        "public_write_actions": public_write_actions,
        "private_content_attempts": private_content_attempts,
        "private_content_actions": private_content_actions,
        "unsafe_events": unsafe_events,
        "allowed_events": allowed_events,
        "blocked_events": blocked_events,
        "events": [],
        "evidence": "Diagnostic only: compact legacy Threads row browser_safety_trace summaries are not collector-level strict acceptance proof.",
    }
    failures = ["legacy compact Threads browser_safety_trace is diagnostic only, not strict acceptance proof"]
    return summary, failures


def _build_threads_trace_summary(events: list[dict[str, Any]], *, evidence: str, metadata: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    unsafe_events = [
        event
        for event in events
        if event.get("unsafe") is True
        or (
            "unsafe" not in event
            and (
                event.get("method") not in {"GET", "HEAD"}
                or event.get("reason") in {"blocked_non_read_method", "blocked_private_or_mutation_surface"}
                or (event.get("action") == "navigate" and event.get("allowed") is not True)
            )
        )
    ]
    mutations = [
        event
        for event in unsafe_events
        if event.get("reason") in {"blocked_non_read_method", "blocked_private_or_mutation_surface"}
    ]
    private_content_attempts = [event for event in unsafe_events if event.get("reason") == "blocked_private_or_mutation_surface"]
    trace_summary = {
        "status": "zero_public_writes_observed" if not unsafe_events else "unsafe_threads_browser_event_observed",
        "mutations": mutations,
        "mutation_attempts": mutations,
        "mutation_attempt_count": len(mutations),
        "public_write_attempts": mutations,
        "public_write_actions": len(mutations),
        "private_content_attempts": private_content_attempts,
        "private_content_actions": len(private_content_attempts),
        "unsafe_events": unsafe_events,
        "allowed_events": sum(1 for event in events if event.get("allowed") is True),
        "blocked_events": sum(1 for event in events if event.get("allowed") is not True),
        "events": events,
        "evidence": evidence,
        **(metadata or {}),
    }
    failures = ["unsafe Threads browser_safety_trace event observed"] if unsafe_events else []
    return trace_summary, failures


if __name__ == "__main__":
    raise SystemExit(main())
