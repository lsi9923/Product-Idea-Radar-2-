from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlsplit, urlunsplit

_WORD_RE = re.compile(r"[\w가-힣ぁ-んァ-ン一-龥]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "your", "you", "are", "was",
    "were", "have", "has", "into", "about", "after", "before", "today", "new", "best",
    "product", "products", "kickstarter", "reddit", "twitter", "x", "instagram", "by",
}


def clean_text(value: str | None, *, max_len: int | None = None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _WS_RE.sub(" ", text).strip()
    if max_len and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(html.unescape(url.strip()))
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path or "/")
    return urlunsplit((parts.scheme.lower() or "https", host, path, "", ""))


def stable_id(source: str, url: str, title: str = "") -> str:
    base = canonical_url(url) or f"{source}:{normalize_key(title)}"
    digest = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{source}:{digest}"


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "") if len(t) > 1]


def normalize_key(text: str, *, limit: int = 14) -> str:
    tokens = [t for t in tokenize(text) if t not in STOPWORDS]
    return " ".join(tokens[:limit])


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


def clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def parse_compact_number(value: str | int | float | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KkMm]?)", text)
    if not match:
        return 0.0
    num = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return num
