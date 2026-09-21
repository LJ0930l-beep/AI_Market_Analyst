"""Strategy cadence and durable account-slot deduplication across restarts."""
from datetime import datetime, timezone


def aligned_at(value, minutes=15, *, next_slot=False):
    if minutes not in {5, 15}:
        raise ValueError('STRATEGY_SCAN_INTERVAL_INVALID')
    interval = minutes * 60
    slot = int(value.timestamp()) // interval + int(next_slot)
    return datetime.fromtimestamp(slot * interval, timezone.utc)


class StrategySchedule:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ai_schedule_slots (
                account_id TEXT NOT NULL, scheduled_at TEXT NOT NULL,
                strategy_revision INTEGER NOT NULL, claimed_at TEXT NOT NULL,
                PRIMARY KEY(account_id, scheduled_at))''')

    def claim(self, account_id, scheduled_at, revision):
        with self.store._connect() as db:
            result = db.execute('''INSERT OR IGNORE INTO ai_schedule_slots
                (account_id, scheduled_at, strategy_revision, claimed_at) VALUES(?,?,?,?)''',
                (account_id, scheduled_at.isoformat(), revision, datetime.now(timezone.utc).isoformat()))
            return result.rowcount == 1
