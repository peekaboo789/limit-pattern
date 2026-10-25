"""盘中「涨幅报警榜」：前一交易日 AI 固定榜排出的那几名，今天单拎出来盯涨幅。

需求原话与四条拍板：

> 前一个交易日收盘分数最高的那几十只个股进榜，交易日当天实时监控它们，
> 哪只涨超过百分之 3 直接弹窗报警提示，直线上涨超过百分之 6 直接置顶窗口。

1. 「分数最高的那几十只」用哪把尺子 → **AI 固定榜的名次**，也就是前一天在「每日 AI 固定榜」
   那一页备过输入、点过排名之后留下的那几名（`ai_daily_rank_boards` 的
   `payload.result.picks`）。池子的**人数**跟着榜的名次条数走（`WATCH_POOL_SIZE =
   DAILY_PICK_COUNT`）：榜改成排 20 名，这一档的池子就是 20 只，这不是这里另挑了一批人。
   这一句必须写清楚**它不是收盘价**：那份榜是**上午口径**排出来的，拿它的名字说成
   「收盘分数最高的 50 只」就是替系统下结论。尺子是模型给的名次，没有回测校准、没有历史命中率。
2. 「直线上涨超过 6%」→ **两个条件都要**：当日累计涨幅 ≥ 6% **且**近 3 分钟的涨幅跳升 ≥ 2 个百分点。
   只看 6% 会把「昨天涨完、今天横着开盘」的股票在 9:30 一进门就摞到窗口最前面；
   加上跳升这一条，才配得上「直线」两个字。
3. 报警落在哪 → 浏览器一层（浮层 + 声音 + 标题）、桌面版一层（真的把窗口摞到最前），
   判据只有这一份，两层都读同一份账。
4. 节奏 → **每只每天每档只报一次**，置顶**到手动点「知道了」才取消**（去重与确认时刻都落库，
   见 `intraday_watch_store`）。

四条纪律决定了这个模块的形状：

* **池子是「前一交易日」那一份，不是「最新那一份」**。库里没有前一天的榜时，这一档就是空池 +
  一句原因，绝不退回到「最近一次跑出来的榜」充数：那可能是十天前的名次表，
  盯它的股票报今天的警，等于拿旧尺子量新行情。
* **只认今天的报价**（`round_date == 今天`）。休市日/盘前数据源回的还是上一个交易日的收盘价，
  那一份价格里「涨 3%」是**前天**发生的事，报出来就是假报警。
* **采样不足 3 分钟时 `rise_3min=None`**，不是 0。服务 10:30 重启、10:31 读到一个 6.5%，
  此刻根本说不出「近 3 分钟跳了多少」；写成 0.0 就是宣布「它没在冲」，那是编出来的结论。
* **轮次由后台线程跑，不挂在请求里**。置顶报警的意义正是「你没盯着页面的时候也要叫你」，
  采样跟着 HTTP 请求走的话，窗口一收进托盘就断采，报警就只剩一个好看的浮层。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core import clock
from backend.app.core.constants import (
    ALERT_PIN_PERCENT,
    ALERT_POOL_RETRY_SECONDS,
    ALERT_POLL_SECONDS,
    ALERT_POPUP_PERCENT,
    ALERT_SAMPLE_KEEP_SECONDS,
    ALERT_SPIKE_POINTS,
    ALERT_SPIKE_WINDOW_SECONDS,
    DAILY_PICK_COUNT,
    IDLE_EXIT_SECONDS,
)
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, TradingDay
from backend.app.services.ai_daily_rank_service import STATUS_DONE, STATUS_PREPARED
from backend.app.services.ai_daily_rank_service import board_view, get_board
from backend.app.services.ai_evidence import AI_DISCLAIMER
from backend.app.services.intraday_watch_store import TIER_ALERT, TIER_SPIKE
from backend.app.services.intraday_watch_store import ack_alerts, alerts_of, record_alert
from backend.app.services.intraday_watch_store import pending_pin_codes
from backend.app.services.realtime_service import RealtimeUnavailable, fetch_spot

logger = get_logger("intraday_watch")

#: 进榜的名次上限：**就是固定榜给的那几名**，不是这里另挑。引用 `DAILY_PICK_COUNT`
#: 而不是重写一个 50，是为了让「榜改到 20 名」只有一处可以改（两处各写一份迟早会漂）。
WATCH_POOL_SIZE = DAILY_PICK_COUNT

POOL_READY = "ready"
POOL_NO_DAY = "no_previous_trade_date"
POOL_ABSENT = "absent"
POOL_PREPARED = "prepared"
POOL_FAILED = "failed"
POOL_BROKEN = "broken"

#: 这一档此刻在干什么，五个值是五件不同的事，页面各说各话，不许混成一句「暂无数据」
STATE_WATCHING = "watching"
STATE_NO_POOL = "no_pool"
STATE_PRE_OPEN = "pre_open"
STATE_AFTER_CLOSE = "after_close"
STATE_UNAVAILABLE = "unavailable"

WATCH_DISCLAIMER = (
    "这里的「名次」与「分数」是前一交易日那份 AI 固定榜的输出（外部大模型给的相对判断分，"
    "没有回测校准、没有历史命中率、不是涨停概率）；报警说的只是「今天到这一刻真的涨过了"
    f"{ALERT_POPUP_PERCENT:.0f}%／{ALERT_PIN_PERCENT:.0f}%」这一件已经发生的事，不是明日预测。"
    "每只股票每一档每天只报一次；置顶要手动点「知道了」才解除。不构成投资建议。"
    + AI_DISCLAIMER
)


# --- 池子（前一交易日那份固定榜）--------------------------------------------


@dataclass(frozen=True)
class WatchPool:
    """今天这一档要盯的那批股票，来自**前一交易日**那份固定榜的名次表。"""

    #: 盯的是哪一个交易日（= 今天的交易日，报警去重的键）
    trade_date: str
    #: 名次表来自哪一天（前一交易日）；None = 连这一天都定不出来
    pool_date: str | None
    status: str
    note: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == POOL_READY and bool(self.rows)


def previous_trade_date(session: Session, today: date) -> date | None:
    """今天之前最近的一个交易日。

    先问交易日历（`trading_days`，同步写进来的），日历空的开发库再退到「有行情的最近一天」。
    退的那一层不是把「最近有行情的那天」当尺子——那仍然是库里最早的一天，而是让刚建库、
    日历还没长出来的时候这一档能说一句「盯哪一天」，而不是直接判成定不出日期。
    """
    day = session.scalars(
        select(TradingDay.trade_date)
        .where(TradingDay.trade_date < today, TradingDay.is_open.is_(True))
        .order_by(TradingDay.trade_date.desc())
        .limit(1)
    ).first()
    if day is not None:
        return day
    return session.scalars(
        select(DailyQuote.trade_date)
        .where(DailyQuote.trade_date < today)
        .order_by(DailyQuote.trade_date.desc())
        .limit(1)
    ).first()


def _picks_of(view: dict) -> list[dict[str, Any]]:
    """从落库那一份榜里取出名次表；结构读不动就返回空列表（不猜半行）。"""
    result = view.get("result") if isinstance(view.get("result"), dict) else {}
    picks = result.get("picks") if isinstance(result.get("picks"), list) else []
    return [item for item in picks if isinstance(item, dict)]


def build_pool(session: Session, today: date) -> WatchPool:
    """凑出今天要盯的那批股票，并把「凑不齐」的原因说清楚。

    四种缺池状态各有自己的话，因为它们对用户是四件不同的事：库里根本没有更早的行情 /
    前一天压根没有那份榜 / 榜只备了输入没花钱排名 / 那一次排名跑失败了。
    把四种都写成「暂无数据」，用户就永远不知道该去点哪一下。
    """
    trade_date = today.isoformat()
    previous = previous_trade_date(session, today)
    if previous is None:
        return WatchPool(
            trade_date=trade_date,
            pool_date=None,
            status=POOL_NO_DAY,
            note=(
                f"库里没有早于 {trade_date} 的行情日，定不出「前一交易日」：这一档没有可盯的池子。"
                "先做一次数据同步，把最近一个交易日的行情补进来。"
            ),
        )
    pool_date = previous.isoformat()
    row = get_board(session, pool_date)
    if row is None:
        return WatchPool(
            trade_date=trade_date,
            pool_date=pool_date,
            status=POOL_ABSENT,
            note=(
                f"库里没有 {pool_date} 这天的 AI 固定榜，所以今天没有可盯的 {WATCH_POOL_SIZE} 只。"
                f"固定榜是按当天备输入、当天排名的（在「每日 AI 固定榜」那一页点两下，"
                f"排名那一下才花钱），跑出来之后明天这一档就有池子了。"
            ),
        )
    view = board_view(row)
    if view.get("status") == STATUS_PREPARED:
        return WatchPool(
            trade_date=trade_date,
            pool_date=pool_date,
            status=POOL_PREPARED,
            note=(
                f"{pool_date} 那份固定榜只备了输入、没有排名（状态是 prepared）："
                "名次表要敲一次外部大模型才有，那一天的输入已经过期，现在也补不出来。"
                f"请把 {trade_date} 这一份排名跑出来，明天这一档就能盯它的前 {WATCH_POOL_SIZE} 名。"
            ),
        )
    if view.get("status") != STATUS_DONE:
        error = str(view.get("error") or "")[:120]
        return WatchPool(
            trade_date=trade_date,
            pool_date=pool_date,
            status=POOL_FAILED,
            note=(
                f"{pool_date} 那一次固定榜排名没有成功（状态是 {view.get('status') or '未知'}）"
                + (f"：{error}" if error else "")
                + "。没有名次表，今天这一档没有可盯的池子。"
            ),
        )
    picks = _picks_of(view)
    if not picks:
        return WatchPool(
            trade_date=trade_date,
            pool_date=pool_date,
            status=POOL_BROKEN,
            note=(
                f"{pool_date} 那份榜标着已排名，但 payload 里没有可用的名次表"
                "（存档结构读不动，或那一次一条都没过校验闸）。没有可盯的池子，"
                "去「每日 AI 固定榜」里翻那一份看当时的账。"
            ),
        )
    rows: list[dict[str, Any]] = []
    for item in picks[:WATCH_POOL_SIZE]:
        code = str(item.get("stock_code") or "").strip()
        if not code:
            continue
        rank = item.get("rank")
        score = item.get("score")
        rows.append(
            {
                "stock_code": code,
                "stock_name": str(item.get("stock_name") or ""),
                #: 名次与分数保持可空：固定榜的校验闸本来会把缺分的行剔掉，但这里不替它补 0，
                #: 读不出来就是 None，页面显示「—」。
                "rank": None if rank is None else int(rank),
                "score": None if score is None else float(score),
                "reason": str(item.get("reason") or ""),
                "counter_evidence": str(item.get("counter_evidence") or ""),
                "feed_rank": item.get("feed_rank"),
            }
        )
    rows.sort(key=lambda item: (item["rank"] if item["rank"] is not None else 10_000, item["stock_code"]))
    return WatchPool(
        trade_date=trade_date,
        pool_date=pool_date,
        status=POOL_READY,
        note=(
            f"池子 = {pool_date} 那份 AI 固定榜的前 {len(rows)} 名（名次按当天模型给的分排，"
            "本系统不改它的顺序、不重打分）。今天盯的是这些股票的涨幅，与它们现在的相似度无关。"
        ),
        rows=rows,
    )


# --- 采样与判据（纯函数，测试可以直接喂时刻与价格）--------------------------


@dataclass
class CodeSeries:
    """一只股票今天的采样序列。环形缓冲只活在内存里，落库的是**报警**而不是价格序列。"""

    samples: deque[tuple[float, float]] = field(default_factory=deque)
    percent: float | None = None
    last_price: float | None = None
    #: 最后一次取到价的那一刻（墙上时间，给人看的）
    sampled_at: str = ""

    def add(self, *, at: float, percent: float | None, last_price: float | None) -> None:
        """记一次采样：最新值直接覆盖，序列只留「满窗够用」的那一段。"""
        self.last_price = last_price
        self.percent = percent
        self.sampled_at = datetime.now().isoformat(timespec="seconds")
        #: 取不到价（停牌、源这一轮没回）不把 `None` 写进序列：序列里混着 None 时，
        #: 「3 分钟前」取到的会是一个 None，减法直接炸——那不是判据，是坏了的量具。
        if percent is not None:
            self.samples.append((at, percent))
        #: 掐头**不看这一轮有没有取到价**：停牌十分钟回来后，缓冲里只剩这一个新点，
        #: 「3 分钟前」取不到参考 → `rise_in_window` 答 None（不知道），这才是对的。
        #: 若只在有价时掐头，那十分钟里旧的样本一直赖着，回来第一轮就会拿
        #: 「十分钟前的那个价」当 3 分钟前的参照，报出一个假的「在冲」。
        floor = at - ALERT_SAMPLE_KEEP_SECONDS
        while self.samples and self.samples[0][0] < floor:
            self.samples.popleft()

    def rise_in_window(self, *, at: float, window: float = ALERT_SPIKE_WINDOW_SECONDS) -> float | None:
        """近 `window` 秒的涨幅跳升（百分点）。**取不到窗口前那一个点就返回 None。**

        取的是「不晚于 at-window 的最后一个样本」，也就是离那一刻最近的满窗参考点，
        而不是缓冲里最老的一个：最老那个可能已经两分半钟以前（受采样节奏抖动影响），
        拿它当「3 分钟前」会把窗口拉得忽长忽短。
        """
        if self.percent is None:
            return None
        target = at - window
        past: float | None = None
        for stamp, percent in self.samples:
            if stamp <= target:
                past = percent
            else:
                break
        if past is None:
            return None
        return round(self.percent - past, 2)

    def span_seconds(self, *, at: float) -> float | None:
        """缓冲已经覆盖多少秒。页面用它解释「为什么这一列现在是空的」。"""
        if not self.samples:
            return None
        return round(at - self.samples[0][0], 1)


def hit_alert(percent: float | None) -> bool:
    """第一档：当日累计涨幅到 3% 没有。**没有价就不算命中**（None 不是 0%）。"""
    return percent is not None and percent >= ALERT_POPUP_PERCENT


def hit_spike(percent: float | None, rise: float | None) -> bool:
    """第二档：累计 6% **且**近 3 分钟跳升够数——两个条件都在才算「直线上涨」。

    `rise is None` 时**不触发**：这一档的性质决定了它必须有涨速证据，
    采样不足就说「还不知道」，不能因为「还没有反证」就把窗口摞到用户脸上。
    """
    if percent is None or rise is None:
        return False
    return percent >= ALERT_PIN_PERCENT and rise >= ALERT_SPIKE_POINTS


# --- 轮次编排 --------------------------------------------------------------


class IntradayWatcher:
    """一个进程内的涨幅监控轮次。页面读 `state()`，桌面版读 `keepalive()` + `pin_codes()`。"""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        #: 一轮采样只许一个跑者（见 `run_round`）；池子、序列、判据都在这条锁里改
        self._round_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: None = 没人在看（从没读过，或 `stop()` 叫停了）。别拿 0.0 当哨兵：`monotonic()` 在 Linux 上
        #: 是开机秒数，刚开机 5 分钟内 `monotonic() - 0.0` 还没过 `IDLE_EXIT_SECONDS`，线程叫不停。
        self._last_read_at: float | None = None

        #: 池子按交易日缓存一份：盘中不许因为「昨天那份榜被重跑了」而换成员
        #: （用户要的是固定榜）。只有跨到下一个交易日才换。
        self._pool: WatchPool | None = None
        self._pool_attempt_at: float | None = None
        self._series: dict[str, CodeSeries] = {}

        self.payload: dict[str, Any] | None = None
        self.rounds = 0
        self.last_round_at: float | None = None
        self.snapshot_date: str = ""
        self.error: str = ""
        self.source_name: str = ""

    # --- 对外 --------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """给页面的一份状态：记下「有人在看」、确保线程在跑、返回最近一轮的结果。

        这里**不**跑网络轮次：报警榜的每一轮由后台线程按固定节奏采，HTTP 请求只读结果。
        挂在请求里跑的写法会把采样节奏绑成「页面刷多快就采多快」，而窗口一关就断采。

        第一拍（还没跑过任何一轮）时直接读库里的账：已经报过哪几条、还有几条没点「知道了」
        跟采样无关，让页面等最坏 3 秒才看见今天已经发生的事，等于把答案藏起来。
        """
        self.keepalive()
        with self._state_lock:
            if self.payload is not None:
                payload = dict(self.payload)
                #: `thread_running` 说的是「此刻」，不是上一轮那一刻：`keepalive()` 刚刚把线程
                #: 拉起来时，上一轮那份里写的还是 False，页面会显示「监控没在跑」。
                sampling = dict(payload.get("sampling") or {})
                sampling["thread_running"] = bool(self._thread and self._thread.is_alive())
                payload["sampling"] = sampling
                return payload
        return self._payload(
            STATE_NO_POOL,
            note="这一档还没有跑过第一轮采样",
            alerts=self._today_alerts(clock.today().isoformat()),
        )

    def keepalive(self) -> None:
        """告诉监控线程「还有人要看」并把它拉起来。桌面版的常驻循环每个周期点一次。"""
        self._last_read_at = time.monotonic()
        self._ensure_thread()

    def status(self) -> dict[str, Any]:
        """阈值与口径的自述：页面那些数字只能从这里来，前端不各写一份常量。

        档位标识（`alert_tier`/`pin_tier`）也在这里给：页面要分清「哪一档是要摞窗口的」，
        在前端抄一份 `'spike_6pct'` 就是第四处可以改动的地方——库里的账、判据、常量、页面。
        """
        return {
            "pool_size": WATCH_POOL_SIZE,
            "alert_tier": TIER_ALERT,
            "pin_tier": TIER_SPIKE,
            "alert_percent": ALERT_POPUP_PERCENT,
            "pin_percent": ALERT_PIN_PERCENT,
            "rise_window_seconds": ALERT_SPIKE_WINDOW_SECONDS,
            "rise_min_points": ALERT_SPIKE_POINTS,
            "poll_seconds": ALERT_POLL_SECONDS,
            "sample_keep_seconds": ALERT_SAMPLE_KEEP_SECONDS,
            "idle_exit_seconds": IDLE_EXIT_SECONDS,
            "disclaimer": WATCH_DISCLAIMER,
        }

    def stop(self) -> None:
        """叫停监控线程：它在下一个检查点自己退出（测试收尾用）。"""
        self._last_read_at = None

    def reset(self) -> None:
        """丢掉池子、采样与结果。**只在测试里用**：换交易日、换内存库时不留上一例的串台。"""
        with self._state_lock:
            self._pool = None
            self._pool_attempt_at = None
            self._series = {}
            self.payload = None
            self.rounds = 0
            self.last_round_at = None
            self.snapshot_date = ""
            self.error = ""

    # --- 线程 --------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="intraday-watch", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        logger.info(
            "涨幅报警榜线程启动：盯前一交易日固定榜前 %s 名，每 %s 秒采一轮，"
            "涨过 %s%% 报一次、涨过 %s%% 且 %s 秒内跳升 %s 个点以上置顶窗口",
            WATCH_POOL_SIZE,
            ALERT_POLL_SECONDS,
            ALERT_POPUP_PERCENT,
            ALERT_PIN_PERCENT,
            int(ALERT_SPIKE_WINDOW_SECONDS),
            ALERT_SPIKE_POINTS,
        )
        try:
            while True:
                last_read = self._last_read_at
                if last_read is None or time.monotonic() - last_read > IDLE_EXIT_SECONDS:
                    logger.info("涨幅报警榜没人再读取，监控线程退出")
                    return
                self.run_round()
                time.sleep(ALERT_POLL_SECONDS)
        except Exception as error:  # pragma: no cover - 线程绝不能静默死掉
            logger.exception("涨幅报警榜线程异常退出：%s", error)
            with self._state_lock:
                self.error = f"监控线程异常退出：{error}"

    # --- 一轮 --------------------------------------------------------------

    def run_round(self) -> dict[str, Any]:
        """一轮采样的入口：同一时刻只允许跑一轮。

        监控线程是唯一的调用者，这条锁是给「以后有人从请求线程里直接调它」留的护栏：
        两轮并行会把同一只股票的采样点写进同一个 deque，还可能各插一条报警
        （第二行会被唯一约束挡掉，但那一次就白撞了一次异常）。
        """
        with self._round_lock:
            return self._round()

    def _round(self) -> dict[str, Any]:
        """采一轮：定池子 → 取今天的快照 → 判两档 → 落报警 → 出状态。

        **任何一步炸了都不许把线程带崩**：报警榜的意义是「一直在看」，
        一次上游超时只能变成状态里的一句 `error`。
        """
        now = time.monotonic()
        pool = self._pool
        today_alerts: dict[str, dict[str, Any]] = {}
        try:
            pool = self._pool_for(now)
            #: 今天的账在**池子是否可用之前**就读：这一份是「哪些已经报过、哪些还没点知道了」的答案，
            #: 而它在池子凑不齐、上游不通、盘前盘后这几种状态下同样成立。漏读一次的效果是
            #: 页面上那条没确认的报警凭空消失、桌面版却还摞着窗口——两层各说一套。
            today_alerts = self._today_alerts(pool.trade_date)
            if not pool.ready:
                return self._finish(
                    pool, STATE_NO_POOL, rows=[], note=pool.note, at=now, alerts=today_alerts
                )
            #: 盘前与盘后都不敲上游：那两段拿到的价格是**上一个交易日**的收盘价，
            #: 而「昨天涨过 3%」报在今天的浮层上是一句真话配错了日期。
            phase = _phase_of(clock.now())
            if phase in (STATE_PRE_OPEN, STATE_AFTER_CLOSE):
                return self._finish(
                    pool,
                    phase,
                    rows=self._rows_out(pool, at=now, alerts=today_alerts),
                    note=_phase_note(phase),
                    at=now,
                    alerts=today_alerts,
                )
            fetched = self._fetch([str(row["stock_code"]) for row in pool.rows])
            if fetched is None:
                return self._finish(
                    pool,
                    STATE_UNAVAILABLE,
                    rows=self._rows_out(pool, at=now, alerts=today_alerts),
                    note=self.error,
                    at=now,
                    alerts=today_alerts,
                )
            round_date, by_code = fetched
            if round_date != pool.trade_date:
                #: 数据源回的还不是今天：这一轮**不采样、不报警**，只把这句话带给页面。
                self.snapshot_date = round_date
                return self._finish(
                    pool,
                    STATE_UNAVAILABLE,
                    rows=self._rows_out(pool, at=now, alerts=today_alerts),
                    note=(
                        f"数据源回的是 {round_date or '未知'} 的价格，而今天是 {pool.trade_date}："
                        "今天不是交易日，或盘后行情还没落到今天的价。这一轮不采样、不报警，"
                        "免得把上一天的涨幅报成今天的事。"
                    ),
                    at=now,
                    alerts=today_alerts,
                )
            self.snapshot_date = round_date
            self.error = ""

            for row in pool.rows:
                code = str(row["stock_code"])
                series = self._series.setdefault(code, CodeSeries())
                record = by_code.get(code)
                series.add(
                    at=now,
                    percent=None if record is None else record[0],
                    last_price=None if record is None else record[1],
                )
            fired = self._judge(pool, at=now, alerts=today_alerts)
            if fired:
                #: 报警一落库就以库为准重读一次；就地拼的那份只用来判断「是不是第一次」。
                #: 库里才是「今天报过什么」的唯一答案（重启、并发、别的进程报的都在里面）。
                today_alerts = self._today_alerts(pool.trade_date)
            return self._finish(
                pool,
                STATE_WATCHING,
                rows=self._rows_out(pool, at=now, alerts=today_alerts),
                note="",
                at=now,
                alerts=today_alerts,
            )
        except Exception as error:  # noqa: BLE001 - 一轮失败只能说清，不能带崩线程
            logger.exception("涨幅报警榜一轮异常：%s", error)
            self.error = str(error) or type(error).__name__
            if pool is None:
                pool = WatchPool(
                    trade_date=clock.today().isoformat(), pool_date=None, status=POOL_BROKEN, note=""
                )
            return self._finish(
                pool, STATE_UNAVAILABLE, rows=[], note=self.error, at=now, alerts=today_alerts
            )

    # --- 池子与取数 --------------------------------------------------------

    def _pool_for(self, now: float) -> WatchPool:
        """今天这一档的池子：跨交易日才换，空池时按 `ALERT_POOL_RETRY_SECONDS` 节流重查。"""
        today = clock.today()
        pool = self._pool
        if pool is not None and pool.trade_date == today.isoformat() and pool.ready:
            return pool
        if (
            pool is not None
            and pool.trade_date == today.isoformat()
            and self._pool_attempt_at is not None
            and now - self._pool_attempt_at < ALERT_POOL_RETRY_SECONDS
        ):
            return pool
        self._pool_attempt_at = now
        try:
            with session_scope() as session:
                fresh = build_pool(session, today)
        except Exception as error:  # noqa: BLE001 - 读库失败只是「这一轮没有池子」
            logger.warning("涨幅报警榜取池子失败：%s", error)
            fresh = WatchPool(
                trade_date=today.isoformat(),
                pool_date=None,
                status=POOL_BROKEN,
                note=f"读前一交易日的固定榜失败：{error}（这一轮没有可盯的池子）",
            )
        if self._pool is None or self._pool.trade_date != fresh.trade_date:
            #: 换天了就把采样序列丢掉：3 分钟窗口不许跨交易日拼（昨天最后 3 分钟
            #: 与今天头 3 分钟拼出来的「跳升」是一个不存在的分钟）。
            self._series = {}
        with self._state_lock:
            self._pool = fresh
        return fresh

    def _fetch(self, codes: list[str]) -> tuple[str, dict[str, tuple[float | None, float | None]]] | None:
        """取这一批代码的盘中快照，返回 `(这一轮的行情日, {代码: (涨幅, 最新价)})`。

        网络这一步**不占数据库连接**：`_pool_for` 与落库各自开各自的 session，
        这一句只在两者之间跑（与综合分析那几百秒不许占连接同一条理由）。
        行情日与 `realtime_service.scan()` 同一口径：各家给的日期取最大，
        一家都没给时按今天算——那里也是这么处理的，报警榜不另立一套。
        """
        try:
            _source, by_row = fetch_spot(codes)
        except RealtimeUnavailable as error:
            #: 一个源都没回：把这句话原样带给页面，而不是让它显示一个空榜、
            #: 以为「今天没有一只在涨」。
            self.error = str(error) or "行情快照是空的"
            return None
        except Exception as error:  # noqa: BLE001 - 上游超时/解析失败都只是「这一轮没取到」
            self.error = f"取盘中快照失败：{error}"
            return None
        if not by_row:
            self.error = "数据源返回了空的盘中快照"
            return None
        self.source_name = str(next(iter(by_row.values()), {}).get("source") or "") or self.source_name
        days = [row.get("trade_date") for row in by_row.values() if isinstance(row.get("trade_date"), date)]
        round_date = (max(days) if days else clock.today()).isoformat()
        out: dict[str, tuple[float | None, float | None]] = {}
        for code, row in by_row.items():
            price = _number(row, "close")
            pre_close = _number(row, "pre_close")
            percent = _number(row, "pct_chg")
            if percent is None and pre_close not in (None, 0) and price is not None:
                percent = (float(price) / float(pre_close) - 1.0) * 100.0
            out[str(code)] = (
                None if percent is None else round(float(percent), 2),
                None if price is None else round(float(price), 2),
            )
        return round_date, out

    def _today_alerts(self, trade_date: str) -> dict[str, dict[str, Any]]:
        """今天已经报过的账，形状 `{代码|档: 那条报警}`。"""
        try:
            with session_scope() as session:
                rows = alerts_of(session, trade_date)
        except Exception as error:  # noqa: BLE001
            logger.warning("涨幅报警榜读今天的账失败：%s", error)
            return {}
        return {f"{item['stock_code']}|{item['tier']}": item for item in rows}

    # --- 判据落地 ----------------------------------------------------------

    def _judge(self, pool: WatchPool, *, at: float, alerts: dict[str, dict[str, Any]]) -> list[str]:
        """把这一轮新触发的两档各记一条账，返回**第一次触发**的那些「代码|档」。"""
        fired: list[str] = []
        for row in pool.rows:
            code = str(row["stock_code"])
            series = self._series.get(code)
            if series is None:
                continue
            percent = series.percent
            rise = series.rise_in_window(at=at)
            checks: list[tuple[str, bool, float | None]] = [
                (TIER_ALERT, hit_alert(percent), None),
                (TIER_SPIKE, hit_spike(percent, rise), rise),
            ]
            for tier, hit, rise_value in checks:
                if not hit:
                    continue
                if f"{code}|{tier}" in alerts:
                    continue  # 每只每天每档只报一次：库里已经有那一行了
                saved = self._record(
                    trade_date=pool.trade_date,
                    stock_code=code,
                    stock_name=str(row.get("stock_name") or ""),
                    tier=tier,
                    percent=percent,
                    rise_3min=rise_value,
                    rank=row.get("rank"),
                    score=row.get("score"),
                )
                if saved is None:
                    continue
                alerts[f"{code}|{tier}"] = saved
                fired.append(f"{code}|{tier}")
                logger.info(
                    "涨幅报警：%s %s 第 %s 名 今日 %s%%%s",
                    code,
                    row.get("stock_name") or "",
                    row.get("rank"),
                    percent,
                    f"（近 {int(ALERT_SPIKE_WINDOW_SECONDS)} 秒跳升 {rise_value} 个点）"
                    if rise_value is not None
                    else "",
                )
        return fired

    def _record(self, **kwargs: Any) -> dict[str, Any] | None:
        """一条报警单开一条 session 落库（撞唯一约束时回滚只影响这一条，见 store）。"""
        try:
            with session_scope() as session:
                return record_alert(session, **kwargs)
        except Exception as error:  # noqa: BLE001 - 报警落不进去也不能把这一轮带崩
            logger.warning(
                "涨幅报警落库失败（%s %s）：%s", kwargs.get("stock_code"), kwargs.get("tier"), error
            )
            return None

    # --- 出状态 ------------------------------------------------------------

    def _rows_out(
        self, pool: WatchPool, *, at: float, alerts: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in pool.rows:
            code = str(row["stock_code"])
            series = self._series.get(code) or CodeSeries()
            rows.append(
                {
                    **row,
                    "last_price": series.last_price,
                    "change_percent": series.percent,
                    "rise_3min": series.rise_in_window(at=at),
                    "sample_span_seconds": series.span_seconds(at=at),
                    "sampled_at": series.sampled_at,
                    "no_snapshot": series.percent is None,
                    "alert": alerts.get(f"{code}|{TIER_ALERT}"),
                    "spike": alerts.get(f"{code}|{TIER_SPIKE}"),
                }
            )
        return rows

    def _finish(
        self,
        pool: WatchPool,
        state: str,
        *,
        rows: list[dict[str, Any]],
        note: str,
        at: float,
        alerts: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            state, pool=pool, rows=rows, note=note, at=at, alerts=alerts if alerts is not None else {}
        )
        with self._state_lock:
            self.payload = payload
            self.last_round_at = at
            self.rounds += 1
        return payload

    def _payload(
        self,
        state: str,
        *,
        pool: WatchPool | None = None,
        rows: list[dict[str, Any]] | None = None,
        note: str = "",
        at: float | None = None,
        alerts: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = at if at is not None else time.monotonic()
        today = clock.today().isoformat()
        ledger = list((alerts or {}).values())
        unacked = [item for item in ledger if not item["acked_at"]]
        pin_codes = sorted({item["stock_code"] for item in unacked if item["tier"] == TIER_SPIKE})
        return {
            "status": state,
            "trade_date": (pool.trade_date if pool else today),
            "session_phase": _phase_of(clock.now()),
            "note": note or (pool.note if pool else ""),
            "pool": (
                None
                if pool is None
                else {
                    "pool_date": pool.pool_date,
                    "status": pool.status,
                    "note": pool.note,
                    "row_count": len(pool.rows),
                }
            ),
            "rows": rows or [],
            "row_count": len(rows or []),
            "alerts": sorted(ledger, key=_alert_order),
            "unacked": sorted(unacked, key=_alert_order),
            "pending_pin_codes": pin_codes,
            "pinning": bool(pin_codes),
            "sampling": {
                "rounds": self.rounds,
                "last_round_at": _wall_of(self.last_round_at, now),
                "last_round_seconds_ago": None
                if self.last_round_at is None
                else round(now - self.last_round_at, 1),
                "snapshot_date": self.snapshot_date,
                "today": today,
                "source": self.source_name,
                "error": self.error,
                "thread_running": bool(self._thread and self._thread.is_alive()),
            },
            **self.status(),
        }

    # --- 解除置顶 ----------------------------------------------------------

    def ack(
        self, trade_date: str, *, stock_code: str | None = None, tier: str | None = None
    ) -> dict[str, Any]:
        """点「知道了」：把未确认的报警写上确认时刻，返回还剩几条、窗口该不该继续置顶。

        这里**不重跑一轮采样**。确认是用户按下按钮那一刻必须生效的事，而一轮采样要敲上游、
        最慢能到秒级——把它塞进这条请求，页面会表现为「点了没反应」。生效的做法是把刚确认完
        那份账就地换进内存里那份状态（`_patch_ledger`）：下一次 `GET` 拿到的 `pinning`
        就是刚变化的那个事实，桌面置顶线程读的 `pin_codes()` 本来就直读库。
        榜上那几行「已确认」的标记最迟 `ALERT_POLL_SECONDS` 秒后由监控线程刷成同一份账。

        确认与「还剩几条」在**同一条 session** 里读：分两次读会拿到「刚确认的那条仍然未确认」
        这种自相矛盾的账，而 `pinning` 就是从这个数来的。
        """
        try:
            with session_scope() as session:
                changed = ack_alerts(session, trade_date, stock_code=stock_code, tier=tier)
                ledger = alerts_of(session, trade_date)
        except Exception as error:  # noqa: BLE001
            logger.warning("涨幅报警确认失败：%s", error)
            return {"ok": False, "acked": 0, "error": str(error) or type(error).__name__}
        remaining = [item for item in ledger if not item["acked_at"]]
        pins = sorted({item["stock_code"] for item in remaining if item["tier"] == TIER_SPIKE})
        self._patch_ledger(ledger, remaining, pins)
        return {
            "ok": True,
            "acked": int(changed),
            "remaining": len(remaining),
            "pinning": bool(pins),
            "pending_pin_codes": pins,
        }

    def _patch_ledger(
        self, alerts: list[dict[str, Any]], unacked: list[dict[str, Any]], pins: list[str]
    ) -> None:
        """把刚确认的那份账换进内存里的 payload：下一次 `state()` 不许还答「没点过」。

        只换账本那四个字段，**不重跑采样、不敲上游**：一轮采样最慢能到秒级，把它塞进
        「知道了」这条请求，那颗按钮就表现为按下去没反应；而同一轮里改 `_series` 会与
        监控线程抢同一个 deque。榜上每一行的「已确认」标记由下一轮刷齐，最迟
        `ALERT_POLL_SECONDS` 秒——那是标记，不是「还置顶着不」的判据。
        """
        with self._state_lock:
            if self.payload is None:
                return
            patched = dict(self.payload)
            patched["alerts"] = sorted(alerts, key=_alert_order)
            patched["unacked"] = sorted(unacked, key=_alert_order)
            patched["pending_pin_codes"] = pins
            patched["pinning"] = bool(pins)
            self.payload = patched

    def pin_codes(self) -> list[str]:
        """桌面版置顶线程读的那一条：现在还有几条未确认的置顶档。

        直接读库而不是读 `self.payload`：内存那份是**上一轮**的结果，最坏会慢 3 秒，
        而「用户已经点了知道了」这件事必须当场生效，否则窗口还摞在最前面，
        用户会觉得那颗按钮是坏的。
        """
        try:
            with session_scope() as session:
                return pending_pin_codes(session, clock.today().isoformat())
        except Exception as error:  # noqa: BLE001
            logger.warning("读未确认的置顶档失败：%s", error)
            return []


def _alert_order(item: dict[str, Any]) -> tuple[str, str, str]:
    """报警的展示顺序：弹窗档在前、同一档内先触发的在前、同一时刻按代码。

    这一条在「每轮出状态」与「确认之后就地改账」两处都要用，所以只写一遍：
    两处各排各的，页面上下两块列表就会给出两种顺序，看着像数据不一致。
    """
    return (str(item["tier"]), str(item["fired_at"]), str(item["stock_code"]))


def _number(row: dict, field: str) -> float | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _phase_of(at: datetime) -> str:
    """把时刻归到这一档的三种状态之一（盘前 / 盘中 / 盘后）。

    不用 `clock.session_phase()` 的三值直接对外：它把 11:30 之后全算 `frozen`，
    而下午一点半和下午三点半对报警榜是两件事——前者还在盯，后者价格已经不动了。
    """
    moment = at.time()
    if moment < clock.MORNING_OPEN:
        return STATE_PRE_OPEN
    if moment >= clock.AFTERNOON_CLOSE:
        return STATE_AFTER_CLOSE
    return STATE_WATCHING


def _phase_note(phase: str) -> str:
    if phase == STATE_PRE_OPEN:
        return (
            f"现在还没到 {clock.MORNING_OPEN:%H:%M}：这一档从开盘起才开始采样与报警。"
            "盘前敲上游只会拿到上一个交易日的收盘价，报出来的会是昨天的事。"
        )
    return (
        f"已经收盘（{clock.AFTERNOON_CLOSE:%H:%M} 之后）：今天的价格不再动，停止采样。"
        "已报的账与还没确认的置顶仍在下面这张表里。"
    )


def _wall_of(mark: float | None, now: float) -> str | None:
    """把 `time.monotonic()` 的读数换算成墙上时间，页面好显示「上一轮是几点」。"""
    if mark is None:
        return None
    return datetime.fromtimestamp(time.time() - (now - mark)).isoformat(timespec="seconds")


#: 进程内单例：一个服务进程一条监控轮次（与 `realtime_tracker.tracker` 同一取向）
watcher = IntradayWatcher()

__all__ = [
    "POOL_ABSENT",
    "POOL_BROKEN",
    "POOL_FAILED",
    "POOL_NO_DAY",
    "POOL_PREPARED",
    "POOL_READY",
    "STATE_AFTER_CLOSE",
    "STATE_NO_POOL",
    "STATE_PRE_OPEN",
    "STATE_UNAVAILABLE",
    "STATE_WATCHING",
    "TIER_ALERT",
    "TIER_SPIKE",
    "WATCH_DISCLAIMER",
    "WATCH_POOL_SIZE",
    "CodeSeries",
    "IntradayWatcher",
    "WatchPool",
    "build_pool",
    "hit_alert",
    "hit_spike",
    "previous_trade_date",
    "watcher",
]
