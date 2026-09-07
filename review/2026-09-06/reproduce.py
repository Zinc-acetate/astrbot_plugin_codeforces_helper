"""Read-only audit harness: real plugin, Quart and SQLite; fake AstrBot/network/Unix flock.

No QQ messages or production databases are touched. Run on the reviewed source:
  .venv/Scripts/python.exe review/2026-09-06/reproduce.py
Exit 1 means at least one intended-behavior assertion fails in the reviewed code.
"""
import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    sys.modules[name] = value
    return value


class Filter:
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def command_group(*args, **kwargs):
        def wrap(func):
            func.command = lambda *a, **k: lambda child: child
            return func
        return wrap

    @staticmethod
    def permission_type(*args, **kwargs):
        return lambda func: func


class Star:
    def __init__(self, context):
        self.context = context


module("astrbot")
module("astrbot.api", logger=logging.getLogger("review"))
module("astrbot.api.event", filter=Filter, AstrMessageEvent=object)
module("astrbot.api.star", Context=object, Star=Star, register=lambda *a, **k: lambda cls: cls)
module("astrbot.core")
module("astrbot.core.message")
module("astrbot.core.message.components", Plain=object, Image=object)
module("astrbot.core.message.message_event_result", MessageChain=object)
module("astrbot.core.utils")
module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_data_path=tempfile.gettempdir)
if sys.platform == "win32":
    module("fcntl", LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *a: None)

main = importlib.import_module(ROOT.name + ".main")
api = importlib.import_module(ROOT.name + ".backend.api")
crawler = importlib.import_module(ROOT.name + ".core.crawler")
webui = importlib.import_module(ROOT.name + ".webui")
results = []
NOW = 1_800_000_000
DAY = 86400


def check(name, expected, actual, detail=None):
    result = {"check": name, "expected": expected, "actual": actual, "passed": expected == actual}
    if detail:
        result["detail"] = detail
    results.append(result)
    print(json.dumps(result, ensure_ascii=False))


@asynccontextmanager
async def fixture():
    with tempfile.TemporaryDirectory(prefix="cf-helper-review-") as directory:
        plugin = main.CodeforcesHelperPlugin(None, {})
        with patch.object(plugin, "_prepare_persistent_db", return_value=Path(directory) / "fixture.db"):
            await plugin.connect_db()
        try:
            webui.app.config.update(TESTING=True, DB_PATH=str(plugin.db_path), PLUGIN_CONFIG={})
            webui.app.secret_key = "isolated-review-fixture"
            yield plugin, webui.app.test_client()
        finally:
            await plugin.db.close()


async def add_user(plugin, qq="10001", handle="old_handle", last=NOW-DAY, history=30):
    await plugin.db.execute(
        """INSERT INTO users(qq_id,name,cf_handle,last_sync_timestamp,history_sync_days,
           cf_rating,cf_rank,cf_max_rating,cf_max_rank,cf_rating_updated_at)
           VALUES(?,?,?,?,?,2500,'grandmaster',2700,'international grandmaster',?)""",
        (qq, "Fixture", handle, last, history, NOW-DAY),
    )
    await plugin.db.commit()


def submission(when, verdict="OK", problem="A"):
    return {"id": when, "creationTimeSeconds": when, "verdict": verdict,
            "problem": {"contestId": 1, "index": problem, "name": "Fixture", "rating": 800}}


def response(*submissions):
    return {"status": "OK", "result": list(submissions)}


async def rows(plugin, sql, params=()):
    async with plugin.db.execute(sql, params) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def authenticate(client):
    async with client.session_transaction() as session:
        session["acm_admin"] = True


async def handle_change():
    async with fixture() as (p, client):
        await add_user(p)
        await authenticate(client)
        r = await client.post("/api/admin/users", json={"users": [
            {"qq_id": "10001", "name": "Fixture", "cf_handle": "new_handle"}]})
        assert r.status_code == 200
        row = (await rows(p, "SELECT cf_handle,cf_rating,cf_max_rating,cf_rating_updated_at FROM users"))[0]
        check("changed_handle_clears_rating", None, row["cf_rating"], row)


async def verdict_lifecycle():
    async with fixture() as (p, client):
        await add_user(p)
        statuses = AsyncMock(side_effect=[response(submission(NOW-1, "TESTING")),
                                         response(submission(NOW-1, "OK"))])
        with patch.object(crawler, "request_cf_api", statuses):
            with patch.object(main.time, "time", return_value=NOW):
                await p.sync_single_user("10001", refresh_cf_profile=False)
            with patch.object(main.time, "time", return_value=NOW+3600):
                await p.sync_single_user("10001", refresh_cf_profile=False)
        check("testing_then_ok_is_eventually_recorded", 1, len(await rows(p, "SELECT * FROM submissions")))
    async with fixture() as (p, client):
        await add_user(p)
        statuses = AsyncMock(side_effect=[response(submission(NOW-1, "OK")),
                                         response(submission(NOW-1, "CHALLENGED"))])
        with patch.object(crawler, "request_cf_api", statuses), patch.object(main.time, "time", return_value=NOW):
            await p.sync_single_user("10001", refresh_cf_profile=False)
            await p.sync_single_user("10001", refresh_cf_profile=False, days=30)
        check("deep_sync_removes_invalidated_acceptance", 0, len(await rows(p, "SELECT * FROM submissions")))


async def shallow_sync_gap(backend=False):
    async with fixture() as (p, client):
        await add_user(p, last=NOW-10*DAY)
        statuses = AsyncMock(return_value=response(submission(NOW-5*DAY)))
        with patch.object(crawler, "request_cf_api", statuses), patch.object(main.time, "time", return_value=NOW):
            if backend:
                with patch.object(crawler.Crawler, "fetch_cf_profiles", AsyncMock(return_value=0)):
                    async with webui.app.app_context():
                        await api.sync_users(days=1)
                        await api.sync_users()
            else:
                await p.sync_single_user("10001", refresh_cf_profile=False, days=1)
                await p.sync_single_user("10001", refresh_cf_profile=False)
        row = (await rows(p, "SELECT last_sync_timestamp,history_sync_days FROM users"))[0]
        check("shallow_sync_preserves_gap_"+("backend" if backend else "main"),
              1, len(await rows(p, "SELECT * FROM submissions")), row)


async def duplicate_handle():
    async with fixture() as (p, client):
        await add_user(p, qq="10001", handle="same_handle")
        await add_user(p, qq="10002", handle="same_handle")
        users = await rows(p, "SELECT * FROM users")
        profile = {"handle": "same_handle", "rating": 1900, "rank": "candidate master"}
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(profile, profile))):
            await crawler.Crawler.fetch_cf_profiles(object(), users, p.db, {})
        data = await rows(p, "SELECT qq_id,cf_rating FROM users ORDER BY qq_id")
        check("shared_handle_updates_both_members", [1900,1900], [row["cf_rating"] for row in data], data)


async def invalid_handle_batch():
    async with fixture() as (p, client):
        await add_user(p, qq="10001", handle="valid_handle")
        await add_user(p, qq="10002", handle="missing_handle")
        users = await rows(p, "SELECT * FROM users")
        async def fake_api(session, method, params, **kwargs):
            if "missing_handle" in params["handles"]:
                return {"status":"FAILED", "comment":"handles: User with handle missing_handle not found"}
            return response({"handle":"valid_handle", "rating":1900})
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=fake_api)):
            await crawler.Crawler.fetch_cf_profiles(object(), users, p.db, {})
        row = (await rows(p, "SELECT cf_rating FROM users WHERE qq_id='10001'"))[0]
        check("invalid_handle_does_not_block_other_profiles", 1900, row["cf_rating"])


async def retained_session():
    async with fixture() as (p, client):
        other = webui.app.test_client()
        for c in (client, other):
            r = await c.post("/api/admin/login", json={"password":"123456"})
            assert r.status_code == 200
        r = await client.put("/api/admin/password", json={"old_password":"123456", "new_password":"new-fixture-password"})
        assert r.status_code == 200
        r = await other.get("/api/admin/users")
        check("password_change_revokes_other_sessions", 401, r.status_code)


async def timing_consistency():
    saved = []
    for incremental in (True, False):
        async with fixture() as (p, client):
            await add_user(p, history=0)
            users = await rows(p, "SELECT * FROM users")
            if incremental:
                with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-10*DAY)))):
                    await crawler.Crawler.fetch_cf_submissions(object(), users[0], NOW-30*DAY, p.db, {})
            with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-DAY), submission(NOW-10*DAY)))):
                await crawler.Crawler.fetch_cf_submissions(object(), users[0], NOW-30*DAY, p.db, {})
            count = await rows(p, "SELECT COUNT(*) AS n FROM submissions WHERE submit_time>=?", (NOW-7*DAY,))
            saved.append(count[0]["n"])
    check("same_api_history_same_weekly_count", saved[0], saved[1], {"incremental":saved[0],"fresh_import":saved[1]})


async def report_gap():
    async with fixture() as (p, client):
        await add_user(p, last=NOW)
        user = (await rows(p, "SELECT * FROM users"))[0]
        with patch.object(main.time, "time", return_value=NOW+3600):
            before_sync = await p._generate_hourly_report_message()
        # A solve at 12:30 is only ingested at the 13:10 sync, after the 13:00 report.
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW+1800)))):
            await crawler.Crawler.fetch_cf_submissions(object(), user, NOW, p.db, {})
        with patch.object(main.time, "time", return_value=NOW+7200):
            next_report = await p._generate_hourly_report_message()
        reported = any("Fixture" in text for text in (before_sync, next_report))
        check("hourly_report_eventually_reports_late_sync", True, reported,
              {"cached_solves":len(await rows(p, "SELECT * FROM submissions"))})


async def run():
    for case in (handle_change, verdict_lifecycle, shallow_sync_gap, duplicate_handle,
                 invalid_handle_batch, retained_session, timing_consistency, report_gap):
        await case()
    await shallow_sync_gap(backend=True)
    (Path(__file__).parent / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = sum(not r["passed"] for r in results)
    print(f"Expected-behavior assertions: {len(results)}; failed: {failures}")
    return failures


if __name__ == "__main__":
    sys.exit(bool(asyncio.run(run())))
