"""生日提醒 / Birthday Reminder plugin for N.E.K.O.

The daily flow is:

1. Early in the day (or as soon as the plugin starts) a *context cue* is fed
   to the character without forcing a turn, so the first proactive chat of
   the day can naturally mention the birthday.
2. After the configured ``remind_after`` time the plugin sends a proactive
   message. The character then speaks to the user in her own voice.
3. If the user has not confirmed yet, follow-up reminders repeat every
   ``repeat_minutes`` until the daily limit. The user can confirm through the
   ``birthday_ack`` LLM tool, the plugin panel, or a plainly worded chat
   message.

All persistent state lives in ``<plugin data>/birthdays.json``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    message,
    neko_plugin,
    plugin_entry,
    timer_interval,
    ui,
)

from .birthday_core import (
    ALL_TAGS,
    MAX_IMPORT_BYTES,
    TAG_SPECIAL,
    BirthdayError,
    ImportResult,
    clock_minutes,
    compose_message,
    day_state,
    default_data,
    detect_ack_text,
    enrich_person,
    import_table,
    list_prep_due,
    list_today,
    list_upcoming,
    load_data,
    mark_acknowledged,
    normalize_settings,
    parse_birthday,
    peek_day_state,
    plan_tick,
    prune_state,
    release_plan,
    resolve_people,
    save_data,
    settings_with_updates,
    validate_person_fields,
)

TAG_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": list(ALL_TAGS)},
    "description": "标记：gift_advance=提前提醒送礼，special_care=特别关心",
}

_ACK_ACTIONS = {"greeted", "stop_today"}


def _submitted(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("submitted"))
    return bool(getattr(result, "submitted", False))


@neko_plugin
class BirthdayReminderPlugin(NekoPluginBase):

    def __init__(self, ctx):
        super().__init__(ctx)
        self._lock = threading.RLock()
        self._tick_guard = threading.Lock()
        self._data_path: Path = self.data_path("birthdays.json")
        self._data: dict[str, Any] = default_data()
        self._load_warning: str | None = None
        self._startup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def startup(self, **_):
        with self._lock:
            self._data, self._load_warning = load_data(self._data_path)
            prune_state(self._data, self._now().date())
            try:
                save_data(self._data_path, self._data)
            except OSError as exc:
                self.logger.warning("Could not persist birthday data at startup: {}", exc)
        if self._load_warning:
            self.logger.warning("{}", self._load_warning)
        people = len(self._data.get("people", []))
        settings = normalize_settings(self._data.get("settings"))
        self.logger.info(
            "Birthday reminder ready: {} people, timezone={}, remind_after={}",
            people,
            settings.get("timezone"),
            settings.get("remind_after"),
        )
        self._startup_task = asyncio.create_task(self._startup_tick())
        return Ok({
            "status": "ready",
            "people": people,
            "timezone": settings.get("timezone"),
            "remind_after": settings.get("remind_after"),
        })

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        task = self._startup_task
        self._startup_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return Ok({"status": "shutdown"})

    async def _startup_tick(self) -> None:
        try:
            await asyncio.sleep(2.5)
            await self._tick(reason="startup")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Startup birthday tick failed")

    @timer_interval(id="birthday_tick", seconds=60, name="生日检查")
    async def birthday_tick(self, **_):
        await self._tick(reason="timer")
        return Ok({"ok": True})

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    def _save_locked(self) -> None:
        try:
            save_data(self._data_path, self._data)
        except OSError as exc:
            self.logger.warning("Failed to save birthday data: {}", exc)

    def _timezone(self) -> Any:
        settings = normalize_settings(self._data.get("settings"))
        name = str(settings.get("timezone") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except Exception:
            local = datetime.now().astimezone().tzinfo
            return local or timezone.utc

    def _now(self) -> datetime:
        return datetime.now(self._timezone())

    def _current_lanlan(self) -> str | None:
        host = getattr(self.ctx, "_host_ctx", None)
        for attr in ("current_lanlan", "lanlan_name"):
            value = getattr(host, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # ------------------------------------------------------------------
    # Tick / reminders
    # ------------------------------------------------------------------

    async def _tick(self, *, reason: str = "timer") -> None:
        if not self._tick_guard.acquire(blocking=False):
            return
        try:
            now = self._now()
            plan = await asyncio.to_thread(self._reserve_plan, now)
            if plan is None:
                await self._maybe_scan_chat(now)
                return

            special = any(
                TAG_SPECIAL in person.get("tags", [])
                for person in list(plan.get("today") or []) + list(plan.get("prep") or [])
            )
            kind = plan.get("kind")
            try:
                text = compose_message(plan)
                result = self.push_message(
                    source="birthday_reminder",
                    visibility=[],
                    ai_behavior="read" if kind == "cue" else "respond",
                    parts=[{"type": "text", "text": text}],
                    priority=9 if special else 8,
                    target_lanlan=self._current_lanlan(),
                    coalesce_key=f"birthday:{plan.get('date')}:{'cue' if kind == 'cue' else plan.get('count')}",
                    metadata={
                        "kind": kind,
                        "date": plan.get("date"),
                        "count": plan.get("count"),
                        "people": [p.get("name") for p in list(plan.get("today") or [])],
                        "advance_people": [p.get("name") for p in list(plan.get("prep") or [])],
                        "reason": reason,
                    },
                )
            except Exception:
                await asyncio.to_thread(self._release_plan, plan)
                raise
            if not _submitted(result):
                await asyncio.to_thread(self._release_plan, plan)
                self.logger.warning(
                    "Birthday push rejected ({}), will retry next tick",
                    result.get("reason") if isinstance(result, Mapping) else "unknown",
                )
                return
            self.logger.info(
                "Birthday {} sent: date={} count={} people={}",
                kind,
                plan.get("date"),
                plan.get("count"),
                [p.get("name") for p in list(plan.get("today") or [])],
            )
        except Exception:
            self.logger.exception("Birthday tick failed")
        finally:
            self._tick_guard.release()

    def _reserve_plan(self, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            plan = plan_tick(self._data, now)
            if plan is not None:
                prune_state(self._data, now.date())
                self._save_locked()
            return plan

    def _release_plan(self, plan: Mapping[str, Any]) -> None:
        with self._lock:
            release_plan(self._data, plan)
            self._save_locked()

    # ------------------------------------------------------------------
    # User-message fallback confirmation
    # ------------------------------------------------------------------

    def _active_people(self, now: datetime) -> list[dict[str, Any]]:
        today = now.date()
        state = peek_day_state(self._data, today)
        acked = state.get("acked", {})
        active = [person for person in list_today(self._data, today) if person.get("id") not in acked]
        active.extend(person for person in list_prep_due(self._data, today) if person.get("id") not in acked)
        return active

    def _ack_from_text(self, text: str, now: datetime, source: str) -> list[str]:
        if not text or len(text) > 500:
            return []
        # Never treat the plugin's own injected cue as a user confirmation.
        if "【生日备忘" in text or "birthday_ack" in text:
            return []
        # Skip questions / hypotheticals unless the user is clearly speaking
        # about themselves ("如果我已经发了呢").
        if ("吗" in text or "?" in text or "？" in text or "如果" in text) and "我" not in text:
            return []
        action = detect_ack_text(text)
        if not action:
            return []
        with self._lock:
            active = self._active_people(now)
            if not active:
                return []
            targets = active
            if action == "greeted":
                mentioned = [
                    person for person in active
                    if person.get("name") and str(person["name"]) in text
                ]
                if mentioned:
                    targets = mentioned
            names = mark_acknowledged(self._data, targets, action, note=text[:80], now=now)
            if names:
                self._save_locked()
                self.logger.info("Acknowledged from {}: {} ({})", source, names, action)
            return names

    async def _maybe_scan_chat(self, now: datetime) -> None:
        settings = normalize_settings(self._data.get("settings"))
        if not bool(settings.get("scan_conversations")):
            return
        with self._lock:
            active = self._active_people(now)
            if not active:
                return
            state = day_state(self._data, now.date())
            last_scan = float(state.get("last_scan_ts") or 0.0)
            if now.timestamp() - last_scan < 45:
                return
            state["last_scan_ts"] = now.timestamp()
            threshold = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            last_reminder = state.get("last_reminder_at")
            if last_reminder:
                try:
                    threshold = max(threshold, datetime.fromisoformat(str(last_reminder)).timestamp())
                except (TypeError, ValueError):
                    pass
            self._save_locked()

        records: list[Any] = []
        try:
            bus = getattr(self, "bus", None)
            conversations = getattr(bus, "conversations", None) if bus is not None else None
            if conversations is None:
                return
            try:
                result = conversations.get(max_count=12)
            except TypeError:
                result = conversations.get(limit=12)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=5.0)
            items = getattr(result, "items", None)
            if items is not None:
                records = list(items)
            elif isinstance(result, (list, tuple)):
                records = list(result)
        except asyncio.TimeoutError:
            self.logger.debug("Conversation scan timed out")
            return
        except Exception as exc:
            self.logger.debug("Conversation scan unavailable: {}", exc)
            return

        for record in records:
            try:
                timestamp = getattr(record, "timestamp", None)
                if isinstance(timestamp, (int, float)) and float(timestamp) < threshold:
                    continue
                turn_type = str(getattr(record, "turn_type", "") or "").lower()
                if turn_type and not any(marker in turn_type for marker in ("user", "human", "master")):
                    continue
                content = str(getattr(record, "content", "") or "")
                if not content:
                    continue
                names = self._ack_from_text(content, now, "conversation")
                if names:
                    return
            except Exception:
                continue

    @message(id="birthday_chat_message", source="chat")
    async def on_chat_message(self, text: Any = "", sender: Any = "", **kwargs):
        sender_text = str(sender or "").lower()
        if not sender_text or not any(marker in sender_text for marker in ("user", "human", "master", "player")):
            return Ok({"handled": False})
        content = text if isinstance(text, str) else str(kwargs.get("content") or text or "")
        if content:
            self._ack_from_text(content, self._now(), "chat message")
        return Ok({"handled": False})

    # ------------------------------------------------------------------
    # LLM tools
    # ------------------------------------------------------------------

    @llm_tool(
        name="birthday_ack",
        description=(
            "当主人表示已经给朋友发送了生日祝福（包括『发了』『说过了』『打过电话了』"
            "『发过消息/微信』这类模糊或等价的说明），或者让猫娘今天不要再提醒生日时调用。"
            "name 留空表示今天所有待办生日；action=greeted 表示已祝福，"
            "action=stop_today 表示今天停止提醒。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "朋友的姓名；留空表示今天所有生日"},
                "action": {
                    "type": "string",
                    "enum": ["greeted", "stop_today"],
                    "description": "greeted=已经祝福；stop_today=今天别再提醒",
                },
                "note": {"type": "string", "description": "主人原话或补充说明，可留空"},
            },
        },
        timeout=20.0,
    )
    async def birthday_ack(self, *, name: str = "", action: str = "greeted", note: str = "") -> dict[str, Any]:
        action = action if action in _ACK_ACTIONS else "greeted"
        now = self._now()
        with self._lock:
            targets = resolve_people(self._data, name, now.date())
            if not targets:
                return {
                    "ok": False,
                    "message": f"今天没有找到与「{name}」相关的生日提醒。" if name else "今天没有待确认的生日提醒。",
                }
            names = mark_acknowledged(self._data, targets, action, note=note, now=now)
            self._save_locked()
        if action == "stop_today":
            message = f"已记录：今天不再提醒（{('、'.join(names))}）。"
        else:
            message = f"已记录：{('、'.join(names))} 的生日祝福已送出。"
        return {"ok": True, "action": action, "acked": names, "message": message}

    @llm_tool(
        name="birthday_query",
        description="查询今天和未来一段时间的生日名单。days 默认 30 天。",
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "向后查看多少天，1-365，默认 30"},
            },
        },
        timeout=15.0,
    )
    async def birthday_query(self, *, days: int = 30) -> dict[str, Any]:
        try:
            horizon = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            horizon = 30
        now = self._now()
        with self._lock:
            today = list_today(self._data, now.date())
            upcoming = list_upcoming(self._data, now.date(), horizon=horizon)
        return {
            "today": [
                {"name": p["name"], "birthday": p["birthday"], "age": p.get("age"), "note": p.get("note", "")}
                for p in today
            ],
            "upcoming": [
                {
                    "name": p["name"],
                    "birthday": p["birthday"],
                    "days_until": p["days_until"],
                    "next_occurrence": p["next_occurrence"],
                    "note": p.get("note", ""),
                    "tags": p.get("tags", []),
                }
                for p in upcoming
            ],
            "count": len(upcoming),
        }

    # ------------------------------------------------------------------
    # Hosted UI context
    # ------------------------------------------------------------------

    @ui.context(id="dashboard")
    async def dashboard(self):
        now = self._now()
        today = now.date()
        with self._lock:
            settings = normalize_settings(self._data.get("settings"))
            state = peek_day_state(self._data, today)
            people = [enrich_person(person, today) for person in self._data.get("people", [])]
            people.sort(key=lambda p: (p["days_until"], 0 if TAG_SPECIAL in p.get("tags", []) else 1, p["name"]))
            today_people = [p for p in people if p["is_today"]]
            acked = state.get("acked", {})
            for person in today_people:
                person["acked"] = person.get("id") in acked
            upcoming = list_upcoming(self._data, today, horizon=30)
            active_count = sum(1 for p in today_people if not p.get("acked"))
            last_import = self._data.get("state", {}).get("meta", {}).get("last_import")
            next_label = ""
            if active_count and not state.get("stopped"):
                if int(state.get("reminders", 0)) == 0:
                    next_label = f"今天 {settings.get('remind_after')} 开始提醒"
                else:
                    next_label = f"已提醒 {state.get('reminders')}/{settings.get('max_reminders')} 次"
            if state.get("stopped"):
                next_label = "今天已停止提醒"
            return {
                "now": now.isoformat(timespec="seconds"),
                "timezone": str(settings.get("timezone")),
                "people": people,
                "today": today_people,
                "upcoming": upcoming,
                "settings": settings,
                "state": {
                    "stopped": bool(state.get("stopped")),
                    "reminders": int(state.get("reminders", 0) or 0),
                    "max_reminders": int(settings.get("max_reminders", 3)),
                    "cue_sent": bool(state.get("cue_sent")),
                    "next_label": next_label,
                },
                "stats": {
                    "total": len(people),
                    "today": len(today_people),
                    "today_pending": active_count,
                    "upcoming_30": len(upcoming),
                },
                "last_import": last_import if isinstance(last_import, Mapping) else None,
                "load_warning": self._load_warning,
            }

    # ------------------------------------------------------------------
    # CRUD entries
    # ------------------------------------------------------------------

    @ui.action(id="add_person", label="添加朋友", tone="primary")
    @plugin_entry(
        id="add_person",
        name="添加生日",
        description="添加一位朋友的生日记录。",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "姓名"},
                "birthday": {"type": "string", "description": "生日，如 1995-08-12 或 08-12"},
                "note": {"type": "string", "description": "备注"},
                "tags": TAG_SCHEMA,
            },
            "required": ["name", "birthday"],
        },
    )
    async def add_person(self, name: str, birthday: str, note: str = "", tags: Any = None, **_):
        now = self._now()
        try:
            fields = validate_person_fields(name, birthday, note, tags, today=now.date())
        except BirthdayError as exc:
            return Err(SdkError(str(exc)))
        with self._lock:
            settings = normalize_settings(self._data.get("settings"))
            if len(self._data.get("people", [])) >= int(settings.get("max_people", 500)):
                return Err(SdkError(f"名单最多 {settings.get('max_people')} 人"))
            for existing in self._data.get("people", []):
                if existing.get("name", "").casefold() == fields["name"].casefold():
                    try:
                        old = parse_birthday(existing.get("birthday", ""))
                        new = parse_birthday(fields["birthday"])
                        if (old["month"], old["day"]) == (new["month"], new["day"]):
                            return Err(SdkError(f"「{fields['name']}」已经有相同的生日记录"))
                    except BirthdayError:
                        pass
            person = {
                "id": "p_" + uuid.uuid4().hex[:10],
                "name": fields["name"],
                "birthday": fields["birthday"],
                "note": fields["note"],
                "tags": fields["tags"],
                "created_at": now.isoformat(timespec="seconds"),
                "updated_at": now.isoformat(timespec="seconds"),
            }
            self._data.setdefault("people", []).append(person)
            self._save_locked()
        return Ok({"person": person, "message": f"已添加 {person['name']} 的生日"})

    @ui.action(id="update_person", label="保存修改", tone="primary")
    @plugin_entry(
        id="update_person",
        name="修改生日",
        description="修改一位朋友已有的生日记录。",
        input_schema={
            "type": "object",
            "properties": {
                "person_id": {"type": "string", "description": "记录 ID"},
                "name": {"type": "string", "description": "姓名；留空表示不修改"},
                "birthday": {"type": "string", "description": "生日；留空表示不修改"},
                "note": {"type": "string", "description": "备注"},
                "tags": TAG_SCHEMA,
            },
            "required": ["person_id"],
        },
    )
    async def update_person(
        self,
        person_id: str,
        name: str = "",
        birthday: str = "",
        note: str | None = None,
        tags: Any = None,
        **_,
    ):
        now = self._now()
        with self._lock:
            person = next((p for p in self._data.get("people", []) if p.get("id") == person_id), None)
            if person is None:
                return Err(SdkError(f"未找到记录：{person_id}"))
            merged_name = name or person.get("name", "")
            merged_birthday = birthday or person.get("birthday", "")
            merged_note = person.get("note", "") if note is None else note
            merged_tags = person.get("tags", []) if tags is None else tags
            try:
                fields = validate_person_fields(
                    merged_name, merged_birthday, merged_note, merged_tags, today=now.date()
                )
            except BirthdayError as exc:
                return Err(SdkError(str(exc)))
            person.update({
                "name": fields["name"],
                "birthday": fields["birthday"],
                "note": fields["note"],
                "tags": fields["tags"],
                "updated_at": now.isoformat(timespec="seconds"),
            })
            self._save_locked()
        return Ok({"person": person, "message": f"已更新 {person['name']} 的生日"})

    @ui.action(id="delete_person", label="删除", tone="danger", confirm=True)
    @plugin_entry(
        id="delete_person",
        name="删除生日",
        description="删除一位朋友的生日记录。",
        input_schema={
            "type": "object",
            "properties": {"person_id": {"type": "string", "description": "记录 ID"}},
            "required": ["person_id"],
        },
    )
    async def delete_person(self, person_id: str, **_):
        with self._lock:
            people = self._data.get("people", [])
            target = next((p for p in people if p.get("id") == person_id), None)
            if target is None:
                return Err(SdkError(f"未找到记录：{person_id}"))
            self._data["people"] = [p for p in people if p.get("id") != person_id]
            self._save_locked()
        return Ok({"deleted": person_id, "message": f"已删除 {target.get('name')} 的生日记录"})

    @ui.action(id="test_reminder", label="测试提醒", tone="info")
    @plugin_entry(
        id="test_reminder",
        name="测试生日提醒",
        description="立即让猫娘说一条今天的生日提醒预览；不会改变「已提醒」次数或确认状态。",
    )
    async def test_reminder(self, **_):
        now = self._now()
        with self._lock:
            today = list_today(self._data, now.date())
            state = peek_day_state(self._data, now.date())
            acked = state.get("acked", {})
            today = [person for person in today if person.get("id") not in acked]
        if not today:
            return Err(SdkError("今天没有待提醒的生日；可以先把某位朋友的生日改成今天再试"))
        plan = {
            "kind": "reminder",
            "date": now.date().isoformat(),
            "today": today,
            "prep": [],
            "count": 1,
        }
        try:
            result = self.push_message(
                source="birthday_reminder",
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": compose_message(plan)}],
                priority=9 if any(TAG_SPECIAL in p.get("tags", []) for p in today) else 8,
                target_lanlan=self._current_lanlan(),
                coalesce_key=f"birthday:test:{now.timestamp()}",
                metadata={"kind": "test", "date": plan["date"], "people": [p.get("name") for p in today]},
            )
        except Exception as exc:
            self.logger.exception("Test reminder push failed")
            return Err(SdkError(f"发送测试提醒失败：{exc}"))
        if not _submitted(result):
            return Err(SdkError("宿主暂时没有接受这条消息，请稍后再试"))
        return Ok({"message": "测试提醒已发送", "people": [p.get("name") for p in today]})

    @plugin_entry(
        id="list_people",
        name="查看生日名单",
        description="列出所有生日记录，以及今天和未来 30 天的生日。",
        llm_result_fields=["total", "today", "upcoming"],
    )
    async def list_people(self, **_):
        now = self._now()
        with self._lock:
            people = [enrich_person(p, now.date()) for p in self._data.get("people", [])]
            today = list_today(self._data, now.date())
            upcoming = list_upcoming(self._data, now.date(), horizon=30)
        return Ok({
            "total": len(people),
            "today": [{"name": p["name"], "age": p.get("age"), "note": p.get("note", "")} for p in today],
            "upcoming": [
                {"name": p["name"], "days_until": p["days_until"], "birthday": p["birthday"]}
                for p in upcoming
            ],
            "people": people,
        })

    # ------------------------------------------------------------------
    # Import / acknowledgement / settings
    # ------------------------------------------------------------------

    @ui.action(id="import_xlsx", label="导入表格", tone="success")
    @plugin_entry(
        id="import_xlsx",
        name="导入生日表格",
        description=(
            "导入 xlsx/xlsm/csv 表格。表头需要包含：姓名、生日、备注、标记。"
            "同姓名且同月日的记录会被更新，否则新增。"
        ),
        timeout=120.0,
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名，如 birthdays.xlsx"},
                "content_base64": {"type": "string", "description": "文件的 base64 内容"},
            },
            "required": ["filename", "content_base64"],
        },
    )
    async def import_xlsx(self, filename: str, content_base64: str, **_):
        try:
            raw = base64.b64decode(str(content_base64), validate=False)
        except (binascii.Error, ValueError) as exc:
            return Err(SdkError(f"文件内容不是有效的 base64：{exc}"))
        if len(raw) > MAX_IMPORT_BYTES:
            return Err(SdkError(f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 限制"))
        now = self._now()
        try:
            result = await asyncio.to_thread(self._run_import, filename, raw, now)
        except BirthdayError as exc:
            return Err(SdkError(str(exc)))
        except Exception as exc:
            self.logger.exception("Birthday import failed")
            return Err(SdkError(f"导入失败：{exc}"))

        payload = result.to_dict()
        payload["errors"] = payload["errors"][:20]
        payload["filename"] = filename
        payload["message"] = (
            f"导入完成：新增 {result.added}，更新 {result.updated}，"
            f"跳过 {result.skipped}，问题 {len(result.errors)} 行"
        )
        return Ok(payload)

    def _run_import(self, filename: str, raw: bytes, now: datetime) -> ImportResult:
        with self._lock:
            result = import_table(self._data, filename, raw, now=now)
            self._save_locked()
        return result

    @ui.action(id="acknowledge", label="已祝福", tone="success")
    @plugin_entry(
        id="acknowledge",
        name="标记已祝福",
        description="标记今天（或指定朋友）的生日祝福已经送出，停止后续提醒。",
        input_schema={
            "type": "object",
            "properties": {
                "person_id": {"type": "string", "description": "记录 ID；留空表示今天所有人"},
                "action": {
                    "type": "string",
                    "enum": ["greeted", "stop_today"],
                    "description": "greeted=已祝福；stop_today=今天别再提醒",
                },
                "note": {"type": "string", "description": "备注"},
            },
        },
    )
    async def acknowledge(self, person_id: str = "", action: str = "greeted", note: str = "", **_):
        action = action if action in _ACK_ACTIONS else "greeted"
        now = self._now()
        with self._lock:
            targets: list[dict[str, Any]] = []
            if person_id:
                target = next((p for p in self._data.get("people", []) if p.get("id") == person_id), None)
                if target is None:
                    return Err(SdkError(f"未找到记录：{person_id}"))
                targets = [enrich_person(target, now.date())]
            else:
                targets = self._active_people(now)
                if not targets and action != "stop_today":
                    return Err(SdkError("今天没有待确认的生日提醒"))
            names = mark_acknowledged(self._data, targets, action, note=note, now=now)
            self._save_locked()
        label = "今天停止提醒" if action == "stop_today" else "已祝福"
        return Ok({"acked": names, "action": action, "message": f"{label}：{'、'.join(names)}"})

    @ui.action(id="stop_today", label="今天别再提醒", tone="warning")
    @plugin_entry(
        id="stop_today",
        name="今天停止提醒",
        description="今天不再发送任何生日提醒（包括提前送礼提醒）。",
    )
    async def stop_today(self, **_):
        now = self._now()
        with self._lock:
            targets = self._active_people(now)
            names = mark_acknowledged(self._data, targets, "stop_today", now=now)
            if not targets:
                state = day_state(self._data, now.date())
                state["stopped"] = True
            self._save_locked()
        return Ok({"stopped": True, "people": names, "message": "今天不会再提醒生日了"})

    @ui.action(id="save_settings", label="保存设置", tone="primary")
    @plugin_entry(
        id="save_settings",
        name="保存提醒设置",
        description="保存生日提醒的时间、重复间隔和提前送礼天数等设置。",
        input_schema={
            "type": "object",
            "properties": {
                "settings": {
                    "type": "object",
                    "description": "要更新的设置字段",
                    "properties": {
                        "remind_after": {"type": "string"},
                        "quiet_after": {"type": "string"},
                        "repeat_minutes": {"type": "integer"},
                        "max_reminders": {"type": "integer"},
                        "gift_advance_days": {"type": "integer"},
                        "context_cue": {"type": "boolean"},
                        "scan_conversations": {"type": "boolean"},
                        "timezone": {"type": "string"},
                    },
                }
            },
            "required": ["settings"],
        },
    )
    async def save_settings(self, settings: Mapping[str, Any] | None = None, **_):
        if not isinstance(settings, Mapping):
            return Err(SdkError("settings 必须是一个对象"))
        with self._lock:
            current = normalize_settings(self._data.get("settings"))
            raw_times = {
                "remind_after": settings.get("remind_after", current["remind_after"]),
                "quiet_after": settings.get("quiet_after", current["quiet_after"]),
            }
            if clock_minutes(raw_times["quiet_after"], "22:30") <= clock_minutes(raw_times["remind_after"], "08:30"):
                return Err(SdkError("停止提醒的时间必须晚于开始提醒的时间"))
            updated = settings_with_updates(current, settings)
            self._data["settings"] = updated
            self._save_locked()
        return Ok({"settings": updated, "message": "提醒设置已保存"})

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @plugin_entry(
        id="debug_status",
        name="调试状态",
        description="返回生日提醒的内部状态，便于排查提醒为什么没有触发。",
    )
    async def debug_status(self, **_):
        now = self._now()
        with self._lock:
            state = peek_day_state(self._data, now.date())
            settings = normalize_settings(self._data.get("settings"))
            return Ok({
                "now": now.isoformat(timespec="seconds"),
                "timezone": str(settings.get("timezone")),
                "settings": settings,
                "day_state": state,
                "today": [p["name"] for p in list_today(self._data, now.date())],
                "prep_due": [p["name"] for p in list_prep_due(self._data, now.date())],
                "people": len(self._data.get("people", [])),
                "data_path": str(self._data_path),
            })
