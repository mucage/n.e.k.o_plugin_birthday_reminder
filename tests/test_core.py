"""Unit tests for birthday_core (no plugin SDK required)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

CORE_PATH = Path(__file__).resolve().parents[1] / "birthday_core.py"
spec = importlib.util.spec_from_file_location("birthday_core_under_test", CORE_PATH)
assert spec and spec.loader
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


class BirthdayParseTests(unittest.TestCase):
    def test_full_dates(self):
        for raw in ("1995-08-12", "1995/8/12", "1995.8.12", "1995年8月12日", "19950812"):
            parsed = core.parse_birthday(raw, today=date(2026, 11, 6))
            self.assertEqual(parsed["canonical"], "1995-08-12", raw)
            self.assertEqual((parsed["month"], parsed["day"], parsed["year"]), (8, 12, 1995))

    def test_month_day_only(self):
        for raw in ("08-12", "8/12", "8月12日", "0812"):
            parsed = core.parse_birthday(raw, today=date(2026, 11, 6))
            self.assertEqual(parsed["canonical"], "08-12", raw)
            self.assertIsNone(parsed["year"])

    def test_datetime_and_date(self):
        self.assertEqual(
            core.parse_birthday(datetime(2000, 2, 29, 10, 0), today=date(2026, 11, 6))["canonical"],
            "2000-02-29",
        )
        self.assertEqual(
            core.parse_birthday(date(1988, 1, 2), today=date(2026, 11, 6))["canonical"],
            "1988-01-02",
        )

    def test_excel_serial(self):
        self.assertEqual(
            core.parse_birthday(32874, today=date(2026, 11, 6))["canonical"],
            "1990-01-01",
        )

    def test_invalid(self):
        for raw in ("", "abc", "1995-02-30", "1995-13-01", "1800-01-01", "2030-01-01"):
            with self.assertRaises(core.BirthdayError, msg=raw):
                core.parse_birthday(raw, today=date(2026, 11, 6))

    def test_leap_day(self):
        parsed = core.parse_birthday("02-29", today=date(2026, 11, 6))
        self.assertEqual(parsed["canonical"], "02-29")
        person = {"name": "小夏", "birthday": "2000-02-29", "tags": []}
        self.assertEqual(core.next_occurrence(person, date(2026, 2, 28)), date(2026, 2, 28))
        self.assertEqual(core.next_occurrence(person, date(2026, 3, 1)), date(2027, 2, 28))


class NextOccurrenceTests(unittest.TestCase):
    def test_today_and_next_year(self):
        person = {"name": "A", "birthday": "11-06", "tags": []}
        today = date(2026, 11, 6)
        self.assertEqual(core.days_until_birthday(person, today), 0)
        self.assertEqual(core.next_occurrence(person, today), date(2026, 11, 6))
        tomorrow = date(2026, 11, 7)
        self.assertEqual(core.next_occurrence(person, tomorrow), date(2027, 11, 6))
        self.assertEqual(core.days_until_birthday(person, tomorrow), 364)

    def test_age(self):
        person = {"name": "A", "birthday": "1990-01-01", "tags": []}
        self.assertEqual(core.age_on_birthday(person, date(2026, 1, 1)), 36)
        self.assertIsNone(core.age_on_birthday({"name": "B", "birthday": "01-01", "tags": []}, date(2026, 1, 1)))


class TagsAndValidationTests(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(
            core.normalize_tags("提前提醒送礼、特别关心"),
            [core.TAG_GIFT, core.TAG_SPECIAL],
        )
        self.assertEqual(core.normalize_tags("gift, special"), [core.TAG_GIFT, core.TAG_SPECIAL])
        self.assertEqual(core.normalize_tags(["special_care"]), [core.TAG_SPECIAL])
        with self.assertRaises(core.BirthdayError):
            core.normalize_tags("不存在的标记")

    def test_validate(self):
        fields = core.validate_person_fields(" 小 林 ", "8/12", " 备注 ", "提前送礼")
        self.assertEqual(fields["name"], "小 林")
        self.assertEqual(fields["birthday"], "08-12")
        self.assertEqual(fields["tags"], [core.TAG_GIFT])
        with self.assertRaises(core.BirthdayError):
            core.validate_person_fields("", "08-12")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.data = core.default_data()
        self.data["people"] = [
            core.new_person(core.validate_person_fields("小林", "1995-11-06", "喜欢咖啡", "特别关心")),
        ]
        self.data["settings"] = core.normalize_settings({})

    def test_cue_before_reminder(self):
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 0, 6, tzinfo=core_zone()))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "cue")
        self.assertEqual(self.data["state"]["days"]["2026-11-06"]["cue_sent"], True)
        self.assertIsNone(core.plan_tick(self.data, datetime(2026, 11, 6, 0, 7, tzinfo=core_zone())))

    def test_first_and_repeat_reminders(self):
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 8, 30, tzinfo=core_zone()))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "reminder")
        self.assertEqual(plan["count"], 1)
        self.assertEqual(len(plan["today"]), 1)
        self.assertIsNone(core.plan_tick(self.data, datetime(2026, 11, 6, 9, 0, tzinfo=core_zone())))
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 9, 30, tzinfo=core_zone()))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["count"], 2)
        self.assertEqual(self.data["state"]["days"]["2026-11-06"]["reminders"], 2)

    def test_release_plan(self):
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 8, 30, tzinfo=core_zone()))
        self.assertIsNotNone(plan)
        core.release_plan(self.data, plan)
        state = self.data["state"]["days"]["2026-11-06"]
        self.assertEqual(state["reminders"], 0)
        self.assertIsNone(state["last_reminder_at"])

    def test_ack_and_stop(self):
        now = datetime(2026, 11, 6, 8, 30, tzinfo=core_zone())
        people = core.resolve_people(self.data, "小林", now.date())
        names = core.mark_acknowledged(self.data, people, "greeted", now=now)
        self.assertEqual(names, ["小林"])
        self.assertIsNone(core.plan_tick(self.data, now))
        acked = self.data["state"]["days"]["2026-11-06"]["acked"]
        self.assertEqual(len(acked), 1)
        self.assertEqual(next(iter(acked.values()))["action"], "greeted")

    def test_stop_today(self):
        now = datetime(2026, 11, 6, 8, 30, tzinfo=core_zone())
        people = core.resolve_people(self.data, "", now.date())
        core.mark_acknowledged(self.data, people, "stop_today", now=now)
        self.assertIsNone(core.plan_tick(self.data, now))

    def test_advance_gift(self):
        self.data["people"] = [
            core.new_person(core.validate_person_fields("阿澄", "1995-11-10", "", "提前提醒送礼")),
        ]
        self.data["settings"] = core.normalize_settings({"gift_advance_days": 7})
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 8, 30, tzinfo=core_zone()))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["today"], [])
        self.assertEqual([p["name"] for p in plan["prep"]], ["阿澄"])
        text = core.compose_message(plan)
        self.assertIn("还有 4 天", text)

    def test_quiet_hours(self):
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 23, 0, tzinfo=core_zone()))
        self.assertIsNone(plan)

    def test_message_contains_action_hint(self):
        plan = core.plan_tick(self.data, datetime(2026, 11, 6, 8, 30, tzinfo=core_zone()))
        text = core.compose_message(plan)
        self.assertIn("birthday_ack", text)
        self.assertIn("小林", text)
        self.assertIn("特别关心", text)


class AckTextTests(unittest.TestCase):
    def test_greeted(self):
        for text in ("我已经发过祝福了", "刚刚给她发了消息", "昨天打过电话了", "祝福过了"):
            self.assertEqual(core.detect_ack_text(text), "greeted", text)

    def test_stop(self):
        for text in ("今天别提醒了", "不用再提醒我", "别再催了", "今天不要提醒"):
            self.assertEqual(core.detect_ack_text(text), "stop_today", text)

    def test_negative(self):
        for text in ("还没发祝福", "我等下再发", "还没有打电话"):
            self.assertIsNone(core.detect_ack_text(text), text)


def core_zone():
    from datetime import timedelta, timezone

    return timezone(timedelta(hours=8))


class ImportTests(unittest.TestCase):
    def test_csv_import(self):
        data = core.default_data()
        csv_bytes = "姓名,生日,备注,标记\n小林,1995-08-12,喜欢咖啡,提前提醒送礼\n阿澄,08-12,同学,特别关心\n".encode("utf-8-sig")
        result = core.import_table(data, "birthdays.csv", csv_bytes, now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual((result.added, result.updated, len(result.errors)), (2, 0, 0))
        self.assertEqual(len(data["people"]), 2)
        self.assertEqual(data["people"][0]["tags"], [core.TAG_GIFT])
        # Re-import updates instead of duplicating.
        result = core.import_table(data, "birthdays.csv", csv_bytes, now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual((result.added, result.updated), (0, 2))
        self.assertEqual(len(data["people"]), 2)

    def test_csv_missing_header(self):
        data = core.default_data()
        csv_bytes = "小林,1995-08-12,喜欢咖啡,提前提醒送礼\n".encode("utf-8")
        result = core.import_table(data, "x.csv", csv_bytes, now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual(result.added, 1)
        self.assertTrue(result.warnings)

    def test_header_below_title_row(self):
        data = core.default_data()
        csv_bytes = "2026 生日名单\n姓名,生日,备注,标记\n小林,1995-08-12,咖啡,提前提醒送礼\n".encode("utf-8")
        result = core.import_table(data, "x.csv", csv_bytes, now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual(result.added, 1, result.to_dict())
        self.assertEqual(data["people"][0]["name"], "小林")

    def test_invalid_rows(self):
        data = core.default_data()
        csv_bytes = "姓名,生日,备注,标记\n,1995-08-12,,\n小林,不是日期,,\n".encode("utf-8")
        result = core.import_table(data, "x.csv", csv_bytes, now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual(result.added, 0)
        self.assertEqual(len(result.errors), 2)

    def test_xlsx_import(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl not installed")
        import io

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["姓名", "生日", "备注", "标记"])
        sheet.append(["小林", "1995-08-12", "喜欢咖啡", "提前提醒送礼,特别关心"])
        sheet.append(["阿澄", date(1990, 1, 2), "同学", "特别关心"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        data = core.default_data()
        result = core.import_table(data, "birthdays.xlsx", buffer.getvalue(), now=datetime(2026, 11, 6, 10, 0))
        self.assertEqual((result.added, result.updated, len(result.errors)), (2, 0, 0), result.to_dict())
        self.assertEqual(data["people"][1]["birthday"], "1990-01-02")
        self.assertEqual(data["people"][0]["tags"], [core.TAG_GIFT, core.TAG_SPECIAL])


if __name__ == "__main__":
    unittest.main()
