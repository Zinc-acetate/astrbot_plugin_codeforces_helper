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
    async def fetch_cf_submissions(session: aiohttp.ClientSession, user_row: aiosqlite.Row, start_timestamp: int, db: aiosqlite.Connection, config: dict) -> int:
        handle = user_row['cf_handle']; qq_id = user_row['qq_id']
        api_key = config.get("cf_api_key"); api_secret = config.get("cf_api_secret")
        method_name = "user.status"; params = {"handle": handle, "from": "1", "count": "100"}

        if api_key and api_secret:
            params["apiKey"] = api_key; params["time"] = str(int(time.time()))
            params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)

        url = f"https://codeforces.com/api/{method_name}?" + '&'.join([f"{k}={v}" for k, v in params.items()])

        added_count = 0
        try:
            async with session.get(url, timeout=15) as response:
                response.raise_for_status(); data = await response.json()
            if data.get('status') != 'OK':
                logger.error(f"CF API 请求失败 (用户: {handle}): {data.get('comment')}")
                return 0

            insert_tasks, processed_in_sync = [], set()
            for sub in data.get('result', []):
                if not isinstance(sub, dict) or sub.get('verdict') != 'OK' or 'problem' not in sub: continue

                prob = sub.get('problem')
                if not isinstance(prob, dict): continue

                if sub.get('creationTimeSeconds', 0) >= start_timestamp:
                    problem_name, contest_id, problem_index = prob.get('name'), prob.get('contestId'), prob.get('index')
                    if not problem_name and not (contest_id and problem_index): continue

                    if contest_id and problem_index:
                        stable_pid = f"cf_{contest_id}{problem_index}"
                    else:
                        name_norm = ''.join(filter(str.isalnum, problem_name or '')).lower()
                        if not name_norm: continue
                        stable_pid = f"cf_{name_norm}_{prob.get('rating', -1)}"

                    if stable_pid in processed_in_sync: continue

                    async with db.execute("SELECT 1 FROM submissions WHERE user_qq_id = ? AND platform = 'codeforces' AND problem_id = ?", (qq_id, stable_pid)) as c:
                        if not await c.fetchone():
                            url_part = f"gym/{contest_id}/problem/{problem_index}" if contest_id and contest_id >= 100000 else f"problemset/problem/{contest_id}/{problem_index}"
                            problem_url = f"https://codeforces.com/{url_part}" if contest_id and problem_index else ""
                            insert_tasks.append((qq_id, 'codeforces', stable_pid, problem_name or "Unknown Problem", str(prob.get('rating', -1)), problem_url, sub['creationTimeSeconds']))
                            processed_in_sync.add(stable_pid)
                elif sub.get('creationTimeSeconds', 0) < start_timestamp:
                    break

            if insert_tasks:
                await db.executemany("INSERT INTO submissions (user_qq_id, platform, problem_id, problem_name, problem_rating, problem_url, submit_time) VALUES (?, ?, ?, ?, ?, ?, ?)", insert_tasks)
                await db.commit()
                added_count += len(insert_tasks)

        except Exception as e:
            logger.error(f"处理 CF 用户 {handle} 时发生严重错误: {e}", exc_info=True)

        return added_count

    @staticmethod
    async def fetch_cf_submissions_paginated(session: aiohttp.ClientSession, user_row: aiosqlite.Row, start_timestamp: int, db: aiosqlite.Connection, config: dict) -> int:
        """深度、分页的CF爬虫，获取指定时间内所有记录。用于/acm sql命令。"""
        handle = user_row['cf_handle']; qq_id = user_row['qq_id']
        api_key = config.get("cf_api_key"); api_secret = config.get("cf_api_secret")
        method_name = "user.status"

        from_index, stop_fetching = 1, False
        all_insert_tasks = []; processed_in_sync = set()
        while not stop_fetching:
            params = {"handle": handle, "from": str(from_index), "count": "100"}
            if api_key and api_secret:
                params["apiKey"] = api_key; params["time"] = str(int(time.time()))
                params["apiSig"] = Crawler._generate_cf_api_sig(method_name, params, api_key, api_secret)

            url = f"https://codeforces.com/api/{method_name}?" + '&'.join([f"{k}={v}" for k, v in params.items()])

            try:
                async with session.get(url, timeout=30) as response:
                    response.raise_for_status(); data = await response.json()
                if data.get('status') != 'OK': logger.error(f"CF API 请求失败 (用户: {handle}, 页码: {from_index // 100 + 1}): {data.get('comment')}"); break

                subs = data.get('result', [])
                if not subs: break

                page_insert_tasks = []
                for sub in subs:
                    if not isinstance(sub, dict) or sub.get('verdict') != 'OK' or 'problem' not in sub: continue
                    prob = sub.get('problem');
                    if not isinstance(prob, dict): continue
                    submission_time = sub.get('creationTimeSeconds', 0)
                    if submission_time >= start_timestamp:
                        problem_name, contest_id, problem_index = prob.get('name'), prob.get('contestId'), prob.get('index')
                        if not all([problem_name, contest_id, problem_index]): continue
                        stable_pid = f"cf_{contest_id}{problem_index}"
                        if stable_pid in processed_in_sync: continue
                        async with db.execute("SELECT 1 FROM submissions WHERE user_qq_id = ? AND platform = 'codeforces' AND problem_id = ?", (qq_id, stable_pid)) as c:
                            if not await c.fetchone():
                                url_part = f"gym/{contest_id}/problem/{problem_index}" if contest_id >= 100000 else f"problemset/problem/{contest_id}/{problem_index}"
                                problem_url = f"https://codeforces.com/{url_part}"
                                page_insert_tasks.append((qq_id, 'codeforces', stable_pid, problem_name, str(prob.get('rating', -1)), problem_url, submission_time))
                                processed_in_sync.add(stable_pid)
                    else:
                        stop_fetching = True

                if page_insert_tasks: all_insert_tasks.extend(page_insert_tasks)
                from_index += 100
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"处理 CF 用户 {handle} (深度同步) 时发生错误: {e}", exc_info=False)
                break

        if all_insert_tasks:
            await db.executemany("INSERT OR IGNORE INTO submissions (user_qq_id, platform, problem_id, problem_name, problem_rating, problem_url, submit_time) VALUES (?, ?, ?, ?, ?, ?, ?)", all_insert_tasks)
            await db.commit()
            return len(all_insert_tasks)
        return 0
