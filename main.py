# Codeforces Helper by Zinc-acetate

import asyncio
import aiohttp
import json
import time
import os
import sqlite3
import secrets
import base64
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import aiosqlite
from multiprocessing import Process
import urllib.parse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
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
    from .core.contest_report_image import render_report_pages
except ImportError:
    logger.error("Pillow 库未安装！图片功能将不可用。")
    PILImage, ImageDraw, ImageFont = None, None, None
    render_report_pages = None

from .webui import run_server
from .core.crawler import Crawler
from .core.cf_api import request_cf_api
from .core.contest_reminder import (
    CONTEST_REMINDER_JOB_PREFIX,
    build_reminder_specs,
    classify_contest,
    contest_is_enabled,
    format_reminder_offset,
    parse_group_whitelist,
    parse_reminder_offsets,
)
from .core.rate_limit import configure_codeforces_api_rate_limiter
from .core.sync_lock import acquire_sync_lock, SyncAlreadyRunning
from .core.sync_state import (
    UserSyncResult, initialize_sync_state, plan_sync, finish_sync, delete_member_records,
    ensure_report_group, pending_report, acknowledge_report,
)
from .core import contest_report as contest_reports


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
CONTEST_CATEGORY_LABELS = {
    "div2": "Div. 2",
    "div3": "Div. 3",
    "div4": "Div. 4",
    "educational": "Educational",
    "other": "其他",
}
CONTEST_FILTER_SWITCHES = (
    ("include_div2", True),
    ("include_div3", True),
    ("include_div4", True),
    ("include_educational", True),
    ("include_other", False),
)

@register(
    "astrbot_plugin_codeforces_helper",
    "Zinc-acetate",
    "Codeforces 训练、Rating 缓存、比赛提醒、赛后战报与管理助手",
    "1.3.4",
)
class CodeforcesHelperPlugin(Star):
    db: aiosqlite.Connection
    db_path: Path
    webui_process: Process | None = None
    scheduler: AsyncIOScheduler

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.FONT_PATH = Path(__file__).parent / "resources" / "SourceHanSansSC-Bold.otf"
        self._contest_report_poll_lock = asyncio.Lock()
        self._contest_report_send_lock = asyncio.Lock()

    async def initialize(self):
        logger.info("Codeforces Helper v1.3.4 开始初始化...")
        await self.connect_db()
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        settings = await self._get_all_settings()
        await self.reschedule_jobs(settings)
        self._contest_reminder_config_signature = self._get_contest_reminder_config_signature()
        self.scheduler.add_job(self._watch_runtime_settings, 'interval', minutes=1, id='settings_watch_job', replace_existing=True)
        self.scheduler.add_job(
            self.refresh_contest_reminder_jobs,
            IntervalTrigger(minutes=10, timezone=SHANGHAI_TZ),
            id='contest_reminder_refresh_job',
            name='Codeforces contest reminder refresh',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(SHANGHAI_TZ),
        )
        self.scheduler.start()
        await self._auto_start_webui_if_enabled()
        logger.info("✅ Codeforces Helper 初始化成功！")

    async def terminate(self):
        logger.info("正在关闭 Codeforces Helper...");
        if hasattr(self, 'scheduler') and self.scheduler.running: self.scheduler.shutdown()
        await self.stop_webui_process(persist=False)
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
        configure_codeforces_api_rate_limiter(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path); self.db.row_factory = aiosqlite.Row
        await self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_enabled', 'true');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_cron_hour', '*');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('report_cron_minute', '0');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('hourly_report_limit', '10');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sync_interval_minutes', '60');")
        await self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password_hash', ?);", (generate_password_hash('123456'),))
        await self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('admin_session_version',?)", (secrets.token_hex(16),))
        await self.db.execute("CREATE TABLE IF NOT EXISTS users (qq_id TEXT PRIMARY KEY, name TEXT NOT NULL, cf_handle TEXT, status TEXT, school TEXT, last_sync_timestamp INTEGER DEFAULT 0);")
        await self.db.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_qq_id TEXT NOT NULL, platform TEXT NOT NULL, problem_id TEXT NOT NULL, problem_name TEXT, problem_rating TEXT, problem_url TEXT, submit_time INTEGER NOT NULL, UNIQUE(user_qq_id, platform, problem_id));")
        async with self.db.execute("PRAGMA table_info(users)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        migrations = {
            'cf_rating': 'INTEGER', 'cf_rank': 'TEXT', 'cf_max_rating': 'INTEGER',
            'cf_max_rank': 'TEXT', 'cf_rating_updated_at': 'INTEGER DEFAULT 0',
            'history_sync_days': 'INTEGER DEFAULT 0',
            'cf_reconciled_at': 'INTEGER DEFAULT 0',
            'cf_coverage_start': 'INTEGER DEFAULT 0',
        }
        for column, sql_type in migrations.items():
            if column not in columns:
                await self.db.execute(f"ALTER TABLE users ADD COLUMN {column} {sql_type}")
        await self.db.execute("DELETE FROM submissions WHERE platform != 'codeforces'")
        await initialize_sync_state(self.db)
        await contest_reports.initialize_report_state(self.db)
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
                await ensure_report_group(self.db, group_id, int(time.time()))
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
            self._run_periodic_sync,
            IntervalTrigger(minutes=interval_minutes, timezone="Asia/Shanghai"),
            id='sync_data_job', name='Codeforces data and rating sync', replace_existing=True,
            max_instances=1, coalesce=True,
        )
        self._scheduled_sync_interval = interval_minutes
        logger.info(f"✅ 数据与 CF 分数自动更新间隔：{interval_minutes} 分钟。")
        await self._configure_contest_report_jobs(settings)

    async def _watch_runtime_settings(self):
        """允许 WebUI 修改数据库设置后，无需重启即可重排同步任务。"""
        try:
            value = int(await self.get_setting('sync_interval_minutes', '60'))
            value = max(5, min(value, 1440))
            if value != getattr(self, '_scheduled_sync_interval', None):
                await self.reschedule_jobs(await self._get_all_settings())
            contest_signature = self._get_contest_reminder_config_signature()
            if contest_signature != getattr(self, '_contest_reminder_config_signature', None):
                self._contest_reminder_config_signature = contest_signature
                await self.refresh_contest_reminder_jobs()
            report_signature = self._get_contest_report_signature(await self.get_setting('notification_group_id'))
            if report_signature != getattr(self, '_contest_report_config_signature', None):
                await self._configure_contest_report_jobs(await self._get_all_settings())
            await self.send_pending_contest_reports()
        except Exception as e:
            logger.error(f"检查运行时设置失败: {e}")

    def _get_contest_reminder_config(self) -> dict:
        value = self.config.get("contest_reminder", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _get_contest_report_config(self):
        value = self.config.get("contest_report", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _get_contest_report_signature(self, fallback_group):
        return json.dumps([self._get_contest_report_config(), fallback_group],
                          sort_keys=True, ensure_ascii=False, default=str)

    async def _get_contest_report_settings(self):
        return contest_reports.parse_report_settings(
            self._get_contest_report_config(), await self.get_setting("notification_group_id"))

    async def _configure_contest_report_jobs(self, settings):
        fallback = settings.get("notification_group_id")
        try:
            options = contest_reports.parse_report_settings(self._get_contest_report_config(), fallback)
        except ValueError as exc:
            logger.error(f"赛后战报配置无效，暂不启用：{exc}")
            options = contest_reports.ReportSettings()
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await contest_reports.configure_report_window(db, options, int(time.time()))
        self._contest_report_config_signature = self._get_contest_report_signature(fallback)
        try:
            self.scheduler.remove_job("contest_report_send_job")
        except JobLookupError:
            pass
        if options.enabled and options.send_mode == "scheduled":
            hour, minute = map(int, options.send_time.split(":"))
            self.scheduler.add_job(
                self.send_pending_contest_reports,
                CronTrigger(hour=hour, minute=minute, timezone=SHANGHAI_TZ),
                id="contest_report_send_job", name="Codeforces post-contest reports",
                replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=900,
            )

    async def _run_periodic_sync(self):
        try:
            await self.sync_all_users_data()
        except Exception:
            logger.exception("周期数据同步失败，继续检查赛后战报。")
        await self.check_contest_reports()

    async def check_contest_reports(self):
        if not self._config_switch(self._get_contest_report_config(), "enabled"):
            return
        if self._contest_report_poll_lock.locked():
            return
        async with self._contest_report_poll_lock:
            try:
                options = await self._get_contest_report_settings()
                interval = max(5, min(int(await self.get_setting("sync_interval_minutes", "60")), 1440))
                with acquire_sync_lock(self.db_path):
                    async with aiosqlite.connect(self.db_path, timeout=30) as db, aiohttp.ClientSession() as http:
                        db.row_factory = aiosqlite.Row
                        window = await contest_reports.report_window(db)
                        if not window["enabled"]:
                            return
                        generation = window["generation"]
                        async with db.execute("SELECT * FROM users WHERE cf_handle IS NOT NULL AND TRIM(cf_handle)!=''") as cursor:
                            users = await cursor.fetchall()
                        if not users:
                            return
                        try:
                            data = await request_cf_api(http, "contest.list", {"gym": "false"}, timeout=30)
                            if data.get("status") != "OK":
                                raise ValueError(data.get("comment", "比赛列表查询失败"))
                            await contest_reports.discover_contests(db, data.get("result"), generation)
                        except Exception as exc:
                            logger.warning(f"赛后战报比赛列表刷新失败，继续检查已保存候选：{exc}")
                        now = int(time.time())
                        candidates = await contest_reports.pending_contests(db, generation, now)
                        affected = set()
                        prepared = []
                        for candidate in candidates:
                            if (not self._config_switch(self._get_contest_report_config(), "enabled")
                                    or not await contest_reports.window_is_current(db, generation)):
                                break
                            cid = candidate["contest_id"]
                            try:
                                data = await request_cf_api(http, "contest.ratingChanges", {"contestId": cid}, timeout=30)
                                if data.get("status") != "OK":
                                    raise ValueError(data.get("comment", "Rating 记录尚不可用"))
                                changes = data.get("result")
                                published = contest_reports.rating_publication_time(changes, cid)
                                if published < window["enabled_since"]:
                                    await contest_reports.record_contest_check(db, cid, generation, now, interval, "before_enable")
                                    continue
                                if (not self._config_switch(self._get_contest_report_config(), "enabled")
                                        or not await contest_reports.window_is_current(db, generation)):
                                    break
                                # The public standings endpoint accepts only contestId, without API credentials.
                                data = await request_cf_api(http, "contest.standings", {"contestId": cid}, timeout=60)
                                if data.get("status") != "OK":
                                    raise ValueError(data.get("comment", "正式榜单查询失败"))
                                standings = data.get("result")
                                if not isinstance(standings, dict) or standings.get("contest", {}).get("id") != cid:
                                    raise ValueError("正式榜单比赛 ID 不匹配")
                                start = standings["contest"].get("startTimeSeconds")
                                if type(start) is not int or start <= 0:
                                    raise ValueError("比赛缺少有效开始时间")
                                prepared.append((cid, standings, changes))
                            except Exception as exc:
                                await contest_reports.record_contest_check(db, cid, generation, now, interval, error=exc)
                                logger.debug(f"赛后战报 {cid} 待重试：{exc}")
                        handles = [u["cf_handle"] for u in users]
                        if prepared and self._config_switch(self._get_contest_report_config(), "enabled"):
                            since = min(item[1]["contest"]["startTimeSeconds"] for item in prepared)
                            evidence = await contest_reports.fetch_member_evidence(
                                http, handles, since, request=request_cf_api)
                        else:
                            evidence = {}
                        for cid, standings, changes in prepared:
                            if (not self._config_switch(self._get_contest_report_config(), "enabled")
                                    or not await contest_reports.window_is_current(db, generation)):
                                break
                            try:
                                report = contest_reports.build_report(standings, changes, handles, evidence)
                                options = await self._get_contest_report_settings()
                                if not options.enabled:
                                    break
                                stored = await contest_reports.save_report(db, report, generation, options, int(time.time()))
                                await contest_reports.record_contest_check(db, cid, generation, now, interval, "captured")
                                if stored and report["rows"]:
                                    affected.update(row["handle"].casefold() for row in report["rows"])
                                    logger.info(f"已保存赛后战报：{report['contest_name']}，{len(report['rows'])} 位成员（含未计分参赛）。")
                            except Exception as exc:
                                await contest_reports.record_contest_check(db, cid, generation, now, interval, error=exc)
                                logger.debug(f"赛后战报 {cid} 待重试：{exc}")
                        if affected:
                            # Refresh current profiles instead of overwriting them with an older contest's rating.
                            targets = [u for u in users if u["cf_handle"].casefold() in affected]
                            await Crawler.fetch_cf_profiles(http, targets, db, self.config)
            except SyncAlreadyRunning:
                logger.info("赛后战报等待当前数据更新完成，下轮继续检查。")
            except Exception:
                logger.exception("赛后战报检查失败，保留待处理记录。")
        await self.send_pending_contest_reports()

    async def send_pending_contest_reports(self):
        if not self._config_switch(self._get_contest_report_config(), "enabled"):
            return
        if self._contest_report_send_lock.locked():
            return
        async with self._contest_report_send_lock:
            try:
                options = await self._get_contest_report_settings()
                if not options.groups:
                    return
                if render_report_pages is None:
                    raise RuntimeError("Pillow 不可用，赛后战报图片暂未生成")
                with acquire_sync_lock(self.db_path):
                    async with aiosqlite.connect(self.db_path, timeout=30) as db:
                        db.row_factory = aiosqlite.Row
                        window = await contest_reports.report_window(db)
                        if not window["enabled"]:
                            return
                        generation = window["generation"]
                        records = await contest_reports.due_reports(db, generation, int(time.time()))
                        if not records:
                            return
                        platform = self.context.get_platform("aiocqhttp")
                        if not platform:
                            logger.warning("赛后战报待发送：QQ 平台暂不可用。")
                            return
                        for record in records:
                            options = await self._get_contest_report_settings()
                            if not options.enabled or not await contest_reports.window_is_current(db, generation):
                                return
                            if not await contest_reports.report_is_due(db, record["contest_id"], generation, options, int(time.time())):
                                continue
                            pages = await asyncio.to_thread(render_report_pages, json.loads(record["report_json"]), self.FONT_PATH)
                            cid = record["contest_id"]
                            for group in options.groups:
                                receipts = await contest_reports.delivered_pages(db, cid, group)
                                for page, content in enumerate(pages):
                                    if page in receipts:
                                        continue
                                    latest = await self._get_contest_report_settings()
                                    if not latest.enabled or not await contest_reports.window_is_current(db, generation):
                                        return
                                    if not await contest_reports.report_is_due(db, cid, generation, latest, int(time.time())):
                                        return
                                    if group not in latest.groups:
                                        break
                                    try:
                                        result = await asyncio.wait_for(platform.bot.send_group_msg(
                                            group_id=group,
                                            message=[{"type": "image", "data": {
                                                "file": "base64://" + base64.b64encode(content).decode("ascii")}}]), timeout=30)
                                        if isinstance(result, dict) and (result.get("status") == "failed" or result.get("retcode", 0) != 0):
                                            raise RuntimeError("OneBot 未确认战报发送成功")
                                        await contest_reports.acknowledge_page(db, cid, group, page, int(time.time()))
                                    except Exception as exc:
                                        logger.warning(f"战报 {cid} 向群 {group} 发送失败，将重试：{exc}")
                                        break
                            latest = await self._get_contest_report_settings()
                            complete = bool(latest.groups)
                            for group in latest.groups:
                                complete &= len(await contest_reports.delivered_pages(db, cid, group)) >= len(pages)
                            if complete:
                                await contest_reports.finish_report(db, cid, generation, int(time.time()))
            except SyncAlreadyRunning:
                pass  # The existing one-minute settings watcher retries due deliveries.
            except Exception:
                logger.exception("赛后战报发送失败，保留待发送记录。")

    def _get_contest_reminder_config_signature(self) -> str:
        return json.dumps(
            self._get_contest_reminder_config(),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _config_switch(config: Mapping, key: str, default: bool = False) -> bool:
        value = config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _remove_stale_contest_reminder_jobs(self, desired_job_ids=frozenset()):
        for job in self.scheduler.get_jobs():
            if job.id.startswith(CONTEST_REMINDER_JOB_PREFIX) and job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)

    async def refresh_contest_reminder_jobs(self):
        config = self._get_contest_reminder_config()
        if not self._config_switch(config, "enabled"):
            self._remove_stale_contest_reminder_jobs()
            return

        try:
            offsets = parse_reminder_offsets(str(config.get("reminder_times", "24 1")))
        except ValueError as exc:
            logger.error(f"CF 比赛提醒时间配置无效: {exc}")
            self._remove_stale_contest_reminder_jobs()
            return
        groups, invalid_groups = parse_group_whitelist(config.get("group_whitelist", []))
        if invalid_groups:
            logger.warning(f"忽略无效的 CF 比赛提醒群号: {', '.join(invalid_groups)}")
        has_enabled_filter = any(
            self._config_switch(config, key, default)
            for key, default in CONTEST_FILTER_SWITCHES
        )
        if not groups or not offsets or not has_enabled_filter:
            self._remove_stale_contest_reminder_jobs()
            return

        try:
            async with aiohttp.ClientSession() as session:
                data = await request_cf_api(
                    session,
                    "contest.list",
                    {"gym": "false"},
                    timeout=20,
                )
            if data.get("status") != "OK":
                logger.warning(f"刷新 CF 比赛提醒失败: {data.get('comment', '未知错误')}")
                return
        except Exception as exc:
            logger.error(f"刷新 CF 比赛提醒失败: {exc}", exc_info=True)
            return

        specs = build_reminder_specs(
            data.get("result", []),
            offsets=offsets,
            filter_config=config,
            now_timestamp=int(time.time()),
        )
        desired_job_ids = {spec.job_id for spec in specs}
        existing_job_ids = {
            job.id
            for job in self.scheduler.get_jobs()
            if job.id.startswith(CONTEST_REMINDER_JOB_PREFIX)
        }
        for spec in specs:
            self.scheduler.add_job(
                self.send_contest_reminder,
                DateTrigger(
                    run_date=datetime.fromtimestamp(spec.run_at_timestamp, tz=SHANGHAI_TZ)
                ),
                args=(
                    spec.contest_id,
                    spec.contest_name,
                    spec.start_timestamp,
                    spec.offset_seconds,
                ),
                id=spec.job_id,
                name=f"CF contest reminder: {spec.contest_name}",
                replace_existing=True,
                misfire_grace_time=30,
            )
        self._remove_stale_contest_reminder_jobs(desired_job_ids)
        if existing_job_ids != desired_job_ids:
            logger.info(
                f"CF 比赛提醒已刷新：{len(specs)} 个任务，{len(groups)} 个白名单群。"
            )

    async def send_contest_reminder(
        self,
        contest_id: int,
        contest_name: str,
        start_timestamp: int,
        offset_seconds: int,
    ):
        config = self._get_contest_reminder_config()
        if not self._config_switch(config, "enabled"):
            return
        try:
            offsets = parse_reminder_offsets(str(config.get("reminder_times", "24 1")))
        except ValueError as exc:
            logger.error(f"CF 比赛提醒时间配置无效: {exc}")
            return
        if int(offset_seconds) not in offsets or not contest_is_enabled(contest_name, config):
            return

        groups, invalid_groups = parse_group_whitelist(config.get("group_whitelist", []))
        if invalid_groups:
            logger.warning(f"忽略无效的 CF 比赛提醒群号: {', '.join(invalid_groups)}")
        if not groups:
            return
        if int(start_timestamp) < int(time.time()) - 30:
            logger.info(f"跳过已开始的 CF 比赛提醒: {contest_name}")
            return

        category = classify_contest(contest_name)
        category_label = CONTEST_CATEGORY_LABELS.get(category, "其他")
        offset_label = format_reminder_offset(offset_seconds)
        start_text = datetime.fromtimestamp(
            int(start_timestamp), tz=SHANGHAI_TZ
        ).strftime("%Y-%m-%d %H:%M:%S")
        message = (
            "⏰ Codeforces 比赛提醒\n"
            f"类型：{category_label}\n"
            f"比赛：{contest_name}\n"
            f"距离开始：{offset_label}\n"
            f"开始时间：{start_text}\n"
            f"报名链接：https://codeforces.com/contestRegistration/{int(contest_id)}"
        )

        qq_platform = self.context.get_platform("aiocqhttp")
        if not qq_platform:
            logger.error("CF 比赛提醒发送失败：无法获取 QQ 平台实例。")
            return
        onebot_message = [{"type": "text", "data": {"text": message}}]
        for group_id in groups:
            try:
                await qq_platform.bot.send_group_msg(
                    group_id=group_id,
                    message=onebot_message,
                )
                logger.info(f"已向白名单群 {group_id} 发送 CF 比赛提醒: {contest_name}")
            except Exception as exc:
                logger.error(
                    f"向白名单群 {group_id} 发送 CF 比赛提醒失败: {exc}",
                    exc_info=True,
                )

    def _set_webui_auto_start(self, enabled: bool) -> bool:
        self.config["webui_auto_start"] = bool(enabled)
        save_config = getattr(self.config, "save_config", None)
        if not callable(save_config):
            logger.warning("当前配置对象不支持持久化 WebUI 自动启动状态。")
            return False
        try:
            save_config()
            return True
        except Exception as exc:
            logger.error(f"保存 WebUI 自动启动状态失败: {exc}", exc_info=True)
            return False

    async def _auto_start_webui_if_enabled(self):
        if not self._config_switch(self.config, "webui_auto_start"):
            return
        message = await self.start_webui_process(persist=False)
        logger.info(f"WebUI 自动启动结果: {message}")

    async def start_webui_process(self, persist: bool = False):
        if self.webui_process and self.webui_process.is_alive():
            if persist:
                self._set_webui_auto_start(True)
            return f"管理后台已在运行！"
        port = self.config.get('webui_port', 8088); logger.info(f"正在端口 {port} 上启动 WebUI 子进程...")
        webui_config = {"cf_api_key": self.config.get("cf_api_key"), "cf_api_secret": self.config.get("cf_api_secret")}
        self.webui_process = Process(target=run_server, args=(str(self.db_path), port, webui_config)); self.webui_process.start(); await asyncio.sleep(2)
        if self.webui_process.is_alive():
            if persist:
                self._set_webui_auto_start(True)
            logger.info(f"WebUI 子进程已启动, PID: {self.webui_process.pid}"); return f"✨ 管理后台已启动！\n请访问: http://<你的服务器IP>:{port}"
        else: logger.error("WebUI 子进程启动失败！"); return "❌ 后台启动失败"

    async def stop_webui_process(self, persist: bool = False):
        if persist:
            self._set_webui_auto_start(False)
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
            time_str = datetime.fromtimestamp(solve['submit_time'], SHANGHAI_TZ).strftime('%H:%M')
            parts.append(f"\n👤 {solve['user_name']} 在 {time_str} 通过了\n💻 {solve['platform']} - {solve['problem_name']}\n📈 难度: {solve['problem_rating'] or 'N/A'}\n🔗 {solve['problem_url']}")
        return "\n".join(parts)

    async def sync_single_user(self, qq_id: str, refresh_cf_profile: bool = True,
                               days: int = None) -> UserSyncResult:
        # A dedicated connection keeps the snapshot transaction separate from chat settings.
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE qq_id = ?", (qq_id,)) as cursor:
                user = await cursor.fetchone()
            if not user or not (user["cf_handle"] or "").strip():
                return UserSyncResult(0, False, None)
            now = int(time.time())
            plan = await plan_sync(db, user, now, days)
            logger.info(f"为用户 {user['name']} 同步 CF 提交（起点 {plan.start}）...")
            async with aiohttp.ClientSession() as session:
                added, complete = await Crawler.fetch_cf_submissions(session, user, plan.start, db, self.config)
                if complete:
                    await finish_sync(db, user, now, plan)
                else:
                    logger.warning(f"用户 {user['name']} 同步未完整完成，不推进同步时间。")
                profile_complete = None
                if refresh_cf_profile:
                    profile_complete = await Crawler.fetch_cf_profile(session, user, db, self.config)
            return UserSyncResult(added, complete, profile_complete)

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
                async with aiohttp.ClientSession() as session, aiosqlite.connect(self.db_path, timeout=30) as db:
                    refreshed = await Crawler.fetch_cf_profiles(session, cf_users, db, self.config)
                logger.info(f"[智能同步] 批量刷新了 {refreshed} 位用户的 CF 分数资料。")
        except SyncAlreadyRunning:
            logger.info("[智能同步] 已有手动或自动更新任务，跳过本轮。")
            return
        logger.info("[智能同步] 本次周期任务完成。")

    async def report_hourly_solves(self):
        group_id = await self.get_setting("notification_group_id")
        if not group_id or await self.get_setting("report_enabled") != "true":
            return
        try:
            # Keep membership changes and concurrent reporters out until acknowledgement.
            with acquire_sync_lock(self.db_path):
                async with aiosqlite.connect(self.db_path, timeout=30) as db:
                    db.row_factory = aiosqlite.Row
                    limit = max(1, min(int(await self.get_setting('hourly_report_limit', 10)), 50))
                    records = await pending_report(db, group_id, int(time.time()), limit)
                    if not records:
                        return
                    platform = self.context.get_platform("aiocqhttp")
                    if not platform:
                        logger.warning("过题播报暂未发送：QQ 平台不可用，将在下次重试。")
                        return
                    parts = [f"📖 新增过题速报（{len(records)} 条，含延迟同步记录）"]
                    for row in records:
                        stamp = datetime.fromtimestamp(row['submit_time'], SHANGHAI_TZ).strftime('%m-%d %H:%M')
                        parts.append(f"\n👤 {row['user_name']} [{stamp}]\n{row['problem_name']} (Rating: {row['problem_rating']})\n{row['problem_url']}")
                    await asyncio.wait_for(platform.bot.send_group_msg(
                        group_id=int(group_id), message=[{"type":"text", "data":{"text":"\n".join(parts)}}]), timeout=30)
                    await acknowledge_report(db, group_id, records, int(time.time()))
                    logger.info(f"已向群 {group_id} 播报 {len(records)} 条新过题记录。")
        except SyncAlreadyRunning:
            logger.info("过题播报等待数据更新完成，未发送记录保留到下次。")
        except Exception as e:
            logger.error(f"过题播报失败，保留待发记录: {e}", exc_info=True)

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
    def _cf_rating_hex(rating: int) -> str:
        """返回 Codeforces Rating 对应的官方风格颜色。"""
        if not isinstance(rating, int): return '#808080'
        if rating < 1200: return '#808080'
        if rating < 1400: return '#008000'
        if rating < 1600: return '#03A89E'
        if rating < 1900: return '#0000FF'
        if rating < 2100: return '#AA00AA'
        if rating < 2400: return '#FF8C00'
        return '#FF0000'

    async def _generate_rating_rank_image(self, title: str, users: list) -> bytes | str:
        if not all([PILImage, ImageDraw, ImageFont]):
            return "❌ 无法生成图片：Pillow 库未正确安装。"
        if not self.FONT_PATH.exists():
            return f"❌ 无法生成图片：字体文件丢失 ({self.FONT_PATH})。"

        width = 760
        title_height, header_height, row_height, footer_height = 82, 50, 52, 24
        height = title_height + header_height + len(users) * row_height + footer_height
        image = PILImage.new('RGB', (width, height), '#FFFFFF')
        draw = ImageDraw.Draw(image)
        try:
            font_title = ImageFont.truetype(str(self.FONT_PATH), 25)
            font_header = ImageFont.truetype(str(self.FONT_PATH), 17)
            font_body = ImageFont.truetype(str(self.FONT_PATH), 18)
        except IOError:
            return f"❌ 无法加载字体文件 {self.FONT_PATH}。"

        draw.rectangle((0, 0, width, title_height), fill='#343A40')
        draw.text((width / 2, title_height / 2), title, font=font_title,
                  fill='#FFFFFF', anchor='mm')
        draw.rectangle((0, title_height, width, title_height + header_height), fill='#246BCE')
        columns = {'排名': 72, 'CF ID': 330, 'Rating': 650}
        for label, x in columns.items():
            draw.text((x, title_height + header_height / 2), label, font=font_header,
                      fill='#FFFFFF', anchor='mm')

        for index, user in enumerate(users, 1):
            top = title_height + header_height + (index - 1) * row_height
            draw.rectangle((0, top, width, top + row_height),
                           fill='#F5F7FA' if index % 2 == 0 else '#FFFFFF')
            center_y = top + row_height / 2
            rating = int(user['rating'])
            color = self._cf_rating_hex(rating)
            handle = str(user['cf_handle'])
            max_width = 390
            if font_body.getlength(handle) > max_width:
                while handle and font_body.getlength(handle + '..') > max_width:
                    handle = handle[:-1]
                handle += '..'
            draw.text((columns['排名'], center_y), str(index), font=font_body,
                      fill='#343A40', anchor='mm')
            draw.text((columns['CF ID'], center_y), handle, font=font_body,
                      fill=color, anchor='mm')
            draw.text((columns['Rating'], center_y), str(rating), font=font_body,
                      fill=color, anchor='mm')
            draw.line((0, top + row_height, width, top + row_height), fill='#E4E8ED', width=1)

        from io import BytesIO
        buffer = BytesIO()
        image.save(buffer, format='PNG', optimize=True)
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
            contest['name'], datetime.fromtimestamp(int(contest['startTimeSeconds']), SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            contest['durationSeconds'] // 3600, contest['durationSeconds'] % 3600 // 60,
            f"https://codeforces.com/contestRegistration/{str(contest['id'])}"
        )

    @filter.command_group("acm")
    def acm_manager(self): pass

    @acm_manager.command("后台启动")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_start_webui(self, event: AstrMessageEvent): msg = await self.start_webui_process(persist=True); yield event.plain_result(msg)

    @acm_manager.command("后台关闭")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_stop_webui(self, event: AstrMessageEvent): msg = await self.stop_webui_process(persist=True); yield event.plain_result(msg)

    @acm_manager.command("rank")
    async def cmd_show_rank(self, event: AstrMessageEvent):
        """单一入口处理周榜和总榜，避免 AstrBot 前缀匹配触发两个处理器。"""
        args = event.message_str.strip().split()[2:]
        if not args:
            days, title = 7, "近 7 日刷题排行榜"
        elif len(args) == 1 and args[0].lower() == "all":
            days, title = None, "生涯总刷题量排行榜"
        else:
            yield event.plain_result("参数错误。\n周榜：/acm rank\n总榜：/acm rank all")
            return
        top_ten = await self._query_rank_data(days=days, limit=10)
        if not top_ten:
            yield event.plain_result(f"{title}暂无数据。")
            return
        parts = [f"🏆 {title} Top 10 🏆"]
        emojis = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
        for i, user in enumerate(top_ten):
            parts.append(f"{emojis[i]} {user['user_name']}: {user['total_count']} 题")
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
        try:
            async with aiohttp.ClientSession() as session:
                data = await request_cf_api(session, "contest.list", {"gym": "false"}, timeout=10)
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
        try:
            options = await self._get_contest_report_settings()
            delivery = "立即发送" if options.send_mode == "immediate" else f"北京时间每天 {options.send_time}"
            contest_report_status = f"开启，{delivery}" if options.enabled else "关闭"
        except ValueError as exc:
            contest_report_status = f"配置无效：{exc}"
        status_text = (f"📊 Codeforces 训练助手当前状态:\n--------------------------\n"
                       f"  - 定时播报: {'✅ 开启' if is_enabled else '❌ 关闭'}\n"
                       f"  - AC 记录与 CF Rating 更新间隔: {sync_interval} 分钟\n"
                       f"  - 近期过题播报上限: {hourly_limit} 题\n"
                       f"  - 目标群聊: {group_id}\n"
                       f"  - CRON 表达式: 小时={cron_hour}, 分钟={cron_minute}\n"
                       f"  - 赛后战报: {contest_report_status}")
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
        updated = (datetime.fromtimestamp(user['cf_rating_updated_at'], SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M:%S')
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

        try:
            image_bytes = await self._generate_rating_rank_image(title, users)
            if isinstance(image_bytes, str):
                yield event.plain_result(image_bytes)
            else:
                yield event.chain_result([Image.fromBytes(image_bytes)])
        except Exception as e:
            logger.error(f"生成 Codeforces Rating 排行榜失败: {e}", exc_info=True)
            yield event.plain_result("❌ Rating 排行榜图片生成失败，请稍后重试。")

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
                result = await self.sync_single_user_for_days(qq_id, days)
        except SyncAlreadyRunning as e:
            yield event.plain_result(str(e))
            return
        submission_status = "完成" if result.submissions_complete else "失败，请查看日志后重试"
        profile_status = ("未执行" if result.profile_complete is None else
                          "完成" if result.profile_complete else "失败，请查看日志后重试")
        yield event.plain_result(
            f"用户 {qq_id} 的同步结果：\n"
            f"提交同步：{submission_status}（新增 {result.added} 题）\n"
            f"Rating 更新：{profile_status}"
        )

    @acm_manager.command("del_user")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_delete_user(self, event: AstrMessageEvent):
        cmd_parts = event.message_str.strip().split()
        if len(cmd_parts) < 3: yield event.plain_result("⚠️ 参数错误，请输入QQ号。\n格式: /acm del_user 12345"); return
        qq_id = cmd_parts[2].strip()
        try:
            with acquire_sync_lock(self.db_path):
                async with self.db.execute("SELECT name FROM users WHERE qq_id = ?", (qq_id,)) as cursor: user = await cursor.fetchone()
                if user:
                    await delete_member_records(self.db, qq_id)
                    await self.db.execute("DELETE FROM users WHERE qq_id = ?", (qq_id,))
                    await self.db.commit()
        except SyncAlreadyRunning as e:
            yield event.plain_result(f"❌ {e}，暂时无法删除成员。")
            return
        if not user:
            yield event.plain_result(f"❌ 删除失败：找不到 QQ号为 {qq_id} 的用户。")
            return
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
            time_str = datetime.fromtimestamp(sub['submit_time'], SHANGHAI_TZ).strftime('%Y-%m-%d %H:%M')
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
                async with self.db.execute("SELECT qq_id FROM users WHERE cf_handle IS NOT NULL AND TRIM(cf_handle) != ''") as cursor:
                    all_users = await cursor.fetchall()
                failed = profile_failed = 0
                for user_row in all_users:
                    result = await self.sync_single_user_for_days(user_row['qq_id'], days)
                    failed += int(not result.submissions_complete)
                    profile_failed += int(result.profile_complete is False)
        except SyncAlreadyRunning as e:
            yield event.plain_result(str(e))
            return
        yield event.plain_result(
            f"同步结束：提交同步失败 {failed} 人，Rating 更新失败 {profile_failed} 人。正在生成榜单..."
        )
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
