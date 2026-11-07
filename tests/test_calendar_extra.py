"""日历：官方公告核对过的日子 + 推算年份自检。"""

import json
from datetime import date

from backend.app.core import calendar


def test_official_2025_2026_closures():
    for day in ("2025-01-01", "2025-01-28", "2025-02-04", "2025-04-04", "2025-05-05", "2025-06-02", "2025-10-08",
                "2026-01-02", "2026-02-16", "2026-02-23", "2026-04-06", "2026-05-05", "2026-06-19", "2026-09-25", "2026-10-07"):
        assert not calendar.is_trading_day(date.fromisoformat(day)), day
    for day in ("2025-02-05", "2025-10-09", "2026-01-05", "2026-02-24", "2026-10-08", "2026-09-28"):
        assert calendar.is_trading_day(date.fromisoformat(day)), day
    assert calendar.calendar_info(2026)["source"].startswith("上证公告〔2025〕45号")
    assert calendar.calendar_info(2027)["estimated"] is True


def test_observe_session_marks_estimated_year(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    from backend.app.core import config

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    day = date(2027, 3, 3)
    assert calendar.is_trading_day(day)
    assert calendar.observe_session(day, 0)["action"] == "marked_closed"
    assert not calendar.is_trading_day(day)
    assert "2027-03-03" in json.loads((tmp_path / "holidays_auto.json").read_text())["closed"]
    assert calendar.observe_session(date(2026, 3, 4), 0)["action"] == "warned"
    assert calendar.is_trading_day(date(2026, 3, 4))
    assert calendar.observe_session(date(2026, 3, 5), 100) is None
