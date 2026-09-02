import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

MAX_REMINDER_SECONDS = 365 * 24 * 60 * 60
CONTEST_REMINDER_JOB_PREFIX = "cf_contest_reminder_"

_TIME_PATTERN = re.compile(r"^(\d+)(min|s|h|d)?$", re.IGNORECASE)
_GROUP_SEPARATOR = re.compile(r"[\s,，;；]+")
_DIVISION_PATTERNS = {
    "div2": re.compile(r"\bdiv(?:ision)?\.?\s*2\b", re.IGNORECASE),
    "div3": re.compile(r"\bdiv(?:ision)?\.?\s*3\b", re.IGNORECASE),
    "div4": re.compile(r"\bdiv(?:ision)?\.?\s*4\b", re.IGNORECASE),
}
_CATEGORY_SWITCHES = {
    "div2": ("include_div2", True),
    "div3": ("include_div3", True),
    "div4": ("include_div4", True),
    "educational": ("include_educational", True),
    "other": ("include_other", False),
}


@dataclass(frozen=True, slots=True)
class ContestReminderSpec:
    contest_id: int
    contest_name: str
    start_timestamp: int
    offset_seconds: int
    category: str

    @property
    def run_at_timestamp(self) -> int:
        return self.start_timestamp - self.offset_seconds

    @property
    def job_id(self) -> str:
        return f"{CONTEST_REMINDER_JOB_PREFIX}{self.contest_id}_{self.offset_seconds}"


def parse_reminder_offsets(value: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise TypeError("提醒时间必须是以空格分隔的文本")
    tokens = value.split()
    if not tokens:
        return ()

    unit_seconds = {None: 3600, "s": 1, "min": 60, "h": 3600, "d": 86400}
    offsets = set()
    for token in tokens:
        match = _TIME_PATTERN.fullmatch(token)
        if not match:
            raise ValueError(f"无效提醒时间：{token}")
        amount = int(match.group(1))
        unit = match.group(2).lower() if match.group(2) else None
        seconds = amount * unit_seconds[unit]
        if seconds <= 0 or seconds > MAX_REMINDER_SECONDS:
            raise ValueError(f"提醒时间必须在 1 秒至 365 天之间：{token}")
        offsets.add(seconds)
    return tuple(sorted(offsets, reverse=True))


def parse_group_whitelist(value) -> tuple[tuple[int, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    parts: Iterable = value if isinstance(value, (list, tuple, set)) else (value,)
    tokens = []
    for part in parts:
        tokens.extend(item for item in _GROUP_SEPARATOR.split(str(part).strip()) if item)

    groups = []
    invalid = []
    seen = set()
    for token in tokens:
        if token.isdigit() and int(token) > 0:
            group_id = int(token)
            if group_id not in seen:
                seen.add(group_id)
                groups.append(group_id)
        elif token not in invalid:
            invalid.append(token)
    return tuple(groups), tuple(invalid)


def classify_contest(name: str) -> str:
    contest_name = str(name or "")
    if "educational" in contest_name.casefold():
        return "educational"
    for category in ("div2", "div3", "div4"):
        if _DIVISION_PATTERNS[category].search(contest_name):
            return category
    return "other"


def _config_bool(config: Mapping, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def contest_is_enabled(name: str, config: Mapping) -> bool:
    category = classify_contest(name)
    key, default = _CATEGORY_SWITCHES[category]
    return _config_bool(config, key, default)


def build_reminder_specs(
    contests,
    offsets: Iterable[int],
    filter_config: Mapping,
    now_timestamp: int,
) -> tuple[ContestReminderSpec, ...]:
    specs = {}
    for contest in contests:
        if not isinstance(contest, Mapping) or contest.get("phase") != "BEFORE":
            continue
        try:
            contest_id = int(contest["id"])
            start_timestamp = int(contest["startTimeSeconds"])
        except (KeyError, TypeError, ValueError):
            continue
        contest_name = str(contest.get("name") or f"Codeforces Contest {contest_id}")
        if not contest_is_enabled(contest_name, filter_config):
            continue
        category = classify_contest(contest_name)
        for offset in offsets:
            offset_seconds = int(offset)
            spec = ContestReminderSpec(
                contest_id=contest_id,
                contest_name=contest_name,
                start_timestamp=start_timestamp,
                offset_seconds=offset_seconds,
                category=category,
            )
            if offset_seconds > 0 and spec.run_at_timestamp > int(now_timestamp):
                specs[spec.job_id] = spec
    return tuple(sorted(specs.values(), key=lambda item: (item.run_at_timestamp, item.job_id)))


def format_reminder_offset(seconds: int) -> str:
    value = int(seconds)
    for unit_seconds, label in ((86400, "天"), (3600, "小时"), (60, "分钟")):
        if value >= unit_seconds and value % unit_seconds == 0:
            return f"{value // unit_seconds}{label}"
    return f"{value}秒"
