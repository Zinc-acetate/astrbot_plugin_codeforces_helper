"""Public, credential-free CF user.info smoke check using an isolated database."""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import aiohttp
from tests.runtime_support import PluginTestCase, crawler


async def run():
    fixture = PluginTestCase()
    await fixture.asyncSetUp()
    try:
        await fixture.add_user(handle="tourist")
        await fixture.add_user(qq="10002", handle="cf_audit_9f6c3d82")
        request = AsyncMock(wraps=crawler.request_cf_api)
        async with aiohttp.ClientSession() as session:
            with patch.object(crawler, "request_cf_api", request):
                count = await crawler.Crawler.fetch_cf_profiles(
                    session, await fixture.rows("SELECT * FROM users"), fixture.plugin.db, {})
        row = (await fixture.rows("SELECT cf_rating FROM users WHERE qq_id='10001'"))[0]
        result = {"checked_at":datetime.now(timezone.utc).isoformat(),
                  "updated_members":count, "api_requests":request.await_count,
                  "valid_member_updated":count == 1 and row["cf_rating"] is not None}
        print(json.dumps(result), flush=True)
        (Path(__file__).parent / "live-api.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        return result["valid_member_updated"]
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__":
    sys.exit(not asyncio.run(run()))
