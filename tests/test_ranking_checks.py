"""AI 标签/理由与日线事实核对（数据取自新浪 2026-10-30 真实日线）。"""

from types import SimpleNamespace as N

from backend.app.services.ranking_checks import check_claims, facts_from_bars


def _bars(closes, opens=None, volumes=None):
    opens = opens or [c * 0.995 for c in closes]
    volumes = volumes or [1000.0] * len(closes)
    return [N(open=o, high=max(o, c) * 1.005, low=min(o, c) * 0.995, close=c, volume=v) for c, o, v in zip(closes, opens, volumes)]


def test_ma_bull_false_is_flagged():
    # 形如 000598：MA5>MA10 但 MA10<MA20
    closes = [7.3] * 10 + [7.0, 6.95, 6.98, 7.0, 6.97, 7.02, 7.0, 7.1, 7.15, 7.2]
    facts = facts_from_bars(_bars(closes))
    assert facts["ma_bull"] is False
    issues = check_claims(["均线多头"], "MA5/10/20保持多头", facts)
    assert issues and issues[0]["claim"] == "均线多头" and "MA10" in issues[0]["fact"]


def test_up_streak_claims():
    rising = facts_from_bars(_bars([10 + i * 0.1 for i in range(25)]))
    assert check_claims([], "五日连续收涨，接近20日高点", rising) == []
    last_down = facts_from_bars(_bars([10 + i * 0.1 for i in range(24)] + [12.2]))
    issues = check_claims([], "连续多日上涨", last_down)
    assert issues and "连续收涨 0 天" in issues[0]["fact"]


def test_near_high_and_volume_claims():
    fallen = facts_from_bars(_bars([12.0] * 5 + [11.0] * 15, volumes=[1000.0] * 19 + [500.0]))
    claims = {item["claim"] for item in check_claims(["温和放量"], "距离近期高点较近", fallen)}
    assert claims == {"接近 20 日高点", "放量"}
    assert check_claims(["缩量回调"], "", fallen) == []
