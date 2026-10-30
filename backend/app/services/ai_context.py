"""喂给 AI 的综合数据：全部由本地日线算出，只用评估日及以前的数据，不看未来。

每只候选压成一行（约 300 字符），市场环境每批只发一次，20 只一批约 5–6k token。
"""

from __future__ import annotations

from datetime import date

from backend.app.models.tables import DailyQuote
from backend.app.providers.markets import is_in_scope

RECENT_BARS = 5
LOOKBACK = 70

LEGEND = (
    "字段说明：近5日每根 = 涨跌%/振幅%/量比(对前5日均量)/换手%/收盘位置(0=最低,1=最高)，以 ; 分隔，最后一根为评估日；"
    "MA=收盘相对MA5/10/20的偏离%，斜率=MA较3日前变化%；"
    "位20/位60=收盘在近20/60日高低区间中的位置(0~1)，距高=距近20日最高%，60日不足按已有天数；"
    "涨停=近30个交易日涨停次数，末板=最近一次涨停日，连板=截至评估日连续涨停数，炸板=近30日触及涨停未封住次数，"
    "回撤=最近一次涨停收盘至今涨跌%；上影=评估日上影线占振幅比例；"
    "相似=综合相似度/形态/量能/量比，样本=前3个最相似涨停样本(代码@涨停日:分)。缺失写 -。"
)


def _f(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def load_bars(session, code: str, day: date, count: int = LOOKBACK) -> list[DailyQuote]:
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date <= day)
        .order_by(DailyQuote.trade_date.desc())
        .limit(count)
        .all()
    )
    return list(reversed(rows))


def _ma(closes: list[float], n: int, end: int | None = None) -> float | None:
    end = len(closes) if end is None else end
    window = closes[max(0, end - n) : end]
    if len(window) < n:
        return None
    return sum(window) / n


def _touched(row: DailyQuote) -> bool:
    return bool(row.limit_up_price and row.high is not None and row.high >= row.limit_up_price - 0.005)


def stock_metrics(bars: list[DailyQuote], pool_days: int = 30) -> dict:
    """纯函数，便于测试。bars 按日期升序，最后一根是评估日。"""
    if not bars:
        return {}
    closes = [b.close for b in bars if b.close is not None]
    last = bars[-1]
    recent = []
    for i in range(max(0, len(bars) - RECENT_BARS), len(bars)):
        b = bars[i]
        prev_vols = [x.volume for x in bars[max(0, i - 5) : i] if x.volume]
        vr = (b.volume / (sum(prev_vols) / len(prev_vols))) if b.volume and prev_vols else None
        amp = ((b.high - b.low) / b.pre_close * 100) if b.high is not None and b.low is not None and b.pre_close else None
        pos = ((b.close - b.low) / (b.high - b.low)) if b.high is not None and b.low is not None and b.high > b.low and b.close is not None else None
        recent.append({"pct": b.change_percent, "amp": amp, "vr": vr, "turn": b.turnover_rate, "pos": pos})
    ma = {}
    for n in (5, 10, 20):
        now_ma, before = _ma(closes, n), _ma(closes, n, len(closes) - 3)
        ma[n] = {
            "dev": (closes[-1] / now_ma - 1) * 100 if now_ma else None,
            "slope": (now_ma / before - 1) * 100 if now_ma and before else None,
        }

    def position(n: int):
        part = bars[-n:]
        highs = [b.high for b in part if b.high is not None]
        lows = [b.low for b in part if b.low is not None]
        if not highs or not lows or max(highs) <= min(lows) or last.close is None:
            return None, None, len(part)
        hi, lo = max(highs), min(lows)
        return (last.close - lo) / (hi - lo), (last.close / hi - 1) * 100, len(part)

    pos20, dist20, _ = position(20)
    pos60, _d, n60 = position(60)
    window = bars[-pool_days:]
    limit_days = [b for b in window if b.is_limit_up]
    broken = sum(1 for b in window if _touched(b) and not b.is_limit_up)
    streak = 0
    for b in reversed(bars):
        if b.is_limit_up:
            streak += 1
        else:
            break
    pullback = None
    if limit_days and last.close is not None and limit_days[-1].close:
        pullback = (last.close / limit_days[-1].close - 1) * 100
    upper = None
    if last.high is not None and last.low is not None and last.high > last.low and last.close is not None and last.open is not None:
        upper = (last.high - max(last.open, last.close)) / (last.high - last.low)
    return {
        "recent": recent,
        "ma": ma,
        "pos20": pos20,
        "dist20": dist20,
        "pos60": pos60,
        "days60": n60,
        "limit_count": len(limit_days),
        "last_limit": limit_days[-1].trade_date.isoformat() if limit_days else None,
        "streak": streak,
        "broken": broken,
        "pullback": pullback,
        "upper_shadow": upper,
    }


def stock_line(item: dict, metrics: dict) -> str:
    recent = ";".join(
        f"{_f(r['pct'])}/{_f(r['amp'])}/{_f(r['vr'], 2)}/{_f(r['turn'])}/{_f(r['pos'], 2)}" for r in metrics.get("recent") or []
    )
    ma = metrics.get("ma") or {}
    ma_text = ",".join(f"{_f((ma.get(n) or {}).get('dev'))}" for n in (5, 10, 20))
    slope = ",".join(f"{_f((ma.get(n) or {}).get('slope'), 2)}" for n in (5, 10, 20))
    samples = ",".join(
        f"{m['code']}@{str(m.get('limit_date', ''))[5:]}:{_f(m.get('score'), 0)}" for m in (item.get("top_matches") or [])[:3]
    ) or "-"
    last_limit = (metrics.get("last_limit") or "-")[5:] if metrics.get("last_limit") else "-"
    return (
        f"{item['code']} {item.get('name', '')} {item.get('board_name') or ''} | 近5日 {recent or '-'} | MA {ma_text} 斜率 {slope} | "
        f"位20 {_f(metrics.get('pos20'), 2)} 位60 {_f(metrics.get('pos60'), 2)}({metrics.get('days60', 0)}日) 距高 {_f(metrics.get('dist20'))} | "
        f"涨停 {metrics.get('limit_count', 0)} 末板 {last_limit} 连板 {metrics.get('streak', 0)} 炸板 {metrics.get('broken', 0)} 回撤 {_f(metrics.get('pullback'))} 上影 {_f(metrics.get('upper_shadow'), 2)} | "
        f"相似 {_f(item.get('score'), 0)}/{_f(item.get('shape_score'), 0)}/{_f(item.get('volume_score'), 0)}/{_f(item.get('volume_pct_score'), 0)} 样本 {samples}"
    )


def market_context(session, day: date) -> dict:
    """评估日全市场情绪：涨停/跌停/炸板/最高连板/涨跌家数，全部来自本地日线。"""
    rows = [r for r in session.query(DailyQuote).filter(DailyQuote.trade_date == day).all() if is_in_scope(r.stock_code)]
    up = sum(1 for r in rows if (r.change_percent or 0) > 0)
    down = sum(1 for r in rows if (r.change_percent or 0) < 0)
    traded = [r for r in rows if r.volume]
    flat = len(traded) - sum(1 for r in traded if (r.change_percent or 0) != 0)
    from backend.app.models.tables import Stock

    listed = sum(1 for (code,) in session.query(Stock.code) if is_in_scope(code))
    listed = max(listed, len(rows))
    suspended = listed - len(traded)
    limit_up = [r for r in rows if r.is_limit_up]
    limit_down = sum(1 for r in rows if r.is_limit_down)
    broken = sum(1 for r in rows if _touched(r) and not r.is_limit_up)
    touched = len(limit_up) + broken
    height, leaders = 0, []
    if limit_up:
        from backend.app.services.summary_service import _streak

        streaks = sorted(((_streak(session, r.stock_code, day), r.stock_code) for r in limit_up), reverse=True)
        height = streaks[0][0]
        leaders = [code for h, code in streaks if h == height][:5]
    pcts = sorted(r.change_percent for r in rows if r.change_percent is not None)
    median = pcts[len(pcts) // 2] if pcts else None
    return {
        "date": day.isoformat(),
        "stocks": len(rows),
        "up": up,
        "down": down,
        "flat": flat,
        "suspended": suspended,
        "listed": listed,
        "up_down_ratio": round(up / down, 2) if down else None,
        "median_pct": round(median, 2) if median is not None else None,
        "limit_up": len(limit_up),
        "limit_down": limit_down,
        "broken": broken,
        "broken_rate": round(broken / touched * 100, 1) if touched else None,
        "max_height": height,
        "leaders": leaders,
    }


def market_line(ctx: dict) -> str:
    return (
        f"市场环境 {ctx['date']}：样本 {ctx['stocks']} 只，上涨 {ctx['up']} 下跌 {ctx['down']} 平盘 {ctx.get('flat', '-')} 停牌 {ctx.get('suspended', '-')}（涨跌比 {_f(ctx.get('up_down_ratio'), 2)}，中位涨跌 {_f(ctx.get('median_pct'), 2)}%），"
        f"涨停 {ctx['limit_up']} 跌停 {ctx['limit_down']} 炸板 {ctx['broken']}（炸板率 {_f(ctx.get('broken_rate'))}%），"
        f"最高连板 {ctx['max_height']}（{','.join(ctx.get('leaders') or []) or '-'}）"
    )
