"""Submission history, continuous sync coverage, and durable report acknowledgements."""
from dataclasses import dataclass

DAY = 86400
RECHECK_OVERLAP = 2 * DAY
RECONCILE_INTERVAL = 7 * DAY


async def initialize_sync_state(db):
    await db.execute("""CREATE TABLE IF NOT EXISTS cf_submission_records (
        user_qq_id TEXT NOT NULL, submission_id TEXT NOT NULL,
        problem_id TEXT NOT NULL, problem_name TEXT, problem_rating TEXT, problem_url TEXT,
        submit_time INTEGER NOT NULL, verdict TEXT NOT NULL, needs_recheck INTEGER NOT NULL,
        PRIMARY KEY(user_qq_id, submission_id))""")
    await db.execute("CREATE INDEX IF NOT EXISTS cf_records_time ON cf_submission_records(user_qq_id, submit_time)")
    await db.execute("CREATE INDEX IF NOT EXISTS cf_records_problem ON cf_submission_records(user_qq_id, verdict, problem_id, submit_time)")
    await db.execute("""CREATE TABLE IF NOT EXISTS solve_report_state (
        group_id TEXT PRIMARY KEY, since_timestamp INTEGER NOT NULL)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS solve_report_receipts (
        group_id TEXT NOT NULL, user_qq_id TEXT NOT NULL, problem_id TEXT NOT NULL,
        reported_at INTEGER NOT NULL, PRIMARY KEY(group_id, user_qq_id, problem_id))""")
    async with db.execute("SELECT value FROM settings WHERE key='submission_history_version'") as cursor:
        migrated = await cursor.fetchone()
    if not migrated:
        # Preserve legacy solves until an authoritative scan covers their timestamps.
        await db.execute("""INSERT OR IGNORE INTO cf_submission_records
            SELECT user_qq_id, 'legacy:' || problem_id, problem_id, problem_name,
                   problem_rating, problem_url, submit_time, 'OK', 0
            FROM submissions WHERE platform='codeforces'""")
        await db.execute("INSERT INTO settings(key,value) VALUES('submission_history_version','1')")


@dataclass(frozen=True)
class SyncPlan:
    start: int
    history_days: int
    reconcile: bool


async def plan_sync(db, user, now, days=None):
    history = int(user["history_sync_days"] or 0)
    last = int(user["last_sync_timestamp"] or 0)
    requested = max(1, min(int(days), 3650)) if days is not None else max(30, history)
    if days is not None or history < 30 or not last:
        start = now - requested * DAY
        if last:
            start = min(start, last - RECHECK_OVERLAP)
    else:
        start = last - RECHECK_OVERLAP
    reconcile = now - int(user["cf_reconciled_at"] or 0) >= RECONCILE_INTERVAL
    # Unlike oldest cached AC, coverage also includes intervals with only WA/no submissions.
    covered_from = int(user["cf_coverage_start"] or 0)
    if not covered_from and history:
        covered_from = max(0, (last or now) - history * DAY)
    if reconcile and covered_from:
        start = min(start, covered_from)
    async with db.execute("""SELECT MIN(submit_time) AS oldest,
        MIN(CASE WHEN needs_recheck=1 THEN submit_time END) AS pending
        FROM cf_submission_records WHERE user_qq_id=?""", (user["qq_id"],)) as cursor:
        coverage = await cursor.fetchone()
    if coverage["pending"] is not None:
        start = min(start, coverage["pending"])
    if reconcile and coverage["oldest"] is not None:
        start = min(start, coverage["oldest"])
    return SyncPlan(max(0, start), max(history, requested), reconcile)


async def finish_sync(db, user, now, plan):
    await db.execute("""UPDATE users SET last_sync_timestamp=MAX(COALESCE(last_sync_timestamp,0),?),
        history_sync_days=?, cf_reconciled_at=CASE WHEN ? THEN ? ELSE cf_reconciled_at END,
        cf_coverage_start=CASE WHEN cf_coverage_start=0 THEN ? ELSE MIN(cf_coverage_start,?) END
        WHERE qq_id=? AND LOWER(cf_handle)=LOWER(?)""",
        (now, plan.history_days, plan.reconcile, now, plan.start, plan.start, user["qq_id"], user["cf_handle"]))
    await db.commit()


async def replace_submission_window(db, qq_id, start, records):
    """Reconcile one complete API window, preserving valid solves outside it.

    Caller owns the transaction and holds the cross-process synchronization lock.
    Aggregated row IDs are preserved, and timestamps follow the first valid AC.
    """
    async with db.execute("SELECT problem_id FROM submissions WHERE user_qq_id=? AND platform='codeforces'", (qq_id,)) as cursor:
        previous = {row[0] for row in await cursor.fetchall()}
    await db.execute("DELETE FROM cf_submission_records WHERE user_qq_id=? AND submit_time>=?", (qq_id, start))
    if records:
        await db.executemany("""INSERT INTO cf_submission_records
            (user_qq_id,submission_id,problem_id,problem_name,problem_rating,problem_url,submit_time,verdict,needs_recheck)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_qq_id,submission_id) DO UPDATE SET
            problem_id=excluded.problem_id,problem_name=excluded.problem_name,
            problem_rating=excluded.problem_rating,problem_url=excluded.problem_url,
            submit_time=excluded.submit_time,verdict=excluded.verdict,needs_recheck=excluded.needs_recheck""", records)
    async with db.execute("""SELECT problem_id,problem_name,problem_rating,problem_url,submit_time FROM (
        SELECT *, ROW_NUMBER() OVER(PARTITION BY problem_id ORDER BY submit_time,submission_id) AS position
        FROM cf_submission_records WHERE user_qq_id=? AND verdict='OK'
        ) WHERE position=1""", (qq_id,)) as cursor:
        accepted = await cursor.fetchall()
    await db.execute("""DELETE FROM submissions WHERE user_qq_id=? AND platform='codeforces'
        AND problem_id NOT IN (SELECT problem_id FROM cf_submission_records WHERE user_qq_id=? AND verdict='OK')""",
        (qq_id, qq_id))
    if accepted:
        await db.executemany("""INSERT INTO submissions
            (user_qq_id,platform,problem_id,problem_name,problem_rating,problem_url,submit_time)
            VALUES(?,'codeforces',?,?,?,?,?) ON CONFLICT(user_qq_id,platform,problem_id) DO UPDATE SET
            problem_name=excluded.problem_name,problem_rating=excluded.problem_rating,
            problem_url=excluded.problem_url,submit_time=excluded.submit_time""",
            [(qq_id, row["problem_id"], row["problem_name"], row["problem_rating"], row["problem_url"], row["submit_time"]) for row in accepted])
    return len({row["problem_id"] for row in accepted} - previous)


async def delete_member_records(db, qq_id):
    for table in ("submissions", "cf_submission_records", "solve_report_receipts"):
        await db.execute(f"DELETE FROM {table} WHERE user_qq_id=?", (qq_id,))


async def ensure_report_group(db, group_id, now):
    # Start with at most the last hour on first activation; thereafter never slide it.
    await db.execute("INSERT OR IGNORE INTO solve_report_state(group_id,since_timestamp) VALUES(?,?)", (str(group_id), now-3600))
    await db.commit()


async def pending_report(db, group_id, now, limit):
    await ensure_report_group(db, group_id, now)
    async with db.execute("""SELECT s.*,u.name AS user_name FROM submissions s
        JOIN users u ON u.qq_id=s.user_qq_id
        JOIN solve_report_state state ON state.group_id=?
        WHERE s.platform='codeforces' AND s.submit_time>=state.since_timestamp
        AND NOT EXISTS(SELECT 1 FROM solve_report_receipts r WHERE r.group_id=state.group_id
            AND r.user_qq_id=s.user_qq_id AND r.problem_id=s.problem_id)
        ORDER BY s.submit_time,s.id LIMIT ?""", (str(group_id), limit)) as cursor:
        return await cursor.fetchall()


async def acknowledge_report(db, group_id, records, now):
    await db.executemany("""INSERT OR IGNORE INTO solve_report_receipts
        (group_id,user_qq_id,problem_id,reported_at) VALUES(?,?,?,?)""",
        [(str(group_id), row["user_qq_id"], row["problem_id"], now) for row in records])
    await db.commit()
