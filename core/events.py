"""Typed event evidence, point-in-time selection and deterministic clustering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

from .instruments import Instrument
from .news_engine import NewsFetchResult, cluster_news_events
from .providers.news import ImpactHorizon, NewsCategory, NewsEvent


EVENT_SCHEMA_VERSION = "event_schema_v1"
EVENT_CLUSTER_VERSION = "event_cluster_v1"
SOURCE_CREDIBILITY_VERSION = "source_credibility_v1"

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class EventEvidence:
    event_id: str
    source: str
    category: str
    event_at: datetime
    published_at: datetime | None
    known_at: datetime | None
    retrieved_at: datetime | None
    importance: int
    affected_symbols: tuple[str, ...]
    title: str
    summary: str | None = None
    url: str | None = None
    sentiment: float | None = None
    source_type: str = "unknown"
    primary_source: bool = False
    reported_credibility: int = 50
    credibility_score: int = 50
    revision_known_at: datetime | None = None
    dedupe_hash: str | None = None
    provider: str = "unknown"
    capability: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not str(self.event_id).strip() or not str(self.source).strip() or not str(self.title).strip():
            raise ValueError("event evidence requires event_id, source, and title")
        for name in ("event_at", "published_at", "known_at", "retrieved_at", "revision_known_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if not 0 <= int(self.importance) <= 100:
            raise ValueError("importance must be in [0, 100]")
        if not 0 <= int(self.reported_credibility) <= 100 or not 0 <= int(self.credibility_score) <= 100:
            raise ValueError("credibility must be in [0, 100]")
        if self.sentiment is not None and not -1.0 <= float(self.sentiment) <= 1.0:
            raise ValueError("sentiment must be in [-1, 1]")
        symbols = tuple(sorted({str(symbol).strip().upper() for symbol in self.affected_symbols if str(symbol).strip()}))
        object.__setattr__(self, "affected_symbols", symbols)
        object.__setattr__(self, "category", str(self.category).strip().lower())
        if self.dedupe_hash is None:
            normalized = _normalize_title(self.title)
            raw = f"{normalized}|{','.join(symbols)}|{self.event_at.astimezone(timezone.utc).date().isoformat()}".encode()
            object.__setattr__(self, "dedupe_hash", hashlib.sha256(raw).hexdigest())

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "id": self.event_id,
            "source": self.source,
            "source_type": self.source_type,
            "category": self.category,
            "event_at": self.event_at.astimezone(timezone.utc).isoformat(),
            "published_at": self.published_at.astimezone(timezone.utc).isoformat() if self.published_at else None,
            "known_at": self.known_at.astimezone(timezone.utc).isoformat() if self.known_at else None,
            "retrieved_at": self.retrieved_at.astimezone(timezone.utc).isoformat() if self.retrieved_at else None,
            "importance": int(self.importance),
            "affected_symbols": list(self.affected_symbols),
            "symbols": list(self.affected_symbols),
            "title": self.title,
            "summary": self.summary,
            "summary_raw": self.summary,
            "url": self.url,
            "sentiment": self.sentiment,
            "primary_source": bool(self.primary_source),
            "reported_credibility": int(self.reported_credibility),
            "credibility_score": int(self.credibility_score),
            "revision_known_at": self.revision_known_at.astimezone(timezone.utc).isoformat() if self.revision_known_at else None,
            "dedupe_hash": self.dedupe_hash,
            "provider": self.provider,
            "capability": dict(self.capability or {}),
        }

    def to_news_event(self) -> NewsEvent:
        category = self.category if self.category in {item.value for item in NewsCategory} else NewsCategory.OTHER.value
        return NewsEvent(
            event_id=self.event_id,
            source=self.source,
            published_at=(self.published_at or self.event_at).astimezone(timezone.utc),
            title=self.title,
            symbols=self.affected_symbols,
            category=category,
            sentiment=self.sentiment or 0.0,
            importance=self.importance,
            summary_raw=self.summary,
            url=self.url,
            credibility=self.credibility_score,
            impact_horizon=ImpactHorizon.UNKNOWN.value,
            dedupe_hash=self.dedupe_hash,
        )


@dataclass(frozen=True, slots=True)
class EventClusterEvidence:
    cluster_id: str
    title: str
    affected_symbols: tuple[str, ...]
    event_ids: tuple[str, ...]
    source_count: int
    primary_source_count: int
    credibility_score: int
    importance: int
    consensus: str
    disagreement: bool
    sources: tuple[dict[str, object], ...]
    as_of: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "title": self.title,
            "affected_symbols": list(self.affected_symbols),
            "symbols": list(self.affected_symbols),
            "event_ids": list(self.event_ids),
            "source_count": self.source_count,
            "primary_source_count": self.primary_source_count,
            "credibility_score": self.credibility_score,
            "importance": self.importance,
            "consensus": self.consensus,
            "disagreement": self.disagreement,
            "sources": [dict(source) for source in self.sources],
            "as_of": self.as_of,
        }


@dataclass(frozen=True, slots=True)
class EventIntelligenceResult:
    schema_version: str
    cluster_version: str
    credibility_version: str
    symbol: str
    as_of: str
    provider: str
    fetched_at: str
    available: bool
    error_code: str | None
    events: tuple[EventEvidence, ...]
    clusters: tuple[EventClusterEvidence, ...]
    capability: dict[str, object]
    provenance: dict[str, object]

    @property
    def high_impact(self) -> bool:
        return any(event.importance >= 70 for event in self.events) or any(cluster.importance >= 70 for cluster in self.clusters)

    def to_news_result(self) -> NewsFetchResult:
        return NewsFetchResult(
            events=tuple(event.to_news_event() for event in self.events),
            provider=self.provider,
            fetched_at=_parse_timestamp(self.fetched_at) or datetime.now(timezone.utc),
            available=self.available,
            error_code=self.error_code,
            clusters=tuple(cluster_news_events([event.to_news_event() for event in self.events])),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cluster_version": self.cluster_version,
            "credibility_version": self.credibility_version,
            "symbol": self.symbol,
            "as_of": self.as_of,
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "available": self.available,
            "error_code": self.error_code,
            "events": [event.to_dict() for event in self.events],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "capability": dict(self.capability),
            "provenance": dict(self.provenance),
        }


class EventProvider(Protocol):
    def get_events(self, instrument: Instrument, *, as_of: datetime, limit: int = 20) -> Iterable[EventEvidence]:
        ...


class NewsEventProviderAdapter:
    """Adapt the accepted NewsProvider contract without claiming historical knowledge."""

    def __init__(self, provider: object) -> None:
        self.provider = provider
        self.provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))

    def get_events(self, instrument: Instrument, *, as_of: datetime, limit: int = 20) -> list[EventEvidence]:
        getter = getattr(self.provider, "get_events")
        try:
            raw_events = getter(instrument, limit=limit)
        except TypeError:
            raw_events = getter(instrument, limit)
        retrieved = _utc(as_of)
        result: list[EventEvidence] = []
        for event in list(raw_events)[: max(1, min(int(limit), 50))]:
            if isinstance(event, EventEvidence):
                result.append(event)
                continue
            if not isinstance(event, NewsEvent):
                raise ValueError("event provider returned an unsupported evidence object")
            result.append(
                EventEvidence(
                    event_id=event.event_id,
                    source=event.source,
                    source_type=_source_type(event.source),
                    category=event.category,
                    event_at=event.published_at,
                    published_at=event.published_at,
                    # Published time is the conservative fallback when the
                    # public provider has no historical known-at contract.
                    known_at=event.published_at,
                    retrieved_at=retrieved,
                    importance=event.importance,
                    affected_symbols=event.symbols,
                    title=event.title,
                    summary=event.summary_raw,
                    url=event.url,
                    sentiment=event.sentiment,
                    primary_source=_is_primary_source(event.source, event.url),
                    reported_credibility=event.credibility,
                    credibility_score=source_credibility(event.source, primary_source=_is_primary_source(event.source, event.url), reported=event.credibility),
                    dedupe_hash=event.dedupe_hash,
                    provider=self.provider_name,
                    capability={"known_time": "published_at_fallback", "historical_point_in_time": False},
                )
            )
        return result


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _normalize_title(value: str) -> str:
    return _SPACE_RE.sub(" ", _TOKEN_RE.sub(" ", value.lower())).strip()


def _title_key(value: str) -> tuple[str, ...]:
    stop = {"the", "a", "an", "to", "of", "and", "for", "on", "in", "today"}
    return tuple(token for token in _normalize_title(value).split() if token not in stop)[:6]


def _source_type(source: str) -> str:
    lowered = source.lower()
    if any(token in lowered for token in ("sec", "ir", "company ir", "investor relations", "federal reserve", "treasury", "official")):
        return "official"
    if any(token in lowered for token in ("reuters", "ap", "cnbc", "bloomberg")):
        return "professional"
    return "secondary"


def _is_primary_source(source: str, url: str | None) -> bool:
    text = f"{source} {url or ''}".lower()
    return any(token in text for token in ("sec.gov", "ir.", "company ir", "investor.", "investor relations", "federalreserve.gov", "official"))


def source_credibility(source: str, *, primary_source: bool = False, reported: int | float | None = None) -> int:
    text = source.lower()
    if primary_source or any(token in text for token in ("sec", "federal reserve", "company ir", "company official", "official ir")):
        baseline = 95
    elif any(token in text for token in ("reuters", "associated press", "cnbc", "bloomberg")):
        baseline = 85
    elif any(token in text for token in ("rss", "google news", "fixture")):
        baseline = 50
    else:
        baseline = 35
    try:
        reported_value = max(0, min(100, int(reported))) if reported is not None else baseline
    except (TypeError, ValueError):
        reported_value = baseline
    # Keep provider-reported evidence visible but do not let an unknown source
    # manufacture a high credibility score.
    return max(baseline, reported_value) if primary_source else min(baseline, reported_value)


def _same_cluster(left: EventEvidence, right: EventEvidence) -> bool:
    if not set(left.affected_symbols).intersection(right.affected_symbols):
        return False
    if left.category != right.category:
        return False
    if abs((_utc(left.event_at) - _utc(right.event_at)).total_seconds()) > 36 * 3600:
        return False
    left_tokens = set(_title_key(left.title))
    right_tokens = set(_title_key(right.title))
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens.intersection(right_tokens)) >= max(1, min(len(left_tokens), len(right_tokens)) // 2)


def cluster_event_evidence(events: Iterable[EventEvidence], *, as_of: datetime) -> tuple[EventClusterEvidence, ...]:
    groups: list[list[EventEvidence]] = []
    ordered = sorted(events, key=lambda event: (event.event_at, event.event_id), reverse=True)
    for event in ordered:
        for group in groups:
            if _same_cluster(event, group[0]):
                group.append(event)
                break
        else:
            groups.append([event])
    clusters: list[EventClusterEvidence] = []
    as_of_text = _utc(as_of).isoformat()
    for group in groups:
        group = sorted(group, key=lambda event: (event.event_at, event.event_id), reverse=True)
        symbols = tuple(sorted({symbol for event in group for symbol in event.affected_symbols}))
        identity = "|".join((_normalize_title(group[0].title), ",".join(symbols), group[0].event_at.date().isoformat()))
        cluster_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        distinct_sources = {f"{event.source.lower()}|{event.url or ''}" for event in group}
        primary_count = sum(1 for event in group if event.primary_source)
        sentiments = []
        for event in group:
            if event.sentiment is not None:
                sentiments.append(float(event.sentiment))
        disagreement = bool(sentiments and min(sentiments) < -0.15 < max(sentiments))
        consensus = "confirmed" if primary_count > 0 or len(distinct_sources) >= 2 else "unconfirmed_single_source"
        clusters.append(
            EventClusterEvidence(
                cluster_id=cluster_id,
                title=group[0].title,
                affected_symbols=symbols,
                event_ids=tuple(event.event_id for event in group),
                source_count=len(distinct_sources),
                primary_source_count=primary_count,
                credibility_score=max(event.credibility_score for event in group),
                importance=max(event.importance for event in group),
                consensus=consensus,
                disagreement=disagreement,
                sources=tuple(
                    {
                        "source": event.source,
                        "source_type": event.source_type,
                        "url": event.url,
                        "primary_source": event.primary_source,
                        "reported_credibility": event.reported_credibility,
                        "credibility_score": event.credibility_score,
                        "event_id": event.event_id,
                    }
                    for event in group
                ),
                as_of=as_of_text,
            )
        )
    return tuple(sorted(clusters, key=lambda cluster: (-cluster.importance, cluster.cluster_id)))


def select_point_in_time_events(events: Iterable[EventEvidence], *, as_of: datetime) -> tuple[EventEvidence, ...]:
    cutoff = _utc(as_of)
    selected: list[EventEvidence] = []
    for event in events:
        known_at = _utc(event.known_at) if event.known_at else None
        published_at = _utc(event.published_at) if event.published_at else None
        revision_at = _utc(event.revision_known_at) if event.revision_known_at else None
        if known_at is None or known_at > cutoff:
            continue
        if published_at is not None and published_at > cutoff:
            continue
        if revision_at is not None and revision_at > cutoff:
            continue
        selected.append(event)
    return tuple(sorted(selected, key=lambda event: (event.event_at, event.event_id), reverse=True))


class EventIntelligenceService:
    def __init__(self, provider: object, *, clock: Any | None = None) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))

    def collect(self, instrument: Instrument, *, as_of: datetime, limit: int = 20) -> EventIntelligenceResult:
        cutoff = _utc(as_of)
        fetched = _utc(self.clock())
        try:
            getter = getattr(self.provider, "get_events")
            try:
                raw = getter(instrument, as_of=cutoff, limit=limit)
            except TypeError:
                raw = getter(instrument, limit=limit)
            evidence: list[EventEvidence] = []
            for event in raw:
                if isinstance(event, EventEvidence):
                    evidence.append(event)
                elif isinstance(event, NewsEvent):
                    evidence.append(
                        EventEvidence(
                            event_id=event.event_id,
                            source=event.source,
                            source_type=_source_type(event.source),
                            category=event.category,
                            event_at=event.published_at,
                            published_at=event.published_at,
                            known_at=event.published_at,
                            retrieved_at=fetched,
                            importance=event.importance,
                            affected_symbols=event.symbols,
                            title=event.title,
                            summary=event.summary_raw,
                            url=event.url,
                            sentiment=event.sentiment,
                            primary_source=_is_primary_source(event.source, event.url),
                            reported_credibility=event.credibility,
                            credibility_score=source_credibility(event.source, primary_source=_is_primary_source(event.source, event.url), reported=event.credibility),
                            dedupe_hash=event.dedupe_hash,
                            provider=self.provider_name,
                            capability={"known_time": "published_at_fallback", "historical_point_in_time": False},
                        )
                    )
                else:
                    raise ValueError("event provider returned an unsupported evidence object")
            selected = select_point_in_time_events(evidence, as_of=cutoff)
            clusters = cluster_event_evidence(selected, as_of=cutoff)
            known_fallback = any((event.capability or {}).get("known_time") == "published_at_fallback" for event in selected)
            capability = {
                "point_in_time": True,
                "historical_known_time": not known_fallback,
                "provider": self.provider_name,
                "future_evidence_excluded": True,
                "revised_future_evidence_excluded": True,
            }
            if known_fallback:
                capability["historical_limitation"] = "known_at inferred from published_at for provider contract"
            return EventIntelligenceResult(
                EVENT_SCHEMA_VERSION,
                EVENT_CLUSTER_VERSION,
                SOURCE_CREDIBILITY_VERSION,
                instrument.symbol,
                cutoff.isoformat(),
                self.provider_name,
                fetched.isoformat(),
                True,
                None,
                selected[: max(1, min(int(limit), 50))],
                clusters,
                capability,
                {"computed_by": "python_deterministic", "read_only": True},
            )
        except Exception as exc:
            code = "event_provider_error"
            if "timeout" in str(exc).lower():
                code = "timeout"
            return EventIntelligenceResult(
                EVENT_SCHEMA_VERSION,
                EVENT_CLUSTER_VERSION,
                SOURCE_CREDIBILITY_VERSION,
                instrument.symbol,
                cutoff.isoformat(),
                self.provider_name,
                fetched.isoformat(),
                False,
                code,
                (),
                (),
                {"point_in_time": False, "future_evidence_excluded": True, "reason": code},
                {"computed_by": "python_deterministic", "read_only": True, "detail": str(exc)},
            )


def event_capabilities() -> dict[str, object]:
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "cluster_version": EVENT_CLUSTER_VERSION,
        "credibility_version": SOURCE_CREDIBILITY_VERSION,
        "supported_categories": [item.value for item in NewsCategory],
        "primary_source_preference": ["SEC", "company IR", "Federal Reserve", "official source"],
        "point_in_time_selection": True,
        "cloud_required": False,
    }
