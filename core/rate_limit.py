import asyncio
import os
import sqlite3
import tempfile
import time
from pathlib import Path


class CrossProcessRateLimiter:
    """Reserve globally spaced request slots through a small SQLite state file."""

    def __init__(self, state_path: Path, min_interval: float):
        if min_interval <= 0:
            raise ValueError("min_interval must be positive")
        self.state_path = Path(state_path)
        self.min_interval = float(min_interval)
        self._local_lock = None
        self._local_lock_owner = None

    def _get_local_lock(self) -> asyncio.Lock:
        owner = (os.getpid(), asyncio.get_running_loop())
        if self._local_lock is None or self._local_lock_owner != owner:
            self._local_lock = asyncio.Lock()
            self._local_lock_owner = owner
        return self._local_lock

    def _reserve_slot(self) -> float:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.state_path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(
                "CREATE TABLE IF NOT EXISTS rate_limit_state "
                "(id INTEGER PRIMARY KEY CHECK(id = 1), next_allowed_at REAL NOT NULL)"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_allowed_at FROM rate_limit_state WHERE id = 1"
            ).fetchone()
            now = time.time()
            scheduled_at = max(now, float(row[0])) if row else now
            db.execute(
                "INSERT INTO rate_limit_state (id, next_allowed_at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET next_allowed_at = excluded.next_allowed_at",
                (scheduled_at + self.min_interval,),
            )
            db.execute("COMMIT")
            return scheduled_at
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    async def wait(self) -> None:
        async with self._get_local_lock():
            scheduled_at = await asyncio.to_thread(self._reserve_slot)
            while True:
                delay = scheduled_at - time.time()
                if delay <= 0:
                    return
                await asyncio.sleep(delay)


CODEFORCES_API_RATE_LIMITER = CrossProcessRateLimiter(
    Path(tempfile.gettempdir()) / "astrbot_codeforces_api_rate_limit.sqlite3",
    min_interval=2.1,
)


def configure_codeforces_api_rate_limiter(db_path) -> None:
    CODEFORCES_API_RATE_LIMITER.state_path = Path(db_path).with_name(
        "codeforces_api_rate_limit.db"
    )
