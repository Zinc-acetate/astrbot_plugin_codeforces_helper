import asyncio
import time

import aiohttp
import aiosqlite
from quart import Blueprint, current_app, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from ..core.crawler import Crawler
from astrbot.api import logger

api = Blueprint("api", __name__)
manual_sync_lock = asyncio.Lock()


async def get_db():
    db_path = current_app.config.get("DB_PATH")
    if not db_path:
        raise ConnectionError("DB path not configured.")
    db = await aiosqlite.connect(db_path, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=30000")
    return db


def admin_required(func):
    async def wrapper(*args, **kwargs):
        if not session.get("acm_admin"):
            return jsonify({"success": False, "message": "请先登录管理后台"}), 401
        return await func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


async def read_setting(db, key, default=None):
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row["value"] if row else default


async def write_setting(db, key, value):
    await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


async def sync_users(qq_ids=None, days=None):
    """WebUI 手动同步入口。与定时任务共用爬虫，并批量刷新 CF 分数缓存。"""
    async with manual_sync_lock:
        db = await get_db()
        try:
            params = []
            sql = "SELECT * FROM users"
            if qq_ids:
                placeholders = ",".join("?" for _ in qq_ids)
                sql += f" WHERE qq_id IN ({placeholders})"
                params.extend(qq_ids)
            async with db.execute(sql, tuple(params)) as cursor:
                users = await cursor.fetchall()
            if not users:
                return {"users": 0, "submissions": 0, "profiles": 0}

            config = current_app.config.get("PLUGIN_CONFIG", {})
            now = int(time.time())
            total_added = 0
            async with aiohttp.ClientSession() as http:
                for user in users:
                    if days is None:
                        start = user["last_sync_timestamp"] or now - 7 * 86400
                    else:
                        start = now - days * 86400
                    if user["cf_handle"]:
                        if days is None:
                            total_added += await Crawler.fetch_cf_submissions(http, user, start, db, config)
                        else:
                            total_added += await Crawler.fetch_cf_submissions_paginated(http, user, start, db, config)
                    await db.execute("UPDATE users SET last_sync_timestamp=? WHERE qq_id=?", (now, user["qq_id"]))
                await db.commit()
                profiles = await Crawler.fetch_cf_profiles(http, users, db, config)
            return {"users": len(users), "submissions": total_added, "profiles": profiles}
        finally:
            await db.close()


@api.route("/register", methods=["POST"])
async def api_register_user():
    """保留原公开接口兼容旧前端；新前端的成员管理走受保护的管理员接口。"""
    return jsonify({"success": False, "message": "成员修改已迁移到管理后台"}), 403


@api.route("/leaderboard", methods=["GET"])
async def api_get_leaderboard():
    """返回指定类型的排行榜；榜单类型使用白名单，避免动态 SQL 注入。"""
    mode = request.args.get("mode", "solved_7").strip().lower()
    modes = {
        "solved_7": {"days": 7, "order": "total_count DESC, u.name ASC"},
        "solved_30": {"days": 30, "order": "total_count DESC, u.name ASC"},
        "current_rating": {
            "days": 7,
            "order": "CASE WHEN u.cf_rating IS NULL THEN 1 ELSE 0 END, u.cf_rating DESC, u.name ASC",
        },
        "max_rating": {
            "days": 7,
            "order": "CASE WHEN u.cf_max_rating IS NULL THEN 1 ELSE 0 END, u.cf_max_rating DESC, u.name ASC",
        },
    }
    selected = modes.get(mode)
    if selected is None:
        return jsonify({"success": False, "message": "不支持的排行榜类型"}), 400

    db = await get_db()
    try:
        since = int(time.time()) - selected["days"] * 86400
        sql = f"""
            SELECT u.qq_id, u.name, u.cf_handle, u.status, u.school,
                   u.cf_rating, u.cf_rank, u.cf_max_rating, u.cf_max_rank, u.cf_rating_updated_at,
                   COUNT(DISTINCT CASE WHEN s.submit_time>=? THEN s.problem_id END) AS cf_count,
                   COUNT(DISTINCT CASE WHEN s.submit_time>=? THEN s.problem_id END) AS total_count
            FROM users u LEFT JOIN submissions s ON u.qq_id=s.user_qq_id AND s.platform='codeforces'
            GROUP BY u.qq_id
            ORDER BY {selected['order']}
        """
        cursor = await db.execute(sql, (since, since))
        return jsonify([dict(row) for row in await cursor.fetchall()])
    except Exception as e:
        logger.error(f"[API Error] /leaderboard: {e}", exc_info=True)
        return jsonify([]), 500
    finally:
        await db.close()


@api.route("/cf-rating", methods=["GET"])
async def api_get_cached_cf_rating():
    handle = request.args.get("handle", "").strip()
    if not handle:
        return jsonify({"success": False, "message": "请输入 Codeforces Handle"}), 400
    db = await get_db()
    try:
        async with db.execute(
            """SELECT name, cf_handle, cf_rating, cf_rank, cf_max_rating, cf_max_rank,
                      cf_rating_updated_at FROM users WHERE LOWER(cf_handle)=LOWER(?)""",
            (handle,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return jsonify({"success": False, "message": "本地成员中没有该 Handle"}), 404
        return jsonify({"success": True, "data": dict(row), "cached": True})
    finally:
        await db.close()


@api.route("/admin/login", methods=["POST"])
async def admin_login():
    data = await request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    db = await get_db()
    try:
        password_hash = await read_setting(db, "admin_password_hash")
        if not password_hash or not check_password_hash(password_hash, password):
            return jsonify({"success": False, "message": "密码错误"}), 401
        session.clear()
        session["acm_admin"] = True
        session.permanent = True
        return jsonify({"success": True, "message": "登录成功"})
    finally:
        await db.close()


@api.route("/admin/logout", methods=["POST"])
@admin_required
async def admin_logout():
    session.clear()
    return jsonify({"success": True})


@api.route("/admin/session", methods=["GET"])
async def admin_session():
    return jsonify({"authenticated": bool(session.get("acm_admin"))})


@api.route("/admin/settings", methods=["GET", "PUT"])
@admin_required
async def admin_settings():
    db = await get_db()
    try:
        if request.method == "GET":
            interval = int(await read_setting(db, "sync_interval_minutes", "60"))
            return jsonify({"success": True, "sync_interval_minutes": interval})
        data = await request.get_json(silent=True) or {}
        try:
            interval = int(data.get("sync_interval_minutes"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "更新间隔必须是整数"}), 400
        if not 5 <= interval <= 1440:
            return jsonify({"success": False, "message": "更新间隔必须在5至1440分钟之间"}), 400
        await write_setting(db, "sync_interval_minutes", interval)
        await db.commit()
        return jsonify({"success": True, "message": "设置已保存，最多一分钟后应用", "sync_interval_minutes": interval})
    finally:
        await db.close()


@api.route("/admin/password", methods=["PUT"])
@admin_required
async def admin_password():
    data = await request.get_json(silent=True) or {}
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    if len(new_password) < 6:
        return jsonify({"success": False, "message": "新密码至少6位"}), 400
    db = await get_db()
    try:
        current_hash = await read_setting(db, "admin_password_hash")
        if not current_hash or not check_password_hash(current_hash, old_password):
            return jsonify({"success": False, "message": "原密码错误"}), 400
        await write_setting(db, "admin_password_hash", generate_password_hash(new_password))
        await db.commit()
        return jsonify({"success": True, "message": "密码已修改"})
    finally:
        await db.close()


@api.route("/admin/users", methods=["GET", "POST", "DELETE"])
@admin_required
async def admin_users():
    db = await get_db()
    try:
        if request.method == "GET":
            async with db.execute("SELECT * FROM users ORDER BY name ASC") as cursor:
                return jsonify({"success": True, "users": [dict(row) for row in await cursor.fetchall()]})
        data = await request.get_json(silent=True) or {}
        if request.method == "POST":
            users = data.get("users") or []
            if not isinstance(users, list) or not users:
                return jsonify({"success": False, "message": "成员列表不能为空"}), 400
            values = []
            for item in users:
                qq_id = str(item.get("qq_id", "")).strip()
                name = str(item.get("name", "")).strip()
                if not qq_id.isdigit() or not name:
                    return jsonify({"success": False, "message": "每位成员都必须提供数字QQ号和姓名"}), 400
                values.append((qq_id, name, str(item.get("cf_handle", "")).strip() or None,
                               str(item.get("status", "")).strip() or None,
                               str(item.get("school", "")).strip() or None))
            await db.executemany(
                """INSERT INTO users (qq_id,name,cf_handle,status,school,last_sync_timestamp)
                   VALUES (?,?,?,?,?,0) ON CONFLICT(qq_id) DO UPDATE SET name=excluded.name,
                   cf_handle=excluded.cf_handle, status=excluded.status, school=excluded.school""", values)
            await db.commit()
            return jsonify({"success": True, "message": f"已新增或更新 {len(values)} 位成员"})
        qq_ids = [str(x).strip() for x in (data.get("qq_ids") or []) if str(x).strip()]
        if not qq_ids:
            return jsonify({"success": False, "message": "请选择要删除的成员"}), 400
        placeholders = ",".join("?" for _ in qq_ids)
        await db.execute(f"DELETE FROM submissions WHERE user_qq_id IN ({placeholders})", qq_ids)
        cursor = await db.execute(f"DELETE FROM users WHERE qq_id IN ({placeholders})", qq_ids)
        await db.commit()
        return jsonify({"success": True, "message": f"已删除 {cursor.rowcount} 位成员及其过题记录"})
    except Exception as e:
        await db.rollback()
        logger.error(f"管理成员失败: {e}", exc_info=True)
        return jsonify({"success": False, "message": "成员操作失败"}), 500
    finally:
        await db.close()


@api.route("/admin/sync", methods=["POST"])
@admin_required
async def admin_sync():
    if manual_sync_lock.locked():
        return jsonify({"success": False, "message": "已有手动更新任务正在运行"}), 409
    data = await request.get_json(silent=True) or {}
    qq_ids = [str(x).strip() for x in (data.get("qq_ids") or []) if str(x).strip()] or None
    days = data.get("days")
    if days is not None:
        try:
            days = max(1, min(int(days), 3650))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "同步天数必须是整数"}), 400
    try:
        result = await sync_users(qq_ids=qq_ids, days=days)
        return jsonify({"success": True, "message": "数据更新完成", "result": result})
    except Exception as e:
        logger.error(f"手动更新失败: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"手动更新失败：{e}"}), 500
