import aiohttp
import time
import aiosqlite
import asyncio
import hashlib
import random
from astrbot.api import logger

class Crawler:
    @staticmethod
    def _generate_cf_api_sig(method_name: str, params: dict, api_key: str, api_secret: str) -> str:
        rand = ''.join([chr(random.randint(ord('a'), ord('z'))) for _ in range(6)])
        param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        text = f"{rand}/{method_name}?{param_str}#{api_secret}"
        sha512_hash = hashlib.sha512(text.encode('utf-8')).hexdigest()
        return f"{rand}{sha512_hash}"

    @staticmethod
    async def fetch_cf_profiles(session: aiohttp.ClientSession, user_rows, db: aiosqlite.Connection, config: dict) -> int:
        """用一次 user.info 批量刷新多个用户的 Codeforces 资料缓存。"""
        rows = [row for row in user_rows if row['cf_handle']]
        if not rows:
            return 0
        handle_to_qq = {row['cf_handle'].lower(): row['qq_id'] for row in rows}
        params = {"handles": ";".join(row['cf_handle'] for row in rows), "checkHistoricHandles": "false"}
        method_name = "user.info"
        api_key, api_secret = config.get("cf_api_key"), config.get("cf_api_secret")
        if api_key and api_secret:
            params["apiKey"] = api_key
            params["time"] = str(int(time.time()))
            params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)
        from urllib.parse import urlencode
        url = f"https://codeforces.com/api/{method_name}?{urlencode(params)}"
        try:
            async with session.get(url, timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
            if data.get("status") != "OK":
                logger.warning(f"批量 CF 用户资料请求失败: {data.get('comment', '未知错误')}")
                return 0
            now, updates = int(time.time()), []
            for profile in data.get("result", []):
                qq_id = handle_to_qq.get(str(profile.get("handle", "")).lower())
                if qq_id:
                    updates.append((profile.get("rating"), profile.get("rank"), profile.get("maxRating"), profile.get("maxRank"), now, qq_id))
            if updates:
                await db.executemany("""UPDATE users SET cf_rating=?, cf_rank=?, cf_max_rating=?,
                    cf_max_rank=?, cf_rating_updated_at=? WHERE qq_id=?""", updates)
                await db.commit()
            return len(updates)
        except Exception as e:
            logger.error(f"批量刷新 CF 用户资料失败: {e}")
            return 0

    @staticmethod
    async def fetch_cf_profile(session: aiohttp.ClientSession, user_row: aiosqlite.Row, db: aiosqlite.Connection, config: dict) -> bool:
        """刷新并缓存 Codeforces 用户资料；仅在正常刷题同步时调用，避免额外高频请求。"""
        handle = user_row['cf_handle']
        if not handle:
            return False
        params = {"handles": handle, "checkHistoricHandles": "false"}
        method_name = "user.info"
        api_key = config.get("cf_api_key")
        api_secret = config.get("cf_api_secret")
        if api_key and api_secret:
            params["apiKey"] = api_key
            params["time"] = str(int(time.time()))
            params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)
        from urllib.parse import urlencode
        url = f"https://codeforces.com/api/{method_name}?{urlencode(params)}"
        try:
            async with session.get(url, timeout=15) as response:
                response.raise_for_status()
                data = await response.json()
            if data.get("status") != "OK" or not data.get("result"):
                logger.warning(f"CF 用户资料请求失败 (用户: {handle}): {data.get('comment', '无数据')}")
                return False
            profile = data["result"][0]
            await db.execute(
                """UPDATE users SET cf_rating = ?, cf_rank = ?, cf_max_rating = ?,
                   cf_max_rank = ?, cf_rating_updated_at = ? WHERE qq_id = ?""",
                (profile.get("rating"), profile.get("rank"), profile.get("maxRating"),
                 profile.get("maxRank"), int(time.time()), user_row["qq_id"]),
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error(f"刷新 CF 用户资料失败 (用户: {handle}): {e}")
            return False

    @staticmethod
    def _submission_identity(prob: dict):
        problem_name = prob.get("name")
        contest_id = prob.get("contestId")
        problem_index = prob.get("index")
        if contest_id is not None and problem_index:
            return f"cf_{contest_id}{problem_index}", problem_name or "Unknown Problem", contest_id, problem_index
        name_norm = "".join(filter(str.isalnum, problem_name or "")).lower()
        if not name_norm:
            return None
        return f"cf_{name_norm}_{prob.get('rating', -1)}", problem_name or "Unknown Problem", None, None

    @staticmethod
    async def fetch_cf_submissions(session: aiohttp.ClientSession, user_row: aiosqlite.Row,
                                   start_timestamp: int, db: aiosqlite.Connection,
                                   config: dict) -> tuple[int, bool]:
        """分页获取起始时间之后的提交；返回（实际新增数，是否完整成功）。"""
        handle, qq_id = user_row["cf_handle"], user_row["qq_id"]
        api_key, api_secret = config.get("cf_api_key"), config.get("cf_api_secret")
        method_name = "user.status"
        from_index = 1
        candidates = []
        processed = set()

        while True:
            params = {"handle": handle, "from": str(from_index), "count": "100"}
            if api_key and api_secret:
                params["apiKey"] = api_key
                params["time"] = str(int(time.time()))
                params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)
            from urllib.parse import urlencode
            url = f"https://codeforces.com/api/{method_name}?{urlencode(params)}"
            try:
                async with session.get(url, timeout=30) as response:
                    response.raise_for_status()
                    data = await response.json()
            except Exception as e:
                logger.error(f"获取 CF 用户 {handle} 提交失败（from={from_index}）: {e}")
                return 0, False
            if data.get("status") != "OK":
                logger.error(f"CF API 请求失败（用户: {handle}）: {data.get('comment')}")
                return 0, False

            submissions = data.get("result", [])
            if not submissions:
                break
            reached_start = False
            for sub in submissions:
                if not isinstance(sub, dict):
                    continue
                submission_time = int(sub.get("creationTimeSeconds", 0) or 0)
                if submission_time < start_timestamp:
                    reached_start = True
                    continue
                if sub.get("verdict") != "OK" or not isinstance(sub.get("problem"), dict):
                    continue
                identity = Crawler._submission_identity(sub["problem"])
                if not identity:
                    continue
                stable_pid, problem_name, contest_id, problem_index = identity
                if stable_pid in processed:
                    continue
                processed.add(stable_pid)
                if contest_id is not None and problem_index:
                    url_part = (f"gym/{contest_id}/problem/{problem_index}" if contest_id >= 100000
                                else f"problemset/problem/{contest_id}/{problem_index}")
                    problem_url = f"https://codeforces.com/{url_part}"
                else:
                    problem_url = ""
                candidates.append((qq_id, "codeforces", stable_pid, problem_name,
                                   str(sub["problem"].get("rating", -1)), problem_url, submission_time))
            if reached_start or len(submissions) < 100:
                break
            from_index += 100
            await asyncio.sleep(0.5)

        before = db.total_changes
        if candidates:
            await db.executemany(
                """INSERT OR IGNORE INTO submissions
                   (user_qq_id, platform, problem_id, problem_name, problem_rating, problem_url, submit_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                candidates,
            )
            await db.commit()
        return db.total_changes - before, True

    @staticmethod
    async def fetch_cf_submissions_paginated(session: aiohttp.ClientSession, user_row: aiosqlite.Row,
                                             start_timestamp: int, db: aiosqlite.Connection,
                                             config: dict) -> tuple[int, bool]:
        """兼容旧调用名；当前普通与深度同步均使用安全分页。"""
        return await Crawler.fetch_cf_submissions(session, user_row, start_timestamp, db, config)
