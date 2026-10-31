"""研究员提示词、综合数据、容错解析。"""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from backend.app.services import ranking_service
from backend.app.services.ai_context import market_line, stock_line, stock_metrics
from backend.app.services.summary_service import AiFailed, _extract_json


def _bars(n=40):
    out, close = [], 10.0
    start = date(2026, 8, 3)
    for i in range(n):
        pre = close
        close = round(pre * (1.1 if i in (30, 31) else 1.01), 2)
        out.append(SimpleNamespace(
            trade_date=start + timedelta(days=i), open=pre, high=close * 1.01, low=pre * 0.99, close=close, pre_close=pre,
            volume=1000 + i * 10, turnover_rate=2.0, change_percent=round((close / pre - 1) * 100, 2),
            is_limit_up=i in (30, 31), limit_up_price=round(pre * 1.1, 2), is_limit_down=False,
        ))
    return out


def test_metrics_no_future_and_fields():
    bars = _bars()
    m = stock_metrics(bars, 30)
    assert m["limit_count"] == 2 and m["streak"] == 0 and m["last_limit"] == bars[31].trade_date.isoformat()
    assert len(m["recent"]) == 5 and m["ma"][20]["dev"] is not None and 0 <= m["pos20"] <= 1
    early = stock_metrics(bars[:31], 30)
    assert early["limit_count"] == 1 and early["streak"] == 1  # 只看到评估日为止
    line = stock_line({"code": "600000", "name": "示例", "score": 88, "shape_score": 80, "volume_score": 70, "volume_pct_score": 60,
                       "top_matches": [{"code": "000001", "limit_date": "2026-09-01", "score": 91}]}, m)
    assert "000001@09-01:91" in line and "连板 0" in line and len(line) < 600


def test_prompt_persona_and_batch_size():
    assert "资深 A 股短线交易与涨停板研究员" in ranking_service.SYSTEM_PROMPT
    assert "不是涨停概率" in ranking_service.SYSTEM_PROMPT and "风险扣分" in ranking_service.SYSTEM_PROMPT
    m = stock_metrics(_bars(), 30)
    items = [{"code": f"6000{i:02d}", "name": "示例", "score": 80, "shape_score": 1, "volume_score": 1, "volume_pct_score": 1, "metrics": m} for i in range(20)]
    ctx = {"date": "2026-10-30", "stocks": 10, "up": 6, "down": 4, "up_down_ratio": 1.5, "median_pct": 0.3,
           "limit_up": 2, "limit_down": 0, "broken": 1, "broken_rate": 33.3, "max_height": 2, "leaders": ["600000"]}
    prompt = ranking_service._prompt(date(2026, 10, 30), items, market_line(ctx))
    assert "炸板率 33.3%" in prompt and prompt.count("600000 示例") == 1
    assert len(prompt) < 14000  # 20 只一批约 5k token 以内


def test_parse_fenced_missing_subscores_and_trailing_comma():
    text = "<think>先分析</think>好的：\n```json\n" + json.dumps({"items": [
        {"code": "600000", "sub": {"kline": 80, "volume": 60}, "risk_deduct": 5, "reason": "缩量回调", "tags": "缩量回调、均线多头"},
        {"code": "sz000001", "score": 120, "reason": "x"},
        {"code": "000002"},
    ]}, ensure_ascii=False)[:-2] + "],}\n```"
    scores = ranking_service._parse_scores(text)
    first = scores["600000"]
    assert first["score"] == 70  # 旧格式只有 sub：取各维度均值当模型自评，只作对照
    assert first["tags"] == ["缩量回调", "均线多头"]
    assert scores["000001"]["score"] == 100
    assert "000002" not in scores


def test_extract_json_errors():
    with pytest.raises(AiFailed):
        _extract_json("没有 JSON")
