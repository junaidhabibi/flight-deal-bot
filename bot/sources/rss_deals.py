"""Error-fare feed watcher.

Genuine mistake fares (the $200 round trips to Europe) almost never show up
in a fare cache before a human spots them. The deal blogs are the fastest
free signal there is, so the bot reads their RSS feeds every run, extracts
the price and destination from the headline, and only surfaces posts that
match a route you actually care about.

Everything is standard-library XML parsing -- no extra dependency.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

PRICE_RE = re.compile(r"[\$£€]\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?")
ROUNDTRIP_RE = re.compile(r"round[\s-]?trip|return", re.I)
ONEWAY_RE = re.compile(r"one[\s-]?way", re.I)


@dataclass
class FeedItem:
    title: str
    link: str
    guid: str
    published: Optional[datetime] = None
    summary: str = ""
    feed_name: str = ""

    price_usd: Optional[float] = None
    is_roundtrip: bool = False
    matched_origins: List[str] = field(default_factory=list)
    matched_destinations: List[str] = field(default_factory=list)
    matched_cities: List[str] = field(default_factory=list)
    is_hot: bool = False

    @property
    def text(self) -> str:
        return f"{self.title} {self.summary}"

    def relevance(self) -> int:
        """Rough ranking: hot keywords and route matches both count."""
        score = 0
        if self.is_hot:
            score += 3
        score += 2 * len(self.matched_destinations)
        score += len(self.matched_origins)
        if self.price_usd and self.price_usd < 400:
            score += 2
        return score


class RSSDealWatcher:
    def __init__(
        self,
        feeds: Sequence[Dict[str, str]],
        timeout: int = 20,
        session: Optional[requests.Session] = None,
    ):
        self.feeds = list(feeds)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "flight-deal-bot/1.0"})

    # ---------- fetching ----------

    def fetch_all(self) -> List[FeedItem]:
        items: List[FeedItem] = []
        for feed in self.feeds:
            name = feed.get("name", feed.get("url", "feed"))
            try:
                items.extend(self.fetch_feed(feed["url"], name))
            except Exception as e:  # a dead blog must never kill the run
                log.warning("RSS feed %s failed: %s", name, e)
        return items

    def fetch_feed(self, url: str, name: str = "") -> List[FeedItem]:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return self.parse(resp.content, name)

    @staticmethod
    def parse(content: bytes, feed_name: str = "") -> List[FeedItem]:
        """Parse RSS 2.0 or Atom into FeedItems."""
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as e:
            raise ValueError(f"unparseable feed: {e}") from e

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items: List[FeedItem] = []

        nodes = root.findall(".//item")
        if nodes:  # RSS 2.0
            for node in nodes:
                title = _text(node.find("title"))
                link = _text(node.find("link"))
                guid = _text(node.find("guid")) or link or title
                summary = _text(node.find("description"))
                published = _parse_date(_text(node.find("pubDate")))
                if not title:
                    continue
                items.append(
                    FeedItem(
                        title=html.unescape(title),
                        link=link,
                        guid=guid,
                        published=published,
                        summary=html.unescape(_strip_tags(summary)),
                        feed_name=feed_name,
                    )
                )
            return items

        for node in root.findall(".//atom:entry", ns):  # Atom
            title = _text(node.find("atom:title", ns))
            link_el = node.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            guid = _text(node.find("atom:id", ns)) or link or title
            summary = _text(node.find("atom:summary", ns)) or _text(
                node.find("atom:content", ns)
            )
            published = _parse_date(
                _text(node.find("atom:published", ns))
                or _text(node.find("atom:updated", ns))
            )
            if not title:
                continue
            items.append(
                FeedItem(
                    title=html.unescape(title),
                    link=link,
                    guid=guid,
                    published=published,
                    summary=html.unescape(_strip_tags(summary)),
                    feed_name=feed_name,
                )
            )
        return items

    # ---------- matching ----------

    def annotate(
        self,
        items: Iterable[FeedItem],
        origin_codes: Sequence[str],
        destination_codes: Sequence[str],
        city_names: Dict[str, str],
        hot_keywords: Sequence[str] = (),
        origin_city_names: Sequence[str] = (),
    ) -> List[FeedItem]:
        """Tag each item with price, matched routes and hotness."""
        hot = [k.lower() for k in hot_keywords]
        out: List[FeedItem] = []

        for item in items:
            text = item.text
            lower = text.lower()

            item.price_usd = extract_price(text)
            item.is_roundtrip = bool(ROUNDTRIP_RE.search(text)) and not bool(
                ONEWAY_RE.search(text)
            )
            item.is_hot = any(k in lower for k in hot)

            item.matched_origins = [
                c for c in origin_codes if _mentions(lower, c, city_names.get(c, ""))
            ]
            for city in origin_city_names:
                if city and city.lower() in lower:
                    item.matched_origins.append(city)
            item.matched_origins = sorted(set(item.matched_origins))

            item.matched_destinations = [
                c for c in destination_codes if _mentions(lower, c, city_names.get(c, ""))
            ]
            item.matched_cities = [
                city_names.get(c, c) for c in item.matched_destinations
            ]
            out.append(item)
        return out

    def relevant(
        self,
        items: Iterable[FeedItem],
        max_price: Optional[float] = None,
        require_destination: bool = True,
        seen: Optional[Set[str]] = None,
    ) -> List[FeedItem]:
        """Filter down to items worth putting in front of a human."""
        seen = seen or set()
        keep: List[FeedItem] = []
        for item in items:
            if item.guid in seen:
                continue
            if require_destination and not item.matched_destinations:
                # An explicit error/mistake fare post still gets through if it
                # at least mentions Europe -- those are worth a look regardless.
                if not (item.is_hot and "europe" in item.text.lower()):
                    continue
            if (
                max_price is not None
                and item.price_usd is not None
                and item.price_usd > max_price
            ):
                continue
            keep.append(item)
        keep.sort(key=lambda i: (-i.relevance(), i.price_usd or 1e9))
        return keep


# ---------- helpers ----------


def _text(node) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _mentions(lower_text: str, code: str, city: str) -> bool:
    """Match an airport code as a whole word, or the city name."""
    if city and city.lower() in lower_text:
        return True
    return re.search(rf"\b{re.escape(code.lower())}\b", lower_text) is not None


def extract_price(text: str) -> Optional[float]:
    """Lowest currency figure in the headline, which is the advertised fare."""
    matches = PRICE_RE.findall(text or "")
    values: List[float] = []
    for m in matches:
        try:
            values.append(float(m.replace(",", "")))
        except ValueError:
            continue
    # Ignore implausible figures (years, mileage counts).
    values = [v for v in values if 20 <= v <= 5000]
    return min(values) if values else None


def _parse_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
