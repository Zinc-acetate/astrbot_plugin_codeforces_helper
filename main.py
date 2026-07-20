# Codeforces Helper by Zinc-acetate

import asyncio
import aiohttp
import time
import os
import sqlite3
from pathlib import Path
import aiosqlite
from multiprocessing import Process
import urllib.parse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError
from werkzeug.security import generate_password_hash

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.components import Plain, Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
except ImportError:
    logger.error("Pillow 库未安装！图片功能将不可用。")
    PILImage, ImageDraw, ImageFont = None, None, None

from .webui import run_server
from .core.crawler import Crawler
from .core.sync_lock import acquire_sync_lock, SyncAlreadyRunning

@register("astrbot_plugin_codeforces_helper", "Zinc-acetate", "Codeforces 训练、Rating 缓存与管理助手", "1.2.0")
class CodeforcesHelperPlugin(Star):
    db: aiosqlite.Connection
    db_path: Path
    webui_process: Process | None = None
    scheduler: AsyncIOScheduler

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.FONT_PATH = Path(__file__).parent / "resources" / "SourceHanSansSC-Bold.otf"

    async def initialize(self):
        logger.info("Codeforces Helper v1.2.0 开始初始化...")
        await self.connect_db()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        settings = await self._get_all_settings()
        await self.reschedule_jobs(settings)
        self.scheduler.add_job(self._watch_runtime_settings, 'interval', minutes=1, id='settings_watch_job', replace_existing=True)
        self.scheduler.start()
        logger.info("✅ Codeforces Helper 初始化成功！")

    async def terminate(self):
        logger.info("正在关闭 Codeforces Helper...");
        if hasattr(self, 'scheduler') and self.scheduler.running: self.scheduler.shutdown()
        await self.stop_webui_process()
        if hasattr(self, 'db') and self.db: await self.db.close()
        logger.info("Codeforces Helper 已安全关闭。")

    def _prepare_persistent_db(self) -> Path:
        """使用 AstrBot 独立数据目录，并安全迁移旧版插件目录内的数据库。"""
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_codeforces_helper"
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "codeforces_helper.db"
        legacy = Path(__file__).parent / "data" / "codeforces_helper.db"

        # 外部持久化库一旦存在，始终以它为准，防止旧库覆盖新数据。
        if target.exists():
            return target
        if not legacy.exists():
            return target

        temporary = data_dir / f".{target.name}.migrating-{os.getpid()}"
        temporary.unlink(missing_ok=True)
        source_db = None
        target_db = None
        try:
            # SQLite backup API 能正确复制 WAL 中尚未合并回主文件的数据。
            source_db = sqlite3.connect(f"file:{legacy.resolve()}?mode=ro", uri=True)
            target_db = sqlite3.connect(temporary)
            source_db.backup(target_db)
            target_db.commit()
            integrity = target_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"数据库完整性检查失败: {integrity}")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if target_db is not None:
                target_db.close()
            if source_db is not None:
                source_db.close()

        # 同一文件系统内原子启用；迁移成功后保留旧库，便于人工回滚。
        os.replace(temporary, target)
        logger.info(f"旧数据库已安全迁移到持久化目录: {target}")
        return target

    async def connect_db(self):
        self.db_path = self._prepare_persistent_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path); self.db.row_factory = aiosqlite.Row
        await self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_enabled', 'true');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_cron_hour', '*');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_cron_minute', '0');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('hourly_report_limit', '10');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sync_interval_minutes', '60');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password_hash', ?);", (generate_password_hash('123456'),))
        await self.db.execute("CREATE TABLE IF NOT EXISTS users (qq_id TEXT PRIMARY KEY, name TEXT NOT NULL, cf_handle TEXT, status TEXT, school TEXT, last_sync_timestamp INTEGER DEFAULT 0);")
        await self.db.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_qq_id TEXT NOT NULL, platform TEXT NOT NULL, problem_id TEXT NOT NULL, problem_name TEXT, problem_rating TEXT, problem_url TEXT, submit_time INTEGER NOT NULL, UNIQUE(user_qq_id, platform, problem_id));")
        async with self.db.execute("PRAGMA table_info(users)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        migrations = {
            'cf_rating': 'INTEGER', 'cf_rank': 'TEXT', 'cf_max_rating': 'INTEGER',
            'cf_max_rank': 'TEXT', 'cf_rating_updated_at': 'INTEGER DEFAULT 0',
            'history_sync_days': 'INTEGER DEFAULT 0'
        }
        for column, sql_type in migrations.items():
            if column not in columns:
                await self.db.execute(f"ALTER TABLE users ADD COLUMN {column} {sql_type}")
        await self.db.execute("DELETE FROM submissions WHERE platform != 'codeforces'")
        await self.db.commit()

    async def get_setting(self, key, default=None):
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor: row = await cursor.fetchone(); return row['value'] if row else default

    async def set_setting(self, key, value):
        await self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))); await self.db.commit()

    async def _get_all_settings(self) -> dict:
        settings = {};
        async with self.db.execute("SELECT key, value FROM settings") as cursor:
            async for row in cursor: settings[row['key']] = row['value']
        return settings

    async def reschedule_jobs(self, settings: dict):
        report_job_id = 'hourly_report_job'
        try: self.scheduler.remove_job(report_job_id)
        except JobLookupError: pass
        is_enabled = settings.get('report_enabled') == 'true'; group_id = settings.get('notification_group_id')
        cron_hour = settings.get('report_cron_hour', '*'); cron_minute = settings.get('report_cron_minute', '0')
        if is_enabled and group_id:
            try:
                trigger = CronTrigger(hour=cron_hour, minute=cron_minute, timezone="Asia/Shanghai")
                self.scheduler.add_job(self.report_hourly_solves, trigger, id=report_job_id, name="Hourly Report")
                logger.info(f"✅ 定时播报任务已更新。群号: {group_id}, CRON: [hour={cron_hour}, minute={cron_minute}]")
            except Exception as e: logger.error(f"❌ 设置定时播报失败: {e}")
        else: logger.info("ℹ️ 定时播报已禁用或未配置群号。")

        try:
            interval_minutes = max(5, min(int(settings.get('sync_interval_minutes', '60')), 1440))
        except (TypeError, ValueError):
            interval_minutes = 60
        self.scheduler.add_job(
            self.sync_all_users_data,
            IntervalTrigger(minutes=interval_minutes, timezone="Asia/Shanghai"),
            id='sync_data_job', name='Codeforces data and rating sync', replace_existing=True,
            max_instances=1, coalesce=True,
        )
        self._scheduled_sync_interval = interval_minutes
        logger.info(f"✅ 数据与 CF 分数自动更新间隔：{interval_minutes} 分钟。")

    async def _watch_runtime_settings(self):
        """允许 WebUI 修改数据库设置后，无需重启即可重排同步任务。"""
        try:
            value = int(await self.get_setting('sync_interval_minutes', '60'))
            value = max(5, min(value, 1440))
            if value != getattr(self, '_scheduled_sync_interval', None):
                await self.reschedule_jobs(await self._get_all_settings())
        except Exception as e:
            logger.error(f"检查运行时设置失败: {e}")

    async def start_webui_process(self):
        if self.webui_process and self.webui_process.is_alive(): return f"管理后台已在运行！"
        port = self.config.get('webui_port', 8088); logger.info(f"正在端口 {port} 上启动 WebUI 子进程...")
        webui_config = {"cf_api_key": self.config.get("cf_api_key"), "cf_api_secret": self.config.get("cf_api_secret")}
        self.webui_process = Process(target=run_server, args=(str(self.db_path), port, webui_config)); self.webui_process.start(); await asyncio.sleep(2)
        if self.webui_process.is_alive(): logger.info(f"WebUI 子进程已启动, PID: {self.webui_process.pid}"); return f"✨ 管理后台已启动！\n请访问: http://<你的服务器IP>:{port}"
        else: logger.error("WebUI 子进程启动失败！"); return "❌ 后台启动失败"

    async def stop_webui_process(self):
        if not self.webui_process or not self.webui_process.is_alive(): return "管理后台未在运行。"
        logger.info(f"正在终止 WebUI 子进程 (PID: {self.webui_process.pid})..."); self.webui_process.terminate(); self.webui_process.join(timeout=5)
        if self.webui_process.is_alive(): self.webui_process.kill()
        self.webui_process = None; logger.info("WebUI 子进程已终止。"); return "✅ 管理后台已关闭。"

    async def _generate_hourly_report_message(self, hours: int = 1) -> str:
        limit = int(await self.get_setting('hourly_report_limit', 10))
        time_since = int(time.time()) - (hours * 3600)
        #query = "SELECT s.problem_name, s.platform, s.problem_rating, s.problem_url, s.submit_time, u.name as user_name FROM submissions s JOIN users u ON s.user_qq_id = u.qq_id WHERE s.platform = 'codeforces' AND s.submit_time >= ? ORDER BY s.submit_time DESC LIMIT ?"
        query = "SELECT s.problem_name, s.platform, s.problem_rating, s.problem_url, s.submit_time, u.name as user_name FROM submissions s JOIN users u ON s.user_qq_id = u.qq_id WHERE s.platform = 'codeforces' AND s.submit_time >= ? ORDER BY s.submit_time DESC LIMIT ?"
        async with self.db.execute(query, (time_since, limit)) as cursor:
            recent_solves = await cursor.fetchall()
        title_hour_str = f"过去 {hours} 小时内" if hours > 1 else "过去一小时内"
        if not recent_solves:
            return f"{title_hour_str}没有新的过题记录哦～"
        parts = [f"📖 {title_hour_str}过题速报 (Top {len(recent_solves)}):"]
        for solve in recent_solves:
            time_str = time.strftime('%H:%M', time.localtime(solve['submit_time']))
            parts.append(f"\n👤 {solve['user_name']} 在 {time_str} 通过了\n💻 {solve['platform']} - {solve['problem_name']}\n📈 难度: {solve['problem_rating'] or 'N/A'}\n🔗 {solve['problem_url']}")
        return "\n".join(parts)

    async def sync_single_user(self, qq_id: str, refresh_cf_profile: bool = True, days: int = None):
        async with self.db.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)) as cursor:
            user = await cursor.fetchone()
        if not user or not user["cf_handle"]:
            return 0, False
        now = int(time.time())
        history_days = int(user["history_sync_days"] or 0)
        if days is not None:
            requested_days = max(1, min(int(days), 3650))
            start_timestamp = now - requested_days * 86400
            sync_type = f"{requested_days}天深度"
        elif history_days < 30:
            requested_days = 30
            start_timestamp = now - 30 * 86400
            sync_type = "30日补全"
        else:
            requested_days = history_days
            start_timestamp = int(user["last_sync_timestamp"] or now - 30 * 86400)
            sync_type = "增量"
        logger.info(f"  -> 为用户 {user['name']} 执行 [{sync_type}] 同步...")
        async with aiohttp.ClientSession() as session:
            added, complete = await Crawler.fetch_cf_submissions(
                session, user, start_timestamp, self.db, self.config
            )
            if refresh_cf_profile:
                await Crawler.fetch_cf_profile(session, user, self.db, self.config)
        if complete:
            new_history_days = max(history_days, requested_days)
            await self.db.execute(
                "UPDATE users SET last_sync_timestamp=?, history_sync_days=? WHERE qq_id=?",
                (now, new_history_days, user["qq_id"]),
            )
            await self.db.commit()
            if added:
                logger.info(f"    为用户 {user['name']} 同步了 {added} 条新记录。")
        else:
            logger.warning(f"用户 {user['name']} 同步未完整完成，不推进同步时间。")
        return added, complete

    async def sync_all_users_data(self):
        logger.info(f"[智能同步] 开始执行 {time.strftime('%H:%M')} 周期的同步任务...")
        try:
            with acquire_sync_lock(self.db_path):
                async with self.db.execute("SELECT qq_id FROM users WHERE cf_handle IS NOT NULL AND TRIM(cf_handle) != ''") as cursor:
                    user_rows = await cursor.fetchall()
                if not user_rows:
                    return
                for user_row in user_rows:
                    await self.sync_single_user(user_row["qq_id"], refresh_cf_profile=False)
                async with self.db.execute("SELECT * FROM users WHERE cf_handle IS NOT NULL AND TRIM(cf_handle) != ''") as cursor:
                    cf_users = await cursor.fetchall()
                async with aiohttp.ClientSession() as session:
                    refreshed = await Crawler.fetch_cf_profiles(session, cf_users, self.db, self.config)
                logger.info(f"[智能同步] 批量刷新了 {refreshed} 位用户的 CF 分数资料。")
        except SyncAlreadyRunning:
            logger.info("[智能同步] 已有手动或自动更新任务，跳过本轮。")
            return
        logger.info("[智能同步] 本次周期任务完成。")

    async def report_hourly_solves(self):
        message_to_send = await self._generate_hourly_report_message(hours=1)
        if "没有新的过题记录" in message_to_send: logger.info("[小时榜] 无新记录。"); return
        group_id = await self.get_setting("notification_group_id")
        if not group_id: logger.warning("[小时榜] 无法发送，未配置群号。"); return
        try:
            qq_platform = self.context.get_platform("aiocqhttp")
            if not qq_platform: logger.error("[小时榜] 无法获取 QQ 平台实例。"); return
            bot = qq_platform.bot
            onebot_message = [{"type": "text", "data": {"text": message_to_send}}]
            await bot.send_group_msg(group_id=int(group_id), message=onebot_message)
            logger.info(f"[小时榜] 已成功向群 {group_id} 发送播报。")
        except Exception as e: logger.error(f"发送小时榜通知失败: {e}", exc_info=True)

    async def sync_single_user_for_days(self, qq_id: str, days: int):
        return await self.sync_single_user(qq_id, refresh_cf_profile=True, days=days)

    async def _generate_rank_image(self, title: str, users_data: list) -> bytes | str:
        if not all([PILImage, ImageDraw, ImageFont]): return "❌ 无法生成图片：Pillow 库未正确安装。"
        if not self.FONT_PATH.exists(): return f"❌ 无法生成图片：字体文件丢失 ({self.FONT_PATH})。"

        header_height = 80; row_height = 50; footer_height = 20
        width = 800
        height = header_height + row_height + len(users_data) * row_height + footer_height
        image = PILImage.new('RGB', (width, height), '#FFFFFF')
        draw = ImageDraw.Draw(image)

        try:
            font_title = ImageFont.truetype(str(self.FONT_PATH), 24)
            font_header = ImageFont.truetype(str(self.FONT_PATH), 16)
            font_body = ImageFont.truetype(str(self.FONT_PATH), 15)
        except IOError: return f"❌ 无法加载字体文件 {self.FONT_PATH}。"

        color_dark_bg = '#343a40'; color_blue_bg = '#007bff'; color_white = '#FFFFFF'
        color_light_gray_bg = '#f8f9fa'; color_text = '#212529'; color_red = '#dc3545'
        color_green = '#28a745'; color_purple = '#6f42c1';

        draw.rectangle([0, 0, width, header_height], fill=color_dark_bg)
        draw.text((width/2, header_height/2), title, font=font_title, fill=color_white, anchor='mm')

        table_header_y = header_height
        draw.rectangle([0, table_header_y, width, table_header_y + row_height], fill=color_blue_bg)

        columns = {'排名': 60, '用户名': 220, 'CF题数': 470, '身份': 680}
        for text, x in columns.items():
            draw.text((x, table_header_y + row_height/2), text, font=font_header, fill=color_white, anchor='mm')

        for i, user in enumerate(users_data):
            y_start = table_header_y + row_height + i * row_height
            bg_color = color_light_gray_bg if i % 2 == 1 else '#FFFFFF'
            draw.rectangle([0, y_start, width, y_start + row_height], fill=bg_color)

            y_text = y_start + row_height / 2
            draw.text((columns['排名'], y_text), str(i + 1), font=font_body, fill=color_text, anchor='mm')

            display_name = user['user_name']
            if font_body.getlength(display_name) > 150:
                while font_body.getlength(display_name + '..') > 150: display_name = display_name[:-1]
                display_name += '..'
            draw.text((columns['用户名'], y_text), display_name, font=font_body, fill=color_text, anchor='mm')

            draw.text((columns['CF题数'], y_text), str(user['cf_count']), font=font_body, fill=color_red, anchor='mm')
            draw.text((columns['身份'], y_text), user['user_status'] or 'N/A', font=font_body, fill=color_purple, anchor='mm')

        for x in list(columns.values())[1:]:
            draw.line([x - 50, table_header_y, x - 50, height - footer_height], fill='#dee2e6', width=1)

        from io import BytesIO
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue()

    @staticmethod
    def _pd_cf_color(rating):
        if not isinstance(rating, int): rating = 0
        if rating < 1200: return "灰名"
        if rating < 1400: return '绿名 Pupil'
        if rating < 1600: return '青名 Specialist'
        if rating < 1900: return '蓝名 Expert'
        if rating < 2100: return '紫名 Candidate Master'
        if rating < 2300: return '橙名 Master'
        if rating < 2400: return '橙名 International Master'
        if rating < 2600: return '红名 Grandmaster'
        if rating < 3000: return '红名 International Grandmaster'
        return '黑红名 Legendary Grandmaster'

    @staticmethod
    def _format_cf_contest(contest):
        return "比赛名称：{}\n开始时间：{}\n持续时间：{}小时{:02d}分钟\n报名链接：{}".format(
            contest['name'], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(contest['startTimeSeconds']))),
            contest['durationSeconds'] // 3600, contest['durationSeconds'] % 3600 // 60,
            f"https://codeforces.com/contestRegistration/{str(contest['id'])}"
        )

    @filter.command_group("acm")
    def acm_manager(self): pass

    @acm_manager.command("后台启动")
    async def cmd_start_webui(self, event: AstrMessageEvent): msg = await self.start_webui_process(); yield event.plain_result(msg)

    @acm_manager.command("后台关闭")
    async def cmd_stop_webui(self, event: AstrMessageEvent): msg = await self.stop_webui_process(); yield event.plain_result(msg)

    @acm_manager.command("rank")
    async def cmd_show_rank(self, event: AstrMessageEvent):
        """生成近7日刷题量的文本排行榜"""
        seven_days_ago = int(time.time()) - (7 * 24 * 60 * 60)
        # 注意: 这里使用 COUNT(s.id) 而不是 COUNT(DISTINCT s.problem_id) 以匹配经典行为
        query = "SELECT u.name, COUNT(s.id) as total_count FROM users u LEFT JOIN submissions s ON u.qq_id = s.user_qq_id WHERE s.platform = 'codeforces' AND s.submit_time >= ? GROUP BY u.qq_id HAVING total_count > 0 ORDER BY total_count DESC, u.name ASC LIMIT 10"

        async with self.db.execute(query, (seven_days_ago,)) as cursor:
            top_ten = await cursor.fetchall()
        if not top_ten:
            yield event.plain_result("近7日排行榜暂无数据。")
            return

        parts = ["🏆 近 7 日刷题排行榜 Top 10 🏆"]
        emojis = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
        for i, user in enumerate(top_ten):
            parts.append(f"{emojis[i]} {user['name']}: {user['total_count']} 题")

        yield event.plain_result("\n".join(parts))

    @acm_manager.command("rank all")
    async def cmd_show_rank_all(self, event: AstrMessageEvent):
        """生成生涯总刷题量的文本排行榜"""
        query = "SELECT u.name, COUNT(s.id) as total_count FROM users u LEFT JOIN submissions s ON u.qq_id = s.user_qq_id AND s.platform = 'codeforces' GROUP BY u.qq_id HAVING total_count > 0 ORDER BY total_count DESC, u.name ASC LIMIT 10"

        async with self.db.execute(query) as cursor:
            top_ten = await cursor.fetchall()

        if not top_ten:
            yield event.plain_result("生涯总榜暂无数据。")
            return

        parts = ["🏆 生涯总刷题量排行榜 Top 10 🏆"]
        emojis = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
        for i, user in enumerate(top_ten):
            parts.append(f"{emojis[i]} {user['name']}: {user['total_count']} 题")

        yield event.plain_result("\n".join(parts))

    @acm_manager.command("hourly")
    async def cmd_report_hourly(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        hours = 1 # 默认1小时
        if len(cmd_parts) > 2 and cmd_parts[2].isdigit():
            custom_hours = int(cmd_parts[2])
            if 1 <= custom_hours <= 256: # 设置1-24小时的合理范围
                hours = custom_hours
            else:
                yield event.plain_result("⚠️ 小时数必须在 1 到 255 之间。")
                return
        yield event.plain_result(f"正在查询过去 {hours} 小时的过题记录...")
        report_message = await self._generate_hourly_report_message(hours=hours)
        yield event.plain_result(report_message)

    @acm_manager.command("contest")
    async def cmd_get_contests(self, event: AstrMessageEvent):
        url = "https://codeforces.com/api/contest.list?gym=false"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response: response.raise_for_status(); data = await response.json()
            if data.get('status') != 'OK': yield event.plain_result("获取比赛列表失败。"); return
            upcoming_contests = [c for c in data.get('result', []) if c.get('phase') == 'BEFORE' and 'Kotlin' not in c['name'] and 'Unrated' not in c['name']]; upcoming_contests.reverse()
            if not upcoming_contests: yield event.plain_result("最近没有找到合适的 Codeforces 比赛～"); return
            res_parts = [f"找到最近的 {min(5, len(upcoming_contests))} 场 CF 比赛:"]
            for contest in upcoming_contests[:5]: res_parts.append("--------------------"); res_parts.append(self._format_cf_contest(contest))
            yield event.plain_result("\n".join(res_parts))
        except Exception as e: logger.error(f"查询 CF 比赛时出错: {e}", exc_info=True); yield event.plain_result("获取比赛列表时发生网络错误。")

    @acm_manager.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        is_enabled = await self.get_setting('report_enabled') == 'true'; group_id = await self.get_setting('notification_group_id', '未设置')
        cron_hour = await self.get_setting('report_cron_hour'); cron_minute = await self.get_setting('report_cron_minute')
        hourly_limit = await self.get_setting('hourly_report_limit', '10')
        sync_interval = await self.get_setting('sync_interval_minutes', '60')
        status_text = (f"📊 Codeforces 训练助手当前状态:\n--------------------------\n"
                       f"  - 定时播报: {'✅ 开启' if is_enabled else '❌ 关闭'}\n"
                       f"  - AC 记录与 CF Rating 更新间隔: {sync_interval} 分钟\n"
                       f"  - 近期过题播报上限: {hourly_limit} 题\n"
                       f"  - 目标群聊: {group_id}\n"
                       f"  - CRON 表达式: 小时={cron_hour}, 分钟={cron_minute}")
        yield event.plain_result(status_text)

    @acm_manager.command("rating")
    async def cmd_get_rating(self, event: AstrMessageEvent):
        """从本地缓存查询成员 CF 分数，不因群聊查询额外调用 CF API。"""
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3:
            yield event.plain_result("参数错误，请输入已登记的 CF handle。\n格式: /acm rating tourist")
            return
        handle = cmd_parts[2].strip()
        async with self.db.execute(
            """SELECT name, cf_handle, cf_rating, cf_rank, cf_max_rating, cf_max_rank,
                      cf_rating_updated_at FROM users WHERE LOWER(cf_handle)=LOWER(?)""", (handle,)
        ) as cursor:
            user = await cursor.fetchone()
        if not user:
            yield event.plain_result(f"本地成员中没有登记 CF Handle：{handle}")
            return
        updated = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(user['cf_rating_updated_at']))
                   if user['cf_rating_updated_at'] else '尚未完成首次自动更新')
        current = user['cf_rating'] if user['cf_rating'] is not None else '未定级'
        maximum = user['cf_max_rating'] if user['cf_max_rating'] is not None else '无'
        yield event.plain_result(
            f"【{user['name']} / {user['cf_handle']}】CF 分数缓存\n"
            f"当前：{current}（{user['cf_rank'] or 'unrated'}）\n"
            f"最高：{maximum}（{user['cf_max_rank'] or 'unrated'}）\n"
            f"更新时间：{updated}"
        )

    @acm_manager.command("rating榜")
    async def cmd_rating_rank(self, event: AstrMessageEvent):
        """显示成员当前或历史最高 Codeforces Rating 排行榜。"""
        cmd_parts = event.message_str.strip().split()
        mode = cmd_parts[2].lower() if len(cmd_parts) >= 3 else "当前"
        current_aliases = {"当前", "current", "now"}
        max_aliases = {"历史", "最高", "max", "history", "historical"}
        if mode in current_aliases:
            rating_column, rank_column = "cf_rating", "cf_rank"
            title = "Codeforces 当前 Rating 排行榜"
        elif mode in max_aliases:
            rating_column, rank_column = "cf_max_rating", "cf_max_rank"
            title = "Codeforces 历史最高 Rating 排行榜"
        else:
            yield event.plain_result(
                "参数错误。\n当前榜：/acm rating榜\n历史榜：/acm rating榜 历史"
            )
            return

        query = f"""SELECT name, cf_handle, {rating_column} AS rating, {rank_column} AS cf_rank
                    FROM users
                    WHERE cf_handle IS NOT NULL AND TRIM(cf_handle) != ''
                      AND {rating_column} IS NOT NULL
                    ORDER BY {rating_column} DESC, name ASC
                    LIMIT 20"""
        async with self.db.execute(query) as cursor:
            users = await cursor.fetchall()
        if not users:
            yield event.plain_result(f"{title}暂无已定级成员。")
            return

        lines = [f"【{title} Top {len(users)}】"]
        for index, user in enumerate(users, 1):
            rating = user['rating']
            lines.append(
                f"{index}. {user['name']} / {user['cf_handle']}："
                f"{rating}（{self._pd_cf_color(rating)}）"
            )
        lines.append("数据来自本地缓存，由插件定时更新。")
        yield event.plain_result("\n".join(lines))

    @acm_manager.command("sync_user")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_sync_user(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 4 or not cmd_parts[3].isdigit():
            yield event.plain_result("参数错误。\n格式: /acm sync_user <QQ号> <天数>")
            return
        qq_id = cmd_parts[2].strip()
        days = max(1, min(int(cmd_parts[3]), 3650))
        yield event.plain_result(f"收到指令，正在为用户 {qq_id} 执行一次同步任务...")
        try:
            with acquire_sync_lock(self.db_path):
                _, complete = await self.sync_single_user_for_days(qq_id, days)
        except SyncAlreadyRunning as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(f"用户 {qq_id} 的同步任务{'已完成' if complete else '未完整完成，请查看日志后重试'}！")

    @acm_manager.command("del_user")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_delete_user(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3: yield event.plain_result("⚠️ 参数错误，请输入QQ号。\n格式: /acm del_user 12345"); return
        qq_id = cmd_parts[2].strip()
        async with self.db.execute("SELECT name FROM users WHERE qq_id = ?", (qq_id,)) as cursor: user = await cursor.fetchone()
        if not user: yield event.plain_result(f"❌ 删除失败：找不到 QQ号为 {qq_id} 的用户。"); return
        await self.db.execute("DELETE FROM submissions WHERE user_qq_id = ?", (qq_id,)); await self.db.execute("DELETE FROM users WHERE qq_id = ?", (qq_id,)); await self.db.commit()
        logger.info(f"管理员 {event.get_sender_id()} 删除了用户 {user['name']} (QQ: {qq_id})。")
        yield event.plain_result(f"✅ 操作成功！\n已永久删除用户【{user['name']}】(QQ: {qq_id})。")

    @acm_manager.command("set group")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_set_group(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 4: yield event.plain_result("⚠️ 参数错误，请输入群号。"); return
        group_id = cmd_parts[3].strip()
        if not group_id.isdigit(): yield event.plain_result("⚠️ 参数错误，群号必须是数字。"); return
        await self.set_setting('notification_group_id', group_id); settings = await self._get_all_settings(); await self.reschedule_jobs(settings)
        yield event.plain_result(f"✅ 操作成功！\n定时播报群已设置为: {group_id}")

    @acm_manager.command("set cron")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_set_cron(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 5: yield event.plain_result("⚠️ 参数错误。\n格式: /acm set cron * 0"); return
        hour, minute = cmd_parts[3], cmd_parts[4]
        try: CronTrigger(hour=hour, minute=minute)
        except Exception as e: yield event.plain_result(f"❌ 表达式无效！\n错误: {str(e)}"); return
        await self.set_setting('report_cron_hour', hour); await self.set_setting('report_cron_minute', minute)
        settings = await self._get_all_settings(); await self.reschedule_jobs(settings)
        yield event.plain_result(f"✅ 操作成功！\n定时播报时间已设置为: hour='{hour}', minute='{minute}'")

    @acm_manager.command("report")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_toggle_report(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3: yield event.plain_result("⚠️ 参数错误，请输入 on 或 off。"); return
        switch = cmd_parts[2].lower().strip()
        if switch in ['on', 'off']:
            await self.set_setting('report_enabled', 'true' if switch == 'on' else 'false')
            settings = await self._get_all_settings(); await self.reschedule_jobs(settings)
            yield event.plain_result(f"✅ 定时播报功能已【{'开启' if switch == 'on' else '关闭'}】。")
        else: yield event.plain_result("无效的开关。")

    @acm_manager.command("查询")
    async def cmd_query_user_submissions(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3 or not cmd_parts[2].isdigit(): yield event.plain_result("⚠️ 格式错误。\n用法: /acm 查询 <QQ号>"); return
        qq_id = cmd_parts[2]
        async with self.db.execute("SELECT name FROM users WHERE qq_id = ?", (qq_id,)) as cursor: user = await cursor.fetchone()
        if not user: yield event.plain_result(f"❌ 找不到QQ号为 {qq_id} 的用户。"); return
        query = "SELECT platform, problem_name, problem_rating, submit_time FROM submissions WHERE user_qq_id = ? AND platform = 'codeforces' ORDER BY submit_time DESC LIMIT 20"
        async with self.db.execute(query, (qq_id,)) as cursor: submissions = await cursor.fetchall()
        if not submissions: yield event.plain_result(f"用户【{user['name']}】暂无过题记录。"); return
        lines = [f"🔍 用户【{user['name']}】最近的20条过题记录:"]
        for sub in submissions:
            time_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(sub['submit_time']))
            lines.append(f"[{time_str}] {sub['platform']} - {sub['problem_name']} (Rating: {sub['problem_rating'] or 'N/A'})")
        yield event.plain_result("\n".join(lines))

    @acm_manager.command("set hourly_limit")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_set_hourly_limit(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 4 or not cmd_parts[3].isdigit(): yield event.plain_result("⚠️ 格式错误。\n用法: /acm set hourly_limit <数量>"); return
        limit = int(cmd_parts[3])
        if not (1 <= limit <= 50): yield event.plain_result("❌ 数量必须在 1 到 50 之间。"); return
        await self.set_setting("hourly_report_limit", str(limit))
        yield event.plain_result(f"✅ 操作成功！小时榜速报上限已设置为 {limit} 题。")

    async def _query_rank_data(self, days: int = None, status: str = None, limit: int = None) -> list:
        params = []
        sql = "SELECT u.name AS user_name, u.status AS user_status, COUNT(s.id) AS total_count, COUNT(s.id) AS cf_count FROM users u LEFT JOIN submissions s ON u.qq_id = s.user_qq_id AND s.platform = 'codeforces'"
        where_clauses = []
        if days is not None: where_clauses.append("s.submit_time >= ?"); params.append(int(time.time()) - (days * 24 * 60 * 60))
        if status is not None: where_clauses.append("u.status = ?"); params.append(status)
        if where_clauses: sql += " WHERE " + " AND ".join(where_clauses)
        sql += " GROUP BY u.qq_id HAVING total_count > 0 ORDER BY total_count DESC, user_name ASC"
        if limit is not None: sql += f" LIMIT {limit}"

        async with self.db.execute(sql, tuple(params)) as cursor: return await cursor.fetchall()

    @acm_manager.command("过题")
    async def cmd_rank_by_status(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3: yield event.plain_result("⚠️ 格式错误。\n用法: /acm 过题 <身份> [天数]"); return
        status = cmd_parts[2]; days = 7
        if len(cmd_parts) > 3 and cmd_parts[3].isdigit(): days = int(cmd_parts[3])
        title = f"近 {days} 天【{status}】过题排行榜"
        users_data = await self._query_rank_data(days=days, status=status, limit=50)
        if not users_data: yield event.plain_result(f"📊 {title}\n\n该条件下暂无过题记录。"); return
        image_bytes = await self._generate_rank_image(title, users_data)
        if isinstance(image_bytes, str): yield event.plain_result(image_bytes)
        else: yield event.chain_result([Image.fromBytes(image_bytes)])

    @acm_manager.command("sql")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_sql_sync(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3 or not cmd_parts[2].isdigit(): yield event.plain_result("⚠️ 格式错误。\n用法: /acm sql <天数>"); return
        days = max(1, min(int(cmd_parts[2]), 3650))
        yield event.plain_result(f"收到指令！正在为所有用户执行【{days}天深度同步】，请耐心等待...")
        try:
            with acquire_sync_lock(self.db_path):
                async with self.db.execute("SELECT qq_id FROM users") as cursor:
                    all_users = await cursor.fetchall()
                failed = 0
                for user_row in all_users:
                    _, complete = await self.sync_single_user_for_days(user_row['qq_id'], days)
                    failed += int(not complete)
        except SyncAlreadyRunning as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(f"✅ 同步结束，失败 {failed} 位。正在生成榜单...")
        title = f"深度同步 · 近 {days} 天过题排行榜"
        users_data = await self._query_rank_data(days=days, limit=50)
        if not users_data: yield event.plain_result(f"📊 {title}\n\n该条件下暂无过题记录。"); return
        image_bytes = await self._generate_rank_image(title, users_data)
        if isinstance(image_bytes, str): yield event.plain_result(image_bytes)
        else: yield event.chain_result([Image.fromBytes(image_bytes)])

    @acm_manager.command("past")
    async def cmd_past_rank(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3 or not cmd_parts[2].isdigit():
            yield event.plain_result("⚠️ 格式错误。\n用法: /acm past <天数>")
            return
        days = int(cmd_parts[2])
        if not 1 <= days <= 3650:
            yield event.plain_result("⚠️ 天数必须在 1 到 3650 之间；查看全部本地记录请使用 /acm 总榜。")
            return
        title = f"数据库 · 近 {days} 天过题排行榜"
        logger.info(f"收到近 {days} 天 Codeforces 榜单查询，发起人: {event.get_sender_id()}")
        try:
            users_data = await self._query_rank_data(days=days, limit=50)
            if not users_data:
                yield event.plain_result(f"📊 {title}\n\n该条件下暂无过题记录。")
                return
            image_bytes = await self._generate_rank_image(title, users_data)
            if isinstance(image_bytes, str):
                yield event.plain_result(image_bytes)
            else:
                yield event.chain_result([Image.fromBytes(image_bytes)])
        except Exception as e:
            logger.error(f"生成近 {days} 天 Codeforces 榜单失败: {e}", exc_info=True)
            yield event.plain_result("❌ 榜单生成或发送失败，请稍后重试并联系管理员查看日志。")

    @acm_manager.command("总榜")
    async def cmd_total_rank(self, event: AstrMessageEvent):
        title = "生涯总过题排行榜"
        users_data = await self._query_rank_data(days=None, limit=None)
        if not users_data: yield event.plain_result("📊 生涯总榜暂无数据。"); return
        image_bytes = await self._generate_rank_image(title, users_data)
        if isinstance(image_bytes, str): yield event.plain_result(image_bytes)
        else: yield event.chain_result([Image.fromBytes(image_bytes)])
