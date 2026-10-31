"""设置保存后各处立即生效（不重启、不吃旧缓存），以及迁移与校验。"""

import json

from backend.app.core import config


def test_user_values_38_30_08_90_save(client):
    body = {"retention_trading_days": 38, "sample_pool_trading_days": 30, "euclidean_similarity_weight": 0.8,
            "cosine_similarity_weight": 0.2, "similarity_threshold_high": 90}
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["retention_trading_days"] == 38 and view["euclidean_similarity_weight"] == 0.8 and view["cosine_similarity_weight"] == 0.2


def test_floor_and_max_messages(client):
    low = client.put("/api/settings", json={"retention_trading_days": 37})
    assert low.status_code == 422 and "38" in low.json()["detail"]
    assert client.put("/api/settings", json={"retention_trading_days": 251}).status_code == 422
    # 样本池 60 时下限 68，同一次保存里一起改也按新样本池算
    assert client.put("/api/settings", json={"sample_pool_trading_days": 60, "retention_trading_days": 60}).status_code == 422
    assert client.put("/api/settings", json={"sample_pool_trading_days": 60, "retention_trading_days": 68}).status_code == 200


def test_fresh_defaults_are_60(client):
    view = client.get("/api/settings").json()
    assert view["retention_trading_days"] == 60 and view["sample_pool_trading_days"] == 30
    assert view["euclidean_similarity_weight"] == 0.8 and view["similarity_threshold_high"] == 90


def test_euclid_only_maps_to_cosine_complement(client):
    r = client.put("/api/settings", json={"euclidean_similarity_weight": 0.7})
    assert r.status_code == 200
    assert config.similarity_weights() == (0.7, 0.3) or tuple(round(x, 4) for x in config.similarity_weights()) == (0.7, 0.3)


def test_partial_save_keeps_other_fields(client):
    client.put("/api/settings", json={"ai_base_url": "https://x.example/v1", "ai_model": "m1", "ai_api_key": "secret-123456",
                                      "ai_batch_size": 7, "retention_trading_days": 80, "data_source": "tencent"})
    client.put("/api/settings", json={"similarity_threshold_high": 85})
    raw = config.read_settings_file()
    assert raw["ai_base_url"] == "https://x.example/v1" and raw["ai_model"] == "m1" and raw["ai_api_key"] == "secret-123456"
    assert raw["ai_batch_size"] == 7 and raw["retention_trading_days"] == 80 and raw["data_source"] == "tencent"
    assert raw["similarity_threshold_high"] == 85
    client.put("/api/settings", json={"ai_api_key": ""})  # 空密钥不清掉
    assert config.read_settings_file()["ai_api_key"] == "secret-123456"


def test_consumers_see_new_values_without_restart(client):
    from backend.app.services import sync_service
    from backend.app.core.config import ai_int, calc_history_days, retention_days, sample_pool_days, similarity_threshold_high

    client.put("/api/settings", json={"sample_pool_trading_days": 20, "retention_trading_days": 45, "similarity_threshold_high": 80,
                                      "euclidean_similarity_weight": 0.6, "cosine_similarity_weight": 0.4,
                                      "ai_timeout_seconds": 200, "ai_batch_size": 5, "ai_parallel": 2,
                                      "alert_trading_hours_only": False, "data_source": "sina", "ai_thinking": "off"})
    assert sample_pool_days() == 20 and retention_days() == 45 and similarity_threshold_high() == 80
    assert tuple(round(x, 4) for x in config.similarity_weights()) == (0.6, 0.4)
    assert ai_int(None, "ai_timeout_seconds") == 200 and ai_int(None, "ai_batch_size") == 5 and ai_int(None, "ai_parallel") == 2
    raw = config.read_settings_file()
    assert raw["alert_trading_hours_only"] is False and raw["data_source"] == "sina" and raw["ai_thinking"] == "off"
    assert calc_history_days() == 93  # max(20,30)+3+60
    from datetime import date
    start = sync_service.history_start(date(2026, 10, 30))
    assert (date(2026, 10, 30) - start).days >= 93 * 7 // 5
    assert client.get("/api/settings").json()["sample_pool_trading_days"] == 20


def test_040_settings_file_below_60_migrates_once(client):
    old = {"retention_trading_days": 40, "sample_pool_trading_days": 30, "settings_schema": 2, "ai_model": "keep-me",
           "euclidean_similarity_weight": 0.75, "cosine_similarity_weight": 0.25}
    config.settings_path().write_text(json.dumps(old), encoding="utf-8")
    config._MIGRATED.clear()
    view = client.get("/api/settings").json()
    assert view["retention_trading_days"] == 60 and view["ai_model"] == "keep-me" and view["euclidean_similarity_weight"] == 0.75
    # 迁移之后用户再改成 38 要尊重
    assert client.put("/api/settings", json={"retention_trading_days": 38}).status_code == 200
    config._MIGRATED.clear()
    assert client.get("/api/settings").json()["retention_trading_days"] == 38


def test_position_60_uses_archived_bars_after_purge(client):
    """明细只留 38 天，但超出部分转存 calc_bars，position_60 仍能看到 60 根。"""
    from backend.app.core.database import session_scope
    from backend.app.models.tables import CalcBar, DailyQuote
    from backend.app.services.history_backfill import load_bars
    from backend.app.services.retention import purge_expired
    from tests.test_api import _save, _series, weekdays

    client.put("/api/settings", json={"retention_trading_days": 38})
    days = weekdays(100)
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={50}))
    purge_expired()
    with session_scope() as s:
        assert s.query(DailyQuote).count() == 38
        assert s.query(CalcBar).count() >= 93 - 38
        bars = load_bars(s, "600000", days[-1])
        assert len(bars) >= 93
