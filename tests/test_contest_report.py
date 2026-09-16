import asyncio
import base64
import json
import types
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tests.runtime_support import DAY, NOW, PluginTestCase, main
from core.contest_report_image import report_row_cells

reports = main.contest_reports
ROOT = Path(__file__).resolve().parents[1]


def contest(cid=1, when=NOW-DAY):
    return {"id": cid, "name": f"Codeforces Fixture {cid}", "phase": "FINISHED",
            "type": "CF", "startTimeSeconds": when, "durationSeconds": 7200}


def change(cid=1, handle="old_handle", when=NOW+1, old=1600, new=1660):
    return {"contestId": cid, "contestName": f"Codeforces Fixture {cid}", "handle": handle,
            "rank": 50, "ratingUpdateTimeSeconds": when, "oldRating": old, "newRating": new}


def standings(cid=1, handles=("old_handle",), ranks=None):
    return {"contest": contest(cid), "problems": [{"index": "A"}, {"index": "B"}],
            "rows": [{"rank": ranks[i] if ranks else i+10,
                      "party": {"participantType": "CONTESTANT", "members": [{"handle": handle}]},
                      "problemResults": [{"points": 500}, {"points": 0}]}
                     for i, handle in enumerate(handles)]}


def synthetic_evidence(snapshots, changes, handles):
    """Independent personal history / submission replies for transport fixtures."""
    since = min(value["contest"]["startTimeSeconds"] for value in snapshots.values())
    sources = {}
    sid = 1
    for handle in handles:
        key = handle.casefold()
        history = [row for batch in changes.values() if isinstance(batch, list)
                   for row in batch if row["handle"].casefold() == key]
        submissions = []
        for cid, data in snapshots.items():
            for row in data["rows"]:
                if not any(member["handle"].casefold() == key for member in row["party"]["members"]):
                    continue
                for i, result in enumerate(row["problemResults"]):
                    if result["points"] > 0:
                        submissions.append({"id": sid, "contestId": cid,
                            "creationTimeSeconds": data["contest"]["startTimeSeconds"]+i+1,
                            "verdict": "OK", "author": row["party"],
                            "problem": {"contestId": cid, "index": data["problems"][i]["index"]}})
                        sid += 1
        sources[key] = {"handle": handle, "history": history, "submissions": submissions,
                        "complete": True, "since": since}
    return sources


class ReportDataTests(unittest.TestCase):
    def test_unrated_members_sort_by_solved_last_and_have_dash_cells(self):
        handles = ["Zulu", "Alpha", "Beta", "Rated"]
        data = standings(handles=handles)
        for row, solved in zip(data["rows"], (2, 0, 1, 0)):
            if row["party"]["members"][0]["handle"] != "Rated":
                row["party"]["participantType"] = "OUT_OF_COMPETITION"
            row["problemResults"] = [{"points": 500 if i < solved else 0} for i in range(2)]
        changes = [change(handle="Rated")]
        evidence = synthetic_evidence({1: data}, {1: changes}, handles)
        result = reports.build_report(data, changes, handles, evidence)
        self.assertEqual([row["handle"] for row in result["rows"]], ["Rated", "Zulu", "Beta", "Alpha"])
        for sequence, row in enumerate(result["rows"][1:], 2):
            self.assertEqual(report_row_cells(row, sequence),
                             ["—", "—", row["handle"], str(row["solved"]), "—", "—"])

    def test_settings_default_off_and_validate_schedule(self):
        self.assertFalse(reports.parse_report_settings({}).enabled)
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["contest_report"]["items"]["enabled"]["default"])
        self.assertEqual(reports.parse_report_settings({"enabled": True}, "12345").groups, (12345,))
        self.assertEqual(reports.parse_report_settings({"enabled": True, "group_whitelist": ["23456"]}, "12345").groups, (23456,))
        for clock in ("24:00", "09:60", "9:00", "* 0"):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                reports.parse_report_settings({"enabled": True, "send_mode": "scheduled", "send_time": clock})

    def test_next_clock_time_is_in_shanghai(self):
        options = reports.ReportSettings(True, "scheduled", "09:00")
        now = int(datetime(2026, 9, 16, 10, tzinfo=reports.SHANGHAI_TZ).timestamp())
        expected = int(datetime(2026, 9, 17, 9, tzinfo=reports.SHANGHAI_TZ).timestamp())
        self.assertEqual(options.due_at(now), expected)
        self.assertEqual(options.due_at(expected), expected)

    def test_official_unrated_members_are_included_and_virtuals_are_excluded(self):
        data = standings(handles=("Other", "OLD_HANDLE", "Unrated", "Virtual"), ranks=(3, 10, 8, 1))
        data["rows"][-1]["party"]["participantType"] = "VIRTUAL"
        handles = ["old_handle", "OLD_HANDLE", "Unrated", "Virtual"]
        evidence = synthetic_evidence({1: data}, {1: [change()]}, handles)
        result = reports.build_report(data, [change()], handles, evidence)
        self.assertEqual([row["handle"] for row in result["rows"]], ["OLD_HANDLE", "Unrated"])
        self.assertIsNone(result["rows"][1]["rank"])
        self.assertIsNone(result["rows"][1]["new_rating"])
        self.assertIsNone(result["rows"][1]["rating_change"])
        self.assertEqual(result["rows"][0]["rating_change"], 60)

    def test_solved_uses_official_results_not_practice_or_partial_ioi_points(self):
        self.assertEqual(reports.count_solved("CF", [{}, {}], [{"points": 0}, {"points": 40}]), 1)
        self.assertEqual(reports.count_solved("IOI", [{"points": 100}, {"points": 100}],
                                              [{"points": 30}, {"points": 100}]), 1)
        self.assertIsNone(reports.count_solved("IOI", [{}], [{"points": 30}]))
        with self.assertRaises(ValueError):
            reports.count_solved("CF", [{}, {}], [{"points": 1}])

    def test_missing_official_member_result_is_retryable(self):
        with self.assertRaises(ValueError):
            reports.build_report(standings(handles=("other",)), [change()], ["old_handle"])

    def test_real_renderer_handles_long_titles_and_pagination(self):
        handles = tuple(f"handle_{i}" for i in range(41))
        data, changes = standings(handles=handles), [change(handle="handle_0")]
        evidence = synthetic_evidence({1: data}, {1: changes}, handles)
        snapshot = reports.build_report(data, changes, handles, evidence)
        snapshot["contest_name"] = "A long Codeforces contest title " * 5
        pages = main.render_report_pages(snapshot, ROOT / "resources/SourceHanSansSC-Bold.otf")
        self.assertEqual(len(pages), 2)
        for content in pages:
            with Image.open(BytesIO(content)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 1160)
                image.verify()
        self.assertEqual(main.render_report_pages({**snapshot, "rows": []}, ROOT / "resources/SourceHanSansSC-Bold.otf"), [])


class ReportEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_reaches_live_entry_behind_practice_and_deduplicates_handles(self):
        def sub(sid, stamp, kind="PRACTICE"):
            return {"id": sid, "contestId": 1, "creationTimeSeconds": stamp, "verdict": "OK",
                    "author": {"participantType": kind, "members": [{"handle": "member"}]},
                    "problem": {"index": "A"}}
        pages = {1: [sub(5, 1400), sub(4, 1300)],
                 3: [sub(3, 1200, "OUT_OF_COMPETITION"), sub(2, 1100)],
                 5: [sub(1, 999)]}
        async def request(session, method, params, **kwargs):
            return {"status": "OK", "result": [] if method == "user.rating" else pages[params["from"]]}
        mock = AsyncMock(side_effect=request)
        evidence = await reports.fetch_member_evidence(object(), ["member", "Member"], 1000, mock, page_size=2)
        self.assertTrue(evidence["member"]["complete"])
        self.assertEqual(len(evidence["member"]["submissions"]), 4)
        self.assertEqual([call.args[2]["from"] for call in mock.call_args_list if call.args[1] == "user.status"], [1, 3, 5])
        self.assertEqual(sum(call.args[1] == "user.rating" for call in mock.call_args_list), 1)

    async def test_network_failure_is_unknown_not_an_empty_member_record(self):
        mock = AsyncMock(side_effect=TimeoutError("fixture unavailable"))
        evidence = await reports.fetch_member_evidence(object(), ["member"], 1000, mock)
        self.assertFalse(evidence["member"]["complete"])
        with self.assertRaises(ValueError):
            reports.build_report(standings(handles=("other",)), [change(handle="other")], ["member"], evidence)

    async def test_missing_handle_is_isolated_without_hiding_network_errors(self):
        async def request(session, method, params, **kwargs):
            if params["handle"] == "missing":
                return {"status": "FAILED", "comment": "handle: User with handle missing not found"}
            return {"status": "OK", "result": []}
        evidence = await reports.fetch_member_evidence(object(), ["missing", "valid"], 1000, request)
        self.assertTrue(evidence["missing"]["invalid_handle"])
        self.assertTrue(evidence["valid"]["complete"])

    async def test_pagination_failure_discards_partial_window(self):
        page = [{"id": i, "creationTimeSeconds": 1001+i, "author": {}, "problem": {}} for i in (2, 1)]
        async def request(session, method, params, **kwargs):
            if method == "user.rating":
                return {"status": "OK", "result": []}
            if params["from"] > 1:
                raise TimeoutError("page two failed")
            return {"status": "OK", "result": page}
        evidence = await reports.fetch_member_evidence(object(), ["member"], 1000, request, page_size=2)
        self.assertFalse(evidence["member"]["complete"])
        self.assertEqual(evidence["member"]["submissions"], [])


class ReportPluginTests(PluginTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.plugin.scheduler = AsyncIOScheduler(timezone=reports.SHANGHAI_TZ)
        self.bot = types.SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 1}))
        self.plugin.context = types.SimpleNamespace(get_platform=lambda name: types.SimpleNamespace(bot=self.bot))

    async def configure(self, now=NOW, **overrides):
        self.plugin.config["contest_report"] = {"enabled": True, "group_whitelist": ["12345"], **overrides}
        with patch.object(main.time, "time", return_value=now):
            await self.plugin._configure_contest_report_jobs(await self.plugin._get_all_settings())

    async def poll(self, snapshots=None, changes=None, now=NOW+60, callback=None, member_sources=None):
        snapshots = snapshots if snapshots is not None else {1: standings()}
        changes = changes if changes is not None else {cid: [change(cid)] for cid in snapshots}
        if member_sources is None:
            users = await self.rows("SELECT cf_handle FROM users WHERE cf_handle IS NOT NULL")
            member_sources = synthetic_evidence(snapshots, changes, [row["cf_handle"] for row in users])
        async def request(http, method, params, **kwargs):
            if callback:
                await callback(method, params)
            if method == "contest.list":
                value = [item["contest"] for item in snapshots.values()]
            elif method == "contest.ratingChanges":
                value = changes[params["contestId"]]
                if isinstance(value, Exception):
                    raise value
            elif method == "contest.standings":
                self.assertEqual(set(params), {"contestId"})
                value = snapshots[params["contestId"]]
            elif method in {"user.rating", "user.status"} and member_sources is not None:
                source = member_sources[params["handle"].casefold()]
                value = source["history" if method == "user.rating" else "submissions"]
                if method == "user.status":
                    value = sorted(value, key=lambda row: row["id"], reverse=True)
                    first, count = int(params["from"])-1, int(params["count"])
                    value = value[first:first+count]
            else:
                raise AssertionError(method)
            return {"status": "OK", "result": value}
        api = AsyncMock(side_effect=request)
        profiles = AsyncMock(return_value=1)
        with patch.object(main, "request_cf_api", api), patch.object(main.Crawler, "fetch_cf_profiles", profiles), \
                patch.object(main.time, "time", return_value=now):
            await self.plugin.check_contest_reports()
        return api, profiles

    async def send(self, now):
        with patch.object(main.time, "time", return_value=now):
            await self.plugin.send_pending_contest_reports()

    async def test_default_disabled_has_no_api_or_messages(self):
        await self.add_user()
        api, profiles = await self.poll()
        api.assert_not_awaited()
        profiles.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_activation_time_survives_reload(self):
        await self.configure()
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.connect_db()
        await self.configure(now=NOW+1000)
        window = await reports.report_window(self.plugin.db)
        self.assertEqual(window["enabled_since"], NOW)
        self.assertEqual(window["generation"], 1)

    async def test_old_start_but_new_publication_is_reported_once(self):
        await self.add_user()
        await self.configure()
        old = standings()
        old["contest"]["startTimeSeconds"] = NOW-100*DAY
        api, profiles = await self.poll({1: old})
        profiles.assert_awaited_once()
        self.assertEqual(self.bot.send_group_msg.await_count, 1)
        stored = (await self.rows("SELECT * FROM cf_contest_reports"))[0]
        self.assertEqual(stored["state"], "sent")
        self.assertEqual(json.loads(stored["report_json"])["rows"][0]["solved"], 1)
        await self.poll({1: old}, now=NOW+3700)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)
        content = self.bot.send_group_msg.call_args.kwargs["message"][0]["data"]["file"]
        self.assertTrue(base64.b64decode(content.removeprefix("base64://")).startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_pre_activation_publications_never_generate_reports(self):
        await self.add_user()
        await self.configure()
        api, _ = await self.poll(changes={1: [change(when=NOW-1)]})
        self.assertEqual(await self.rows("SELECT * FROM cf_contest_reports"), [])
        self.bot.send_group_msg.assert_not_awaited()
        self.assertNotIn("contest.standings", [call.args[1] for call in api.call_args_list])

    async def test_multiple_contests_are_separate_saved_images(self):
        await self.add_user()
        await self.configure()
        await self.poll({1: standings(1), 2: standings(2)})
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        self.assertEqual(len(await self.rows("SELECT * FROM cf_contest_reports WHERE state='sent'")), 2)
        messages = [call.kwargs["message"] for call in self.bot.send_group_msg.call_args_list]
        self.assertTrue(all(len(message) == 1 and message[0]["type"] == "image" for message in messages))

    async def test_no_members_is_saved_but_not_sent(self):
        await self.add_user()
        await self.configure()
        await self.poll({1: standings(handles=("outsider",))}, {1: [change(handle="outsider")]})
        stored = (await self.rows("SELECT * FROM cf_contest_reports"))[0]
        self.assertEqual(stored["state"], "no_members")
        self.assertEqual(json.loads(stored["report_json"])["rows"], [])
        self.bot.send_group_msg.assert_not_awaited()

    async def test_report_is_sent_even_when_all_local_participants_are_unrated(self):
        await self.add_user()
        await self.configure()
        await self.poll(changes={1: [change(handle="other_rated_user")]})
        stored = (await self.rows("SELECT report_json FROM cf_contest_reports"))[0]
        row = json.loads(stored["report_json"])["rows"][0]
        self.assertIsNone(row["rating_change"])
        self.assertIsNone(row["new_rating"])
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_scheduled_mode_waits_until_due_time(self):
        now = int(datetime(2026, 9, 16, 8, 30, tzinfo=reports.SHANGHAI_TZ).timestamp())
        await self.add_user()
        await self.configure(now=now, send_mode="scheduled", send_time="09:00")
        await self.poll(changes={1: [change(when=now+1)]}, now=now+60)
        self.bot.send_group_msg.assert_not_awaited()
        due = now+1800
        self.assertEqual((await self.rows("SELECT due_at FROM cf_contest_reports"))[0]["due_at"], due)
        await self.send(due-1)
        self.bot.send_group_msg.assert_not_awaited()
        await self.send(due)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_changing_delivery_time_reschedules_pending_reports(self):
        now = int(datetime(2026, 9, 16, 8, 30, tzinfo=reports.SHANGHAI_TZ).timestamp())
        await self.add_user()
        await self.configure(now=now, send_mode="scheduled", send_time="09:00")
        await self.poll(changes={1: [change(when=now+1)]}, now=now+60)
        await self.configure(now=now+120, send_mode="scheduled", send_time="10:00")
        await self.send(now+1800)
        self.bot.send_group_msg.assert_not_awaited()
        await self.send(now+5400)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_failed_group_retry_does_not_repeat_successful_group(self):
        await self.add_user()
        await self.configure(group_whitelist=["12345", "23456"])
        async def send(**kwargs):
            if kwargs["group_id"] == 23456:
                raise RuntimeError("fixture failed delivery")
            return {"message_id": 1}
        self.bot.send_group_msg.side_effect = send
        await self.poll()
        self.assertEqual((await self.rows("SELECT state FROM cf_contest_reports"))[0]["state"], "pending")
        self.bot.send_group_msg.side_effect = None
        await self.send(NOW+120)
        self.assertEqual([call.kwargs["group_id"] for call in self.bot.send_group_msg.call_args_list], [12345, 23456, 23456])
        self.assertEqual((await self.rows("SELECT state FROM cf_contest_reports"))[0]["state"], "sent")

    async def test_failed_page_retries_only_unsent_pages(self):
        await self.add_user()
        await self.configure()
        self.bot.send_group_msg.side_effect = [{"message_id": 1}, RuntimeError("second page failed")]
        with patch.object(main, "render_report_pages", return_value=[b"first-page", b"second-page"]):
            await self.poll()
            self.bot.send_group_msg.side_effect = None
            await self.send(NOW+120)
        sent = [base64.b64decode(call.kwargs["message"][0]["data"]["file"].removeprefix("base64://"))
                for call in self.bot.send_group_msg.call_args_list]
        self.assertEqual(sent, [b"first-page", b"second-page", b"second-page"])

    async def test_disabling_cancels_backlog_and_reenabling_resets_cutoff(self):
        await self.add_user()
        await self.configure(send_mode="scheduled", send_time="09:00")
        await self.poll()
        await self.configure(now=NOW+100, enabled=False)
        await self.configure(now=NOW+200)
        window = await reports.report_window(self.plugin.db)
        self.assertEqual(window["enabled_since"], NOW+200)
        self.assertEqual(window["generation"], 2)
        self.assertEqual((await self.rows("SELECT state FROM cf_contest_reports"))[0]["state"], "cancelled")
        await self.send(NOW+10000)
        self.bot.send_group_msg.assert_not_awaited()

    async def test_disabling_while_fetching_prevents_saving_and_sending(self):
        await self.add_user()
        await self.configure()
        async def callback(method, params):
            if method == "contest.ratingChanges":
                await self.configure(now=NOW+60, enabled=False)
        await self.poll(callback=callback)
        self.assertEqual(await self.rows("SELECT * FROM cf_contest_reports"), [])
        self.bot.send_group_msg.assert_not_awaited()

    async def test_one_contest_failure_does_not_block_other_contests(self):
        await self.add_user()
        await self.configure()
        await self.poll({1: standings(1), 2: standings(2)}, {1: TimeoutError("fixture offline"), 2: [change(2)]})
        self.assertEqual(self.bot.send_group_msg.await_count, 1)
        waiting = await self.rows("SELECT state FROM cf_contest_report_candidates WHERE contest_id=1")
        self.assertEqual(waiting[0]["state"], "waiting")
        await self.poll({1: standings(1), 2: standings(2)}, now=NOW+3700)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_restart_preserves_pending_delivery_and_receipts(self):
        await self.add_user()
        await self.configure()
        self.bot.send_group_msg.side_effect = RuntimeError("fixture offline")
        await self.poll()
        await self.plugin.db.close()
        with patch.object(self.plugin, "_prepare_persistent_db", return_value=self.plugin.db_path):
            await self.plugin.connect_db()
        await self.configure(now=NOW+100)
        self.bot.send_group_msg.side_effect = None
        await asyncio.gather(self.send(NOW+120), self.send(NOW+120))
        await self.send(NOW+180)
        self.assertEqual(self.bot.send_group_msg.await_count, 2)

    async def test_no_recipient_keeps_pending_snapshot(self):
        await self.add_user()
        await self.configure(group_whitelist=[])
        await self.poll()
        self.bot.send_group_msg.assert_not_awaited()
        self.assertEqual((await self.rows("SELECT state FROM cf_contest_reports"))[0]["state"], "pending")
        await self.plugin.set_setting("notification_group_id", "12345")
        await self.send(NOW+120)
        self.assertEqual(self.bot.send_group_msg.await_count, 1)

    async def test_discovery_is_bounded_and_resumes_older_candidates(self):
        await self.configure()
        candidates = [contest(cid) for cid in range(1, 11)]
        await reports.discover_contests(self.plugin.db, candidates, 1)
        first = await reports.pending_contests(self.plugin.db, 1, NOW)
        self.assertEqual(len(first), reports.MAX_CONTEST_CHECKS)
        for row in first:
            await reports.record_contest_check(self.plugin.db, row["contest_id"], 1, NOW, 60, "before_enable")
        second = await reports.pending_contests(self.plugin.db, 1, NOW+3600)
        self.assertEqual(len(second), 2)

    async def test_polling_uses_the_existing_sync_interval(self):
        await self.configure()
        await self.plugin.set_setting("sync_interval_minutes", 17)
        await self.plugin.reschedule_jobs(await self.plugin._get_all_settings())
        job = self.plugin.scheduler.get_job("sync_data_job")
        self.assertEqual(job.trigger.interval.total_seconds(), 17*60)
        self.assertEqual(job.func, self.plugin._run_periodic_sync)
        calls = []
        async def sync():
            calls.append("sync")
        async def poll():
            calls.append("reports")
        with patch.object(self.plugin, "sync_all_users_data", sync), patch.object(self.plugin, "check_contest_reports", poll):
            await job.func()
        self.assertEqual(calls, ["sync", "reports"])

    async def test_admin_status_shows_report_mode(self):
        await self.configure(send_mode="scheduled", send_time="21:30")
        event = types.SimpleNamespace(plain_result=lambda value: value)
        messages = [value async for value in self.plugin.cmd_status(event)]
        self.assertIn("赛后战报: 开启，北京时间每天 21:30", messages[0])

    async def real_round(self, cid):
        fixture = json.loads((ROOT / "tests/fixtures/contest_reports_2262_2264.json").read_text(encoding="utf-8"))
        sample = next(item for item in fixture["contests"] if item["contest_id"] == cid)
        for i, handle in enumerate(("StarSilk", "Zinc-acetate", "rainboy")):
            await self.add_user(qq=str(10001+i), handle=handle)
        published = sample["rating_changes"][0]["ratingUpdateTimeSeconds"]
        await self.configure(now=published-1)
        await self.poll({cid: sample["standings"]}, {cid: sample["rating_changes"]},
                        now=published+10, member_sources=fixture["members"])
        saved = (await self.rows("SELECT report_json FROM cf_contest_reports WHERE contest_id=?", (cid,)))[0]
        return json.loads(saved["report_json"]), fixture["expected"][str(cid)]

    async def test_real_round1121_includes_live_unrated_members(self):
        report, expected = await self.real_round(2264)
        self.assertEqual({r["handle"] for r in report["rows"]}, set(expected))
        for row in report["rows"]:
            self.assertEqual(row["solved"], expected[row["handle"]]["solved"])

    async def test_real_round1121_rank_matches_personal_contests_page(self):
        report, expected = await self.real_round(2264)
        row = next(r for r in report["rows"] if r["handle"] == "rainboy")
        self.assertEqual(row["rank"], expected["rainboy"]["rank"])

    async def test_real_round1120_rank_solved_and_changes_match_personal_pages(self):
        report, expected = await self.real_round(2262)
        self.assertEqual([row["rank"] for row in report["rows"]], [7, 96, 607])
        for row in report["rows"]:
            for key, value in expected[row["handle"]].items():
                self.assertEqual(row[key], value)
