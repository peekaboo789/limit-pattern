from datetime import datetime

from backend.app.core.clock import SHANGHAI
from backend.app.services.intraday_service import elapsed_minutes, scale_partial, volume_fraction


def _at(h, m):
    return datetime(2026, 10, 30, h, m, tzinfo=SHANGHAI)


def test_elapsed_excludes_lunch():
    assert elapsed_minutes(_at(9, 0)) == 0
    assert elapsed_minutes(_at(10, 30)) == 60
    assert elapsed_minutes(_at(12, 15)) == 120
    assert elapsed_minutes(_at(14, 0)) == 180
    assert elapsed_minutes(_at(15, 30)) == 240


def test_u_profile_front_loaded_and_monotonic():
    assert volume_fraction(0) == 0 and abs(volume_fraction(240) - 1) < 1e-9
    assert abs(volume_fraction(120) - 0.5) < 1e-9
    assert volume_fraction(30) > 30 / 240  # 开盘活跃
    values = [volume_fraction(t) for t in range(0, 241, 10)]
    assert values == sorted(values)
    assert volume_fraction(60, "linear") == 0.25


def test_scale_partial():
    row = scale_partial({"volume": 100.0, "amount": 1000.0, "turnover_rate": 1.0}, 0.5)
    assert row["volume"] == 200 and row["amount"] == 2000 and row["turnover_rate"] == 2 and row["volume_estimated"]
    assert "volume_estimated" not in scale_partial({"volume": 1.0}, 1.0)
