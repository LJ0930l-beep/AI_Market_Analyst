"""Evidence-backed daily crypto brief generation and local 09:00 scheduling."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .consult import QwenConsultService, parse_consult_request
from .market_intelligence import brief_source_evidence, build_market_intelligence
from .storage import SQLiteStore


# Shanghai's civil clock is UTC+08:00.  A fixed-offset stdlib timezone keeps
# the packaged Windows sidecar independent of a bundled IANA tzdata database.
BRIEF_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


async def generate_daily_brief_document(
    store: SQLiteStore,
    service: QwenConsultService,
    *,
    language: str = "zh-CN",
    model_preference: str = "auto",
    scheduled_local_date: str | None = None,
) -> dict[str, Any]:
    """Generate one brief from saved evidence; never substitute invented prices."""
    if language not in {"en", "zh-CN"}:
        raise ValueError("INVALID_BRIEF_LANGUAGE")
    view = build_market_intelligence(store)
    source_hash, sources, missing = brief_source_evidence(view)
    evidence = json.dumps(
        {key: view[key] for key in ("as_of", "pulse", "calendar", "watchlist", "heatmap", "news")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    instruction = (
        "请仅依据以下本地已保存证据生成简洁的每日加密货币市场简报，分为隔夜变化、今日事件、热门领域、关注资产和风险；缺失项要明确说明。"
        if language == "zh-CN"
        else "Using only the following locally saved evidence, write a concise daily crypto market brief covering overnight changes, today's events, hot areas, focus assets, and risks. State missing evidence explicitly."
    )
    evidence_budget = max(512, service.config.max_message_chars - len(instruction) - 96)
    request = parse_consult_request(
        {
            "language": language, "model_preference": model_preference, "task": "daily_brief",
            "messages": [{"role": "user", "content": f"{instruction}\nBEGIN_SAVED_EVIDENCE\n{evidence[:evidence_budget]}\nEND_SAVED_EVIDENCE"}],
        },
        service.config,
    )
    session = await service.open(request, store=store)
    content: list[str] = []
    metadata: dict[str, Any] = {}
    async for event in session.events():
        if event.get("type") == "meta":
            metadata = event
        elif event.get("type") == "delta" and isinstance(event.get("content"), str):
            content.append(event["content"])
        elif event.get("type") == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            raise RuntimeError(f"{error.get('code', 'MODEL_STREAM_FAILED')}: {error.get('message', 'Daily brief generation failed')}")
    rendered = "".join(content).strip()
    if not rendered:
        raise RuntimeError("MODEL_EMPTY_BRIEF")
    route = metadata.get("model_route") if isinstance(metadata.get("model_route"), dict) else {}
    capability = {"provider": metadata.get("provider"), "contract_version": "daily_brief_v1"}
    if scheduled_local_date:
        capability.update(generation="scheduled_09_asia_shanghai", scheduled_local_date=scheduled_local_date)
    else:
        capability.update(generation="manual", explicit_generation=True)
    payload = {
        "brief_id": str(uuid4()), "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": view["as_of"], "language": language,
        "model_id": metadata.get("model_id", "unknown"), "model_tier": metadata.get("model_tier", "unknown"),
        "route_reason": route.get("reason", "unknown"), "source_hash": source_hash,
        "sources": sources, "missing": missing, "content": rendered, "capability": capability,
    }
    store.save_daily_brief(payload)
    return payload


class DailyBriefSchedule:
    """One durable claim per Shanghai calendar day, with retry after failures."""

    def __init__(
        self, store: SQLiteStore, service: QwenConsultService,
        *, clock: Callable[[], datetime] | None = None, retry_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self.store = store
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retry_after = retry_after
        self._task: asyncio.Task[None] | None = None
        with self.store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS daily_brief_schedule (
                local_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                claimed_at TEXT NOT NULL, completed_at TEXT, error_code TEXT
            )""")

    def _claim(self, local_date: str, now: datetime) -> bool:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A process can exit after saving the document but before marking
            # the claim complete.  The saved scheduled document is the source
            # of truth on the next start; never generate a duplicate for it.
            saved = db.execute(
                "SELECT 1 FROM daily_briefs WHERE json_extract(capability_json, '$.scheduled_local_date')=? LIMIT 1",
                (local_date,),
            ).fetchone()
            if saved:
                db.execute(
                    """INSERT INTO daily_brief_schedule(local_date, status, claimed_at, completed_at, error_code)
                       VALUES(?, 'SUCCEEDED', ?, ?, NULL)
                       ON CONFLICT(local_date) DO UPDATE SET status='SUCCEEDED', completed_at=excluded.completed_at, error_code=NULL""",
                    (local_date, now.isoformat(), now.isoformat()),
                )
                return False
            row = db.execute("SELECT status, claimed_at FROM daily_brief_schedule WHERE local_date=?", (local_date,)).fetchone()
            if row:
                if row["status"] == "SUCCEEDED":
                    return False
                claimed = datetime.fromisoformat(row["claimed_at"])
                if now - claimed < self.retry_after:
                    return False
            db.execute(
                """INSERT INTO daily_brief_schedule(local_date, status, claimed_at, completed_at, error_code)
                   VALUES(?, 'RUNNING', ?, NULL, NULL)
                   ON CONFLICT(local_date) DO UPDATE SET status='RUNNING', claimed_at=excluded.claimed_at,
                       completed_at=NULL, error_code=NULL""",
                (local_date, now.isoformat()),
            )
        return True

    async def run_due_once(self) -> bool:
        now = self.clock().astimezone(timezone.utc)
        local = now.astimezone(BRIEF_TIMEZONE)
        if local.hour < 9:
            return False
        local_date = local.date().isoformat()
        if not self._claim(local_date, now):
            return False
        try:
            await generate_daily_brief_document(
                self.store, self.service, language="zh-CN", scheduled_local_date=local_date,
            )
        except Exception as exc:
            with self.store._connect() as db:
                db.execute("UPDATE daily_brief_schedule SET status='FAILED', completed_at=?, error_code=? WHERE local_date=?",
                           (datetime.now(timezone.utc).isoformat(), type(exc).__name__, local_date))
            return False
        with self.store._connect() as db:
            db.execute("UPDATE daily_brief_schedule SET status='SUCCEEDED', completed_at=? WHERE local_date=?",
                       (datetime.now(timezone.utc).isoformat(), local_date))
        return True

    def status(self) -> dict[str, Any]:
        local_date = self.clock().astimezone(BRIEF_TIMEZONE).date().isoformat()
        with self.store._connect() as db:
            row = db.execute("SELECT status, claimed_at, completed_at, error_code FROM daily_brief_schedule WHERE local_date=?", (local_date,)).fetchone()
        return {"timezone": "Asia/Shanghai", "time": "09:00", "local_date": local_date,
                "status": row["status"] if row else "PENDING",
                "claimed_at": row["claimed_at"] if row else None,
                "completed_at": row["completed_at"] if row else None,
                "error_code": row["error_code"] if row else None}

    async def _worker(self) -> None:
        while True:
            await self.run_due_once()
            await asyncio.sleep(60)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="daily-crypto-brief-09-shanghai")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
