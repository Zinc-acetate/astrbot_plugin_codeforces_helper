import asyncio
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from core.cf_api import request_cf_api
from core.rate_limit import (
    CODEFORCES_API_RATE_LIMITER,
    CrossProcessRateLimiter,
    configure_codeforces_api_rate_limiter,
)


def wait_in_child(state_path, interval, ready, start, results):
    limiter = CrossProcessRateLimiter(Path(state_path), min_interval=interval)
    ready.put(True)
    start.wait()
    asyncio.run(limiter.wait())
    results.put(time.time())


class FakeLimiter:
    def __init__(self):
        self.calls = 0

    async def wait(self):
        self.calls += 1


class FakeResponse:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if isinstance(self.data, Exception):
            raise self.data
        return None

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        return FakeResponse(next(self.responses))


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class CodeforcesApiTests(unittest.IsolatedAsyncioTestCase):
    def test_production_interval_exceeds_official_minimum(self):
        self.assertGreater(CODEFORCES_API_RATE_LIMITER.min_interval, 2.0)

    def test_runtime_limiter_is_placed_next_to_plugin_database(self):
        original = CODEFORCES_API_RATE_LIMITER.state_path
        try:
            configure_codeforces_api_rate_limiter(Path("plugin-data") / "codeforces_helper.db")
            self.assertEqual(
                CODEFORCES_API_RATE_LIMITER.state_path,
                Path("plugin-data") / "codeforces_api_rate_limit.db",
            )
        finally:
            CODEFORCES_API_RATE_LIMITER.state_path = original

    async def test_retries_call_limit_failures_through_limiter(self):
        limiter = FakeLimiter()
        session = FakeSession([
            {"status": "FAILED", "comment": "Call limit exceeded"},
            {"status": "OK", "result": [1]},
        ])

        result = await request_cf_api(
            session,
            "user.info",
            {"handles": "a;b"},
            limiter=limiter,
        )

        self.assertEqual(result, {"status": "OK", "result": [1]})
        self.assertEqual(limiter.calls, 2)
        self.assertEqual(len(session.urls), 2)
        self.assertIn("handles=a%3Bb", session.urls[0][0])

    async def test_non_limit_failure_is_not_retried(self):
        limiter = FakeLimiter()
        session = FakeSession([
            {"status": "FAILED", "comment": "handle: User not found"},
        ])

        result = await request_cf_api(
            session,
            "user.status",
            {"handle": "missing"},
            limiter=limiter,
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(limiter.calls, 1)

    async def test_retries_http_429(self):
        limiter = FakeLimiter()
        session = FakeSession([
            FakeHttpError(429),
            {"status": "OK", "result": []},
        ])

        result = await request_cf_api(
            session,
            "contest.list",
            {"gym": "false"},
            limiter=limiter,
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(limiter.calls, 2)

    async def test_rate_limiter_spaces_independent_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "rate.sqlite3"
            limiter_a = CrossProcessRateLimiter(state_path, min_interval=0.08)
            limiter_b = CrossProcessRateLimiter(state_path, min_interval=0.08)

            async def run(limiter):
                await limiter.wait()
                return time.monotonic()

            starts = sorted(await asyncio.gather(run(limiter_a), run(limiter_b)))

        self.assertGreaterEqual(starts[1] - starts[0], 0.06)

    def test_rate_limiter_spaces_separate_processes(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "process-rate.sqlite3")
            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=wait_in_child,
                    args=(state_path, 0.08, ready, start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=10)
            start.set()
            starts = sorted(results.get(timeout=10) for _ in processes)
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

        self.assertGreaterEqual(starts[1] - starts[0], 0.06)
