"""Behavior regressions found in the 1.3.2 audit."""
import ast
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.runtime_support import (
    DAY, NOW, PluginTestCase, crawler, main, response, submission,
)
from tests.test_cf_api import FakeLimiter, FakeResponse
from core.cf_api import request_cf_api


class ChatEvent:
    def __init__(self, text):
        self.message_str = text

    def plain_result(self, text):
        return text

    def get_sender_id(self):
        return "10001"


class RevisionRegressionTests(PluginTestCase):
    async def test_terminal_pretests_do_not_expand_incremental_window(self):
        await self.add_user()
        old = NOW - 100 * DAY
        records = [submission(old, "WRONG_ANSWER", testset="PRETESTS"),
                   submission(old + 1, "TIME_LIMIT_EXCEEDED", testset="SAMPLES")]
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(*records))):
            await self.sync(days=365)
        user = (await self.rows("SELECT * FROM users"))[0]
        plan = await main.plan_sync(self.plugin.db, user, NOW + 3600)
        self.assertFalse(plan.reconcile)
        self.assertEqual(plan.start, NOW - 2 * DAY)
        self.assertEqual([r["needs_recheck"] for r in await self.rows(
            "SELECT needs_recheck FROM cf_submission_records")], [0, 0])

    async def test_upgrade_clears_only_terminal_pending_flags(self):
        await self.add_user()
        for sid, verdict in enumerate(("WRONG_ANSWER", "TIME_LIMIT_EXCEEDED", "OK", "TESTING", "SUBMITTED")):
            await self.plugin.db.execute("""INSERT INTO cf_submission_records
                (user_qq_id,submission_id,problem_id,submit_time,verdict,needs_recheck)
                VALUES('10001',?,'cf_1A',?,?,1)""", (str(sid), NOW - 100 * DAY, verdict))
        await self.plugin.db.commit()
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.connect_db()
        flags = {r["verdict"]: r["needs_recheck"] for r in await self.rows(
            "SELECT verdict,needs_recheck FROM cf_submission_records")}
        self.assertEqual(flags, {"WRONG_ANSWER": 0, "TIME_LIMIT_EXCEEDED": 0,
                                 "OK": 1, "TESTING": 1, "SUBMITTED": 1})

    async def test_periodic_reconciliation_still_revisits_terminal_failures(self):
        await self.add_user()
        old = NOW - 100 * DAY
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=[
            response(submission(old, "WRONG_ANSWER", testset="PRETESTS")),
            response(submission(old, "OK", testset="TESTS")),
        ])):
            await self.sync(days=365)
            self.assertEqual(await self.sync(now=NOW + 8 * DAY), (1, True))

    async def test_http_400_limit_retries_and_recovers(self):
        calls = []
        class Session:
            def get(self, *args, **kwargs):
                calls.append(args)
                limited = len(calls) == 1
                reply = FakeResponse({"status": "FAILED", "comment": "Call limit exceeded"}
                                     if limited else response())
                reply.status = 400 if limited else 200
                return reply
        limiter = FakeLimiter()
        result = await request_cf_api(Session(), "user.status", limiter=limiter)
        self.assertEqual(result, response())
        self.assertEqual(limiter.calls, 2)

    async def test_http_400_limit_stops_at_retry_budget(self):
        class Session:
            def get(self, *args, **kwargs):
                reply = FakeResponse({"status": "FAILED", "comment": "Call limit exceeded"})
                reply.status = 400
                return reply
        limiter = FakeLimiter()
        result = await request_cf_api(Session(), "user.status", limiter=limiter)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(limiter.calls, 3)

    async def rank_replies(self, command):
        # AstrBot matches complete command names followed by whitespace or EOF.
        # Check all declared registrations, then call the selected real handler.
        tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        handlers = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id == "acm_manager"
                        and decorator.func.attr == "command"):
                    full = "/acm " + decorator.args[0].value
                    if command == full or command.startswith(full + " "):
                        handlers.append(getattr(self.plugin, node.name))
        self.assertEqual(len(handlers), 1, "One rank command must select exactly one handler")
        return [reply async for reply in handlers[0](ChatEvent(command))]

    async def seed_rank(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(
            submission(NOW - DAY, problem="A"), submission(NOW - 10 * DAY, problem="B")))):
            await self.sync(days=30)

    async def test_rank_all_has_one_handler_and_uses_all_history(self):
        await self.seed_rank()
        with patch.object(main.time, "time", return_value=NOW):
            replies = await self.rank_replies("/acm rank all")
        self.assertEqual(len(replies), 1)
        self.assertIn("生涯", replies[0])
        self.assertIn("Fixture: 2 题", replies[0])

    async def test_rank_default_keeps_seven_day_window(self):
        await self.seed_rank()
        with patch.object(main.time, "time", return_value=NOW):
            replies = await self.rank_replies("/acm rank")
        self.assertEqual(len(replies), 1)
        self.assertIn("近 7 日", replies[0])
        self.assertIn("Fixture: 1 题", replies[0])

    async def test_rank_rejects_unknown_mode(self):
        replies = await self.rank_replies("/acm rank unknown")
        self.assertIn("参数", replies[0])

    async def test_chat_timestamps_are_shanghai_on_utc_host(self):
        when = 1789300800  # 2026-09-13 20:00:00, Asia/Shanghai
        await self.add_user()
        await self.plugin.db.execute("UPDATE users SET cf_rating_updated_at=?", (when,))
        await self.plugin.db.execute("""INSERT INTO submissions
            (user_qq_id,platform,problem_id,problem_name,submit_time)
            VALUES('10001','codeforces','cf_1A','Fixture',?)""", (when,))
        await self.plugin.db.commit()
        with patch.object(main.time, "localtime", time.gmtime), patch.object(main.time, "time", return_value=when + 60):
            contest = self.plugin._format_cf_contest(
                {"id": 1, "name": "Fixture", "startTimeSeconds": when, "durationSeconds": 7200})
            self.assertIn("2026-09-13 20:00:00", contest)
            hourly = await self.plugin._generate_hourly_report_message()
            self.assertIn("20:00", hourly)
            solves = [r async for r in self.plugin.cmd_query_user_submissions(ChatEvent("/acm 查询 10001"))]
            self.assertIn("2026-09-13 20:00", solves[0])
            rating = [r async for r in self.plugin.cmd_get_rating(ChatEvent("/acm rating old_handle"))]
            self.assertIn("2026-09-13 20:00:00", rating[0])

    async def test_single_chat_sync_reports_rating_failure_and_commits_solves(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW - 1)))), \
                patch.object(crawler.Crawler, "fetch_cf_profile", AsyncMock(return_value=False)), \
                patch.object(main.time, "time", return_value=NOW):
            replies = [r async for r in self.plugin.cmd_sync_user(ChatEvent("/acm sync_user 10001 1"))]
        self.assertIn("提交同步：完成", replies[-1])
        self.assertIn("Rating 更新：失败", replies[-1])
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)
        user = (await self.rows("SELECT * FROM users"))[0]
        self.assertEqual(user["last_sync_timestamp"], NOW)
        self.assertEqual(user["cf_rating_updated_at"], NOW - DAY)

    async def test_single_chat_sync_reports_submission_failure_separately(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(side_effect=TimeoutError("fixture offline"))), \
                patch.object(crawler.Crawler, "fetch_cf_profile", AsyncMock(return_value=True)):
            replies = [r async for r in self.plugin.cmd_sync_user(ChatEvent("/acm sync_user 10001 1"))]
        self.assertIn("提交同步：失败", replies[-1])
        self.assertIn("Rating 更新：完成", replies[-1])

    async def test_batch_chat_sync_counts_rating_failures(self):
        await self.add_user()
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response())), \
                patch.object(crawler.Crawler, "fetch_cf_profile", AsyncMock(return_value=False)):
            replies = [r async for r in self.plugin.cmd_sql_sync(ChatEvent("/acm sql 1"))]
        self.assertTrue(any("提交同步失败 0 人，Rating 更新失败 1 人" in r for r in replies))
