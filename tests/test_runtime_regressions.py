import asyncio
import sqlite3
import types
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.runtime_support import PluginTestCase, NOW, DAY, main, api, crawler, webui, submission, response


class SyncRegressionTests(PluginTestCase):
    async def test_successful_pagination_reaches_older_acceptance(self):
        await self.add_user()
        page = [submission(NOW-i, sid=1000+i) for i in range(100)]
        request = AsyncMock(side_effect=[response(*page), response(submission(NOW-10*DAY))])
        with patch.object(crawler, "request_cf_api", request):
            self.assertEqual(await self.sync(days=30), (1, True))
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.call_args_list[1].args[2]["from"], "101")
        self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-10*DAY)

    async def test_malformed_response_does_not_erase_prior_records(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[response(submission(NOW-1)), {"status":"OK"}])):
            await self.sync()
            self.assertEqual(await self.sync(now=NOW+3600), (0, False))
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)

    async def test_transaction_failure_restores_original_snapshot(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-1)))):
            await self.sync()
            async def fail_after_delete(db, *args):
                await db.execute("DELETE FROM cf_submission_records")
                await db.execute("DELETE FROM submissions")
                raise RuntimeError("injected write failure")
            with patch.object(crawler, "replace_submission_window", fail_after_delete), self.assertRaises(RuntimeError):
                await self.sync(now=NOW+3600)
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM cf_submission_records")), 1)
        self.assertEqual((await self.rows("SELECT last_sync_timestamp FROM users"))[0]["last_sync_timestamp"], NOW)

    async def test_pending_verdict_is_revisited_even_after_overlap_window(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(NOW-1, "TESTING")), response(submission(NOW-1, "OK"))])):
            self.assertEqual(await self.sync(), (0, True))
            self.assertEqual(await self.sync(now=NOW+5*DAY), (1, True))
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)

    async def test_pretests_acceptance_is_rechecked(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(NOW-1, testset="PRETESTS")),
            response(submission(NOW-1, "CHALLENGED", testset="TESTS"))])):
            await self.sync()
            await self.sync(now=NOW+5*DAY)
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])

    async def test_short_manual_sync_does_not_skip_unsynced_gap_in_both_entrypoints(self):
        await self.add_user(last=NOW-10*DAY)
        for backend in (False, True):
            with self.subTest(backend=backend):
                await self.plugin.db.execute("DELETE FROM submissions")
                await self.plugin.db.execute("UPDATE users SET last_sync_timestamp=?", (NOW-10*DAY,))
                await self.plugin.db.commit()
                with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-5*DAY)))):
                    if backend:
                        with patch.object(crawler.Crawler, "fetch_cf_profiles", AsyncMock(return_value=0)), patch.object(main.time, "time", return_value=NOW):
                            async with webui.app.app_context():
                                await api.sync_users(days=1)
                                await api.sync_users()
                    else:
                        await self.sync(days=1)
                        await self.sync()
                self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)

    async def test_first_valid_acceptance_is_independent_of_import_order(self):
        await self.add_user()
        old, new = submission(NOW-10*DAY), submission(NOW-DAY)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(new, old))):
            await self.sync(days=30)
        self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-10*DAY)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(new, old))):
            self.assertEqual(await self.sync(days=30), (0, True))

    async def test_deep_sync_corrects_rejudge_without_deleting_other_valid_ac(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(NOW-DAY), submission(NOW-10*DAY)),
            response(submission(NOW-DAY, "CHALLENGED")),
            response(submission(NOW-DAY, "CHALLENGED"), submission(NOW-10*DAY, "WRONG_ANSWER"))])):
            await self.sync(days=30)
            await self.sync(days=2)
            self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-10*DAY)
            await self.sync(days=30)
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])

    async def test_first_ac_invalidated_moves_to_next_valid_ac(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(NOW-DAY), submission(NOW-10*DAY)),
            response(submission(NOW-DAY), submission(NOW-10*DAY, "CHALLENGED"))])):
            await self.sync(days=30)
            await self.sync(days=30)
        self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-DAY)

    async def test_failed_second_page_does_not_write_or_advance_cursor(self):
        await self.add_user(last=NOW-3*DAY)
        page = [submission(NOW-i, sid=i) for i in range(100)]
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[response(*page), RuntimeError("offline")])):
            self.assertEqual(await self.sync(), (0, False))
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])
        self.assertEqual((await self.rows("SELECT last_sync_timestamp FROM users"))[0]["last_sync_timestamp"], NOW-3*DAY)

    async def test_periodic_reconciliation_corrects_very_old_rejudge(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(NOW-100*DAY)), response(submission(NOW-100*DAY, "CHALLENGED"))])):
            await self.sync(days=365)
            await self.sync(now=NOW+8*DAY)
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])

    async def test_handle_changed_during_request_discards_inflight_data(self):
        await self.add_user()
        async def changed(*a, **kw):
            await self.plugin.db.execute("UPDATE users SET cf_handle='new_handle'")
            await self.plugin.db.commit()
            return response(submission(NOW-1))
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=changed)):
            self.assertEqual(await self.sync(), (0, False))
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])


class MemberRegressionTests(PluginTestCase):
    async def test_console_html_and_json_are_not_cached(self):
        for path in ("/", "/index.html", "/api/leaderboard"):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    async def test_concurrent_password_rotation_cannot_mint_new_session_with_old_password(self):
        real_check = api.check_password_hash
        def check_then_rotate(password_hash, password):
            valid = real_check(password_hash, password)
            with closing(sqlite3.connect(self.plugin.db_path)) as db, db:
                db.execute("UPDATE settings SET value='rotated-version' WHERE key='admin_session_version'")
            return valid
        with patch.object(api, "check_password_hash", check_then_rotate):
            await self.login()
        self.assertEqual((await self.client.get("/api/admin/users")).status_code, 401)

    async def test_clearing_handle_or_deleting_user_cleans_submission_history(self):
        await self.add_user()
        await self.login()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-1)))):
            await self.sync()
        await self.plugin.db.execute("INSERT INTO solve_report_receipts VALUES('12345','10001','cf_1A',?)", (NOW,))
        await self.plugin.db.commit()
        r = await self.client.post("/api/admin/users", json={"users":[{"qq_id":"10001", "name":"Fixture", "cf_handle":""}]})
        self.assertEqual(r.status_code, 200)
        for table in ("submissions", "cf_submission_records", "solve_report_receipts"):
            self.assertEqual(await self.rows("SELECT * FROM "+table), [])
        row = (await self.rows("SELECT * FROM users"))[0]
        self.assertIsNone(row["cf_handle"])
        self.assertIsNone(row["cf_rating"])
        r = await self.client.delete("/api/admin/users", json={"qq_ids":["10001"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(await self.rows("SELECT * FROM users"), [])

    async def test_member_edit_is_rejected_while_real_sync_lock_is_held(self):
        await self.add_user()
        await self.login()
        with main.acquire_sync_lock(self.plugin.db_path):
            r = await self.client.post("/api/admin/users", json={"users":[{"qq_id":"10001", "name":"Fixture", "cf_handle":"new_handle"}]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((await self.rows("SELECT cf_handle FROM users"))[0]["cf_handle"], "old_handle")

    async def test_handle_change_clears_all_account_cache(self):
        await self.add_user()
        await self.login()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-1)))):
            await self.sync()
        r = await self.client.post("/api/admin/users", json={"users":[
            {"qq_id":"10001", "name":"Fixture", "cf_handle":"new_handle"}]})
        self.assertEqual(r.status_code, 200)
        row = (await self.rows("SELECT * FROM users"))[0]
        for key in ("cf_rating", "cf_max_rating", "cf_rank", "cf_max_rank"):
            self.assertIsNone(row[key], key)
        for key in ("cf_rating_updated_at", "last_sync_timestamp", "history_sync_days"):
            self.assertEqual(row[key], 0, key)
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])

    async def test_member_name_and_handle_validation_is_atomic(self):
        await self.login()
        for bad in ('bad" onclick="alert(1)', "bad;other", "bad handle", "<svg>"):
            r = await self.client.post("/api/admin/users", json={"users":[
                {"qq_id":"10001", "name":"Valid", "cf_handle":"tourist"},
                {"qq_id":"10002", "name":"Invalid", "cf_handle":bad}]})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(await self.rows("SELECT * FROM users"), [])

    async def test_case_only_handle_edit_preserves_records(self):
        await self.add_user()
        await self.login()
        r = await self.client.post("/api/admin/users", json={"users":[
            {"qq_id":"10001", "name":"Updated", "cf_handle":"OLD_HANDLE"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.rows("SELECT cf_rating FROM users"))[0]["cf_rating"], 2500)

    async def test_duplicate_handle_updates_each_member_once(self):
        await self.add_user(handle="same_handle")
        await self.add_user(qq="10002", handle="SAME_HANDLE")
        request = AsyncMock(return_value=response({"handle":"same_handle", "rating":1900}))
        with patch.object(crawler, "request_cf_api", request):
            updated = await crawler.Crawler.fetch_cf_profiles(object(), await self.rows("SELECT * FROM users"), self.plugin.db, {})
        self.assertEqual(updated, 2)
        self.assertEqual([x["cf_rating"] for x in await self.rows("SELECT cf_rating FROM users")], [1900,1900])
        self.assertEqual(request.call_args.args[2]["handles"].lower(), "same_handle")

    async def test_invalid_handle_is_isolated_from_valid_handles(self):
        await self.add_user(handle="valid_handle")
        await self.add_user(qq="10002", handle="missing_handle")
        async def fake(session, method, params, **kw):
            if "missing_handle" in params["handles"]:
                return {"status":"FAILED", "comment":"handles: User with handle missing_handle not found"}
            return response({"handle":"valid_handle", "rating":1900})
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=fake)):
            count = await crawler.Crawler.fetch_cf_profiles(object(), await self.rows("SELECT * FROM users"), self.plugin.db, {})
        self.assertEqual(count, 1)
        self.assertEqual((await self.rows("SELECT cf_rating FROM users WHERE qq_id='10001'"))[0]["cf_rating"], 1900)

    async def test_service_outage_does_not_trigger_per_member_retry_storm(self):
        for i in range(4):
            await self.add_user(qq=str(10001+i), handle="handle"+str(i))
        request = AsyncMock(side_effect=TimeoutError("offline"))
        with patch.object(crawler, "request_cf_api", request):
            self.assertEqual(await crawler.Crawler.fetch_cf_profiles(object(), await self.rows("SELECT * FROM users"), self.plugin.db, {}), 0)
        self.assertEqual(request.await_count, 1)

    async def test_old_profile_cannot_overwrite_changed_handle(self):
        await self.add_user()
        users = await self.rows("SELECT * FROM users")
        await self.plugin.db.execute("UPDATE users SET cf_handle='new_handle',cf_rating=NULL")
        await self.plugin.db.commit()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response({"handle":"old_handle", "rating":1900}))):
            self.assertEqual(await crawler.Crawler.fetch_cf_profiles(object(), users, self.plugin.db, {}), 0)
        self.assertIsNone((await self.rows("SELECT cf_rating FROM users"))[0]["cf_rating"])

    async def test_password_change_revokes_other_sessions(self):
        other = webui.app.test_client()
        await self.login()
        await self.login(other)
        r = await self.client.put("/api/admin/password", json={"old_password":"123456", "new_password":"new-fixture-password"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await other.get("/api/admin/users")).status_code, 401)
        self.assertFalse((await (await other.get("/api/admin/session")).get_json())["authenticated"])
        await self.login(other, "new-fixture-password")
        self.assertEqual((await other.get("/api/admin/users")).status_code, 200)


class ReportRegressionTests(PluginTestCase):
    async def configure(self):
        await self.add_user(last=NOW)
        await self.plugin.set_setting("notification_group_id", "12345")
        self.bot = types.SimpleNamespace(send_group_msg=AsyncMock())
        self.plugin.context = types.SimpleNamespace(get_platform=lambda name: types.SimpleNamespace(bot=self.bot))

    async def report(self, now):
        with patch.object(main.time, "time", return_value=now):
            await self.plugin.report_hourly_solves()

    async def test_late_ingestion_is_sent_once_and_survives_restart(self):
        await self.configure()
        await self.report(NOW+3600)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW+1800)))):
            await self.sync(now=NOW+4200)
        await self.report(NOW+7200)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.connect_db()
        await self.report(NOW+10800)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_send_failure_keeps_pending_record_for_retry(self):
        await self.configure()
        await self.report(NOW)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW+60)))):
            await self.sync(now=NOW+120)
        self.bot.send_group_msg.side_effect = RuntimeError("OneBot offline")
        await self.report(NOW+3600)
        self.bot.send_group_msg.side_effect = None
        await self.report(NOW+7200)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        await self.report(NOW+10800)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_limit_does_not_discard_remaining_pending_records(self):
        await self.configure()
        await self.plugin.set_setting("hourly_report_limit", "1")
        await self.report(NOW)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(
                submission(NOW+20, problem="B"), submission(NOW+10)))):
            await self.sync(now=NOW+30)
        await self.report(NOW+3600)
        await self.report(NOW+7200)
        await self.report(NOW+10800)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_old_history_backfill_does_not_flood_group(self):
        await self.configure()
        await self.report(NOW)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-20*DAY)))):
            await self.sync(days=30)
        await self.report(NOW+3600)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_concurrent_reporters_do_not_send_same_record_twice(self):
        await self.configure()
        await self.report(NOW)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW+60)))):
            await self.sync(now=NOW+120)
        await asyncio.gather(self.report(NOW+3600), self.report(NOW+3600))
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_manual_hourly_query_keeps_requested_time_window(self):
        await self.configure()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-3*3600)))):
            await self.sync()
        with patch.object(main.time, "time", return_value=NOW):
            self.assertNotIn("Fixture", await self.plugin._generate_hourly_report_message(hours=1))
            self.assertIn("Fixture", await self.plugin._generate_hourly_report_message(hours=4))


class MigrationRegressionTests(PluginTestCase):
    async def test_upgrade_rechecks_entire_previously_covered_window(self):
        await self.create_old_database()
        await self.plugin.db.execute("UPDATE users SET history_sync_days=30,last_sync_timestamp=?", (NOW,))
        await self.plugin.db.execute("UPDATE submissions SET submit_time=?", (NOW-DAY,))
        await self.plugin.db.execute("UPDATE cf_submission_records SET submit_time=?", (NOW-DAY,))
        await self.plugin.db.commit()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-DAY), submission(NOW-10*DAY)))):
            await self.sync()
        self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-10*DAY)

    async def create_old_database(self):
        await self.plugin.db.close()
        path = Path(self.temporary.name) / "old-schema.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE users(qq_id TEXT PRIMARY KEY,name TEXT NOT NULL,cf_handle TEXT,
                    status TEXT,school TEXT,last_sync_timestamp INTEGER DEFAULT 0);
                INSERT INTO users(qq_id,name,cf_handle) VALUES('10001','Fixture','old_handle');
                CREATE TABLE submissions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_qq_id TEXT NOT NULL,
                    platform TEXT NOT NULL,problem_id TEXT NOT NULL,problem_name TEXT,problem_rating TEXT,
                    problem_url TEXT,submit_time INTEGER NOT NULL,UNIQUE(user_qq_id,platform,problem_id));""")
            db.execute("INSERT INTO submissions(user_qq_id,platform,problem_id,problem_name,submit_time) VALUES('10001','codeforces','cf_1A','Fixture',?)", (NOW-10*DAY,))
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=path):
            await self.plugin.connect_db()

    async def test_upgrade_preserves_legacy_solves_until_successful_reconciliation(self):
        await self.create_old_database()
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)
        self.assertTrue((await self.rows("SELECT submission_id FROM cf_submission_records"))[0]["submission_id"].startswith("legacy:"))
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=RuntimeError("offline"))):
            self.assertEqual(await self.sync(), (0, False))
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-10*DAY, "CHALLENGED")))):
            await self.sync()
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.connect_db()
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])
        self.assertEqual((await self.rows("PRAGMA integrity_check"))[0]["integrity_check"], "ok")

    async def test_partial_window_preserves_legacy_solve_outside_it(self):
        await self.create_old_database()
        user = (await self.rows("SELECT * FROM users"))[0]
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW-DAY, "WRONG_ANSWER")))):
            result = await crawler.Crawler.fetch_cf_submissions(object(), user, NOW-2*DAY, self.plugin.db, {})
        self.assertEqual(result, (0, True))
        self.assertEqual((await self.rows("SELECT submit_time FROM submissions"))[0]["submit_time"], NOW-10*DAY)


class RuntimeSmokeTests(PluginTestCase):
    async def test_real_pillow_renders_both_rank_images(self):
        solved = await self.plugin._generate_rank_image("验收榜单", [
            {"user_name":"验收成员", "user_status":"队员", "cf_count":3}])
        rating = await self.plugin._generate_rating_rank_image("Rating 验收", [
            {"cf_handle":"tourist", "rating":3000}])
        for output in (solved, rating):
            self.assertIsInstance(output, bytes)
            self.assertTrue(output.startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_real_scheduler_initializes_and_terminates(self):
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.initialize()
        try:
            self.assertTrue(self.plugin.scheduler.running)
            self.assertIsNotNone(self.plugin.scheduler.get_job("sync_data_job"))
            self.assertIsNotNone(self.plugin.scheduler.get_job("contest_reminder_refresh_job"))
        finally:
            await self.plugin.terminate()
        self.assertFalse(self.plugin.scheduler.running)
