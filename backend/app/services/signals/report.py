"""验证报告：从事件表重放，算 T+1~T+5 超额收益、显著性、校准、分市场状态。

验证永远在事件表上重放，不用当天收盘后的聚合数回算。基准 = 同期全市场等权中位数收益。
"""

from __future__ import annotations

from datetime import date

from backend.app.models.tables import DailyQuote, SignalEvent, SignalOutcome, Stock
from backend.app.providers.markets import is_in_scope
from backend.app.services.retention import quote_dates
from backend.app.services.signals import fill, stats
from backend.app.services.signals.event_study import summary
from backend.app.services.signals.market_state import state_key

HOLDS = (1, 2, 3, 4, 5)


def _benchmark_return(session, day: date, hold: int, days: list[date]) -> float | None:
    """信号日收盘买入、持有 hold 个交易日后的同期全市场等权中位数收益（%）。

    先算每只股票的信号日收盘 → 卖出日收盘的收益，再取中位数。卖出日用信号日后第 hold 个交易日。
    """
    if hold < 1:
        return None
    idx = days.index(day) if day in days else None
    if idx is None or idx + hold >= len(days):
        return None
    sell_day = days[idx + hold]
    rows = session.query(DailyQuote).filter(DailyQuote.trade_date == day).all()
    sells = {
        row.stock_code: row
        for row in session.query(DailyQuote).filter(DailyQuote.trade_date == sell_day).all()
    }
    returns = []
    for row in rows:
        if not is_in_scope(row.stock_code):
            continue
        if not row.close or not row.pre_close:
            continue
        sell = sells.get(row.stock_code)
        if sell is None or not sell.close:
            continue
        returns.append((sell.close - row.close) / row.close * 100)
    if not returns:
        return None
    ordered = sorted(returns)
    return ordered[len(ordered) // 2]


def _bars_for(session, code: str, signal_day: date, days: list[date], hold: int) -> list[dict] | None:
    idx = days.index(signal_day) if signal_day in days else None
    if idx is None or idx + hold >= len(days):
        return None
    wanted = days[idx : idx + hold + 1]
    rows = {
        row.trade_date: row
        for row in session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date.in_(wanted))
        .all()
    }
    out = []
    for d in wanted:
        row = rows.get(d)
        if row is None:
            return None
        out.append(
            {
                "code": code,
                "trade_date": d,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "limit_up_price": row.limit_up_price,
                "limit_down_price": row.limit_down_price,
            }
        )
    return out


def settle_event(session, event: SignalEvent, days: list[date]) -> dict[str, dict]:
    """给一条事件补 T+1~T+5 结果。返回 {hold: outcome dict}。已算好的直接复用。"""
    outcomes: dict[int, dict] = {}
    existing = {
        row.hold: row
        for row in session.query(SignalOutcome).filter(SignalOutcome.signal_id == event.signal_id).all()
    }
    for hold in HOLDS:
        if hold in existing and existing[hold].net_return_pct is not None:
            row = existing[hold]
            outcomes[hold] = {
                "buyable": row.buyable,
                "sellable": row.sellable,
                "net_return_pct": row.net_return_pct,
                "benchmark_return_pct": row.benchmark_return_pct,
                "excess_return_pct": row.excess_return_pct,
                "hit": row.hit,
            }
            continue
        bars = _bars_for(session, event.stock_code, event.signal_date, days, hold)
        benchmark = _benchmark_return(session, event.signal_date, hold, days)
        if bars is None:
            outcome = None
            buyable = sellable = None
            net = None
        else:
            buyable = fill.buyable(bars[0])
            sellable = fill.sellable(bars[hold])
            result = fill.outcome(bars, hold)
            net = result["net_return_pct"] if result else None
            outcome = result
        excess = (net - benchmark) if (net is not None and benchmark is not None) else None
        hit = (net is not None and net > 0) if net is not None else None
        out = {
            "buyable": buyable,
            "sellable": sellable,
            "net_return_pct": net,
            "benchmark_return_pct": benchmark,
            "excess_return_pct": excess,
            "hit": hit,
        }
        session.add(
            SignalOutcome(
                signal_id=event.signal_id,
                hold=hold,
                buyable=buyable,
                sellable=sellable,
                net_return_pct=net,
                benchmark_return_pct=benchmark,
                excess_return_pct=excess,
                hit=hit,
            )
        )
        outcomes[hold] = out
    session.commit()
    return outcomes


def build_report(session, signal_type: str | None = None, *, settle: bool = True) -> dict:
    """按信号类型产出完整有效性报告。signal_type 为空时汇总全部。"""
    from backend.app.services.signals.calibration_report import report as calibration
    from backend.app.services.signals.stats import bh_adjusted, holm_adjusted, sign_pvalue

    query = session.query(SignalEvent)
    if signal_type:
        query = query.filter(SignalEvent.signal_type == signal_type)
    events = query.order_by(SignalEvent.signal_date, SignalEvent.id).all()
    if not events:
        return {
            "signal_type": signal_type or "all",
            "n_events": 0,
            "empty": True,
            "disclaimer": "还没有信号事件。先在影子模式下积累样本。",
        }
    days = quote_dates(session)
    names = {row.code: row.name for row in session.query(Stock)}
    per_signal: dict[str, dict] = {}
    market_states: dict[str, list[dict]] = {}
    for event in events:
        outcomes = settle_event(session, event, days) if settle else {}
        ctx = {}
        for hold in HOLDS:
            o = outcomes.get(hold) or {}
            for key in ("net_return_pct", "benchmark_return_pct", "excess_return_pct", "hit"):
                ctx[f"{key}_{hold}"] = o.get(key)
        row = {
            "signal_id": event.signal_id,
            "signal_date": event.signal_date.isoformat(),
            "signal_type": event.signal_type,
            "code": event.stock_code,
            "name": names.get(event.stock_code, ""),
            "confidence": event.confidence,
            "model": event.model,
            "prompt_version": event.prompt_version,
            "temperature": event.temperature,
        }
        row.update(ctx)
        per_signal[event.signal_id] = row
        market_states.setdefault(event.signal_type, []).append(
            {
                "signal_type": event.signal_type,
                "signal_date": event.signal_date,
                "state": state_key(_market_ctx(session, event.signal_date)),
                **ctx,
            }
        )

    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event.signal_type, []).append(per_signal[event.signal_id])

    report_by_type = {}
    for sig_type, rows in by_type.items():
        holds = {}
        pvalues = []
        hold_labels = []
        for hold in HOLDS:
            excess_key = f"excess_return_pct_{hold}"
            raw_key = f"net_return_pct_{hold}"
            excess = [r[excess_key] for r in rows if r.get(excess_key) is not None]
            raw = [r[raw_key] for r in rows if r.get(raw_key) is not None]
            s = summary(excess)
            pv = sign_pvalue(excess, alternative="greater")["p_value"]
            pvalues.append(pv if pv is not None else 1.0)
            hold_labels.append(f"T+{hold}")
            holds[hold] = {
                "n": s["n"],
                "excess_mean": s["mean"],
                "excess_median": s["median"],
                "win_rate": s["win_rate"],
                "profit_loss_ratio": s["profit_loss_ratio"],
                "t_stat": s["t_stat"],
                "raw_mean": summary(raw)["mean"],
                "p_value": pv,
            }
        bh = bh_adjusted(pvalues)
        holm = holm_adjusted(pvalues)
        for i, hold in enumerate(HOLDS):
            holds[hold]["p_bh"] = bh[i]
            holds[hold]["p_holm"] = holm[i]
            holds[hold]["significant_bh"] = bh[i] < 0.05
            holds[hold]["significant_holm"] = holm[i] < 0.05
        probs = [r["confidence"] for r in rows if r.get("confidence") is not None]
        labels = [1 if r.get("hit_1") else 0 for r in rows if r.get("confidence") is not None and r.get("hit_1") is not None]
        cal = calibration([float(p) / 100.0 for p in probs], labels) if probs and len(probs) == len(labels) else None
        report_by_type[sig_type] = {
            "signal_type": sig_type,
            "n_events": len(rows),
            "holds": holds,
            "calibration": cal,
            "market_states": _market_state_summary(market_states.get(sig_type, [])),
        }

    return {
        "signal_type": signal_type or "all",
        "n_events": len(events),
        "empty": False,
        "disclaimer": "相似度 / AI 置信度都不是概率；本报告产出的是历史统计，不构成投资建议。",
        "events": list(per_signal.values()),
        "by_type": report_by_type,
        "hold_labels": [f"T+{hold}" for hold in HOLDS],
    }


def _market_ctx(session, day: date) -> dict:
    from backend.app.services.ai_context import market_context

    return market_context(session, day)


def _market_state_summary(rows: list[dict]) -> dict:
    groups: dict[str, list[float]] = {}
    for row in rows:
        excess = row.get("excess_return_pct_1")
        if excess is None:
            continue
        groups.setdefault(row["state"], []).append(excess)
    return {state: summary(values) for state, values in groups.items()}


def list_signal_types(session) -> list[dict]:
    rows = session.query(SignalEvent.signal_type).distinct().all()
    return [{"signal_type": row[0]} for row in rows]
