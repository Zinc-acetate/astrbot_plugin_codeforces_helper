"""Disposable localhost-only acceptance fixture; never uses a production database."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.runtime_support import PluginTestCase, webui
from hypercorn.asyncio import serve
from hypercorn.config import Config


async def run():
    fixture = PluginTestCase()
    await fixture.asyncSetUp()
    try:
        await fixture.add_user()
        # Simulate malicious legacy data from before server-side validation existed.
        payload = 'audit" onmouseover="document.getElementById(\'rating-result\').textContent=\'INJECTION_EXECUTED\'" data-fixture="'
        await fixture.plugin.db.execute("UPDATE users SET name=?,cf_handle=?",
                                       ('Legacy " Member <test>', payload))
        await fixture.plugin.db.commit()
        config = Config()
        config.bind = ["127.0.0.1:18788"]
        print("Local acceptance fixture ready", flush=True)
        await serve(webui.app, config)
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(run())
