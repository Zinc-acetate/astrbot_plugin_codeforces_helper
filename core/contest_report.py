"""Persistent, opt-in post-contest reports and official standings snapshots."""
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .contest_reminder import parse_group_whitelist
from .cf_api import request_cf_api

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_CONTEST_CHECKS = 8
LIVE_PARTICIPATION_TYPES = {"CONTESTANT", "OUT_OF_COMPETITION"}


@dataclass(frozen=True)
class ReportSettings:
    enabled: bool = False
    send_mode: str = "immediate"
    send_time: str = "09:00"
    groups: tuple[int, ...] = ()

    @property
    def schedule_signature(self):
        return f"{self.send_mode}:{self.send_time}"

    def due_at(self, now):
        if self.send_mode == "immediate":
            return int(now)
        hour, minute = map(int, self.send_time.split(":"))
        current = datetime.fromtimestamp(now, SHANGHAI_TZ)
        due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due < current:
            due += timedelta(days=1)
        return int(due.timestamp())


def parse_report_settings(config, fallback_group=None):
    value = config.get("enabled", False)
    enabled = value.strip().lower() in {"true", "1", "yes", "on"} if isinstance(value, str) else bool(value)
    if not enabled:
        return ReportSettings()
    mode = str(config.get("send_mode", "immediate"))
    if mode not in {"immediate", "scheduled"}:
        raise ValueError("赛后战报发送方式必须是 immediate 或 scheduled")
    clock = str(config.get("send_time", "09:00")).strip()
    if mode == "scheduled":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            raise ValueError("赛后战报发送时间必须为北京时间 HH:MM，例如 09:00")
    else:
        clock = "09:00"
    configured = config.get("group_whitelist", [])
    groups, invalid = parse_group_whitelist(configured or fallback_group)
    if invalid:
        raise ValueError("赛后战报群号无效：" + ", ".join(invalid))
    return ReportSettings(True, mode, clock, groups)


async def initialize_report_state(db):
    await db.execute("""CREATE TABLE IF NOT EXISTS cf_contest_report_state (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
        enabled_since INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
        schedule_signature TEXT NOT NULL DEFAULT '')""")
    await db.execute("INSERT OR IGNORE INTO cf_contest_report_state(id) VALUES(1)")
    await db.execute("""CREATE TABLE IF NOT EXISTS cf_contest_report_candidates (
        contest_id INTEGER PRIMARY KEY, contest_name TEXT NOT NULL, end_time INTEGER NOT NULL,
        generation INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
        last_checked_at INTEGER NOT NULL DEFAULT 0, next_check_at INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '')""")
    await db.execute("""CREATE TABLE IF NOT EXISTS cf_contest_reports (
        contest_id INTEGER PRIMARY KEY, contest_name TEXT NOT NULL, generation INTEGER NOT NULL,
        rating_updated_at INTEGER NOT NULL, captured_at INTEGER NOT NULL,
        due_at INTEGER NOT NULL, report_json TEXT NOT NULL,
        state TEXT NOT NULL, sent_at INTEGER NOT NULL DEFAULT 0)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS cf_contest_report_receipts (
        contest_id INTEGER NOT NULL, group_id TEXT NOT NULL, page INTEGER NOT NULL,
        sent_at INTEGER NOT NULL, PRIMARY KEY(contest_id,group_id,page))""")


async def report_window(db):
    async with db.execute("SELECT * FROM cf_contest_report_state WHERE id=1") as cursor:
        return dict(await cursor.fetchone())


async def configure_report_window(db, settings, now):
    """Call on configuration changes; reloading an enabled plugin preserves its epoch."""
    previous = await report_window(db)
    if bool(previous["enabled"]) != settings.enabled:
        await db.execute("UPDATE cf_contest_reports SET state='cancelled' WHERE state='pending'")
        await db.execute("DELETE FROM cf_contest_report_candidates")
        await db.execute("""UPDATE cf_contest_report_state SET enabled=?,
            enabled_since=CASE WHEN ? THEN ? ELSE enabled_since END,
            generation=generation+?, schedule_signature=? WHERE id=1""",
            (int(settings.enabled), int(settings.enabled), int(now), int(settings.enabled),
             settings.schedule_signature))
    elif settings.enabled and previous["schedule_signature"] != settings.schedule_signature:
        await db.execute("UPDATE cf_contest_reports SET due_at=? WHERE state='pending' AND generation=?",
                         (settings.due_at(now), previous["generation"]))
        await db.execute("UPDATE cf_contest_report_state SET schedule_signature=? WHERE id=1",
                         (settings.schedule_signature,))
    await db.commit()
    return await report_window(db)


async def window_is_current(db, generation):
    current = await report_window(db)
    return bool(current["enabled"]) and current["generation"] == generation


async def discover_contests(db, contests, generation):
    if not isinstance(contests, list):
        raise ValueError("CF 比赛列表不是有效数组")
    if not await window_is_current(db, generation):
        return
    values = []
    for contest in contests:
        if not isinstance(contest, dict) or contest.get("phase") != "FINISHED":
            continue
        try:
            cid = int(contest["id"])
            end = int(contest["startTimeSeconds"]) + int(contest["durationSeconds"])
        except (KeyError, TypeError, ValueError):
            continue
        values.append((cid, str(contest.get("name", cid)), end, generation))
    await db.executemany("""INSERT OR IGNORE INTO cf_contest_report_candidates
        (contest_id,contest_name,end_time,generation) VALUES(?,?,?,?)""", values)
    await db.commit()


async def pending_contests(db, generation, now, limit=MAX_CONTEST_CHECKS):
    # Prefer recent contests, but retain older candidates: old contests can publish late.
    async with db.execute("""SELECT c.* FROM cf_contest_report_candidates c
        WHERE c.generation=? AND c.state='waiting' AND c.next_check_at<=?
        AND NOT EXISTS(SELECT 1 FROM cf_contest_reports r WHERE r.contest_id=c.contest_id)
        ORDER BY (c.end_time>=?) DESC,c.last_checked_at,c.end_time DESC,c.contest_id DESC
        LIMIT ?""", (generation, int(now), int(now)-14*86400, limit)) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def record_contest_check(db, contest_id, generation, now, interval, state="waiting", error=""):
    await db.execute("""UPDATE cf_contest_report_candidates
        SET state=?,last_checked_at=?,next_check_at=?,last_error=?
        WHERE contest_id=? AND generation=?""",
        (state, int(now), int(now)+interval*60, str(error)[:500], contest_id, generation))
    await db.commit()


def rating_publication_time(changes, contest_id):
    if not isinstance(changes, list) or not changes:
        raise ValueError("该场尚无可用 Rating 变动记录")
    timestamps = []
    for row in changes:
        if (not isinstance(row, dict) or row.get("contestId") != contest_id
                or not isinstance(row.get("handle"), str) or not row["handle"].strip()
                or any(type(row.get(key)) is not int for key in
                       ("oldRating", "newRating", "ratingUpdateTimeSeconds"))
                or row["ratingUpdateTimeSeconds"] <= 0):
            raise ValueError("该场 Rating 变动记录不完整")
        timestamps.append(row["ratingUpdateTimeSeconds"])
    return max(timestamps)


def count_solved(contest_type, problems, results):
    if not isinstance(results, list) or len(results) != len(problems):
        raise ValueError("参赛成员的题目结果不完整")
    solved = 0
    for problem, result in zip(problems, results):
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, (int, float)) or not math.isfinite(points):
            raise ValueError("参赛成员的题目分数无效")
        if contest_type == "IOI":
            full = problem.get("points")
            if not isinstance(full, (int, float)) or full <= 0:
                return None
            solved += points >= full
        else:
            solved += points > 0
    return solved


async def fetch_member_evidence(session, member_handles, since, request=None, page_size=1000):
    """Public standings omit unrated participants; inspect each member's own records.

    One rating-history request and a complete submission window per unique handle
    can serve several contests in the same poll. Incomplete evidence is never an
    empty participation record.
    """
    request = request or request_cf_api
    if since <= 0 or page_size < 1:
        raise ValueError("成员提交查询需要有效时间边界和分页大小")
    evidence = {}

    class MissingHandle(ValueError):
        pass

    async def get_list(method, params):
        data = await request(session, method, params, timeout=30)
        if not isinstance(data, dict) or data.get("status") != "OK":
            comment = str(data.get("comment", "CF 响应无效")) if isinstance(data, dict) else "CF 响应无效"
            if "handle" in comment.lower() and "not found" in comment.lower():
                raise MissingHandle(comment)
            raise ValueError(comment)
        if not isinstance(data.get("result"), list):
            raise ValueError(f"{method} 没有返回完整数组")
        return data["result"]

    for handle in dict.fromkeys(str(h).strip().casefold() for h in member_handles if h):
        source = {"handle": handle, "since": since, "complete": False,
                  "history": [], "submissions": []}
        evidence[handle] = source
        try:
            history = await get_list("user.rating", {"handle": handle})
            for row in history:
                if (not isinstance(row, dict) or not isinstance(row.get("handle"), str)
                        or any(type(row.get(key)) is not int for key in
                               ("contestId", "rank", "oldRating", "newRating", "ratingUpdateTimeSeconds"))
                        or row["rank"] < 1):
                    raise ValueError("个人 Rating 历史不完整")
            source["history"] = history
            records, first = {}, 1
            while True:
                page = await get_list("user.status", {"handle": handle, "from": first, "count": page_size})
                reached_start, added = False, 0
                for item in page:
                    if (not isinstance(item, dict) or type(item.get("creationTimeSeconds")) is not int
                            or type(item.get("id")) is not int):
                        raise ValueError("成员提交分页不完整")
                    if item["creationTimeSeconds"] < since:
                        reached_start = True
                        continue
                    if not isinstance(item.get("author"), dict) or not isinstance(item.get("problem"), dict):
                        raise ValueError("成员提交缺少参赛类型或题目信息")
                    added += item["id"] not in records
                    records[item["id"]] = item
                if reached_start or len(page) < page_size:
                    break
                if not added:
                    raise ValueError("成员提交分页未前进")
                first += page_size
            source["submissions"] = list(records.values())
            source["complete"] = True
        except MissingHandle:
            source.update(complete=True, invalid_handle=True, history=[], submissions=[])
        except Exception as exc:
            source["error"] = f"{handle}: {exc}"
    return evidence


def build_report(standings, changes, member_handles, member_evidence=None):
    if not isinstance(standings, dict):
        raise ValueError("CF 比赛榜单无效")
    contest = standings.get("contest", {})
    cid = contest.get("id")
    if type(cid) is not int or contest.get("phase") != "FINISHED":
        raise ValueError("战报需要已结束的比赛榜单")
    published = rating_publication_time(changes, cid)
    problems = standings.get("problems")
    official_rows = standings.get("rows")
    if not isinstance(problems, list) or not isinstance(official_rows, list):
        raise ValueError("CF 比赛榜单缺少题目或参赛记录")
    if not isinstance(member_evidence, dict):
        raise ValueError("需要完整的个人 Rating 历史和参赛提交记录")
    start = contest.get("startTimeSeconds")
    duration = contest.get("durationSeconds")
    if type(start) is not int or type(duration) is not int or start <= 0 or duration <= 0:
        raise ValueError("比赛时间边界不完整")
    end = start + duration
    members = {str(handle).strip().casefold() for handle in member_handles if handle}
    published_handles = {item["handle"].casefold() for item in changes}
    visible_parties = {}
    for result in official_rows:
        party = result.get("party", {})
        if party.get("participantType") not in LIVE_PARTICIPATION_TYPES or party.get("ghost"):
            continue
        for member in party.get("members", []):
            visible_parties[str(member.get("handle", "")).casefold()] = result
    rows = []
    for key in members:
        source = member_evidence.get(key, {})
        if not source.get("complete") or source.get("since", start+1) > start:
            raise ValueError(source.get("error", f"{key} 的参赛记录尚未完整获取"))
        if source.get("invalid_handle"):
            if key in visible_parties or key in published_handles:
                raise ValueError(f"{key} 的参赛记录与账号查询结果不一致")
            continue
        history = source["history"]
        matches = [row for row in history if row["contestId"] == cid]
        rating = max(matches, key=lambda row: row["ratingUpdateTimeSeconds"]) if matches else None
        if key in published_handles and rating is None:
            raise ValueError(f"{key} 的个人出分历史尚未更新，稍后重试")
        live = [item for item in source["submissions"]
                if item.get("contestId") == cid and start <= item["creationTimeSeconds"] <= end
                and item.get("author", {}).get("participantType") in LIVE_PARTICIPATION_TYPES
                and not item["author"].get("ghost")]
        official = visible_parties.get(key)
        if not rating and not live and not official:
            continue
        # Personal contest history is the canonical Rank shown on each member's page.
        # Filtered public standings can renumber participants; never use that Rank.
        rank = rating["rank"] if rating else None
        if rating and (type(rank) is not int or rank < 1):
            raise ValueError("个人出分名次无效")
        accepted = set()
        for item in live:
            if item.get("verdict") == "OK":
                index = item["problem"].get("index")
                if not index:
                    raise ValueError("成员通过的题目缺少编号")
                accepted.add(index)
        solved = len(accepted) if contest.get("type") in {"CF", "ICPC"} else None
        if official is not None:
            official_solved = count_solved(contest.get("type"), problems, official.get("problemResults"))
            if live and solved is not None and official_solved != solved:
                raise ValueError(f"{key} 的榜单与提交过题数尚未一致，稍后重试")
            solved = official_solved
        handle = rating["handle"] if rating else source.get("handle", key)
        for item in live:
            for member in item["author"].get("members", []):
                if str(member.get("handle", "")).casefold() == key:
                    handle = member["handle"]
        latest = max(history, key=lambda row: row["ratingUpdateTimeSeconds"]) if history else None
        rows.append({
            "rank": rank, "handle": handle, "solved": solved,
            "participant_type": ("CONTESTANT" if rating else
                                 live[0]["author"]["participantType"] if live else official["party"]["participantType"]),
            "old_rating": rating["oldRating"] if rating else None,
            "rating_change": rating["newRating"]-rating["oldRating"] if rating else None,
            "new_rating": rating["newRating"] if rating else None,
            "display_rating": rating["newRating"] if rating else latest["newRating"] if latest else None,
        })
    return {
        "contest_id": cid, "contest_name": str(contest.get("name") or f"Codeforces Contest {cid}"),
        "contest_type": str(contest.get("type", "CF")),
        "start_time": int(contest.get("startTimeSeconds", 0)),
        "rating_updated_at": published,
        "rank_basis": "personal_contest_history",
        "rows": sorted(rows, key=lambda row: (
            row["rank"] is None,
            row["rank"] if row["rank"] is not None else -(row["solved"] if row["solved"] is not None else -1),
            row["handle"].casefold(),
        )),
    }


async def save_report(db, report, generation, settings, now):
    """Freeze one report per contest, including empty snapshots for auditability."""
    window = await report_window(db)
    if not window["enabled"] or window["generation"] != generation:
        return False
    if report["rating_updated_at"] < window["enabled_since"]:
        return False
    cursor = await db.execute("""INSERT OR IGNORE INTO cf_contest_reports
        (contest_id,contest_name,generation,rating_updated_at,captured_at,due_at,report_json,state)
        VALUES(?,?,?,?,?,?,?,?)""",
        (report["contest_id"], report["contest_name"], generation, report["rating_updated_at"],
         int(now), settings.due_at(now), json.dumps(report, ensure_ascii=False),
         "pending" if report["rows"] else "no_members"))
    await db.commit()
    return cursor.rowcount == 1


async def due_reports(db, generation, now):
    async with db.execute("""SELECT * FROM cf_contest_reports
        WHERE generation=? AND state='pending' AND due_at<=?
        ORDER BY rating_updated_at,contest_id""", (generation, int(now))) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def report_is_due(db, contest_id, generation, settings, now):
    async with db.execute("""SELECT 1 FROM cf_contest_reports r
        JOIN cf_contest_report_state s ON s.id=1
        WHERE r.contest_id=? AND r.generation=? AND r.state='pending' AND r.due_at<=?
        AND s.enabled=1 AND s.generation=r.generation AND s.schedule_signature=?""",
        (contest_id, generation, int(now), settings.schedule_signature)) as cursor:
        return bool(await cursor.fetchone())


async def delivered_pages(db, contest_id, group_id):
    async with db.execute("SELECT page FROM cf_contest_report_receipts WHERE contest_id=? AND group_id=?",
                          (contest_id, str(group_id))) as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def acknowledge_page(db, contest_id, group_id, page, now):
    await db.execute("INSERT OR IGNORE INTO cf_contest_report_receipts VALUES(?,?,?,?)",
                     (contest_id, str(group_id), page, int(now)))
    await db.commit()


async def finish_report(db, contest_id, generation, now):
    await db.execute("""UPDATE cf_contest_reports SET state='sent',sent_at=?
        WHERE contest_id=? AND generation=? AND state='pending'""", (int(now), contest_id, generation))
    await db.commit()
