import aiohttp
import time
import aiosqlite
import hashlib
import random
from collections import defaultdict
from astrbot.api import logger

from .cf_api import request_cf_api
from .sync_state import replace_submission_window

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
        """去重分批刷新资料；仅对坏 Handle 拆批，不放大网络故障。"""
        rows = [row for row in user_rows if row['cf_handle']]
        if not rows:
            return 0
        handle_to_qq = defaultdict(list)
        for row in rows:
            handle_to_qq[row['cf_handle'].lower()].append(row['qq_id'])

        async def fetch_batch(handles):
            params = {"handles": ";".join(handles), "checkHistoricHandles": "false"}
            key, secret = config.get("cf_api_key"), config.get("cf_api_secret")
            if key and secret:
                params.update(apiKey=key, time=str(int(time.time())))
                params["apiSig"] = Crawler._generate_cf_api_sig("user.info", params, key, secret)
            try:
                data = await request_cf_api(session, "user.info", params, timeout=20)
            except Exception as exc:
                logger.warning(f"CF 资料请求失败（{len(handles)} 个 Handle）: {type(exc).__name__}")
                return []
            if not isinstance(data, dict):
                return []
            if data.get("status") == "OK":
                return data.get("result", [])
            comment = str(data.get("comment", ""))
            invalid_handle = "handle" in comment.lower() and any(
                word in comment.lower() for word in ("not found", "invalid", "should contain"))
            if invalid_handle and len(handles) > 1:
                middle = len(handles) // 2
                left = await fetch_batch(handles[:middle])
                return left + await fetch_batch(handles[middle:])
            logger.warning(f"CF 资料请求失败（{';'.join(handles)}）: {comment}")
            return []

        handles = list(handle_to_qq)
        profiles = {}
        for index in range(0, len(handles), 100):
            for profile in await fetch_batch(handles[index:index+100]):
                profiles[str(profile.get("handle", "")).lower()] = profile
        updated = 0
        for handle, profile in profiles.items():
            for qq_id in handle_to_qq.get(handle, []):
                cursor = await db.execute("""UPDATE users SET cf_rating=?,cf_rank=?,cf_max_rating=?,
                    cf_max_rank=?,cf_rating_updated_at=? WHERE qq_id=? AND LOWER(cf_handle)=?""",
                    (profile.get("rating"), profile.get("rank"), profile.get("maxRating"),
                     profile.get("maxRank"), int(time.time()), qq_id, handle))
                updated += cursor.rowcount
        await db.commit()
        return updated

    @staticmethod
    async def fetch_cf_profile(session: aiohttp.ClientSession, user_row: aiosqlite.Row, db: aiosqlite.Connection, config: dict) -> bool:
        """刷新并缓存 Codeforces 用户资料；仅在正常刷题同步时调用，避免额外高频请求。"""
        return bool(await Crawler.fetch_cf_profiles(session, [user_row], db, config))

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
        candidates = {}

        while True:
            params = {"handle": handle, "from": str(from_index), "count": "100"}
            if api_key and api_secret:
                params["apiKey"] = api_key
                params["time"] = str(int(time.time()))
                params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)
            try:
                data = await request_cf_api(session, method_name, params, timeout=30)
            except Exception as e:
                logger.error(f"获取 CF 用户 {handle} 提交失败（from={from_index}）: {e}")
                return 0, False
            if not isinstance(data, dict):
                return 0, False
            if data.get("status") != "OK":
                logger.error(f"CF API 请求失败（用户: {handle}）: {data.get('comment')}")
                return 0, False

            submissions = data.get("result")
            if not isinstance(submissions, list):
                return 0, False
            if not submissions:
                break
            reached_start = False
            for sub in submissions:
                if not isinstance(sub, dict) or not isinstance(sub.get("creationTimeSeconds"), int):
                    return 0, False
                submission_time = sub["creationTimeSeconds"]
                if submission_time < start_timestamp:
                    reached_start = True
                    continue
                if not isinstance(sub.get("problem"), dict) or not isinstance(sub.get("id"), int):
                    return 0, False
                identity = Crawler._submission_identity(sub["problem"])
                if not identity:
                    return 0, False
                stable_pid, problem_name, contest_id, problem_index = identity
                if contest_id is not None and problem_index:
                    url_part = (f"gym/{contest_id}/problem/{problem_index}" if contest_id >= 100000
                                else f"problemset/problem/{contest_id}/{problem_index}")
                    problem_url = f"https://codeforces.com/{url_part}"
                else:
                    problem_url = ""
                verdict = sub.get("verdict") or "SUBMITTED"
                # Failed pretests are terminal; CF keeps their PRETESTS label forever.
                pending = verdict in {"SUBMITTED", "TESTING"} or (
                    verdict == "OK" and sub.get("testset") in {"PRETESTS", "SAMPLES"}
                )
                candidates[str(sub["id"])] = (qq_id, str(sub["id"]), stable_pid, problem_name,
                    str(sub["problem"].get("rating", -1)), problem_url, submission_time, verdict, int(pending))
            if reached_start or len(submissions) < 100:
                break
            from_index += 100

        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT cf_handle FROM users WHERE qq_id = ?", (qq_id,)) as cursor:
                current_user = await cursor.fetchone()
            current_handle = current_user["cf_handle"] if current_user else None
            if not current_handle or str(current_handle).lower() != str(handle).lower():
                logger.warning(f"用户 {qq_id} 的 CF Handle 在同步期间发生变化，丢弃本轮旧数据。")
                await db.rollback()
                return 0, False
            added = await replace_submission_window(db, qq_id, start_timestamp, list(candidates.values()))
            await db.commit()
            return added, True
        except BaseException:
            await db.rollback()
            raise

    @staticmethod
    async def fetch_cf_submissions_paginated(session: aiohttp.ClientSession, user_row: aiosqlite.Row,
                                             start_timestamp: int, db: aiosqlite.Connection,
                                             config: dict) -> tuple[int, bool]:
        """兼容旧调用名；当前普通与深度同步均使用安全分页。"""
        return await Crawler.fetch_cf_submissions(session, user_row, start_timestamp, db, config)
