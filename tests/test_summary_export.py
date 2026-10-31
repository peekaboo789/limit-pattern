"""v0.4.7 每日总结区间导出：区间过滤、缺失日、四种格式、编码（BOM）、校验。"""

import csv
import io
import json
from datetime import date

from backend.app.core.database import session_scope
from backend.app.services.summary_service import _save_summary


def _summary(day, lu, rate, height, extra=None):
    payload = {
        "date": day, "limitup_count": lu, "headline": f"{day} 共 {lu} 只涨停", "source": "statistics", "model": "",
        "market": {"up": 1000, "down": 3000, "flat": 50, "suspended": 2, "listed": 4052, "up_down_ratio": 0.33, "median_pct": -1.2,
                   "limit_up": lu, "limit_down": 9, "broken": 10, "broken_rate": rate, "max_height": height, "leaders": ["600825"]},
        "market_review": {"sentiment": "偏弱", "height": f"最高 {height} 连板"},
        "key_points": [f"{day} 要点一"], "sectors": [{"name": "主板", "count": lu}],
        "ladder": [{"height": 1, "count": lu - 1, "codes": ["600000"]}, {"height": height, "count": 1, "codes": ["600825"]}],
        "generated_at": f"{day}T15:30:00+08:00",
    }
    payload.update(extra or {})
    return payload


def _seed():
    with session_scope() as s:
        # 2024 年的远早于日线保留期也要能导出
        _save_summary(s, date(2024, 3, 1), _summary("2024-03-01", 30, 20.0, 3))
        _save_summary(s, date(2026, 10, 26), _summary("2026-10-26", 50, 30.5, 4))
        _save_summary(s, date(2026, 10, 27), _summary("2026-10-27", 60, 25.0, 5, {"summary": "AI 总结：情绪回暖。", "custom_field": {"x": 1}}))
        # 10-28 缺
        _save_summary(s, date(2026, 10, 29), _summary("2026-10-29", 40, 40.0, 6))
        _save_summary(s, date(2026, 10, 30), _summary("2026-10-30", 47, 39.7, 5))


def _get(client, **params):
    return client.get("/api/daily-summary/export", params=params)


def test_txt_range_missing_and_bom(client):
    _seed()
    r = _get(client, start="2026-10-26", end="2026-10-29")
    assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf")
    assert "每日总结_2026-10-26_2026-10-29.txt" in __import__("urllib.parse").parse.unquote(r.headers["content-disposition"])
    text = r.content.decode("utf-8-sig")
    assert "\r\n" in text
    assert "有总结：3 天" in text and "缺总结：1 天（2026-10-28）" in text and "4 个交易日" in text
    assert text.index("2026-10-26") < text.index("==== 2026-10-27") < text.index("==== 2026-10-29")
    assert "2026-10-30" not in text.split("区间汇总")[0].split("导出时间")[1]  # 区间外的不导
    assert "AI 总结：情绪回暖。" in text and "custom_field" in text and "炸板率 30.5%" in text and "区间汇总" in text
    assert r.headers["x-summary-count"] == "3" and r.headers["x-summary-missing"] == "1"


def test_md_json_csv(client):
    _seed()
    md = _get(client, start="2026-10-26", end="2026-10-30", format="md")
    assert md.status_code == 200 and not md.content.startswith(b"\xef\xbb\xbf") and md.text.startswith("# 每日总结")
    assert "| 2026-10-29 | 40 | 9 | 40.0 | 6 |" in md.text
    js = _get(client, start="2026-10-26", end="2026-10-30", format="json")
    doc = json.loads(js.content.decode("utf-8"))  # JSON 不带 BOM
    assert [i["date"] for i in doc["items"]] == ["2026-10-26", "2026-10-27", "2026-10-29", "2026-10-30"]
    assert doc["missing"] == ["2026-10-28"] and doc["items"][1]["custom_field"] == {"x": 1}
    c = _get(client, start="2026-10-26", end="2026-10-30", format="csv")
    assert c.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(c.content.decode("utf-8-sig"))))
    assert rows[0][:6] == ["日期", "涨停", "跌停", "炸板", "炸板率%", "最高连板"] and len(rows) == 5
    assert rows[4][:6] == ["2026-10-30", "47", "9", "10", "39.7", "5"]


def test_old_summaries_and_all(client):
    _seed()
    r = _get(client, start="2024-03-01", end="2024-03-01")
    assert r.status_code == 200 and "有总结：1 天" in r.content.decode("utf-8-sig")
    assert client.get("/api/daily-summary/range").json()["earliest"] == "2024-03-01"
    everything = _get(client, format="json").json()  # 不给日期 = 全部
    assert everything["start"] == "2024-03-01" and everything["count"] == 5


def test_validation(client):
    _seed()
    bad = _get(client, start="2026-10-30", end="2026-10-01")
    assert bad.status_code == 422 and "晚于" in bad.json()["detail"]
    assert _get(client, start="2020-01-01", end="2026-10-30").status_code == 422  # 超过 5 年
    assert _get(client, start="2026-13-01", end="2026-10-30").status_code == 422
    assert _get(client, start="2026-10-01", end="2026-10-30", format="pdf").status_code == 422


def test_empty_range_still_exports_header(client):
    _seed()
    r = _get(client, start="2026-10-12", end="2026-10-16")
    text = r.content.decode("utf-8-sig")
    assert r.status_code == 200 and "有总结：0 天" in text and "缺总结：5 天" in text


def test_no_summaries_at_all(client):
    assert _get(client).status_code == 404
