from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import RawItem
from .utils import clean_text, stable_id

ATOM = "{http://www.w3.org/2005/Atom}"
RSS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def _child_text(node: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _atom_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{ATOM}link"):
        if link.attrib.get("rel", "alternate") == "alternate" and link.attrib.get("href"):
            return link.attrib["href"]
    link = entry.find(f"{ATOM}link")
    return link.attrib.get("href", "") if link is not None else ""


def _html_text_and_images(content: str) -> tuple[str, list[str]]:
    decoded = html.unescape(content or "")
    soup = BeautifulSoup(decoded, "html.parser")
    media = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            media.append(src)
    text = clean_text(soup.get_text(" ", strip=True))
    text = text.replace("Discussion | Link", "").replace("[link] [comments]", "")
    return clean_text(text), media


def parse_feed_items(
    xml_text: str,
    *,
    source: str,
    platform: str,
    base_url: str = "",
    limit: int | None = None,
    raw_extra: dict[str, Any] | None = None,
) -> list[RawItem]:
    text = xml_text.strip()
    if text.startswith("\ufeff"):
        text = text[1:]
    root = ET.fromstring(text)
    items: list[RawItem] = []

    if root.tag.endswith("feed"):
        entries = root.findall(f"{ATOM}entry") or root.findall("entry")
        for entry in entries:
            title = _child_text(entry, [f"{ATOM}title", "title"])
            url = urljoin(base_url, _atom_link(entry))
            content = _child_text(entry, [f"{ATOM}content", f"{ATOM}summary", "content", "summary"])
            summary, media = _html_text_and_images(content)
            author_node = entry.find(f"{ATOM}author")
            if author_node is None:
                author_node = entry.find("author")
            author = None
            if author_node is not None:
                author = _child_text(author_node, [f"{ATOM}name", "name"]) or clean_text(author_node.text)
            published = _child_text(entry, [f"{ATOM}published", f"{ATOM}updated", "published", "updated"])
            item_id = stable_id(source, url, title)
            items.append(
                RawItem(
                    id=item_id,
                    source=source,
                    platform=platform,
                    url=url,
                    title=clean_text(title),
                    text=summary,
                    author=author or None,
                    published_at=published or None,
                    media_urls=media,
                    raw={"feed_id": _child_text(entry, [f"{ATOM}id", "id"]), **(raw_extra or {})},
                )
            )
            if limit and len(items) >= limit:
                return items
        return items

    channel = root.find("channel")
    if channel is None:
        channel = root
    for node in channel.findall("item"):
        title = _child_text(node, ["title"])
        url = urljoin(base_url, _child_text(node, ["link", "guid"]))
        content = _child_text(node, [f"{RSS_CONTENT}encoded", "description"])
        summary, media = _html_text_and_images(content)
        item_id = stable_id(source, url, title)
        items.append(
            RawItem(
                id=item_id,
                source=source,
                platform=platform,
                url=url,
                title=clean_text(title),
                text=summary,
                author=_child_text(node, ["author", "creator"]) or None,
                published_at=_child_text(node, ["pubDate", "date"]) or None,
                media_urls=media,
                raw={"feed_id": _child_text(node, ["guid"]), **(raw_extra or {})},
            )
        )
        if limit and len(items) >= limit:
            break
    return items
