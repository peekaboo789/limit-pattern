"""页面上那句「近 N 日」的写法：凡是跟着对比窗口跑的数，句子里不许写死天数。

对比窗口（`compare_window_trading_days`）在设置里改得动，形态窗口才是固定 3 根。
两个数以前混在一句话里写过「近三日涨停样本」，窗口一改成 5，卡片上的数变了、
标题还写着三日。这里不测数值，只守住那句标题：视图层不许再出现写死的
「近三日/近五日涨停」。
"""

from pathlib import Path

VIEWS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "views"
STORES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "stores"

#: 跟着对比窗口跑的措辞：一个都不许在视图层写死天数。
BANNED = ("近三日涨停", "近五日涨停", "三天涨停", "近3日涨停", "近5日涨停")


def test_no_view_labels_the_compare_window_with_a_hardcoded_day_count():
    assert VIEWS.is_dir(), f"找不到前端视图目录：{VIEWS}"
    hits = []
    for file in sorted(VIEWS.glob("*.vue")):
        text = file.read_text(encoding="utf-8")
        for phrase in BANNED:
            if phrase in text:
                hits.append(f"{file.name}: {phrase}")
    assert not hits, f"这些标题把对比窗口的天数写死了：{hits}"


def test_the_shared_label_helper_falls_back_instead_of_inventing_three():
    """设置还没读到时说「对比窗口内」，不许编一个 3 出来当真值。"""
    helper = (STORES / "session.ts").read_text(encoding="utf-8")
    assert "最近 ${days} 个交易日" in helper
    assert "'对比窗口内'" in helper


def test_the_fixed_shape_window_is_still_described_as_three_bars():
    """形态窗口是固定的 3 根：聚类页那几句「三日形态」是另一种东西，不许被一起改掉。"""
    clusters = (VIEWS / "ClustersView.vue").read_text(encoding="utf-8")
    assert "组内三日形态中位数" in clusters
    assert "形态三天" in clusters
