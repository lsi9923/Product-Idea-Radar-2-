from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import RawItem
from .utils import clean_text

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = {
    "addtag", "ascsubtag", "camp", "campaignid", "campaigntype", "clickbeacon", "contentcategory",
    "contentkeyword", "contenttype", "creative", "creativeasin", "ctime", "ctag", "deviceid", "fbclid",
    "gclid", "gbraid", "igshid", "impressionid", "itime", "landing_exp", "linkcode", "lptag", "mc_cid",
    "mc_eid", "mcid", "offerid", "pageid", "pagetype", "pagevalue", "placementid", "portal", "psc",
    "puid", "puidtype", "redirect", "ref", "ref_src", "requestid", "sfid", "sig", "spec", "spm",
    "src", "subid", "subparam", "tag", "token", "traceid", "tsource", "wpcid", "wref", "wtime", "wbraid",
}
DEFAULT_TIMEOUT_SECONDS = 8.0
RESOLVER_SUCCESS_STATUSES = {"resolved", "pre_resolved", "resolved_http_restricted"}
RESOLVER_FAILURE_STATUSES = {"timeout", "unresolved", "error", "http_error", "blocked", "invalid", "unsupported", "failed"}
DEFAULT_MAX_REDIRECTS = 5


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass(frozen=True, slots=True)
class CanonicalPurchaseEvidence:
    id: str
    kind: str
    raw_url: str | None
    raw_value: str | None
    provenance: dict[str, Any]
    affiliate: dict[str, Any]
    resolver: dict[str, Any]
    dedupe_key: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "raw_url": self.raw_url,
            "raw_value": self.raw_value,
            "provenance": dict(self.provenance),
            "affiliate": dict(self.affiliate),
            "resolver": dict(self.resolver),
            "dedupe_key": self.dedupe_key,
        }


def normalize_purchase_evidence(item: RawItem) -> list[dict[str, Any]]:
    """Return canonical purchase evidence dicts without performing network I/O."""
    raw = item.raw if isinstance(item.raw, dict) else {}
    existing = raw.get("purchase_evidence")
    rows: list[Any]
    if isinstance(existing, list):
        rows = existing
    elif isinstance(existing, dict):
        rows = [existing]
    else:
        rows = []

    rows.extend(_product_page_rows(item, raw))

    legacy_rows = _legacy_purchase_rows(item, raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate([*rows, *legacy_rows]):
        evidence = _normalize_one(item, row, index=index)
        if evidence is None:
            continue
        data = evidence.to_dict()
        key = data.get("dedupe_key") or data.get("raw_url") or data.get("raw_value") or data["id"]
        if key in seen:
            continue
        seen.add(str(key))
        out.append(data)
    return out


def resolve_item_purchase_evidence(
    item: RawItem,
    source_config: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
) -> RawItem | None:
    """Normalize and resolve purchase evidence after quality admission.

    Pre-resolved evidence is accepted without network. Unresolved conformant URL evidence must resolve to
    a normalized final_url dedupe key; source URL fallback is intentionally not used.
    """
    evidence = normalize_purchase_evidence(item)
    if not evidence:
        return None

    resolved: list[dict[str, Any]] = []
    for row in evidence:
        normalized = dict(row)
        resolver = dict(normalized.get("resolver") or {})
        dedupe_key = normalized.get("dedupe_key")
        final_url = clean_final_url(str(resolver.get("final_url") or ""))
        if _resolver_status_failed(resolver):
            normalized["resolver"] = resolver
            resolved.append(normalized)
            continue
        if isinstance(dedupe_key, str) and dedupe_key.startswith("final_url:"):
            suffix_url = clean_final_url(dedupe_key.removeprefix("final_url:"))
            final_url = final_url or suffix_url
            if final_url:
                resolver.setdefault("status", "pre_resolved")
                resolver["final_url"] = final_url
                normalized["dedupe_key"] = f"final_url:{final_url}"
        elif final_url:
            resolver.setdefault("status", "pre_resolved")
            resolver["final_url"] = final_url
            normalized["dedupe_key"] = f"final_url:{final_url}"
        else:
            raw_url = normalized.get("raw_url")
            if isinstance(raw_url, str) and raw_url.strip():
                final_url, resolver = _resolve_url(raw_url, source_config, opener=opener)
                if final_url:
                    normalized["dedupe_key"] = f"final_url:{final_url}"
            else:
                resolver.setdefault("status", "invalid")
                resolver.setdefault("error", "missing_raw_url")
        normalized["resolver"] = resolver
        resolved.append(normalized)

    raw = dict(item.raw or {})
    raw["purchase_evidence"] = resolved
    discovery_method = str(raw.get("discovery_method") or "").lower()
    is_fixture = bool(raw.get("fixture") or raw.get("is_fixture") or "fixture" in discovery_method)
    is_cache_fallback = bool(raw.get("cache_source") or raw.get("is_cache_fallback") or discovery_method in {"cache", "cached", "cache_fallback"})

    primary: dict[str, Any] | None = None
    primary_validation: dict[str, bool] | None = None
    for row in resolved:
        validation = _strict_row_validation(item, raw, row)
        if validation["conformant"]:
            primary = row
            primary_validation = validation
            break
    if primary is None or primary_validation is None:
        raw["schema_version"] = 2
        raw["is_fixture"] = is_fixture
        raw["is_cache_fallback"] = is_cache_fallback
        raw["is_live_real_time"] = False
        raw["public_only"] = False
        raw["source_evidence_policy_passed"] = False
        raw["conformant"] = False
        reasons = raw.get("rejection_reasons")
        if not isinstance(reasons, list):
            reasons = []
        raw["rejection_reasons"] = [*reasons, "purchase_evidence_noncanonical"]
        return replace(item, raw=raw)

    primary_id = primary.get("id")
    primary_resolver = primary.get("resolver") if isinstance(primary.get("resolver"), dict) else {}
    final_url = clean_final_url(str(primary_resolver.get("final_url") or ""))
    raw["schema_version"] = 2
    raw["primary_purchase_evidence_id"] = primary_id
    raw["dedupe_key"] = f"final_url:{final_url}"
    raw["dedupe_key_namespace"] = "final_url"
    raw["resolved_purchase_url"] = final_url
    raw["purchase_urls"] = [
        clean_final_url(str((row.get("resolver") or {}).get("final_url") or ""))
        for row in resolved
        if isinstance(row.get("resolver"), dict) and clean_final_url(str((row.get("resolver") or {}).get("final_url") or ""))
    ]
    raw.setdefault("product_tags", [])
    raw["purchase_resolver_status"] = str(primary_resolver.get("status") or "")
    raw["public_only"] = primary_validation["public_only"]
    raw["source_evidence_policy_passed"] = primary_validation["source_evidence_policy_passed"]
    raw["conformant"] = primary_validation["conformant"]
    raw["is_fixture"] = is_fixture
    raw["is_cache_fallback"] = is_cache_fallback
    raw["is_live_real_time"] = raw.get("is_live_real_time") is True and not (is_fixture or is_cache_fallback or raw.get("provisional"))
    raw.setdefault("rejection_reasons", [])
    return replace(item, raw=raw)


def purchase_evidence_dedupe_keys(item: RawItem, *, require_final_url: bool = True) -> list[str]:
    keys: list[str] = []
    for row in normalize_purchase_evidence(item):
        key = row.get("dedupe_key")
        if not isinstance(key, str):
            continue
        if require_final_url:
            if not key.startswith("final_url:"):
                continue
            final_url = clean_final_url(key.removeprefix("final_url:"))
            if not final_url:
                continue
            keys.append(f"final_url:{final_url}")
        else:
            keys.append(key)
    return keys


def has_raw_purchase_or_tag_evidence(item: RawItem) -> bool:
    if normalize_purchase_evidence(item):
        return True
    tags = item.tags or []
    return any(isinstance(tag, str) and (tag.startswith("#") or tag.startswith("@")) for tag in tags)


def has_public_provenance(item: RawItem) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    if raw.get("private") or raw.get("requires_login"):
        return False
    if _raw_is_fixture_cache_or_provisional(raw):
        return False
    if raw.get("public_only") is True:
        return True
    if _collector_public_read_proof_passed(item, raw):
        return True
    method = str(raw.get("discovery_method") or raw.get("provenance") or "").lower()
    if method in {"public_web", "official_api", "public_api", "rss", "feed"}:
        return True
    for row in normalize_purchase_evidence(item):
        provenance = row.get("provenance")
        if isinstance(provenance, dict):
            visibility = str(provenance.get("visibility") or provenance.get("access") or "").lower()
            if visibility == "public" or provenance.get("public_only") is True:
                return True
    return False


def clean_final_url(url: str) -> str:
    text = clean_text(url)
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.netloc:
        return ""
    if parts.scheme and parts.scheme.lower() not in {"http", "https"}:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_param(key)
    ]
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path.rstrip("/") or "/", urlencode(query, doseq=True), ""))


def _normalize_one(item: RawItem, value: Any, *, index: int) -> CanonicalPurchaseEvidence | None:
    if isinstance(value, str):
        row: dict[str, Any] = {"raw_url": value} if value.startswith(("http://", "https://")) else {"raw_value": value}
    elif isinstance(value, dict):
        row = dict(value)
    else:
        return None

    raw_url = _first_str(row, "raw_url", "url", "href", "purchase_url", "product_url", "affiliate_url")
    raw_value = _first_str(row, "raw_value", "value", "label", "tag", "product", "product_name")
    final_url = clean_final_url(_first_str(row, "final_url") or _first_nested_str(row, "resolver", "final_url") or "")
    dedupe_key = _first_str(row, "dedupe_key")
    resolver = _normalize_object(row.get("resolver"))
    if _resolver_status_failed(resolver):
        final_url = ""
        if isinstance(dedupe_key, str) and dedupe_key.startswith("final_url:"):
            dedupe_key = None
    else:
        if final_url:
            resolver.setdefault("final_url", final_url)
            resolver.setdefault("status", "pre_resolved")
            dedupe_key = f"final_url:{final_url}"
        elif dedupe_key and dedupe_key.startswith("final_url:"):
            final_url = clean_final_url(dedupe_key.removeprefix("final_url:"))
            if final_url:
                resolver.setdefault("final_url", final_url)
                resolver.setdefault("status", "pre_resolved")
                dedupe_key = f"final_url:{final_url}"

    if not raw_url and not raw_value and not dedupe_key:
        return None

    kind = clean_text(str(row.get("kind") or ("url" if raw_url else "tag"))).lower() or "unknown"
    provenance = _normalize_provenance(item, row.get("provenance"))
    affiliate = _normalize_object(row.get("affiliate"))
    if not affiliate and any(key in row for key in ("affiliate_network", "affiliate_id", "affiliate_tag")):
        affiliate = {
            "network": row.get("affiliate_network"),
            "id": row.get("affiliate_id"),
            "tag": row.get("affiliate_tag"),
        }
        affiliate = {key: value for key, value in affiliate.items() if value not in (None, "")}
    evidence_id = clean_text(str(row.get("id") or "")) or _stable_evidence_id(item, raw_url or raw_value or dedupe_key or str(index))
    return CanonicalPurchaseEvidence(evidence_id, kind, raw_url, raw_value, provenance, affiliate, resolver, dedupe_key)


def _public_proof_passed(item: RawItem, raw: dict[str, Any], row: dict[str, Any], *, final_url: str) -> bool:
    if _raw_is_fixture_cache_or_provisional(raw):
        return False
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    conformance = row.get("conformance") if isinstance(row.get("conformance"), dict) else {}
    explicit_public = (
        raw.get("public_only") is True
        or row.get("public_only") is True
        or provenance.get("public_only") is True
        or resolver.get("public_only") is True
        or conformance.get("public_only") is True
        or _collector_public_read_proof_passed(item, raw)
    )
    if explicit_public:
        return True
    return _is_product_page_public_feed_evidence(item, row, final_url=final_url)


def _row_conformant(item: RawItem, raw: dict[str, Any], row: dict[str, Any], *, public_only: bool) -> bool:
    if _raw_is_fixture_cache_or_provisional(raw):
        return False
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    canonical = _is_canonical_purchase_evidence(item, raw, row)
    resolver_status = str(resolver.get("status") or "").lower()
    valid_final_url = bool(clean_final_url(str(resolver.get("final_url") or "")))
    return (
        public_only
        and _source_policy_passed(item, raw, row)
        and valid_final_url
        and canonical
        and resolver_status in RESOLVER_SUCCESS_STATUSES
    )


def _strict_row_validation(item: RawItem, raw: dict[str, Any], row: dict[str, Any]) -> dict[str, bool]:
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    final_url = clean_final_url(str(resolver.get("final_url") or ""))
    public_only = _public_proof_passed(item, raw, row, final_url=final_url)
    source_policy_passed = _source_policy_passed(item, raw, row)
    conformant = (
        public_only
        and source_policy_passed
        and _row_conformant(item, raw, row, public_only=public_only)
    )
    return {
        "public_only": public_only,
        "source_evidence_policy_passed": source_policy_passed,
        "conformant": conformant,
    }

def _source_policy_passed(item: RawItem, raw: dict[str, Any], row: dict[str, Any]) -> bool:
    if _raw_is_fixture_cache_or_provisional(raw):
        return False
    if raw.get("source_evidence_policy_passed") is True:
        return True
    if raw.get("safety_conformant") is True and _collector_public_read_proof_passed(item, raw):
        return True
    discovery_method = str(raw.get("discovery_method") or "").lower()
    if discovery_method in {"official_api", "public_api"}:
        return True
    trace = raw.get("browser_safety_trace")
    if isinstance(trace, dict) and trace.get("conformant") is True:
        event_count = trace.get("event_count")
        events = trace.get("events")
        if not ((isinstance(event_count, int) and event_count > 0) or (isinstance(events, list) and events)):
            return False
        try:
            unsafe_count = int(trace.get("unsafe_count") or trace.get("public_write_actions") or trace.get("private_content_actions") or 0)
        except (TypeError, ValueError):
            return False
        return unsafe_count <= 0
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    final_url = clean_final_url(str(resolver.get("final_url") or ""))
    return _is_product_page_public_feed_evidence(item, row, final_url=final_url)


def _is_canonical_purchase_evidence(item: RawItem, raw: dict[str, Any], row: dict[str, Any]) -> bool:
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    if (
        row.get("canonical") is True
        or row.get("canonical_purchase_evidence") is True
        or provenance.get("canonical_product_page") is True
        or row.get("kind") in {"product_page", "canonical_purchase_evidence"}
        or (row.get("kind") == "same_author_affiliate_followup" and _same_author_followup_verified(item, raw, row))
    ):
        return True
    return (
        item.platform == "threads"
        and row.get("kind") == "purchase_url"
        and _collector_public_read_proof_passed(item, raw)
        and (raw.get("safety_conformant") is True or raw.get("source_evidence_policy_passed") is True)
    )


def _collector_public_read_proof_passed(item: RawItem, raw: dict[str, Any]) -> bool:
    proof = raw.get("collector_public_read_proof")
    if not isinstance(proof, dict):
        return False
    if proof.get("public_only") is not True:
        return False
    method = str(proof.get("method") or proof.get("http_method") or "").upper()
    if method not in {"GET", "HEAD"}:
        return False
    if proof.get("fetch_ok") is False:
        return False
    platform = clean_text(str(proof.get("platform") or ""))
    if not platform or platform.lower() != item.platform.lower():
        return False
    surface = clean_text(str(proof.get("surface") or proof.get("url") or proof.get("source_url") or ""))
    if item.platform in {"threads", "x", "instagram"} and not surface:
        return False
    return True


def _same_author_followup_verified(item: RawItem, raw: dict[str, Any], row: dict[str, Any]) -> bool:
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    item_author = _canonical_author(item.author)
    reply_author = _canonical_author(provenance.get("reply_author") or row.get("reply_author") or provenance.get("author") or row.get("author"))
    source_author = _canonical_author(provenance.get("author") or row.get("author") or item.author)
    if not item_author or reply_author != item_author or source_author != item_author:
        return False
    return _has_reply_binding(item, row, provenance) or _collector_same_author_reply_proof_passed(item, raw, provenance)


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


def _collector_same_author_reply_proof_passed(item: RawItem, raw: dict[str, Any], provenance: dict[str, Any]) -> bool:
    proof = raw.get("collector_public_read_proof")
    if not _collector_public_read_proof_passed(item, raw) or not isinstance(proof, dict):
        return False
    item_author = _canonical_author(item.author)
    proof_author = _canonical_author(proof.get("author") or proof.get("item_author") or provenance.get("author"))
    proof_reply_author = _canonical_author(proof.get("reply_author") or provenance.get("reply_author") or provenance.get("author"))
    if proof_author != item_author or proof_reply_author != item_author:
        return False
    return any(
        isinstance(proof.get(key), str) and clean_text(proof.get(key))
        for key in ("reply_url", "reply_id", "captured_reply_url", "captured_reply_id", "root_url", "root_id", "root_post_url", "root_post_id")
    )


def _url_matches_item(item_url: str, values: list[Any]) -> bool:
    normalized_item_url = clean_text(item_url).rstrip("/")
    if not normalized_item_url:
        return False
    return any(isinstance(value, str) and clean_text(value).rstrip("/") == normalized_item_url for value in values)


def _canonical_author(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return clean_text(value).strip().lstrip("@").lower()

def _is_product_page_public_feed_evidence(item: RawItem, row: dict[str, Any], *, final_url: str) -> bool:
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    resolver = row.get("resolver") if isinstance(row.get("resolver"), dict) else {}
    return (
        item.platform in {"producthunt", "kickstarter"}
        and row.get("kind") == "product_page"
        and provenance.get("canonical_product_page") is True
        and str(resolver.get("status") or "").lower() in RESOLVER_SUCCESS_STATUSES
        and bool(clean_final_url(final_url))
    )


def _raw_is_fixture_cache_or_provisional(raw: dict[str, Any]) -> bool:
    discovery_method = str(raw.get("discovery_method") or "").lower()
    return bool(
        raw.get("fixture")
        or raw.get("is_fixture")
        or raw.get("provisional")
        or raw.get("cache_source")
        or raw.get("is_cache_fallback")
        or discovery_method in {"fixture", "cache", "cached", "cache_fallback"}
    )


def _legacy_purchase_rows(item: RawItem, raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("purchase_url", "product_url", "affiliate_url", "shop_url", "store_url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            rows.append({"kind": "url", "raw_url": value, "provenance": {"source": f"raw.{key}"}})
    value = raw.get("product_tag")
    if isinstance(value, str) and value.strip():
        rows.append({"kind": "tag", "raw_value": value, "provenance": {"source": "raw.product_tag"}})
    return rows


def _product_page_rows(item: RawItem, raw: dict[str, Any]) -> list[dict[str, Any]]:
    if item.platform not in {"producthunt", "kickstarter"}:
        return []
    url = clean_final_url(str(item.url or ""))
    if not url:
        return []
    return [
        {
            "kind": "product_page",
            "raw_url": url,
            "final_url": url,
            "provenance": {
                "source": f"{item.platform}_product_page",
                "public_only": True,
                "canonical_product_page": True,
            },
            "resolver": {"status": "pre_resolved", "final_url": url, "source": "item.url"},
            "dedupe_key": f"final_url:{url}",
        }
    ]


def _resolve_url(url: str, source_config: dict[str, Any], *, opener: Callable[..., Any] | None) -> tuple[str, dict[str, Any]]:
    resolver_config = source_config.get("resolver") if isinstance(source_config.get("resolver"), dict) else {}
    timeout = float(resolver_config.get("timeout_seconds") or source_config.get("resolver_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    max_redirects = int(resolver_config.get("max_redirects") or source_config.get("resolver_max_redirects") or DEFAULT_MAX_REDIRECTS)
    fetch = opener or build_opener(_NoRedirectHandler()).open
    current = url
    chain: list[str] = []
    for depth in range(max(1, max_redirects) + 1):
        chain.append(current)
        try:
            request = Request(current, method="HEAD", headers={"User-Agent": "ProductIdeaRadar/1.0"})
            response = fetch(request, timeout=timeout)
        except HTTPError as exc:
            location = _redirect_location(exc)
            if location and depth < max_redirects:
                current = location
                continue
            if exc.code in {405, 403}:
                try:
                    request = Request(current, method="GET", headers={"User-Agent": "ProductIdeaRadar/1.0"})
                    response = fetch(request, timeout=timeout)
                except HTTPError as get_exc:
                    location = _redirect_location(get_exc)
                    if location and depth < max_redirects:
                        current = location
                        continue
                    restricted_final = clean_final_url(current) if current != url else ""
                    if restricted_final:
                        return restricted_final, {
                            "status": "resolved_http_restricted",
                            "final_url": restricted_final,
                            "http_status": get_exc.code,
                            "chain": chain,
                            "depth": depth,
                        }
                    return "", {"status": "error", "error": str(get_exc), "chain": chain, "depth": depth}
                except (URLError, TimeoutError, OSError) as get_exc:
                    return "", {"status": "error", "error": str(get_exc), "chain": chain, "depth": depth}
            else:
                return "", {"status": "error", "error": str(exc), "chain": chain, "depth": depth}
        except (URLError, TimeoutError, OSError) as exc:
            return "", {"status": "error", "error": str(exc), "chain": chain, "depth": depth}

        location = _redirect_location(response)
        if location and depth < max_redirects:
            current = location
            continue
        final = clean_final_url(_response_url(response) or current)
        if final:
            return final, {"status": "resolved", "final_url": final, "chain": chain, "depth": depth}
        break
    return "", {"status": "error", "error": "redirect_depth_exceeded", "chain": chain, "depth": len(chain) - 1}


def _redirect_location(response: Any) -> str:
    code = getattr(response, "code", None) or getattr(response, "status", None)
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return ""
    if numeric_code < 300 or numeric_code >= 400:
        return ""
    headers = getattr(response, "headers", None)
    location = ""
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            location = str(getter("Location") or getter("location") or "")
    if not location:
        return ""
    return urljoin(_response_url(response), location)

def _response_url(response: Any) -> str:
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        return str(geturl() or "")
    return str(getattr(response, "url", "") or "")


def _first_str(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_nested_str(row: dict[str, Any], object_key: str, value_key: str) -> str | None:
    nested = row.get(object_key)
    if isinstance(nested, dict):
        value = nested.get(value_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolver_status_failed(resolver: dict[str, Any]) -> bool:
    return str(resolver.get("status") or "").lower() in RESOLVER_FAILURE_STATUSES


def _normalize_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_provenance(item: RawItem, value: Any) -> dict[str, Any]:
    provenance = _normalize_object(value)
    provenance.setdefault("source", item.source)
    provenance.setdefault("platform", item.platform)
    if item.raw.get("public_only") is True:
        provenance.setdefault("public_only", True)
    if item.raw.get("discovery_method"):
        provenance.setdefault("discovery_method", item.raw.get("discovery_method"))
    return provenance


def _stable_evidence_id(item: RawItem, value: str) -> str:
    digest = hashlib.sha1(f"{item.id}:{value}".encode("utf-8", "ignore")).hexdigest()[:12]
    return f"pe:{digest}"


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or any(lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)
