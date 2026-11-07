"""Walk-forward 样本外评估：按时间滚动训练 / 评估窗口，报告样本外表现。

窗口按交易日滚动：train 是评估日之前的连续 N 天，test 是紧接着的 M 天。纯函数，输入是按日排列的事件列表。
"""

from __future__ import annotations

from datetime import date

from backend.app.services.signals.event_study import summary


def split_windows(
    events: list[dict],
    *,
    train_days: int = 30,
    test_days: int = 5,
    date_key: str = "signal_date",
) -> list[dict]:
    """把事件按日期切成一串 (train, test) 窗口。

    events 必须按日期升序。窗口互不重叠：train 覆盖 train_days 个交易日，test 覆盖其后 test_days 个交易日。
    返回 [{"train": [...], "test": [...], "test_start": date, "test_end": date}]。
    """
    ordered = sorted(events, key=lambda e: e[date_key])
    if not ordered:
        return []
    windows: list[dict] = []
    i = 0
    n = len(ordered)
    while i < n:
        train_start = ordered[i][date_key]
        # 收集 train 窗口（train_days 个不同交易日）
        train: list[dict] = []
        days: set = set()
        while i < n and len(days) < train_days:
            train.append(ordered[i])
            days.add(ordered[i][date_key])
            i += 1
        # 收集 test 窗口（test_days 个不同交易日）
        test: list[dict] = []
        days = set()
        while i < n and len(days) < test_days:
            test.append(ordered[i])
            days.add(ordered[i][date_key])
            i += 1
        if test:
            test_dates = sorted(e[date_key] for e in test)
            windows.append(
                {
                    "train": train,
                    "test": test,
                    "test_start": test_dates[0],
                    "test_end": test_dates[-1],
                }
            )
        _ = train_start
    return windows


def walk_forward(
    events: list[dict],
    *,
    return_key: str = "net_return_pct",
    train_days: int = 30,
    test_days: int = 5,
) -> dict:
    """滚动样本外：每个窗口用 test 窗口的表现汇总，最后给样本外均值与「达标窗口占比」。"""
    windows = split_windows(events, train_days=train_days, test_days=test_days, date_key="signal_date")
    oos: list[float] = []
    summaries: list[dict] = []
    for win in windows:
        returns = [float(e[return_key]) for e in win["test"] if e.get(return_key) is not None]
        s = summary(returns)
        if s["mean"] is not None:
            oos.append(s["mean"])
        summaries.append(
            {
                "test_start": win["test_start"].isoformat() if isinstance(win["test_start"], date) else str(win["test_start"]),
                "test_end": win["test_end"].isoformat() if isinstance(win["test_end"], date) else str(win["test_end"]),
                "n": s["n"],
                "mean": s["mean"],
                "win_rate": s["win_rate"],
            }
        )
    positive = sum(1 for v in oos if v > 0)
    return {
        "n_windows": len(windows),
        "oos_mean": round(sum(oos) / len(oos), 4) if oos else None,
        "oos_win_rate": round(positive / len(oos), 4) if oos else None,
        "positive_windows": positive,
        "windows": summaries,
    }
