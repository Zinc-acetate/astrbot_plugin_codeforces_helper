import importlib
import importlib.util
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def install_runtime_stubs():
    if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = object
        sys.modules["aiohttp"] = aiohttp
    if "aiosqlite" not in sys.modules and importlib.util.find_spec("aiosqlite") is None:
        aiosqlite = types.ModuleType("aiosqlite")
        aiosqlite.Connection = object
        aiosqlite.Row = dict
        sys.modules["aiosqlite"] = aiosqlite
    if "astrbot" not in sys.modules and importlib.util.find_spec("astrbot") is None:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(error=lambda *args, **kwargs: None,
                                           warning=lambda *args, **kwargs: None)
        astrbot.api = api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api


install_runtime_stubs()

Crawler = importlib.import_module("core.crawler").Crawler


class FakeCursor:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchone(self):
        return self.row


class FakeDb:
    def __init__(self, current_handle):
        self.current_handle = current_handle
        self.total_changes = 0
        self.inserted = []

    def execute(self, sql, params):
        return FakeCursor({"cf_handle": self.current_handle})

    async def executemany(self, sql, values):
        self.inserted.extend(values)
        self.total_changes += len(values)

    async def commit(self):
        return None


class CrawlerConsistencyTests(unittest.IsolatedAsyncioTestCase):
    submission_result = {
        "status": "OK",
        "result": [{
            "creationTimeSeconds": 200,
            "verdict": "OK",
            "problem": {"contestId": 1, "index": "A", "name": "Theatre Square", "rating": 800},
        }],
    }

    async def test_changed_handle_discards_in_flight_submissions(self):
        db = FakeDb("new_handle")
        user = {"cf_handle": "old_handle", "qq_id": "10001"}
        with patch("core.crawler.request_cf_api", AsyncMock(return_value=self.submission_result)):
            result = await Crawler.fetch_cf_submissions(object(), user, 100, db, {})

        self.assertEqual(result, (0, False))
        self.assertEqual(db.inserted, [])

    async def test_unchanged_handle_writes_submissions(self):
        db = FakeDb("old_handle")
        user = {"cf_handle": "old_handle", "qq_id": "10001"}
        with patch("core.crawler.request_cf_api", AsyncMock(return_value=self.submission_result)):
            result = await Crawler.fetch_cf_submissions(object(), user, 100, db, {})

        self.assertEqual(result, (1, True))
        self.assertEqual(len(db.inserted), 1)
