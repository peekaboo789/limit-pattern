"""技术指标与区间收益率（开发要求第九节）。

`compute_indicator_series()` 是唯一的入口：给一段按时间升序的行情，
返回**等长**的逐日指标字典列表，第 i 根的所有指标只允许看到 `bars[0..i]`。

「只看过去」是结构性保证，不靠调用方自觉：

- 所有窗口都写成右端含当日、不含未来的切片；
- EMA 与 Wilder 平滑都是单向递推，从序列头往尾巴推进，回看时不可能用到后面的值；
- 单元测试断言「把序列尾部截掉若干根后，前面每一天的指标值逐位不变」——
  任何形式的前视泄漏都会让这个断言当场炸掉。

窗口长度不足时返回 None 而不是补 0：0 是「价格刚好等于均线」的合法取值，
拿它当缺失会污染下游。

指标口径（业内通用，写在这里以免日后争辩）：

- MA(5/10/20/30/60)：收盘价简单移动平均；`price_maX_ratio` = (close/MA − 1) × 100。
- MACD(12, 26, 9)：`macd_dif` = EMA12 − EMA26，`macd_dea` = EMA9(DIF)，
  `macd` = 2 × (DIF − DEA)（国内软件红绿柱口径）。
- RSI(14)：Wilder 平滑，`RSI = 100 × 均涨 / (均涨 + 均跌)`。
- KDJ(9, 3, 3)：`K = (2K′ + RSV)/3`、`D = (2D′ + K)/3`，初值 50；`J = 3K − 2D`。
- BOLL(20, 2)：中轨 SMA20，上下轨 ±2 倍**总体**标准差（ddof=0，与通达信一致）；
  另给 `boll_position`（收盘在带内的相对位置 0~1）与 `boll_band_percent`（带宽/中轨×100）。
- ATR(14)：Wilder 平滑的真实波幅；另给 `atr_percent` = ATR / close × 100。

价格缺失或 OHLC 自相矛盾的K线（`is_valid_bar` 为假）不参与任何窗口与递推，
其自身指标全为 None——否则一段停牌会把均线拖成水平线、把 ATR 打成 0。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.app.algorithms.features import Bar

#: 均线窗口（开发要求第九节列出的 5/10/20/30/60）
MA_WINDOWS: tuple[int, ...] = (5, 10, 20, 30, 60)
#: 区间收益率（开发要求第九节：1/3/5/10/20 日）
RETURN_WINDOWS: tuple[int, ...] = (1, 3, 5, 10, 20)

#: 算齐全部指标所需的最少有效K线数（MACD 的 EMA26 之后再叠 EMA9 最慢）。
#: 名字短的这一个是对外的：训练集、特征页都问它。
MIN_BARS_FOR_INDICATORS = 60
MIN_BARS_FOR_FULL_INDICATORS = MIN_BARS_FOR_INDICATORS

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
KDJ_PERIOD = 9
KDJ_SMOOTH = 3
BOLL_PERIOD = 20
BOLL_MULTIPLIER = 2.0
ATR_PERIOD = 14


def indicator_feature_names() -> tuple[str, ...]:
    """全部指标列名（29 个）。特征表、质量检查与训练集都从这里取字段清单。"""
    names: list[str] = [f"ma{window}" for window in MA_WINDOWS]
    names += [f"price_ma{window}_ratio" for window in MA_WINDOWS]
    names += [f"return_{window}d" for window in RETURN_WINDOWS]
    names += ["macd_dif", "macd_dea", "macd"]
    names += ["rsi_14"]
    names += ["kdj_k", "kdj_d", "kdj_j"]
    names += ["boll_mid", "boll_upper", "boll_lower", "boll_position", "boll_band_percent"]
    names += ["atr_14", "atr_percent"]
    return tuple(names)


def is_valid_bar(bar: Bar) -> bool:
    """这一根 K 线能不能参与指标。缺价、非正、或 high < low 这类自相矛盾的都不行。"""
    values = (bar.open, bar.high, bar.low, bar.close)
    if any(value is None for value in values):
        return False
    numbers = [float(value) for value in values]  # type: ignore[arg-type]
    if not all(math.isfinite(value) for value in numbers):
        return False
    open_, high, low, close = numbers
    if min(high, low, close, open_) <= 0:
        return False
    return high >= max(open_, close) >= min(open_, close) >= low


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return _mean(values[-window:])


def _stdev(values: Sequence[float]) -> float | None:
    """总体标准差（ddof=0），与通达信 / 同花顺 BOLL 口径一致。"""
    if len(values) < 2:
        return None
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


@dataclass
class _Ema:
    """单向递推 EMA：前 `period` 根以 SMA 起步，之后按 α = 2/(period+1) 迭代。"""

    period: int
    _seed: list[float] = field(default_factory=list)
    value: float | None = None

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._seed.append(price)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            return self.value
        alpha = 2.0 / (self.period + 1.0)
        self.value = alpha * price + (1.0 - alpha) * self.value
        return self.value


@dataclass
class _Wilder:
    """Wilder 平滑（RSI / ATR 用）：首个值取前 period 项均值，之后 avg += (x-avg)/period。"""

    period: int
    _seed: list[float] = field(default_factory=list)
    value: float | None = None

    def update(self, sample: float) -> float | None:
        if self.value is None:
            self._seed.append(sample)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + sample) / self.period
        return self.value


@dataclass
class _Kdj:
    """KDJ 的 K/D 递推，初值 50（与「从 50 起步」的券商软件一致）。"""

    k: float | None = None
    d: float | None = None

    def update(self, rsv: float) -> tuple[float, float, float]:
        previous_k = 50.0 if self.k is None else self.k
        previous_d = 50.0 if self.d is None else self.d
        self.k = (previous_k * (KDJ_SMOOTH - 1) + rsv) / KDJ_SMOOTH
        self.d = (previous_d * (KDJ_SMOOTH - 1) + self.k) / KDJ_SMOOTH
        return self.k, self.d, 3.0 * self.k - 2.0 * self.d


def _true_range(high: float, low: float, previous_close: float | None) -> float:
    if previous_close is None:
        return high - low
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


class _IndicatorEngine:
    """把「已经看到哪些有效K线」收在一处，逐个指标推进并吐出当日快照。"""

    def __init__(self) -> None:
        self.closes: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        self._ema_fast = _Ema(MACD_FAST)
        self._ema_slow = _Ema(MACD_SLOW)
        self._ema_signal = _Ema(MACD_SIGNAL)
        self._avg_gain = _Wilder(RSI_PERIOD)
        self._avg_loss = _Wilder(RSI_PERIOD)
        self._atr = _Wilder(ATR_PERIOD)
        self._kdj_state = _Kdj()
        self._previous_close: float | None = None

    def step(self, bar: Bar) -> dict[str, float | None]:
        out: dict[str, float | None] = {name: None for name in indicator_feature_names()}
        if not is_valid_bar(bar):
            # 停牌/无效K线：不推进任何递推，也不充当下一日的 TR 基准
            self._previous_close = None
            return out

        close = float(bar.close)  # type: ignore[arg-type]
        high = float(bar.high)  # type: ignore[arg-type]
        low = float(bar.low)  # type: ignore[arg-type]
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)

        self._ma(close, out)
        self._returns(out)
        self._macd(close, out)
        self._rsi(close, out)
        self._kdj(close, out)
        self._boll(close, out)
        self._atr_step(high, low, close, out)

        self._previous_close = close
        return out

    # --- 各指标 -----------------------------------------------------------

    def _ma(self, close: float, out: dict[str, float | None]) -> None:
        for window in MA_WINDOWS:
            moving = _sma(self.closes, window)
            out[f"ma{window}"] = _round(moving, 4)
            if moving:
                out[f"price_ma{window}_ratio"] = _round((close / moving - 1.0) * 100.0)

    def _returns(self, out: dict[str, float | None]) -> None:
        for window in RETURN_WINDOWS:
            if len(self.closes) <= window:
                continue
            base = self.closes[-1 - window]
            if base:
                out[f"return_{window}d"] = _round((self.closes[-1] / base - 1.0) * 100.0)

    def _macd(self, close: float, out: dict[str, float | None]) -> None:
        ema_fast = self._ema_fast.update(close)
        ema_slow = self._ema_slow.update(close)
        if ema_fast is None or ema_slow is None:
            return
        dif = ema_fast - ema_slow
        dea = self._ema_signal.update(dif)
        out["macd_dif"] = _round(dif, 4)
        if dea is None:
            return
        out["macd_dea"] = _round(dea, 4)
        out["macd"] = _round((dif - dea) * 2.0, 4)

    def _rsi(self, close: float, out: dict[str, float | None]) -> None:
        if self._previous_close is None:
            return  # 序列第一根没有涨跌幅，跳过（含停牌后的重新起算）
        change = close - self._previous_close
        average_gain = self._avg_gain.update(max(change, 0.0))
        average_loss = self._avg_loss.update(max(-change, 0.0))
        if average_gain is None or average_loss is None:
            return
        denominator = average_gain + average_loss
        if denominator <= 0:
            out["rsi_14"] = 50.0  # 连续 14 根纹丝不动：中性，不当成 0 或 100
            return
        out["rsi_14"] = _round(average_gain / denominator * 100.0)

    def _kdj(self, close: float, out: dict[str, float | None]) -> None:
        if len(self.highs) < KDJ_PERIOD:
            return
        top = max(self.highs[-KDJ_PERIOD:])
        bottom = min(self.lows[-KDJ_PERIOD:])
        span = top - bottom
        rsv = 50.0 if span <= 0 else (close - bottom) / span * 100.0
        k, d, j = self._kdj_state.update(rsv)
        out["kdj_k"], out["kdj_d"], out["kdj_j"] = _round(k), _round(d), _round(j)

    def _boll(self, close: float, out: dict[str, float | None]) -> None:
        mid = _sma(self.closes, BOLL_PERIOD)
        if not mid:
            return
        sigma = _stdev(self.closes[-BOLL_PERIOD:]) or 0.0
        upper = mid + BOLL_MULTIPLIER * sigma
        lower = mid - BOLL_MULTIPLIER * sigma
        out["boll_mid"] = _round(mid, 4)
        out["boll_upper"] = _round(upper, 4)
        out["boll_lower"] = _round(lower, 4)
        span = upper - lower
        if span > 0:
            out["boll_position"] = _round((close - lower) / span)
        out["boll_band_percent"] = _round(span / mid * 100.0)

    def _atr_step(self, high: float, low: float, close: float, out: dict[str, float | None]) -> None:
        value = self._atr.update(_true_range(high, low, self._previous_close))
        if value is None:
            return
        out["atr_14"] = _round(value, 4)
        if close:
            out["atr_percent"] = _round(value / close * 100.0)


def compute_indicator_series(bars: Sequence[Bar]) -> list[dict[str, float | None]]:
    """逐日计算全部技术指标，返回与 `bars` 等长的字典列表。

    `bars` 必须按 `trade_date` 升序；乱序输入直接报错，而不是悄悄算出错值。
    """
    _assert_ascending(bars)
    engine = _IndicatorEngine()
    return [engine.step(bar) for bar in bars]


def indicators_at(bars: Sequence[Bar]) -> dict[str, float | None]:
    """只看最后一根 K 线那一天的指标。空序列返回「全 None」而不是空字典：
    下游按 `indicator_feature_names()` 取列，缺列和缺值是两件事。
    """
    series = compute_indicator_series(list(bars))
    if not series:
        return {name: None for name in indicator_feature_names()}
    return series[-1]


def _assert_ascending(bars: Sequence[Bar]) -> None:
    dates = [bar.trade_date for bar in bars]
    if any(later < earlier for earlier, later in zip(dates, dates[1:])):
        raise ValueError("指标计算要求行情按 trade_date 升序；乱序会直接产生前视数据")


__all__ = [
    "ATR_PERIOD",
    "BOLL_MULTIPLIER",
    "BOLL_PERIOD",
    "KDJ_PERIOD",
    "KDJ_SMOOTH",
    "MA_WINDOWS",
    "MACD_FAST",
    "MACD_SIGNAL",
    "MACD_SLOW",
    "MIN_BARS_FOR_FULL_INDICATORS",
    "MIN_BARS_FOR_INDICATORS",
    "RETURN_WINDOWS",
    "RSI_PERIOD",
    "compute_indicator_series",
    "indicator_feature_names",
    "indicators_at",
    "is_valid_bar",
]
