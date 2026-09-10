"""Immutable News Revision tracking system.

Fulfills R10 & AT19 requirements:
- Immutable historical news revisions identified by unique `revision_id` and sha256 `content_hash`.
- When news is retracted or corrected, a new revision is created with `previous_revision_id` and `correction_reason`.
- Decisions link to immutable `revision_id`. When replaying a decision, the historical snapshot is preserved.
- Retractions or corrections produce a list of affected historical decisions without mutating original records.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class NewsRevision:
    news_id: str
    revision_id: str
    source_url: str
    publisher: str
    published_at: str
    first_seen_at: str
    fetched_at: str
    content_hash: str
    headline: str
    body_or_excerpt: str
    claim_status: str  # "UNVERIFIED", "PRIMARY_SOURCE_VERIFIED", "CORROBORATED", "DISPUTED", "RETRACTED"
    sentiment: float
    macro_policy: str
    source_tier: str  # "TIER_A_OFFICIAL", "TIER_B_MEDIA", "TIER_C_SOCIAL"
    previous_revision_id: Optional[str] = None
    correction_reason: Optional[str] = None
    affected_decision_ids: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["affected_decision_ids"] = list(self.affected_decision_ids)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> NewsRevision:
        return cls(
            news_id=str(data["news_id"]),
            revision_id=str(data["revision_id"]),
            source_url=str(data.get("source_url", "")),
            publisher=str(data.get("publisher", "")),
            published_at=str(data.get("published_at", "")),
            first_seen_at=str(data.get("first_seen_at", "")),
            fetched_at=str(data.get("fetched_at", "")),
            content_hash=str(data["content_hash"]),
            headline=str(data.get("headline", "")),
            body_or_excerpt=str(data.get("body_or_excerpt", "")),
            claim_status=str(data.get("claim_status", "UNVERIFIED")),
            sentiment=float(data.get("sentiment", 0.0)),
            macro_policy=str(data.get("macro_policy", "NEUTRAL")),
            source_tier=str(data.get("source_tier", "TIER_C_SOCIAL")),
            previous_revision_id=data.get("previous_revision_id"),
            correction_reason=data.get("correction_reason"),
            affected_decision_ids=tuple(data.get("affected_decision_ids", ())),
        )


class NewsRevisionRegistry:
    """Registry and store for immutable news revisions and decision associations."""

    def __init__(self, store=None):
        self.store = store
        self._in_memory_revisions: Dict[str, NewsRevision] = {}  # revision_id -> NewsRevision
        self._news_to_revisions: Dict[str, List[str]] = {}  # news_id -> list[revision_id]
        self._decision_to_revision: Dict[str, str] = {}  # decision_id -> revision_id
        self._revision_to_decisions: Dict[str, List[str]] = {}  # revision_id -> list[decision_id]
        if self.store is not None:
            self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self.store._connect() as db:
            db.execute("""
            CREATE TABLE IF NOT EXISTS news_revisions (
                revision_id TEXT PRIMARY KEY,
                news_id TEXT NOT NULL,
                source_url TEXT,
                publisher TEXT,
                published_at TEXT,
                first_seen_at TEXT,
                fetched_at TEXT,
                content_hash TEXT NOT NULL,
                headline TEXT,
                body_or_excerpt TEXT,
                claim_status TEXT NOT NULL,
                sentiment REAL NOT NULL,
                macro_policy TEXT NOT NULL,
                source_tier TEXT NOT NULL,
                previous_revision_id TEXT,
                correction_reason TEXT,
                affected_decision_ids_json TEXT
            )
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS decision_news_links (
                decision_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)

    @staticmethod
    def compute_content_hash(headline: str, body: str) -> str:
        content = f"{headline.strip()}\n{body.strip()}".encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def record_news(
        self,
        news_id: str,
        headline: str,
        body_or_excerpt: str,
        *,
        source_url: str = "",
        publisher: str = "",
        published_at: str = "",
        first_seen_at: str = "",
        claim_status: str = "UNVERIFIED",
        sentiment: float = 0.0,
        macro_policy: str = "NEUTRAL",
        source_tier: str = "TIER_C_SOCIAL",
    ) -> NewsRevision:
        now_iso = datetime.now(timezone.utc).isoformat()
        content_hash = self.compute_content_hash(headline, body_or_excerpt)
        revision_id = f"rev_{uuid.uuid4().hex[:12]}"

        rev = NewsRevision(
            news_id=news_id,
            revision_id=revision_id,
            source_url=source_url,
            publisher=publisher,
            published_at=published_at or now_iso,
            first_seen_at=first_seen_at or now_iso,
            fetched_at=now_iso,
            content_hash=content_hash,
            headline=headline,
            body_or_excerpt=body_or_excerpt,
            claim_status=claim_status,
            sentiment=sentiment,
            macro_policy=macro_policy,
            source_tier=source_tier,
            previous_revision_id=None,
            correction_reason=None,
            affected_decision_ids=(),
        )

        self._in_memory_revisions[revision_id] = rev
        self._news_to_revisions.setdefault(news_id, []).append(revision_id)

        if self.store is not None:
            with self.store._connect() as db:
                db.execute(
                    """
                    INSERT INTO news_revisions (
                        revision_id, news_id, source_url, publisher, published_at,
                        first_seen_at, fetched_at, content_hash, headline, body_or_excerpt,
                        claim_status, sentiment, macro_policy, source_tier,
                        previous_revision_id, correction_reason, affected_decision_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev.revision_id, rev.news_id, rev.source_url, rev.publisher, rev.published_at,
                        rev.first_seen_at, rev.fetched_at, rev.content_hash, rev.headline, rev.body_or_excerpt,
                        rev.claim_status, rev.sentiment, rev.macro_policy, rev.source_tier,
                        rev.previous_revision_id, rev.correction_reason, json.dumps([]),
                    ),
                )
        return rev

    def correct_news(
        self,
        news_id: str,
        new_headline: str,
        new_body: str,
        *,
        correction_reason: str,
        claim_status: str = "RETRACTED",
        sentiment: float = 0.0,
        macro_policy: str = "NEUTRAL",
        source_url: str = "",
        source_tier: str = "",
    ) -> NewsRevision:
        """Create a new immutable revision correcting or retracting earlier news.
        
        Calculates affected decisions that relied on prior revisions of this news.
        """
        prior_revision_ids = self._news_to_revisions.get(news_id, [])
        if not prior_revision_ids and self.store is not None:
            # A correction may be received by a fresh process after restart.
            # Load the durable chain before creating a new revision; otherwise
            # the correction path would incorrectly report that the news did
            # not exist and would not invalidate plans holding its old ID.
            with self.store._connect() as db:
                prior_revision_ids = [
                    str(row["revision_id"])
                    for row in db.execute(
                        "SELECT revision_id FROM news_revisions WHERE news_id=? ORDER BY fetched_at ASC",
                        (news_id,),
                    ).fetchall()
                ]
            self._news_to_revisions[news_id] = prior_revision_ids
        if not prior_revision_ids:
            raise ValueError(f"Cannot correct non-existent news_id: {news_id}")

        latest_prev_id = prior_revision_ids[-1]
        prev_rev = self.get_revision(latest_prev_id)

        # Collect all decisions that referenced any prior revision of this news
        affected = []
        for pid in prior_revision_ids:
            affected.extend(self._revision_to_decisions.get(pid, []))
        affected_tuple = tuple(sorted(set(affected)))

        now_iso = datetime.now(timezone.utc).isoformat()
        content_hash = self.compute_content_hash(new_headline, new_body)
        revision_id = f"rev_{uuid.uuid4().hex[:12]}"

        new_rev = NewsRevision(
            news_id=news_id,
            revision_id=revision_id,
            source_url=source_url or (prev_rev.source_url if prev_rev else ""),
            publisher=prev_rev.publisher if prev_rev else "",
            published_at=now_iso,
            first_seen_at=prev_rev.first_seen_at if prev_rev else now_iso,
            fetched_at=now_iso,
            content_hash=content_hash,
            headline=new_headline,
            body_or_excerpt=new_body,
            claim_status=claim_status,
            sentiment=sentiment,
            macro_policy=macro_policy,
            source_tier=source_tier or (prev_rev.source_tier if prev_rev else "TIER_C_SOCIAL"),
            previous_revision_id=latest_prev_id,
            correction_reason=correction_reason,
            affected_decision_ids=affected_tuple,
        )

        self._in_memory_revisions[revision_id] = new_rev
        self._news_to_revisions.setdefault(news_id, []).append(revision_id)

        if self.store is not None:
            with self.store._connect() as db:
                db.execute(
                    """
                    INSERT INTO news_revisions (
                        revision_id, news_id, source_url, publisher, published_at,
                        first_seen_at, fetched_at, content_hash, headline, body_or_excerpt,
                        claim_status, sentiment, macro_policy, source_tier,
                        previous_revision_id, correction_reason, affected_decision_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_rev.revision_id, new_rev.news_id, new_rev.source_url, new_rev.publisher, new_rev.published_at,
                        new_rev.first_seen_at, new_rev.fetched_at, new_rev.content_hash, new_rev.headline, new_rev.body_or_excerpt,
                        new_rev.claim_status, new_rev.sentiment, new_rev.macro_policy, new_rev.source_tier,
                        new_rev.previous_revision_id, new_rev.correction_reason, json.dumps(list(affected_tuple)),
                    ),
                )
        return new_rev

    def create_revision(
        self,
        news_id: str,
        headline: str = "",
        body_or_excerpt: str = "",
        *,
        source_url: str = "",
        publisher: str = "",
        published_at: str = "",
        first_seen_at: str = "",
        claim_status: str = "UNVERIFIED",
        sentiment: float = 0.0,
        macro_policy: str = "NEUTRAL",
        source_tier: str = "TIER_C_SOCIAL",
        previous_revision_id: Optional[str] = None,
        correction_reason: Optional[str] = None,
        affected_decision_ids: tuple[str, ...] = (),
    ) -> NewsRevision:
        """Alias for record_news matching specification naming."""
        return self.record_news(
            news_id=news_id,
            headline=headline,
            body_or_excerpt=body_or_excerpt,
            source_url=source_url,
            publisher=publisher,
            published_at=published_at,
            first_seen_at=first_seen_at,
            claim_status=claim_status,
            sentiment=sentiment,
            macro_policy=macro_policy,
            source_tier=source_tier,
        )

    def record_revision(self, rev: NewsRevision) -> NewsRevision:
        """Idempotent recording of a NewsRevision."""
        self._in_memory_revisions[rev.revision_id] = rev
        if rev.revision_id not in self._news_to_revisions.get(rev.news_id, []):
            self._news_to_revisions.setdefault(rev.news_id, []).append(rev.revision_id)
        return rev

    def record_correction(
        self,
        news_id: str,
        *,
        correction_reason: str,
        headline: str = "",
        body_or_excerpt: str = "",
        new_headline: str = "",
        new_body: str = "",
        claim_status: str = "RETRACTED",
        sentiment: float = 0.0,
        macro_policy: str = "NEUTRAL",
        source_url: str = "",
        source_tier: str = "",
    ) -> NewsRevision:
        """Alias for correct_news matching specification naming."""
        h = headline or new_headline
        b = body_or_excerpt or new_body
        return self.correct_news(
            news_id=news_id,
            new_headline=h,
            new_body=b,
            correction_reason=correction_reason,
            claim_status=claim_status,
            sentiment=sentiment,
            macro_policy=macro_policy,
            source_url=source_url,
            source_tier=source_tier,
        )

    def link_decision(self, arg1: str, arg2: str) -> None:
        """Associate a trade or AI decision with an immutable revision.
        
        Supports both (decision_id, revision_id) and (revision_id, decision_id).
        """
        if arg2.startswith("rev_") or arg2 in self._in_memory_revisions:
            decision_id = arg1
            revision_id = arg2
        elif arg1.startswith("rev_") or arg1 in self._in_memory_revisions:
            revision_id = arg1
            decision_id = arg2
        else:
            decision_id = arg1
            revision_id = arg2

        if revision_id not in self._in_memory_revisions and self.store is not None:
            self.get_revision(revision_id)

        self._decision_to_revision[decision_id] = revision_id
        self._revision_to_decisions.setdefault(revision_id, []).append(decision_id)

        if self.store is not None:
            with self.store._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO decision_news_links (decision_id, revision_id, created_at) VALUES (?, ?, ?)",
                    (decision_id, revision_id, datetime.now(timezone.utc).isoformat()),
                )

    def get_revision(self, revision_id: str) -> Optional[NewsRevision]:
        if revision_id in self._in_memory_revisions:
            return self._in_memory_revisions[revision_id]
        if self.store is not None:
            with self.store._connect() as db:
                row = db.execute(
                    "SELECT * FROM news_revisions WHERE revision_id = ?", (revision_id,)
                ).fetchone()
                if row:
                    data = dict(row)
                    data["affected_decision_ids"] = json.loads(data.get("affected_decision_ids_json") or "[]")
                    rev = NewsRevision.from_dict(data)
                    self._in_memory_revisions[revision_id] = rev
                    return rev
        return None

    def get_revisions_for_news(self, news_id: str) -> List[NewsRevision]:
        rev_ids = self._news_to_revisions.get(news_id, [])
        if not rev_ids and self.store is not None:
            with self.store._connect() as db:
                rows = db.execute(
                    "SELECT revision_id FROM news_revisions WHERE news_id = ? ORDER BY fetched_at ASC",
                    (news_id,),
                ).fetchall()
                rev_ids = [r["revision_id"] for r in rows]
                self._news_to_revisions[news_id] = rev_ids
        return [self.get_revision(rid) for rid in rev_ids if self.get_revision(rid) is not None]

    def get_linked_revision_for_decision(self, decision_id: str) -> Optional[NewsRevision]:
        rid = self._decision_to_revision.get(decision_id)
        if not rid and self.store is not None:
            with self.store._connect() as db:
                row = db.execute(
                    "SELECT revision_id FROM decision_news_links WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if row:
                    rid = row["revision_id"]
                    self._decision_to_revision[decision_id] = rid
        if rid:
            return self.get_revision(rid)
        return None
