"""Pure business logic for the birthday reminder plugin.

This module deliberately has **no dependency on the plugin SDK** so the date,
storage, import and scheduling logic can be tested with plain Python.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG_GIFT = "gift_advance"
TAG_SPECIAL = "special_care"
ALL_TAGS = (TAG_GIFT, TAG_SPECIAL)
TAG_LABELS = {
    TAG_GIFT: "提前提醒送礼",
    TAG_SPECIAL: "特别关心",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "remind_after": "08:30",
    "quiet_after": "22:30",
    "repeat_minutes": 60,
    "max_reminders": 3,
    "gift_advance_days": 7,
    "context_cue": True,
    "cue_time": "00:05",
    "scan_conversations": True,
    "max_people": 500,
}

DATA_VERSION = 1
MAX_PEOPLE = 500
MAX_NOTE_LEN = 200
MAX_NAME_LEN = 40
MAX_IMPORT_BYTES = 4 * 1024 * 1024
STATE_KEEP_DAYS = 60

_WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")


class BirthdayError(ValueError):
    """Raised when a birthday / person / table value is invalid."""


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_width(value: str) -> str:
    """Normalize full-width digits and separators commonly typed on a CN IME."""
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19:  # full-width digits
            out.append(chr(code - 0xFEE0))
        elif ch in "／":
            out.append("/")
        elif ch == "．":
            out.append(".")
        elif ch in "－—–":
            out.append("-")
        elif ch == "　":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _clean_cell(value: Any) -> str:
    return _normalize_width(_text(value)).replace("\u200b", "").strip()


def _today_key(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


def _parse_clock(raw: Any, default: str) -> tuple[int, int]:
    """Parse ``HH:MM`` and return (hour, minute). Invalid values use default."""
    text = _clean_cell(raw)
    for candidate in (text, default):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", candidate)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return 8, 30


def clock_minutes(raw: Any, default: str) -> int:
    hour, minute = _parse_clock(raw, default)
    return hour * 60 + minute


def normalize_clock(raw: Any, default: str) -> str:
    hour, minute = _parse_clock(raw, default)
    return f"{hour:02d}:{minute:02d}"


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------

_EXCEL_EPOCH = datetime(1899, 12, 30)


def _is_leap(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0


def _safe_date(year: int, month: int, day: int) -> date:
    """Build a date, mapping Feb 29 to Feb 28 in non-leap years."""
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise


def _excel_serial_to_date(value: float) -> date | None:
    if value < 1 or value > 80000:
        return None
    try:
        dt = _EXCEL_EPOCH + timedelta(days=float(value))
    except (OverflowError, ValueError):
        return None
    if dt.year < 1900 or dt.year > 2200:
        return None
    return dt.date()


def parse_birthday(raw: Any, *, today: date | None = None) -> dict[str, Any]:
    """Parse a birthday and return ``{year, month, day, canonical}``.

    Accepted forms include ``1995-08-12``, ``1995/8/12``, ``1995.8.12``,
    ``1995年8月12日``, ``08-12``, ``8月12日``, ``19950812``, ``0812``,
    Excel date serials and :class:`datetime` / :class:`date` values.

    ``canonical`` is ``YYYY-MM-DD`` when the year is known, otherwise ``MM-DD``.
    """
    if isinstance(raw, datetime):
        return parse_birthday(raw.date(), today=today)
    if isinstance(raw, date):
        return {
            "year": raw.year,
            "month": raw.month,
            "day": raw.day,
            "canonical": f"{raw.year:04d}-{raw.month:02d}-{raw.day:02d}",
        }
    if isinstance(raw, bool):
        raise BirthdayError("生日格式无法识别")
    if isinstance(raw, (int, float)):
        number = float(raw)
        if number.is_integer():
            integer = int(number)
            # Eight-digit YYYYMMDD typed as a number.
            if 19000101 <= integer <= 22001231:
                raw = str(integer)
            # Four-digit MMDD typed as a number.
            elif 101 <= integer <= 1231 and len(str(integer)) in (3, 4):
                raw = f"{integer:04d}"
            else:
                converted = _excel_serial_to_date(number)
                if converted is None:
                    raise BirthdayError("生日格式无法识别")
                return parse_birthday(converted, today=today)
        else:
            converted = _excel_serial_to_date(number)
            if converted is None:
                raise BirthdayError("生日格式无法识别")
            return parse_birthday(converted, today=today)

    text = _clean_cell(raw)
    text = text.replace("。", ".").replace("、", " ")
    text = re.sub(r"\s+", " ", text)
    # Drop a trailing time part.
    text = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?$", "", text).strip()

    patterns = (
        (r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", True),
        (r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$", True),
        (r"^(\d{1,2})[-/.](\d{1,2})$", False),
        (r"^(\d{1,2})月(\d{1,2})日?$", False),
    )
    year: int | None = None
    month = day = 0
    for pattern, has_year in patterns:
        match = re.fullmatch(pattern, text)
        if not match:
            continue
        if has_year:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
        else:
            month = int(match.group(1))
            day = int(match.group(2))
        break
    else:
        compact = re.sub(r"\D", "", text)
        if len(compact) == 8:
            year, month, day = int(compact[:4]), int(compact[4:6]), int(compact[6:])
        elif len(compact) == 4:
            month, day = int(compact[:2]), int(compact[2:])
        else:
            raise BirthdayError(f"无法识别生日：{text or '(空)'}")

    current = today or date.today()
    if month < 1 or month > 12 or day < 1 or day > 31:
        raise BirthdayError(f"生日月份或日期不合法：{text}")
    try:
        _safe_date(year or 2000, month, day)
    except ValueError:
        raise BirthdayError(f"生日日期不存在：{text}") from None

    if year is not None:
        if year < 1900:
            raise BirthdayError("生日年份不能早于 1900 年")
        if year > current.year:
            raise BirthdayError("生日年份不能晚于今年")
        try:
            date(year, month, day)
        except ValueError:
            raise BirthdayError(f"生日日期不存在：{text}") from None
        canonical = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        canonical = f"{month:02d}-{day:02d}"
    return {"year": year, "month": month, "day": day, "canonical": canonical}


def birthday_parts(person: Mapping[str, Any], *, today: date | None = None) -> tuple[int, int, int | None]:
    parsed = parse_birthday(person.get("birthday", ""), today=today)
    return int(parsed["month"]), int(parsed["day"]), parsed["year"]


def next_occurrence(person: Mapping[str, Any], today: date) -> date:
    month, day, _year = birthday_parts(person, today=today)
    candidate = _safe_date(today.year, month, day)
    if candidate < today:
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def days_until_birthday(person: Mapping[str, Any], today: date) -> int:
    return (next_occurrence(person, today) - today).days


def age_on_birthday(person: Mapping[str, Any], occurrence: date) -> int | None:
    month, day, year = birthday_parts(person)
    if year is None:
        return None
    age = occurrence.year - year
    if age < 0 or age > 150:
        return None
    return age


def enrich_person(person: Mapping[str, Any], today: date) -> dict[str, Any]:
    occurrence = next_occurrence(person, today)
    days = (occurrence - today).days
    enriched = dict(person)
    enriched.update(
        {
            "next_occurrence": occurrence.isoformat(),
            "days_until": days,
            "is_today": days == 0,
            "age": age_on_birthday(person, occurrence),
            "next_label": _format_occurrence(occurrence, days, today),
            "tags_label": "、".join(TAG_LABELS.get(tag, tag) for tag in person.get("tags", [])),
        }
    )
    return enriched


def _format_occurrence(occurrence: date, days: int, today: date) -> str:
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days < 7:
        return f"{days} 天后（周{_WEEKDAY_CN[occurrence.weekday()]}）"
    return f"{occurrence.month} 月 {occurrence.day} 日（{days} 天后）"


# ---------------------------------------------------------------------------
# People / settings validation
# ---------------------------------------------------------------------------


def normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，、;；/|]+|\s{2,}", _clean_cell(raw))
        if len(parts) == 1 and " " in parts[0]:
            # Single spaces are accepted as separators too, but Chinese tag
            # names themselves contain no ASCII spaces.
            parts = [piece for piece in parts[0].split(" ") if piece]
    elif isinstance(raw, (list, tuple, set)):
        parts = [_clean_cell(item) for item in raw]
    else:
        parts = [_clean_cell(raw)]

    tags: list[str] = []
    for part in parts:
        token = _clean_cell(part)
        if not token:
            continue
        compact = token.lower().replace(" ", "").replace("-", "_")
        if compact in {
            TAG_GIFT,
            "gift",
            "gift_advance",
            "advance",
            "提前",
            "提前提醒",
            "提前提醒送礼",
            "提前送礼",
            "送礼",
            "礼物",
            "买礼物",
        }:
            tag = TAG_GIFT
        elif compact in {
            TAG_SPECIAL,
            "special",
            "special_care",
            "star",
            "特别关心",
            "特别",
            "关心",
            "重要",
        }:
            tag = TAG_SPECIAL
        else:
            raise BirthdayError(f"不认识的标记：{token}")
        if tag not in tags:
            tags.append(tag)
    return tags


def validate_person_fields(
    name: Any,
    birthday: Any,
    note: Any = "",
    tags: Any = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    clean_name = re.sub(r"\s+", " ", _clean_cell(name))
    if not clean_name:
        raise BirthdayError("姓名不能为空")
    if len(clean_name) > MAX_NAME_LEN:
        raise BirthdayError(f"姓名不能超过 {MAX_NAME_LEN} 个字符")
    parsed = parse_birthday(birthday, today=today)
    clean_note = _clean_cell(note)
    if len(clean_note) > MAX_NOTE_LEN:
        raise BirthdayError(f"备注不能超过 {MAX_NOTE_LEN} 个字符")
    return {
        "name": clean_name,
        "birthday": parsed["canonical"],
        "note": clean_note,
        "tags": normalize_tags(tags),
    }


def new_person(fields: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    person = {
        "id": "p_" + uuid.uuid4().hex[:10],
        "name": fields["name"],
        "birthday": fields["birthday"],
        "note": fields.get("note", ""),
        "tags": list(fields.get("tags", [])),
        "created_at": stamp,
        "updated_at": stamp,
    }
    return person


def normalize_settings(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: source[k] for k in DEFAULT_SETTINGS if k in source})

    settings["remind_after"] = normalize_clock(settings.get("remind_after"), DEFAULT_SETTINGS["remind_after"])
    settings["quiet_after"] = normalize_clock(settings.get("quiet_after"), DEFAULT_SETTINGS["quiet_after"])
    settings["cue_time"] = normalize_clock(settings.get("cue_time"), DEFAULT_SETTINGS["cue_time"])

    for key, low, high, fallback in (
        ("repeat_minutes", 5, 720, DEFAULT_SETTINGS["repeat_minutes"]),
        ("max_reminders", 1, 12, DEFAULT_SETTINGS["max_reminders"]),
        ("gift_advance_days", 0, 60, DEFAULT_SETTINGS["gift_advance_days"]),
        ("max_people", 1, 2000, DEFAULT_SETTINGS["max_people"]),
    ):
        try:
            value = int(settings.get(key, fallback))
        except (TypeError, ValueError):
            value = int(fallback)
        settings[key] = max(low, min(high, value))

    settings["context_cue"] = bool(settings.get("context_cue", True))
    settings["scan_conversations"] = bool(settings.get("scan_conversations", True))
    settings["timezone"] = _text(settings.get("timezone")) or DEFAULT_SETTINGS["timezone"]

    # A cue after the first reminder has no purpose; keep the cue inside the
    # pre-reminder window instead of silently never firing it.
    if clock_minutes(settings["cue_time"], DEFAULT_SETTINGS["cue_time"]) >= clock_minutes(
        settings["remind_after"], DEFAULT_SETTINGS["remind_after"]
    ):
        settings["cue_time"] = "00:05"
    if clock_minutes(settings["quiet_after"], DEFAULT_SETTINGS["quiet_after"]) <= clock_minutes(
        settings["remind_after"], DEFAULT_SETTINGS["remind_after"]
    ):
        settings["quiet_after"] = "23:59"
    return settings


def settings_with_updates(current: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in updates.items():
        if key in DEFAULT_SETTINGS:
            merged[key] = value
    return normalize_settings(merged)


# ---------------------------------------------------------------------------
# Data file
# ---------------------------------------------------------------------------


def default_data() -> dict[str, Any]:
    return {
        "version": DATA_VERSION,
        "people": [],
        "settings": dict(DEFAULT_SETTINGS),
        "state": {"days": {}, "meta": {"last_import": None}},
    }


def normalize_data(raw: Any) -> dict[str, Any]:
    data = default_data()
    if not isinstance(raw, Mapping):
        return data

    people: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_people = raw.get("people")
    if isinstance(raw_people, list):
        for item in raw_people:
            if not isinstance(item, Mapping):
                continue
            try:
                fields = validate_person_fields(
                    item.get("name"),
                    item.get("birthday"),
                    item.get("note", ""),
                    item.get("tags", []),
                )
            except BirthdayError:
                continue
            person_id = _text(item.get("id")) or "p_" + uuid.uuid4().hex[:10]
            if person_id in seen_ids:
                person_id = "p_" + uuid.uuid4().hex[:10]
            seen_ids.add(person_id)
            person = {
                "id": person_id,
                "name": fields["name"],
                "birthday": fields["birthday"],
                "note": fields["note"],
                "tags": fields["tags"],
                "created_at": _text(item.get("created_at")) or datetime.now().isoformat(timespec="seconds"),
                "updated_at": _text(item.get("updated_at")) or datetime.now().isoformat(timespec="seconds"),
            }
            people.append(person)

    data["people"] = people
    data["settings"] = normalize_settings(raw.get("settings"))
    state = raw.get("state")
    if isinstance(state, Mapping):
        days = state.get("days")
        if isinstance(days, Mapping):
            data["state"]["days"] = {str(k): v for k, v in days.items() if isinstance(v, Mapping)}
        meta = state.get("meta")
        if isinstance(meta, Mapping):
            data["state"]["meta"].update({str(k): v for k, v in meta.items()})
    return data


def load_data(path: Path) -> tuple[dict[str, Any], str | None]:
    """Load a data file. Returns ``(data, warning)``; never raises for a bad file."""
    if not path.exists():
        return default_data(), None
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return normalize_data(raw), None
    except Exception as exc:  # corrupt JSON, permission, ...
        backup = path.with_name(path.name + ".corrupt")
        try:
            path.replace(backup)
        except OSError:
            pass
        return default_data(), f"数据文件损坏，已备份并重新开始：{exc}"


def save_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def day_state(data: dict[str, Any], day: date | str) -> dict[str, Any]:
    key = _today_key(day)
    days = data.setdefault("state", {}).setdefault("days", {})
    state = days.get(key)
    if not isinstance(state, dict):
        state = {}
        days[key] = state
    state.setdefault("acked", {})
    state.setdefault("prep_sent", {})
    state.setdefault("reminders", 0)
    state.setdefault("last_reminder_at", None)
    state.setdefault("cue_sent", False)
    state.setdefault("stopped", False)
    state.setdefault("last_scan_ts", 0.0)
    if not isinstance(state.get("acked"), dict):
        state["acked"] = {}
    if not isinstance(state.get("prep_sent"), dict):
        state["prep_sent"] = {}
    try:
        state["reminders"] = int(state.get("reminders", 0))
    except (TypeError, ValueError):
        state["reminders"] = 0
    return state


def peek_day_state(data: Mapping[str, Any], day: date | str) -> dict[str, Any]:
    """Read today's state without creating it (safe for context rendering)."""
    key = _today_key(day)
    days = data.get("state", {}).get("days", {}) if isinstance(data.get("state"), Mapping) else {}
    state = days.get(key) if isinstance(days, Mapping) else None
    if not isinstance(state, Mapping):
        return {
            "acked": {},
            "prep_sent": {},
            "reminders": 0,
            "last_reminder_at": None,
            "cue_sent": False,
            "stopped": False,
            "last_scan_ts": 0.0,
        }
    return dict(state)


def prune_state(data: dict[str, Any], today: date, keep_days: int = STATE_KEEP_DAYS) -> None:
    days = data.setdefault("state", {}).setdefault("days", {})
    cutoff = today - timedelta(days=keep_days)
    for key in list(days):
        try:
            day = date.fromisoformat(key)
        except ValueError:
            days.pop(key, None)
            continue
        if day < cutoff:
            days.pop(key, None)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def list_today(data: Mapping[str, Any], today: date) -> list[dict[str, Any]]:
    return [enrich_person(person, today) for person in data.get("people", []) if days_until_birthday(person, today) == 0]


def list_prep_due(data: Mapping[str, Any], today: date) -> list[dict[str, Any]]:
    settings = normalize_settings(data.get("settings"))
    advance = int(settings["gift_advance_days"])
    if advance <= 0:
        return []
    state = peek_day_state(data, today)
    acked = state.get("acked", {})
    prep_sent = state.get("prep_sent", {})
    result: list[dict[str, Any]] = []
    for person in data.get("people", []):
        if TAG_GIFT not in person.get("tags", []):
            continue
        if person.get("id") in acked or person.get("id") in prep_sent:
            continue
        days = days_until_birthday(person, today)
        if 0 < days <= advance:
            enriched = enrich_person(person, today)
            enriched["days_before"] = days
            result.append(enriched)
    return result


def list_upcoming(data: Mapping[str, Any], today: date, horizon: int = 30) -> list[dict[str, Any]]:
    upcoming: list[dict[str, Any]] = []
    for person in data.get("people", []):
        days = days_until_birthday(person, today)
        if 0 <= days <= horizon:
            upcoming.append(enrich_person(person, today))
    return sorted(upcoming, key=lambda item: (item["days_until"], item["name"]))


def plan_tick(data: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Decide what to do at ``now`` and reserve the action in the state file.

    Returns a plan dict, or ``None`` when nothing is due. The caller must call
    :func:`release_plan` if sending the message fails.
    """
    settings = normalize_settings(data.get("settings"))
    today = now.date()
    state = day_state(data, today)
    if state.get("stopped"):
        return None

    today_people = list_today(data, today)
    acked = state.get("acked", {})
    today_people = [person for person in today_people if person.get("id") not in acked]
    prep_people = list_prep_due(data, today)
    if not today_people and not prep_people:
        return None

    minutes = now.hour * 60 + now.minute
    quiet_minutes = clock_minutes(settings.get("quiet_after"), DEFAULT_SETTINGS["quiet_after"])
    remind_minutes = clock_minutes(settings.get("remind_after"), DEFAULT_SETTINGS["remind_after"])
    cue_minutes = clock_minutes(settings.get("cue_time"), DEFAULT_SETTINGS["cue_time"])
    if minutes >= quiet_minutes:
        return None

    if minutes < remind_minutes:
        if (
            bool(settings.get("context_cue"))
            and minutes >= cue_minutes
            and not state.get("cue_sent")
            and today_people
        ):
            state["cue_sent"] = True
            return {
                "kind": "cue",
                "date": today.isoformat(),
                "today": today_people,
                "prep": [],
                "count": 0,
                "prev": {"cue_sent": False},
            }
        return None

    reminders = int(state.get("reminders", 0) or 0)
    max_reminders = int(settings.get("max_reminders", 3))
    if reminders >= max_reminders:
        return None
    if reminders > 0:
        last_raw = state.get("last_reminder_at")
        try:
            last_at = datetime.fromisoformat(str(last_raw))
        except (TypeError, ValueError):
            last_at = None
        if last_at is not None:
            if last_at.tzinfo is None and now.tzinfo is not None:
                last_at = last_at.replace(tzinfo=now.tzinfo)
            elapsed = (now - last_at).total_seconds() / 60.0
            if elapsed < int(settings.get("repeat_minutes", 60)):
                return None

    prev_last = state.get("last_reminder_at")
    prev_prep = dict(state.get("prep_sent", {}))
    state["reminders"] = reminders + 1
    state["last_reminder_at"] = now.isoformat(timespec="seconds")
    prep_added: list[str] = []
    for person in prep_people:
        person_id = person.get("id")
        if person_id:
            state.setdefault("prep_sent", {})[person_id] = today.isoformat()
            prep_added.append(person_id)
    return {
        "kind": "reminder",
        "date": today.isoformat(),
        "today": today_people,
        "prep": prep_people,
        "count": reminders + 1,
        "prev": {
            "reminders": reminders,
            "last_reminder_at": prev_last,
            "prep_sent": prev_prep,
        },
        "prep_added": prep_added,
    }


def release_plan(data: dict[str, Any], plan: Mapping[str, Any]) -> None:
    """Undo the reservation when the host rejected the message."""
    today = date.fromisoformat(str(plan.get("date")))
    state = day_state(data, today)
    if plan.get("kind") == "cue":
        state["cue_sent"] = False
        return
    prev = plan.get("prev") or {}
    try:
        state["reminders"] = int(prev.get("reminders", max(0, int(state.get("reminders", 1)) - 1)))
    except (TypeError, ValueError):
        state["reminders"] = max(0, int(state.get("reminders", 1)) - 1)
    state["last_reminder_at"] = prev.get("last_reminder_at")
    previous_prep = prev.get("prep_sent")
    if isinstance(previous_prep, Mapping):
        state["prep_sent"] = dict(previous_prep)


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------


def resolve_people(data: Mapping[str, Any], name: str, today: date) -> list[dict[str, Any]]:
    """Resolve a user-spoken name to people relevant today / in the gift window."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for person in list_today(data, today) + list_prep_due(data, today):
        if person["id"] not in seen:
            candidates.append(person)
            seen.add(person["id"])
    needle = _clean_cell(name).casefold()
    if not needle:
        return candidates
    exact = [person for person in candidates if person["name"].casefold() == needle]
    if exact:
        return exact
    partial = [person for person in candidates if needle in person["name"].casefold()]
    if partial:
        return partial
    # Fall back to the full list so the user can acknowledge an upcoming
    # birthday that is not in today's reminder window yet.
    all_matches = [
        enrich_person(person, today)
        for person in data.get("people", [])
        if needle == person["name"].casefold() or needle in person["name"].casefold()
    ]
    return all_matches


def mark_acknowledged(
    data: dict[str, Any],
    people: Sequence[Mapping[str, Any]],
    action: str,
    *,
    note: str = "",
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now()
    stamp = current.isoformat(timespec="seconds")
    today = current.date()
    state = day_state(data, today)
    names: list[str] = []
    for person in people:
        person_id = _text(person.get("id"))
        if not person_id:
            continue
        state.setdefault("acked", {})[person_id] = {
            "at": stamp,
            "action": "stop_today" if action == "stop_today" else "greeted",
            "note": _clean_cell(note)[:MAX_NOTE_LEN],
        }
        names.append(_text(person.get("name")))
    if action == "stop_today":
        state["stopped"] = True
    return names


# Regexes intentionally err on the side of *not* acknowledging: the LLM tool
# sees the full sentence and can make a better judgement.
_ACK_PATTERNS = (
    re.compile(r"(已经|已|刚刚|刚|昨天|昨晚|今早|今天早上)[^，。！？\n]{0,12}(发|送|说|打|寄|订|买)[^，。！？\n]{0,12}(祝福|生日|礼物|花|蛋糕|红包|电话|消息|微信)?"),
    re.compile(r"(祝福|问候)[^，。！？\n]{0,5}(过|了|完)"),
    re.compile(r"(发|送|说|打)(过|了)[^，。！？\n]{0,8}(祝福|生日|电话|消息|微信)"),
)
_NEGATIVE_PATTERNS = (
    re.compile(r"(还没|还没有|没有|没|未|尚未)[^，。！？\n]{0,8}(发|送|说|祝福|打|买)"),
    re.compile(r"(等|待|稍后|一会|晚点|明天)[^，。！？\n]{0,6}(再|就)?(发|送|说|祝福|买)"),
)
_STOP_PATTERNS = (
    re.compile(r"(今天|今日)?[^，。！？\n]{0,8}(别|不用|不要|无需|停止|取消)[^，。！？\n]{0,8}(提醒|催)"),
    re.compile(r"(别|不要|不用)(再)?(提醒|催)我?"),
    re.compile(r"今天[^，。！？\n]{0,6}(不|别)(用|要)?(提醒|催)"),
)


def detect_ack_text(text: Any) -> str | None:
    """Best-effort classification of a user sentence.

    Returns ``"greeted"``, ``"stop_today"`` or ``None``.
    """
    content = _clean_cell(text)
    if not content:
        return None
    if any(pattern.search(content) for pattern in _STOP_PATTERNS):
        return "stop_today"
    if any(pattern.search(content) for pattern in _NEGATIVE_PATTERNS):
        return None
    if any(pattern.search(content) for pattern in _ACK_PATTERNS):
        return "greeted"
    return None


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def _format_iso_date(raw: Any) -> str:
    try:
        value = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return str(raw or "")
    return f"{value.month} 月 {value.day} 日"


def _person_line(person: Mapping[str, Any], *, prep: bool = False) -> str:
    name = person.get("name", "朋友")
    bits = [f"今天是{name}的生日"]
    if not prep:
        age = person.get("age")
        if isinstance(age, int) and age >= 0:
            bits[0] += f"（{age} 岁）"
        note = person.get("note")
        if note:
            bits.append(f"备注：{note}")
        if TAG_SPECIAL in person.get("tags", []):
            bits.append("TA 是你特别关心的人")
        if TAG_GIFT in person.get("tags", []):
            bits.append("记得准备/带上礼物")
    else:
        days = person.get("days_before")
        bits = [f"还有 {days} 天是{name}的生日（{_format_iso_date(person.get('next_occurrence'))}）"]
        note = person.get("note")
        if note:
            bits.append(f"备注：{note}")
        bits.append("你标记了「提前提醒送礼」，记得提前准备礼物")
        if TAG_SPECIAL in person.get("tags", []):
            bits.append("TA 是你特别关心的人")
    return "，".join(bits)


def compose_message(plan: Mapping[str, Any]) -> str:
    """Build the text injected into the AI context."""
    today_people = list(plan.get("today") or [])
    prep_people = list(plan.get("prep") or [])
    lines: list[str] = []
    for person in today_people:
        lines.append(_person_line(person))
    for person in prep_people:
        lines.append(_person_line(person, prep=True))
    body = "\n".join(f"- {line}" for line in lines) if lines else "- （今天没有生日记录）"

    kind = plan.get("kind")
    if kind == "cue":
        header = (
            "【生日备忘｜上下文】下面是今天可能要用到的生日信息。"
            "如果这是你今天第一次主动找{MASTER_NAME}搭话，请自然地把这件事带进对话；"
            "如果现在只是普通对话，就先记住，不要生硬插入。"
        )
    else:
        count = int(plan.get("count", 1) or 1)
        if count <= 1:
            header = "【生日备忘｜系统提醒】这是今天的第一次生日提醒。请以你平时的口吻主动对{MASTER_NAME}开口，把下面的事情自然地告诉{MASTER_NAME}。"
        else:
            header = (
                f"【生日备忘｜系统提醒】{{MASTER_NAME}}还没有确认已经送出祝福，这是今天的第 {count} 次提醒。"
                "请换一种自然的说法再提醒一次，不要机械重复上一句。"
            )

    footer = (
        "如果{MASTER_NAME}表示已经祝福过（包括「发了」「说过了」「打过电话了」这类模糊或等价的说明），"
        "或者让你今天别再提醒了，请调用工具 birthday_ack 登记（action=greeted 或 stop_today）。"
        "不要提到「插件」「系统提示」「上下文」这些词，也不要说自己是程序。"
    )
    return f"{header}\n{body}\n{footer}"


# ---------------------------------------------------------------------------
# Import (xlsx / csv)
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    people: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


_HEADER_ALIASES = {
    "name": {"姓名", "名字", "名称", "name", "fullname", "friend"},
    "birthday": {"生日", "出生日期", "出生年月日", "生辰", "birthday", "birth", "birthdate", "date"},
    "note": {"备注", "说明", "备注信息", "note", "notes", "remark", "remarks", "memo"},
    "tags": {"标记", "标签", "标记/标签", "tag", "tags", "mark", "marks", "label", "labels"},
}


def _normalize_header(value: Any) -> str:
    text = _clean_cell(value).lower()
    return re.sub(r"[\s_\-:：()（）\[\]【】]+", "", text)


def _match_header(value: Any) -> str | None:
    normalized = _normalize_header(value)
    if not normalized:
        return None
    for field_name, aliases in _HEADER_ALIASES.items():
        if normalized in {_normalize_header(alias) for alias in aliases}:
            return field_name
    return None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return _clean_cell(value)


def _rows_to_records(rows: list[list[Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not rows:
        return [], [{"row": 0, "message": "表格是空的"}], warnings

    mapped: dict[str, int] = {}
    header_index: int | None = None
    for row_index, row in enumerate(rows[:10]):
        candidate: dict[str, int] = {}
        for index, value in enumerate(row):
            field_name = _match_header(value)
            if field_name and field_name not in candidate:
                candidate[field_name] = index
        if "name" in candidate and "birthday" in candidate:
            mapped = candidate
            header_index = row_index
            break

    if header_index is not None:
        header_row = True
        start = header_index + 1
        mapped.setdefault("name", 0)
        mapped.setdefault("birthday", 1)
    else:
        mapped = {"name": 0, "birthday": 1, "note": 2, "tags": 3}
        header_row = False
        start = 0
        warnings.append("没有找到「姓名 / 生日」表头，已按 A=姓名、B=生日、C=备注、D=标记 读取。")

    records: list[dict[str, Any]] = []
    for offset, row in enumerate(rows[start:], start=start + 1):
        if not row or not any(_cell_text(cell) for cell in row):
            continue
        def cell(field_name: str) -> Any:
            index = mapped.get(field_name)
            if index is None or index >= len(row):
                return ""
            return row[index]

        name = _cell_text(cell("name"))
        if not name:
            if _cell_text(cell("birthday")) or _cell_text(cell("note")):
                errors.append({"row": offset, "message": "姓名为空，已跳过"})
            continue
        records.append(
            {
                "row": offset,
                "name": name,
                "birthday": cell("birthday"),
                "note": _cell_text(cell("note")),
                "tags": cell("tags"),
            }
        )
    if header_row and not records:
        errors.append({"row": (header_index or 0) + 1, "message": "表头之后没有数据行"})
    return records, errors, warnings


def _xlsx_rows(data: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BirthdayError("文件不是有效的 xlsx 表格（无法解压）") from exc

    with archive:
        names = set(archive.namelist())
        try:
            total_uncompressed = sum(info.file_size for info in archive.infolist())
        except Exception:
            total_uncompressed = 0
        if total_uncompressed > MAX_IMPORT_BYTES * 8:
            raise BirthdayError("表格解压后过大，已拒绝导入")
        if "xl/workbook.xml" not in names:
            raise BirthdayError("文件不是有效的 xlsx 表格（缺少 workbook.xml）")

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                texts = [node.text or "" for node in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
                shared.append("".join(texts))

        date_styles: list[bool] = []
        if "xl/styles.xml" in names:
            try:
                root = ET.fromstring(archive.read("xl/styles.xml"))
                custom: dict[int, str] = {}
                for number_format in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}numFmt"):
                    try:
                        custom[int(number_format.attrib.get("numFmtId", "0"))] = number_format.attrib.get("formatCode", "")
                    except ValueError:
                        pass
                builtin_date_ids = set(range(14, 23)) | {45, 46, 47}
                for cell_xf in root.findall(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}cellXfs/"
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}xf"
                ):
                    try:
                        number_format_id = int(cell_xf.attrib.get("numFmtId", "0"))
                    except ValueError:
                        number_format_id = 0
                    code = custom.get(number_format_id, "")
                    date_styles.append(
                        number_format_id in builtin_date_ids
                        or bool(re.search(r"[yYdD]", code))
                        or ("m" in code.lower() and any(sep in code for sep in ("/", "-", "月", "日")))
                    )
            except ET.ParseError:
                date_styles = []

        sheet_path = _first_sheet_path(archive, names)
        root = ET.fromstring(archive.read(sheet_path))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        rows: list[list[str]] = []
        for row_node in root.iter(ns + "row"):
            cells: dict[int, str] = {}
            max_index = -1
            for cell in row_node.findall(ns + "c"):
                ref = cell.attrib.get("r", "")
                index = _column_index(ref)
                if index < 0:
                    continue
                max_index = max(max_index, index)
                value = _xlsx_cell_value(cell, shared)
                if value is None:
                    continue
                if cell.attrib.get("t") in (None, "n"):
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        number = None
                    if number is not None:
                        style_index = _int_or_none(cell.attrib.get("s"))
                        is_date_style = bool(
                            style_index is not None and 0 <= style_index < len(date_styles) and date_styles[style_index]
                        )
                        if is_date_style:
                            converted = _excel_serial_to_date(number)
                            if converted is not None:
                                value = converted.strftime("%Y-%m-%d")
                cells[index] = value
            if max_index < 0:
                continue
            rows.append([cells.get(index, "") for index in range(max_index + 1)])
        return rows


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _column_index(ref: str) -> int:
    letters = re.match(r"^([A-Za-z]+)", ref)
    if not letters:
        return -1
    index = 0
    for char in letters.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str | None:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cell_type = cell.attrib.get("t")
    value_node = cell.find(ns + "v")
    if cell_type == "inlineStr":
        inline = cell.find(ns + "is")
        if inline is None:
            return None
        return "".join(node.text or "" for node in inline.iter(ns + "t"))
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return None
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if cell_type == "e":
        return None
    return raw


def _first_sheet_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    ns_pkg = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    fallback = "xl/worksheets/sheet1.xml"
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = workbook.findall(f".//{ns_main}sheet")
        if not sheets:
            return fallback
        rel_id = sheets[0].attrib.get(f"{ns_rel}id")
        if not rel_id or "xl/_rels/workbook.xml.rels" not in names:
            return fallback
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.findall(f"{ns_pkg}Relationship"):
            if rel.attrib.get("Id") == rel_id:
                target = rel.attrib.get("Target", "")
                if target.startswith("/"):
                    return target.lstrip("/")
                if target.startswith("xl/"):
                    return target
                return "xl/" + target
    except Exception:
        return fallback
    return fallback


def _csv_rows(data: bytes) -> list[list[str]]:
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BirthdayError("CSV 编码无法识别，请另存为 UTF-8")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return [list(row) for row in reader]


def parse_table_bytes(filename: str, data: bytes) -> list[list[str]]:
    if len(data) > MAX_IMPORT_BYTES:
        raise BirthdayError(f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 限制")
    lower = (filename or "").lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return _xlsx_rows(data)
    if lower.endswith(".xls"):
        raise BirthdayError("不支持旧版 .xls，请在 Excel 中另存为 .xlsx 后再导入")
    if lower.endswith(".csv") or lower.endswith(".txt"):
        return _csv_rows(data)
    # Sniff the ZIP magic so a misnamed .xlsx still works.
    if data[:2] == b"PK":
        return _xlsx_rows(data)
    return _csv_rows(data)


def build_import_result(
    data: dict[str, Any],
    rows: list[list[Any]],
    *,
    now: datetime | None = None,
) -> ImportResult:
    records, errors, warnings = _rows_to_records(rows)
    result = ImportResult(errors=errors, warnings=warnings)
    now = now or datetime.now()
    today = now.date()
    people = data.setdefault("people", [])
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    for person in people:
        try:
            month, day, _ = birthday_parts(person, today=today)
        except BirthdayError:
            continue
        existing[(person.get("name", "").casefold(), f"{month:02d}-{day:02d}")] = person

    for record in records:
        try:
            fields = validate_person_fields(
                record.get("name"),
                record.get("birthday"),
                record.get("note", ""),
                record.get("tags", []),
                today=today,
            )
        except BirthdayError as exc:
            if str(exc).startswith("不认识的标记"):
                try:
                    fields = validate_person_fields(
                        record.get("name"),
                        record.get("birthday"),
                        record.get("note", ""),
                        [],
                        today=today,
                    )
                except BirthdayError as inner:
                    result.errors.append({"row": record.get("row", 0), "message": str(inner)})
                    continue
                result.warnings.append(f"第 {record.get('row', 0)} 行：{exc}，已忽略该标记")
            else:
                result.errors.append({"row": record.get("row", 0), "message": str(exc)})
                continue

        parsed = parse_birthday(fields["birthday"], today=today)
        key = (fields["name"].casefold(), f"{parsed['month']:02d}-{parsed['day']:02d}")
        matched = existing.get(key)
        if matched is not None:
            matched.update(
                {
                    "name": fields["name"],
                    "birthday": fields["birthday"],
                    "note": fields["note"],
                    "tags": fields["tags"],
                    "updated_at": now.isoformat(timespec="seconds"),
                }
            )
            result.updated += 1
            result.people.append(dict(matched))
        else:
            if len(people) >= MAX_PEOPLE:
                result.errors.append({"row": record.get("row", 0), "message": f"名单最多 {MAX_PEOPLE} 人"})
                continue
            person = new_person(fields, now=now)
            people.append(person)
            existing[key] = person
            result.added += 1
            result.people.append(dict(person))
    return result


def import_table(
    data: dict[str, Any],
    filename: str,
    content: bytes,
    *,
    now: datetime | None = None,
) -> ImportResult:
    rows = parse_table_bytes(filename, content)
    result = build_import_result(data, rows, now=now)
    if result.added or result.updated:
        data.setdefault("state", {}).setdefault("meta", {})["last_import"] = {
            "at": (now or datetime.now()).isoformat(timespec="seconds"),
            "filename": filename,
            "added": result.added,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors": len(result.errors),
        }
    return result
