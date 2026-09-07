from unittest.mock import AsyncMock, patch

from tests.runtime_support import PluginTestCase, crawler, response, submission, NOW


class CrawlerConsistencyTests(PluginTestCase):
    async def test_changed_handle_discards_in_flight_submissions(self):
        await self.add_user(handle="new_handle")
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW)))):
            result = await crawler.Crawler.fetch_cf_submissions(
                object(), {"cf_handle":"old_handle", "qq_id":"10001"}, NOW-1, self.plugin.db, {})
        self.assertEqual(result, (0, False))
        self.assertEqual(await self.rows("SELECT * FROM submissions"), [])

    async def test_unchanged_handle_writes_submissions(self):
        await self.add_user(handle="old_handle")
        with patch.object(crawler, "request_cf_api", AsyncMock(return_value=response(submission(NOW)))):
            result = await crawler.Crawler.fetch_cf_submissions(
                object(), {"cf_handle":"old_handle", "qq_id":"10001"}, NOW-1, self.plugin.db, {})
        self.assertEqual(result, (1, True))
        self.assertEqual(len(await self.rows("SELECT * FROM submissions")), 1)
