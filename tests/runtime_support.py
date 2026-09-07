"""Real plugin dependencies; only the unavailable AstrBot host is substituted."""
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def install_astrbot_stub():
    if "astrbot.api.star" in sys.modules:
        return

    def module(name, **attrs):
        value = types.ModuleType(name)
        value.__dict__.update(attrs)
        sys.modules[name] = value
        return value

    class Filter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")

        @staticmethod
        def command_group(*args, **kwargs):
            def decorate(func):
                func.command = lambda *a, **k: lambda child: child
                return func
            return decorate

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda func: func

    class Star:
        def __init__(self, context):
            self.context = context

    class Image:
        @staticmethod
        def fromBytes(value):
            return value

    module("astrbot")
    module("astrbot.api", logger=logging.getLogger("cf-helper-tests"))
    module("astrbot.api.event", filter=Filter, AstrMessageEvent=object)
    module("astrbot.api.star", Context=object, Star=Star, register=lambda *a, **k: lambda cls: cls)
    module("astrbot.core")
    module("astrbot.core.message")
    module("astrbot.core.message.components", Plain=object, Image=Image)
    module("astrbot.core.message.message_event_result", MessageChain=object)
    module("astrbot.core.utils")
    module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_data_path=tempfile.gettempdir)


install_astrbot_stub()
main = importlib.import_module(ROOT.name + ".main")
api = importlib.import_module(ROOT.name + ".backend.api")
crawler = importlib.import_module(ROOT.name + ".core.crawler")
webui = importlib.import_module(ROOT.name + ".webui")
NOW = 1_800_000_000
DAY = 86400


def submission(when, verdict="OK", problem="A", sid=None, **kwargs):
    return {"id": sid if sid is not None else when,
            "creationTimeSeconds": when, "verdict": verdict,
            "problem": {"contestId": 1, "index": problem, "name": "Fixture", "rating": 800},
            **kwargs}


def response(*submissions):
    return {"status": "OK", "result": list(submissions)}


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cf-helper-test-")
        self.plugin = main.CodeforcesHelperPlugin(None, {})
        with patch.object(self.plugin, "_prepare_persistent_db",
                          return_value=Path(self.temporary.name) / "fixture.db"):
            await self.plugin.connect_db()
        webui.app.config.update(TESTING=True, DB_PATH=str(self.plugin.db_path), PLUGIN_CONFIG={})
        webui.app.secret_key = "disposable-test-fixture"
        self.client = webui.app.test_client()

    async def asyncTearDown(self):
        await self.plugin.db.close()
        self.temporary.cleanup()

    async def add_user(self, qq="10001", handle="old_handle", last=NOW-DAY, history=30):
        await self.plugin.db.execute(
            """INSERT INTO users(qq_id,name,cf_handle,last_sync_timestamp,history_sync_days,
               cf_rating,cf_rank,cf_max_rating,cf_max_rank,cf_rating_updated_at)
               VALUES(?,?,?,?,?,2500,'grandmaster',2700,'international grandmaster',?)""",
            (qq, "Fixture", handle, last, history, NOW-DAY))
        await self.plugin.db.commit()

    async def rows(self, sql, params=()):
        async with self.plugin.db.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def login(self, client=None, password="123456"):
        r = await (client or self.client).post("/api/admin/login", json={"password": password})
        self.assertEqual(r.status_code, 200)

    async def sync(self, now=NOW, days=None):
        with patch.object(main.time, "time", return_value=now):
            return await self.plugin.sync_single_user("10001", refresh_cf_profile=False, days=days)
