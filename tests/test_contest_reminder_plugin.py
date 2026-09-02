import importlib
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = ROOT.name


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class DummyTrigger:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


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


class FakeClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))


def install_main_import_stubs():
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))

    aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    aiohttp.ClientSession = FakeClientSession
    aiosqlite = sys.modules.setdefault("aiosqlite", types.ModuleType("aiosqlite"))
    aiosqlite.Connection = object
    aiosqlite.Row = dict

    apscheduler = sys.modules.setdefault("apscheduler", types.ModuleType("apscheduler"))
    schedulers = types.ModuleType("apscheduler.schedulers")
    schedulers_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
    schedulers_asyncio.AsyncIOScheduler = object
    triggers = types.ModuleType("apscheduler.triggers")
    triggers_cron = types.ModuleType("apscheduler.triggers.cron")
    triggers_cron.CronTrigger = DummyTrigger
    triggers_date = types.ModuleType("apscheduler.triggers.date")
    triggers_date.DateTrigger = DummyTrigger
    triggers_interval = types.ModuleType("apscheduler.triggers.interval")
    triggers_interval.IntervalTrigger = DummyTrigger
    jobstores = types.ModuleType("apscheduler.jobstores")
    jobstores_base = types.ModuleType("apscheduler.jobstores.base")
    jobstores_base.JobLookupError = LookupError
    apscheduler.schedulers = schedulers
    apscheduler.triggers = triggers
    apscheduler.jobstores = jobstores
    sys.modules.update({
        "apscheduler.schedulers": schedulers,
        "apscheduler.schedulers.asyncio": schedulers_asyncio,
        "apscheduler.triggers": triggers,
        "apscheduler.triggers.cron": triggers_cron,
        "apscheduler.triggers.date": triggers_date,
        "apscheduler.triggers.interval": triggers_interval,
        "apscheduler.jobstores": jobstores,
        "apscheduler.jobstores.base": jobstores_base,
    })

    werkzeug = sys.modules.setdefault("werkzeug", types.ModuleType("werkzeug"))
    werkzeug_security = types.ModuleType("werkzeug.security")
    werkzeug_security.generate_password_hash = lambda value: f"hash:{value}"
    werkzeug.security = werkzeug_security
    sys.modules["werkzeug.security"] = werkzeug_security

    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 1
    fcntl.LOCK_NB = 2
    fcntl.LOCK_UN = 8
    fcntl.flock = lambda *args, **kwargs: None
    sys.modules.setdefault("fcntl", fcntl)

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = DummyLogger()
    event = types.ModuleType("astrbot.api.event")

    class FilterStub:
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def command_group(*args, **kwargs):
            def decorate(func):
                func.command = lambda *a, **k: (lambda child: child)
                return func
            return decorate

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda func: func

    event.filter = FilterStub
    event.AstrMessageEvent = object
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    star.register = lambda *args, **kwargs: (lambda cls: cls)
    api.event = event
    api.star = star
    astrbot.api = api

    components = types.ModuleType("astrbot.core.message.components")
    components.Plain = object

    class Image:
        @staticmethod
        def fromBytes(value):
            return value

    components.Image = Image
    result = types.ModuleType("astrbot.core.message.message_event_result")
    result.MessageChain = object
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_plugin_data_path = lambda: str(ROOT / ".test-data")
    sys.modules.update({
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.components": components,
        "astrbot.core.message.message_event_result": result,
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": astrbot_path,
    })

    webui = types.ModuleType(f"{PACKAGE_NAME}.webui")
    webui.run_server = lambda *args, **kwargs: None
    sys.modules[f"{PACKAGE_NAME}.webui"] = webui


install_main_import_stubs()
plugin_module = importlib.import_module(f"{PACKAGE_NAME}.main")


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
        self.assertEqual(job.trigger.kwargs["run_date"].timestamp(), start - 3600)

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
        self.assertIn("提前：1秒", message)
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


if __name__ == "__main__":
    unittest.main()
