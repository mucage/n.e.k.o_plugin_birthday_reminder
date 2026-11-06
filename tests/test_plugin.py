"""Smoke tests for the plugin class using a tiny SDK stub."""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _paths import ROOT
from birthday_reminder import BirthdayReminderPlugin
from birthday_reminder.birthday_core import default_data

TZ = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 6, hour, minute, tzinfo=TZ)


class HostCtx:
    current_lanlan = "小八"


class Ctx:
    plugin_id = "birthday_reminder"
    metadata = {}
    bus = None

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._host_ctx = HostCtx()
        self.pushed: list[dict] = []

    def push_message(self, **kwargs):
        self.pushed.append(kwargs)
        return {"submitted": True}


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ctx = Ctx(self.tempdir.name)
        self.plugin = BirthdayReminderPlugin(self.ctx)
        self.plugin._data = default_data()
        self.plugin._data_path = Path(self.tempdir.name) / "birthdays.json"
        self.plugin._now = lambda: at(8, 30)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_startup_and_shutdown(self):
        result = await self.plugin.startup()
        self.assertEqual(result.value["status"], "ready")
        self.assertTrue(self.plugin._data_path.exists())
        task = self.plugin._startup_task
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        shutdown = await self.plugin.shutdown()
        self.assertEqual(shutdown.value["status"], "shutdown")

    async def test_add_and_dashboard(self):
        result = await self.plugin.add_person(name="小林", birthday="11-06", note="喜欢咖啡", tags=["special_care"])
        self.assertIsInstance(result, object)
        self.assertEqual(result.value["person"]["name"], "小林")
        dashboard = await self.plugin.dashboard()
        self.assertEqual(dashboard["stats"]["total"], 1)
        self.assertEqual(dashboard["stats"]["today"], 1)
        self.assertTrue(dashboard["today"][0]["is_today"])
        self.assertEqual(dashboard["today"][0]["tags"], ["special_care"])

    async def test_tick_sends_reminder_and_ack_stops_it(self):
        await self.plugin.add_person(name="小林", birthday="1995-11-06", tags=["special_care"])
        await self.plugin._tick()
        self.assertEqual(len(self.ctx.pushed), 1)
        push = self.ctx.pushed[0]
        self.assertEqual(push["ai_behavior"], "respond")
        self.assertEqual(push["visibility"], [])
        self.assertEqual(push["target_lanlan"], "小八")
        self.assertIn("小林", push["parts"][0]["text"])
        self.assertIn("birthday_ack", push["parts"][0]["text"])

        ack = await self.plugin.birthday_ack(name="小林", action="greeted", note="已经发了")
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["acked"], ["小林"])

        self.plugin._now = lambda: at(9, 30)
        await self.plugin._tick()
        self.assertEqual(len(self.ctx.pushed), 1)

    async def test_context_cue(self):
        await self.plugin.add_person(name="小林", birthday="11-06")
        self.plugin._now = lambda: at(0, 6)
        await self.plugin._tick()
        self.assertEqual(len(self.ctx.pushed), 1)
        self.assertEqual(self.ctx.pushed[0]["ai_behavior"], "read")
        self.assertIn("上下文", self.ctx.pushed[0]["parts"][0]["text"])

        self.plugin._now = lambda: at(0, 7)
        await self.plugin._tick()
        self.assertEqual(len(self.ctx.pushed), 1)

    async def test_stop_today(self):
        await self.plugin.add_person(name="小林", birthday="11-06")
        result = await self.plugin.stop_today()
        self.assertTrue(result.value["stopped"])
        await self.plugin._tick()
        self.assertEqual(self.ctx.pushed, [])

    async def test_test_reminder_does_not_consume_quota(self):
        await self.plugin.add_person(name="小林", birthday="11-06")
        result = await self.plugin.test_reminder()
        self.assertEqual(result.value["people"], ["小林"])
        self.assertEqual(len(self.ctx.pushed), 1)
        state = self.plugin._data["state"]["days"].get("2026-11-06", {})
        self.assertEqual(state.get("reminders", 0), 0)

    async def test_import_csv_base64(self):
        content = "姓名,生日,备注,标记\n小林,1995-08-12,咖啡,提前提醒送礼\n阿澄,08-12,同学,特别关心\n".encode("utf-8-sig")
        result = await self.plugin.import_xlsx(
            filename="birthdays.csv",
            content_base64=base64.b64encode(content).decode("ascii"),
        )
        self.assertEqual(result.value["added"], 2)
        self.assertEqual(result.value["updated"], 0)
        dashboard = await self.plugin.dashboard()
        self.assertEqual(dashboard["stats"]["total"], 2)

    async def test_import_xlsx_sample(self):
        sample = ROOT / "examples" / "示例名单.xlsx"
        if not sample.exists():
            self.skipTest("sample workbook missing")
        result = await self.plugin.import_xlsx(
            filename=sample.name,
            content_base64=base64.b64encode(sample.read_bytes()).decode("ascii"),
        )
        self.assertEqual(result.value["added"], 4, result.value)
        dashboard = await self.plugin.dashboard()
        self.assertEqual(dashboard["stats"]["total"], 4)
        summer = [p for p in dashboard["people"] if p["name"] == "小夏"][0]
        self.assertEqual(summer["birthday"], "2000-02-29")

    async def test_duplicate_and_invalid(self):
        await self.plugin.add_person(name="小林", birthday="11-06")
        duplicate = await self.plugin.add_person(name="小林", birthday="11-06")
        self.assertIsInstance(duplicate, object)
        self.assertTrue(hasattr(duplicate, "error"))
        invalid = await self.plugin.add_person(name="阿澄", birthday="不是日期")
        self.assertTrue(hasattr(invalid, "error"))

    async def test_update_and_delete(self):
        created = await self.plugin.add_person(name="小林", birthday="11-06", note="旧备注")
        person_id = created.value["person"]["id"]
        updated = await self.plugin.update_person(person_id=person_id, note="新备注", tags=["gift_advance"])
        self.assertEqual(updated.value["person"]["note"], "新备注")
        deleted = await self.plugin.delete_person(person_id=person_id)
        self.assertEqual(deleted.value["deleted"], person_id)
        dashboard = await self.plugin.dashboard()
        self.assertEqual(dashboard["stats"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
