import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.runtime_support import main as plugin_module

ROOT = Path(__file__).resolve().parents[1]


class FakeJob:
    def __init__(self, job_id, func=None, trigger=None, args=(), kwargs=None):
        self.id = job_id
        self.func = func
        self.trigger = trigger
        self.args = args
        self.kwargs = kwargs or {}


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger=None, args=(), id=None, **kwargs):
        self.jobs[id] = FakeJob(id, func=func, trigger=trigger, args=args, kwargs=kwargs)
        return self.jobs[id]

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id):
        self.jobs.pop(job_id)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeProcess:
    next_pid = 4200

    def __init__(self, target=None, args=()):
        self.target = target
        self.args = args
        self.alive = False
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def join(self, timeout=None):
        return None

    def kill(self):
        self.alive = False


class FailingProcess(FakeProcess):
    def start(self):
        self.alive = False


class ContestReminderPluginTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, config):
        plugin = plugin_module.CodeforcesHelperPlugin.__new__(
            plugin_module.CodeforcesHelperPlugin
        )
        plugin.config = {"contest_reminder": config}
        plugin.scheduler = FakeScheduler()
        bot = FakeBot()
        platform = types.SimpleNamespace(bot=bot)
        plugin.context = types.SimpleNamespace(get_platform=lambda name: platform)
        return plugin, bot

    async def test_refresh_schedules_enabled_contest_and_removes_stale_job(self):
        config = {
            "enabled": True,
            "group_whitelist": ["123456"],
            "reminder_times": "1",
            "include_div2": True,
            "include_div3": False,
            "include_div4": False,
            "include_educational": False,
            "include_other": False,
        }
        plugin, _ = self.make_plugin(config)
        plugin.scheduler.jobs["cf_contest_reminder_999_3600"] = FakeJob(
            "cf_contest_reminder_999_3600"
        )
        start = int(time.time()) + 7200
        plugin_module.request_cf_api = AsyncMock(return_value={
            "status": "OK",
            "result": [{
                "id": 100,
                "name": "Codeforces Round 1100 (Div. 2)",
                "phase": "BEFORE",
                "startTimeSeconds": start,
            }],
        })

        await plugin.refresh_contest_reminder_jobs()

        self.assertNotIn("cf_contest_reminder_999_3600", plugin.scheduler.jobs)
        job = plugin.scheduler.jobs["cf_contest_reminder_100_3600"]
        self.assertEqual(job.args, (100, "Codeforces Round 1100 (Div. 2)", start, 3600))
        self.assertEqual(job.trigger.run_date.timestamp(), start - 3600)

    async def test_sender_only_targets_current_whitelist(self):
        config = {
            "enabled": True,
            "group_whitelist": ["123456", "789012"],
            "reminder_times": "1s",
            "include_div2": True,
        }
        plugin, bot = self.make_plugin(config)

        await plugin.send_contest_reminder(
            100,
            "Codeforces Round 1100 (Div. 2)",
            int(time.time()) + 1,
            1,
        )

        self.assertEqual([group_id for group_id, _ in bot.sent], [123456, 789012])
        message = bot.sent[0][1][0]["data"]["text"]
        self.assertIn("距离开始：1秒", message)
        self.assertIn("https://codeforces.com/contestRegistration/100", message)

        bot.sent.clear()
        plugin.config["contest_reminder"]["group_whitelist"] = ["123456"]
        await plugin.send_contest_reminder(
            100,
            "Codeforces Round 1100 (Div. 2)",
            int(time.time()) + 1,
            1,
        )
        self.assertEqual([group_id for group_id, _ in bot.sent], [123456])

    async def test_refresh_skips_api_when_all_filters_are_disabled(self):
        config = {
            "enabled": True,
            "group_whitelist": ["123456"],
            "reminder_times": "1",
            "include_div2": False,
            "include_div3": False,
            "include_div4": False,
            "include_educational": False,
            "include_other": False,
        }
        plugin, _ = self.make_plugin(config)
        plugin.scheduler.jobs["cf_contest_reminder_100_3600"] = FakeJob(
            "cf_contest_reminder_100_3600"
        )
        request = AsyncMock()
        plugin_module.request_cf_api = request

        await plugin.refresh_contest_reminder_jobs()

        request.assert_not_awaited()
        self.assertEqual(plugin.scheduler.jobs, {})

    async def test_manual_webui_start_and_stop_persist_desired_state(self):
        plugin, _ = self.make_plugin({})
        plugin.config = FakeConfig({"webui_auto_start": False, "webui_port": 8088})
        plugin.db_path = ROOT / "fake.db"
        plugin.webui_process = None

        with (
            patch.object(plugin_module, "Process", FakeProcess),
            patch.object(plugin_module.asyncio, "sleep", AsyncMock()),
        ):
            start_message = await plugin.start_webui_process(persist=True)

        self.assertIn("后台已启动", start_message)
        self.assertTrue(plugin.config["webui_auto_start"])
        self.assertEqual(plugin.config.save_count, 1)

        stop_message = await plugin.stop_webui_process(persist=True)
        self.assertIn("后台已关闭", stop_message)
        self.assertFalse(plugin.config["webui_auto_start"])
        self.assertEqual(plugin.config.save_count, 2)

    async def test_lifecycle_stop_preserves_auto_start_setting(self):
        plugin, _ = self.make_plugin({})
        plugin.config = FakeConfig({"webui_auto_start": True})
        plugin.webui_process = FakeProcess()
        plugin.webui_process.start()

        await plugin.stop_webui_process(persist=False)

        self.assertTrue(plugin.config["webui_auto_start"])
        self.assertEqual(plugin.config.save_count, 0)

    async def test_failed_manual_start_does_not_persist_enabled_state(self):
        plugin, _ = self.make_plugin({})
        plugin.config = FakeConfig({"webui_auto_start": False, "webui_port": 8088})
        plugin.db_path = ROOT / "fake.db"
        plugin.webui_process = None

        with (
            patch.object(plugin_module, "Process", FailingProcess),
            patch.object(plugin_module.asyncio, "sleep", AsyncMock()),
        ):
            message = await plugin.start_webui_process(persist=True)

        self.assertIn("启动失败", message)
        self.assertFalse(plugin.config["webui_auto_start"])
        self.assertEqual(plugin.config.save_count, 0)

    async def test_initialize_helper_starts_webui_when_configured(self):
        plugin, _ = self.make_plugin({})
        plugin.config = FakeConfig({"webui_auto_start": True})
        plugin.start_webui_process = AsyncMock(return_value="ok")

        await plugin._auto_start_webui_if_enabled()

        plugin.start_webui_process.assert_awaited_once_with(persist=False)

    async def test_webui_commands_persist_user_intent(self):
        plugin, _ = self.make_plugin({})
        plugin.start_webui_process = AsyncMock(return_value="started")
        plugin.stop_webui_process = AsyncMock(return_value="stopped")
        event = types.SimpleNamespace(plain_result=lambda value: value)

        start_results = [item async for item in plugin.cmd_start_webui(event)]
        stop_results = [item async for item in plugin.cmd_stop_webui(event)]

        self.assertEqual(start_results, ["started"])
        self.assertEqual(stop_results, ["stopped"])
        plugin.start_webui_process.assert_awaited_once_with(persist=True)
        plugin.stop_webui_process.assert_awaited_once_with(persist=True)


if __name__ == "__main__":
    unittest.main()
