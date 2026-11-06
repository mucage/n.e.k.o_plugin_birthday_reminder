"""Release smoke test: manifest, UI assets and pure core module.

This file is intentionally named ``test_smoke.py`` because the N.E.K.O. plugin
release validator expects it. It runs with pytest during ``neko-plugin check -r``.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_manifest_identity_and_entry():
    manifest = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))
    plugin = manifest["plugin"]
    assert plugin["id"] == "birthday_reminder"
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", plugin["version"])
    assert plugin["entry"] == "plugin.plugins.birthday_reminder:BirthdayReminderPlugin"
    assert plugin["name"]
    assert plugin["sdk"]["supported"] == ">=0.1.0,<0.3.0"
    assert plugin["ui"]["panel"][0]["context"] == "dashboard"


def test_ui_assets_exist():
    for relative in (
        "ui/panel.tsx",
        "docs/quickstart.md",
        "i18n/zh-CN.json",
        "i18n/en.json",
        "examples/生日模板.xlsx",
        "examples/示例名单.xlsx",
        ".github/workflows/verify.yml",
        ".github/workflows/release.yml",
    ):
        assert (ROOT / relative).is_file(), relative


def test_i18n_catalogs_have_the_same_keys():
    zh = json.loads((ROOT / "i18n" / "zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert set(zh) == set(en)
    assert zh["panel.title"]


def test_core_module_imports_and_parses():
    import birthday_core

    parsed = birthday_core.parse_birthday("1995-08-12")
    assert parsed["canonical"] == "1995-08-12"
    data = birthday_core.default_data()
    person = birthday_core.new_person(
        birthday_core.validate_person_fields("小林", "08-12", "喜欢咖啡", "提前提醒送礼")
    )
    data["people"].append(person)
    assert birthday_core.list_today(data, person_date(8, 12))[0]["name"] == "小林"


def person_date(month: int, day: int):
    from datetime import date

    return date(2026, month, day)
