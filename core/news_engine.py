"""News collection, normalization, deduplication, and honest fallback state."""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from .instruments import Instrument
from .providers.news import ImpactHorizon, NewsCategory, NewsEvent, NewsProvider


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_POSITIVE = {"beat", "growth", "surge", "strong", "upgrade", "record", "profit", "approved", "launch"}
_NEGATIVE = {"miss", "fall", "drop", "weak", "downgrade", "loss", "probe", "lawsuit", "recall", "cut"}
_NEGATION = {"not", "no", "never", "without", "failed", "fail", "didn't", "didnt", "未", "没有", "未能", "不"}


def _clean_text(value: str | None, max_chars: int = 800) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    return _SPACE_RE.sub(" ", text).strip()[:max_chars]


def _parse_date(value: str | None) -> datetime | None:
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
    return None


def _classify(title: str, summary: str) -> tuple[str, float, int, int, str]:
    text = f"{title} {summary}".lower()
    tokens = re.findall(r"[a-z]+(?:['’][a-z]+)?|[\u4e00-\u9fff]+", text)
    token_set = set(tokens)
    if token_set.intersection({"earnings", "quarter", "revenue", "guidance", "profit", "eps", "beat", "miss"}):
        category = NewsCategory.EARNINGS.value
        importance = 80
        horizon = ImpactHorizon.DAYS_1_TO_3.value
    elif any(word in text for word in ("sec", "regulator", "antitrust", "government", "ban")):
        category = NewsCategory.REGULATION.value
        importance = 85
        horizon = ImpactHorizon.WEEKS_1_TO_4.value
    elif any(word in text for word in ("fed", "cpi", "inflation", "interest rate", "jobs")):
        category = NewsCategory.MACRO.value
        importance = 75
        horizon = ImpactHorizon.DAYS_1_TO_3.value
    elif any(word in text for word in ("merger", "acquire", "acquisition")):
        category = NewsCategory.MERGER.value
        importance = 90
        horizon = ImpactHorizon.WEEKS_1_TO_4.value
    elif any(word in text for word in ("lawsuit", "court", "legal", "investigation", "probe")):
        category = NewsCategory.LEGAL.value
        importance = 80
        horizon = ImpactHorizon.WEEKS_1_TO_4.value
    elif any(word in text for word in ("analyst", "rating", "price target", "upgrade", "downgrade")):
        category = NewsCategory.ANALYST.value
        importance = 55
        horizon = ImpactHorizon.INTRADAY.value
    elif any(word in text for word in ("product", "launch", "release")):
        category = NewsCategory.PRODUCT.value
        importance = 60
        horizon = ImpactHorizon.DAYS_1_TO_3.value
    else:
        category = NewsCategory.OTHER.value
        importance = 30
        horizon = ImpactHorizon.UNKNOWN.value
    # Score tokens, with a small local negation window.  Substring matching
    # made "drop"/"not beat" and publisher names produce the wrong direction.
    score = 0.0
    for index, token in enumerate(tokens):
        if token not in _POSITIVE and token not in _NEGATIVE:
            continue
        negated = any(previous in _NEGATION for previous in tokens[max(0, index - 3):index])
        weight = -1.0 if token in _NEGATIVE else 1.0
        score += -weight if negated else weight
    if re.search(r"(?:did\s+not|didn['’]?t|not|failed\s+to|未能|没有)\s+(?:beat|exceed|meet)", text):
        score = min(score, -1.0)
    sentiment = max(-1.0, min(1.0, score / 3.0))
    credibility = 85 if token_set.intersection({"reuters", "sec", "cnbc"}) or "federal reserve" in text else 60
    return category, sentiment, importance, credibility, horizon


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    """Reject an unsafe redirect before urllib opens the next hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urljoin(req.full_url, newurl)
        from .security.local_guard import is_safe_outbound_url

        allowed, reason = is_safe_outbound_url(target)
        if not allowed:
            raise RuntimeError(f"unsafe RSS redirect: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _safe_urlopen(request: Request, *, timeout: float):
    from .security.local_guard import is_safe_outbound_url

    allowed, reason = is_safe_outbound_url(request.full_url)
    if not allowed:
        raise RuntimeError(f"unsafe RSS URL: {reason}")
    return build_opener(_ValidatedRedirectHandler()).open(request, timeout=timeout)


def dedupe_news_events(events: list[NewsEvent], *, max_items: int | None = None) -> list[NewsEvent]:
    """Keep the newest event for each normalized title/URL fingerprint."""

    seen: set[str] = set()
    result: list[NewsEvent] = []
    for event in sorted(events, key=lambda item: item.published_at, reverse=True):
        key = event.dedupe_hash or hashlib.sha256(event.title.strip().lower().encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
        if max_items is not None and len(result) >= max_items:
            break
    return result


@dataclass(frozen=True, slots=True)
class EventCluster:
    """Small multi-source event grouping kept outside the main LLM prompt."""

    cluster_id: str
    symbols: tuple[str, ...]
    title: str
    events: tuple[NewsEvent, ...]
    sentiment: float
    importance: int

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "symbols": list(self.symbols),
            "title": self.title,
            "event_ids": [event.event_id for event in self.events],
            "sentiment": self.sentiment,
            "importance": self.importance,
        }


def cluster_news_events(events: list[NewsEvent]) -> list[EventCluster]:
    """Group close title fingerprints so duplicate-source coverage is visible."""

    groups: dict[str, list[NewsEvent]] = {}
    for event in events:
        normalized_title = re.sub(r"[^a-z0-9 ]", " ", event.title.lower())
        normalized_title = _SPACE_RE.sub(" ", normalized_title).strip()
        words = normalized_title.split()[:4]
        key = hashlib.sha256(f"{' '.join(words)}|{'/'.join(event.symbols)}".encode("utf-8")).hexdigest()
        groups.setdefault(key, []).append(event)
    clusters: list[EventCluster] = []
    for key, grouped in groups.items():
        ordered = tuple(sorted(grouped, key=lambda event: event.published_at, reverse=True))
        clusters.append(
            EventCluster(
                cluster_id=key[:24],
                symbols=tuple(sorted({symbol for event in ordered for symbol in event.symbols})),
                title=ordered[0].title,
                events=ordered,
                sentiment=round(sum(event.sentiment for event in ordered) / len(ordered), 4),
                importance=max(event.importance for event in ordered),
            )
        )
    return sorted(clusters, key=lambda cluster: cluster.importance, reverse=True)


class RSSNewsProvider(NewsProvider):
    """Public RSS provider using Google News search feeds by default."""

    provider_name = "google_news_rss"

    def __init__(self, timeout: float | None = None, retries: int = 1, feed_template: str | None = None) -> None:
        self.timeout = timeout or float(os.environ.get("NEWS_PROVIDER_TIMEOUT_SEC", "10"))
        self.retries = max(0, min(retries, 2))
        self.feed_template = feed_template or os.environ.get(
            "NEWS_RSS_TEMPLATE",
            "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
        )

    def _fetch_xml(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/rss+xml, application/xml", "User-Agent": "ai-market-analyst/0.2"})
                with _safe_urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    return response.read()
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"RSS fetch failed: {last_error}") from last_error

    def get_events(self, instrument: Instrument, limit: int = 20) -> list[NewsEvent]:
        fallback = f'{instrument.symbol[:-4]} cryptocurrency' if instrument.symbol.endswith('USDT') else instrument.symbol
        topic = {"BTCUSDT":"Bitcoin cryptocurrency", "ETHUSDT":"Ethereum cryptocurrency", "SOLUSDT":"Solana cryptocurrency", "NVDA":"NVIDIA earnings"}.get(instrument.symbol, fallback)
        query = quote_plus(f"{topic} when:2d -site:tradingview.com")
        root = ElementTree.fromstring(self._fetch_xml(self.feed_template.format(query=query)))
        events: list[NewsEvent] = []
        cutoff = datetime.now(timezone.utc)
        for item in root.findall(".//item")[:50]:
            title = _clean_text(item.findtext("title"), 240)
            url = _clean_text(item.findtext("link"), 500) or None
            summary = _clean_text(item.findtext("description"), 800)
            source = _clean_text(item.findtext("source"), 120) or self.provider_name
            published_at = _parse_date(item.findtext("pubDate"))
            if not title or published_at is None or not cutoff - timedelta(hours=48) <= published_at <= cutoff:
                continue
            category, sentiment, importance, credibility, horizon = _classify(title, summary)
            event_id = hashlib.sha256(f"{title.lower()}|{url or ''}|{published_at.date()}".encode("utf-8")).hexdigest()[:24]
            dedupe_hash = hashlib.sha256(f"{title.lower()}|{url or ''}".encode("utf-8")).hexdigest()
            events.append(
                NewsEvent(
                    event_id=event_id,
                    source=source,
                    published_at=published_at,
                    title=title,
                    symbols=(instrument.symbol,),
                    category=category,
                    sentiment=sentiment,
                    importance=importance,
                    summary_raw=summary or None,
                    url=url,
                    credibility=credibility,
                    impact_horizon=horizon,
                    dedupe_hash=dedupe_hash,
                )
            )
        return dedupe_news_events(events, max_items=limit)


@dataclass(frozen=True, slots=True)
class NewsFetchResult:
    events: tuple[NewsEvent, ...]
    provider: str
    fetched_at: datetime
    available: bool
    error_code: str | None = None
    clusters: tuple[EventCluster, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat(),
            "available": self.available,
            "error_code": self.error_code,
            "events": [event.to_dict() for event in self.events],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


class NewsEngine:
    def __init__(self, provider: NewsProvider) -> None:
        self.provider = provider

    def collect(self, instrument: Instrument, limit: int = 10) -> NewsFetchResult:
        fetched_at = datetime.now(timezone.utc)
        provider_name = str(getattr(self.provider, "provider_name", self.provider.__class__.__name__.lower()))
        try:
            events = dedupe_news_events(self.provider.get_events(instrument, limit), max_items=limit)
            history_available = getattr(self.provider, "history_available", True)
            if history_available is False:
                return NewsFetchResult(
                    tuple(events),
                    provider_name,
                    fetched_at,
                    False,
                    "historical_news_unavailable",
                    tuple(cluster_news_events(events)),
                )
            return NewsFetchResult(tuple(events), provider_name, fetched_at, True, None, tuple(cluster_news_events(events)))
        except Exception as exc:  # pragma: no cover - network dependent
            code = "news_provider_error"
            if "timeout" in str(exc).lower():
                code = "timeout"
            return NewsFetchResult((), provider_name, fetched_at, False, code)
