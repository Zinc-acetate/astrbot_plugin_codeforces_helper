"""Repeat the 1.3.3 acceptance checks without production data or QQ access."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def command_check(name, command):
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    output = completed.stdout + completed.stderr
    (OUTPUT / f"{name}.log").write_text(output, encoding="utf-8")
    result = {"command": command, "exit_code": completed.returncode,
              "seconds": round(time.monotonic() - started, 3)}
    if name == "python-tests":
        count = re.search(r"Ran (\d+) tests? in", output)
        if count:
            result["tests"] = int(count[1])
    elif name == "frontend-tests":
        for label in ("tests", "pass", "fail", "skipped", "cancelled"):
            count = re.search(rf"^# {label} (\d+)$", output, re.MULTILINE)
            if count:
                result[label] = int(count[1])
    print(f"{name}: {'PASS' if completed.returncode == 0 else 'FAIL'}", flush=True)
    return result


async def live_api_check():
    import aiohttp
    from tests.runtime_support import PluginTestCase, crawler

    started = int(time.time())
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
        row = (await fixture.rows("SELECT cf_rating,cf_rating_updated_at FROM users WHERE qq_id='10001'"))[0]
        return {"passed": count == 1 and row["cf_rating"] is not None
                          and row["cf_rating_updated_at"] >= started,
                "updated_members": count, "api_requests": request.await_count}
    finally:
        await fixture.asyncTearDown()


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-api", action="store_true", help="Also check public CF bad-handle isolation")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required for the frontend acceptance tests")
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.3.3",
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "node": subprocess.check_output([node, "--version"], text=True).strip(),
                        "packages": {name: importlib.metadata.version(name) for name in
                                     ("aiohttp", "aiosqlite", "Quart", "APScheduler", "Pillow")}},
        "red_baseline": {"commit": "7fe40f2b6afddcef33e394139aaeb124ded89a45",
                         "python_new_tests": 12, "python_failures": 10,
                         "frontend_tests": 5, "frontend_failures": 5},
        "checks": {},
        "boundaries": ["AstrBot host and QQ sending are substituted in tests",
                       "Frontend scripts use a controlled DOM and network in Node.js",
                       "No full AstrBot/QQ deployment or Linux execution is included"],
    }
    result["checks"]["python"] = command_check("python-tests", [
        sys.executable, "-X", "utf8", "-B", "-W", "error", "-m", "unittest", "discover", "-s", "tests", "-v"])
    result["checks"]["frontend"] = command_check("frontend-tests", [
        node, "--test", "--test-reporter=tap", "tests/test_frontend.cjs"])
    result["checks"]["diff"] = command_check("diff-check", ["git", "diff", "--check"])
    paths = [ROOT / "main.py", ROOT / "webui.py", Path(__file__)]
    for directory in ("core", "backend", "tests"):
        paths.extend(sorted((ROOT / directory).glob("*.py")))
    for path in paths:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    result["checks"]["compile"] = {"exit_code": 0, "python_files": len(paths)}
    if args.live_api:
        result["live_api"] = asyncio.run(live_api_check())
        print("live-api: " + ("PASS" if result["live_api"]["passed"] else "FAIL"), flush=True)
    result["passed"] = all(check["exit_code"] == 0 for check in result["checks"].values())
    if args.live_api:
        result["passed"] &= result["live_api"]["passed"]
    paths.extend((ROOT / "public/index.html", ROOT / "tests/test_frontend.cjs",
                  ROOT / "metadata.yaml", ROOT / "README.md", ROOT / "CHANGELOG.md"))
    # Canonical text hashes stay stable across Git's Windows CRLF conversion.
    result["sha256_utf8_lf"] = {str(path.relative_to(ROOT)).replace("\\", "/"):
                               hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                               for path in paths}
    (OUTPUT / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result["passed"]


if __name__ == "__main__":
    sys.exit(not run())
