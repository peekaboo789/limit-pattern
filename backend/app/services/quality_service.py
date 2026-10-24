"""数据质量闸门：只读检查、写账、不删数据。

这一层存在的理由是那句老事故——「检查写了但没跑」。所以这里两件事是分开的：
`run_checks()` 只往 `data_quality_errors` 写账，一个行情字段都不改；删脏行是
`purge_service` 的活，而且必须用户明说才做。

检查项按这一版库里真有什么字段来定，不照抄一张跑不出结果的清单。每一项都写清 error
还是 warning：**只有 error 会挡住下游**（建样本、构造训练集都要先问过这道闸门），
warning 只记账。这么分是因为每个公开源的日线字段完整度不一样——腾讯和新浪经常只给量
不给额，把「没有成交额」判成 error 等于把整个源判死，那会把闸门用成删数据的刀。
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import median

from sqlalchemy import func

from backend.app.core.clock import today
from backend.app.core.config import read_settings_file, stamp
from backend.app.core.constants import (
    QUALITY_CHANGE_TOLERANCE_PERCENT,
    QUALITY_PRE_CLOSE_TOLERANCE,
    QUALITY_UNIVERSE_MIN_RATIO,
)
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, DataQualityError, Stock
from backend.app.providers.markets import classify, normalize_code
from backend.app.services.limit_up_service import ratio_for

logger = get_logger("quality")

ERROR = "error"
WARNING = "warning"

#: 十四项检查。名字是对外口径（接口、页面、账表都读它），label 是给人看的那一句。
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("code_check", "代码不是六位数字", ERROR),
    ("market_check", "代码前缀与库里的交易所不一致", ERROR),
    ("security_type_check", "板块判不出来", WARNING),
    ("name_check", "股票名称为空", WARNING),
    ("trade_date_check", "行情日期落在未来或周末", ERROR),
    ("ohlc_check", "开高低收缺项、非正数或高低关系不成立", ERROR),
    ("volume_check", "成交量缺项或为负", ERROR),
    ("amount_check", "成交额为负", ERROR),
    ("duplicate_check", "同一只股票同一天有多行", ERROR),
    ("missing_check", "这只股票在这一段里缺了行情日", WARNING),
    ("change_percent_check", "当日涨跌幅超出该板块通用涨跌停幅度", ERROR),
    ("code_name_match_check", "库里的名称与本轮股票列表对不上", WARNING),
    ("universe_size_check", "这一天有行情的股票数明显少于近期水位", ERROR),
    ("pre_close_continuity_check", "前收盘价接不上上一根的收盘价", ERROR),
)

CHECK_LABELS: dict[str, str] = {name: label for name, label, _ in CHECKS}
CHECK_SEVERITY: dict[str, str] = {name: severity for name, _, severity in CHECKS}
CHECK_NAMES: tuple[str, ...] = tuple(name for name, _, _ in CHECKS)

#: 逐只取数一次取多少只。全市场四千多只，分块才不会让一次检查变成一次全表装载。
_CHUNK = 400


def is_open_day(day: date) -> bool:
    """周六日一定没有行情。

    法定节假日这里判不出来——这一版没有交易日历源，所以只在周末那一头判 error；
    节假日缺行由 `missing_check` 以 warning 记账，不去猜日历。
    """
    return day.weekday() < 5


def duplicate_rows(bars: list[dict]) -> list[dict]:
    """同一只股票同一天出现多行。纯函数，只吃字典。

    库里有 `(stock_code, trade_date)` 唯一约束，正常写入走不到这一项，它守的是约束被改掉
    或有人绕过 ORM 写库那一类事。留着它的代价是一次分组，省掉的代价是那种脏行一路流进
    形态窗口——三天窗口里同一天出现两次，等于把那天数了两遍。
    """
    seen: set[date] = set()
    doomed: list[dict] = []
    for bar in bars:
        day = bar.get("trade_date") if isinstance(bar, dict) else bar.trade_date
        if day is None:
            continue
        if day in seen:
            doomed.append(bar)
        else:
            seen.add(day)
    return doomed


class QualityRunner:
    """跑一段区间的质量检查并写账。不缓存、不跨调用留状态。"""

    def __init__(self, session) -> None:
        self.session = session
        self.written = 0
        self.existing = 0
        self.by_check: dict[str, int] = {name: 0 for name in CHECK_NAMES}
        self.errors = 0
        self.warnings = 0

    # --- 写账 --------------------------------------------------------------

    def record(self, day: date | None, code: str, name: str, detail: str) -> None:
        """一条「日期 + 代码 + 检查项」只记一次。

        重复不累加计数：这张表没有 `hit_count`，而每轮同步都会重写同一批行，累加只会让
        一个一直没修的问题在页面上越看越严重。已存在就跳过，回包里另有 `existing` 说明
        跳过了几条——「跳过了」和「没查」是两件事，不能都写成 0。
        """
        severity = CHECK_SEVERITY[name]
        exists = (
            self.session.query(DataQualityError.id)
            .filter(
                DataQualityError.trade_date == day,
                DataQualityError.stock_code == (code or ""),
                DataQualityError.check_name == name,
            )
            .first()
        )
        if exists is not None:
            self.existing += 1
            return
        self.session.add(
            DataQualityError(
                trade_date=day,
                stock_code=code or "",
                check_name=name,
                severity=severity,
                detail=detail[:300],
            )
        )
        self.session.flush()
        self.written += 1
        self.by_check[name] += 1
        if severity == ERROR:
            self.errors += 1
        else:
            self.warnings += 1

    # --- 入口 --------------------------------------------------------------

    def run(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        names: dict[str, str] | None = None,
        max_days: int | None = None,
    ) -> dict:
        #: 默认区间就是库里那一头一尾。明细本来就只留几个交易日，
        #: 用「今天往前推 30 天」当默认值会把补进来的旧日期整段看成空库。
        low = self.session.query(func.min(DailyQuote.trade_date)).scalar()
        high = self.session.query(func.max(DailyQuote.trade_date)).scalar()
        end = end or high or today()
        start = start or low or end
        if start > end:
            start, end = end, start

        days = [
            row[0]
            for row in self.session.query(DailyQuote.trade_date)
            .filter(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
            .distinct()
            .order_by(DailyQuote.trade_date)
            .all()
        ]
        if max_days is not None and len(days) > max_days:
            days = days[-max_days:]
            start = days[0]
        skipped: list[dict] = []
        ran_at = datetime.utcnow().isoformat(timespec="seconds")

        rows_in_range = int(
            self.session.query(func.count(DailyQuote.id))
            .filter(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
            .scalar()
            or 0
        )
        #: 空库不是「全部通过」。这一趟一行都没看，回包必须这么说。
        if not days or not rows_in_range:
            return {
                "ok": True,
                "scanned_days": 0,
                "scanned_rows": 0,
                "written": 0,
                "existing": 0,
                "errors": 0,
                "warnings": 0,
                "by_check": {name: 0 for name in CHECK_NAMES},
                "skipped_checks": [
                    {"check": "全部十四项", "reason": f"{start}~{end} 区间里没有行情行"}
                ],
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "ran_at": ran_at,
            }

        self._universe_size(days, start, end)
        if names is None:
            skipped.append(
                {
                    "check": "code_name_match_check",
                    "reason": "这一趟没有带本轮股票列表（只有同步那一趟有）：名称核对没做成，不是通过",
                }
            )

        codes = [
            row[0]
            for row in self.session.query(DailyQuote.stock_code)
            .filter(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
            .distinct()
            .order_by(DailyQuote.stock_code)
            .all()
        ]
        for offset in range(0, len(codes), _CHUNK):
            self._per_code(codes[offset : offset + _CHUNK], start, end, days, names)

        return {
            "ok": True,
            "scanned_days": len(days),
            "scanned_rows": rows_in_range,
            "written": self.written,
            "existing": self.existing,
            "errors": self.errors,
            "warnings": self.warnings,
            "by_check": dict(self.by_check),
            "skipped_checks": skipped,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "ran_at": ran_at,
        }

    # --- 分块检查 ----------------------------------------------------------

    def _per_code(
        self,
        chunk: list[str],
        start: date,
        end: date,
        days: list[date],
        names: dict[str, str] | None,
    ) -> None:
        bars = (
            self.session.query(DailyQuote)
            .filter(
                DailyQuote.stock_code.in_(chunk),
                DailyQuote.trade_date >= start,
                DailyQuote.trade_date <= end,
            )
            .order_by(DailyQuote.stock_code, DailyQuote.trade_date)
            .all()
        )
        stock_rows = {
            row.code: row
            for row in self.session.query(Stock).filter(Stock.code.in_(chunk)).all()
        }
        by_code: dict[str, list[DailyQuote]] = {}
        for bar in bars:
            by_code.setdefault(bar.stock_code, []).append(bar)

        day_set = set(days)
        for code, rows in by_code.items():
            stock = stock_rows.get(code)
            board = (stock.board if stock else "") or ""
            exchange = (stock.exchange if stock else "") or ""
            is_st = bool(stock.is_st) if stock else False

            if code != normalize_code(code) or len(code) != 6 or not code.isdigit():
                self.record(rows[0].trade_date, code, "code_check", f"{code} 不是六位数字代码")
            expected_exchange, expected_board = classify(code)
            if exchange and expected_exchange and exchange != expected_exchange:
                self.record(
                    rows[0].trade_date,
                    code,
                    "market_check",
                    f"库里记 {exchange}，按代码前缀应为 {expected_exchange}",
                )
            if expected_board is None:
                self.record(rows[0].trade_date, code, "security_type_check", f"{code} 归不到任何板块")
            if stock is not None and not (stock.name or "").strip():
                self.record(rows[-1].trade_date, code, "name_check", f"{code} 在股票主档里没有名称")

            #: 缺行只对「这一天整个市场有行情」的股票说：停牌的那只在谁都缺的日子不该单独记账
            open_days = [day for day in days if day in day_set]
            own = {row.trade_date for row in rows}
            absent = [day for day in open_days if day not in own]
            if absent and len(absent) <= max(1, len(open_days) // 2):
                self.record(
                    absent[-1],
                    code,
                    "missing_check",
                    f"这段区间里缺 {len(absent)} 个行情日，最后一个 {absent[-1]}",
                )

            for index, bar in enumerate(rows):
                self._check_bar(bar, code, board, exchange, is_st, rows[index - 1] if index else None)

            for bar in duplicate_rows(rows):
                self.record(bar.trade_date, code, "duplicate_check", f"{bar.trade_date} 同一天有多行")

            if names is not None and stock is not None:
                listed = names.get(code)
                if listed is not None and (stock.name or "").strip() != listed.strip():
                    #: 名称不属于任何一天：这条账的 trade_date 留空，清理脏行时也不该按天删它
                    self.record(
                        None,
                        code,
                        "code_name_match_check",
                        f"库里记「{stock.name or ''}」，本轮股票列表给的是「{listed}」",
                    )

    def _check_bar(
        self,
        bar: DailyQuote,
        code: str,
        board: str,
        exchange: str,
        is_st: bool,
        previous: DailyQuote | None,
    ) -> None:
        day = bar.trade_date
        if day is not None and (day > today() or not is_open_day(day)):
            self.record(
                day,
                code,
                "trade_date_check",
                f"{day} {'晚于今天' if day > today() else '是周末'}，不该有行情",
            )
        if _ohlc_broken(bar):
            self.record(
                day,
                code,
                "ohlc_check",
                f"{day} 开高低收不成立：开 {bar.open} 高 {bar.high} 低 {bar.low} 收 {bar.close}",
            )
        if bar.volume is None or bar.volume < 0:
            self.record(day, code, "volume_check", f"{day} 成交量是 {bar.volume}")
        if bar.amount is not None and bar.amount < 0:
            self.record(day, code, "amount_check", f"{day} 成交额为负：{bar.amount}")

        percent = bar.change_percent
        if percent is not None and day is not None:
            cap = float(ratio_for(board or "main_board", is_st, day)) * 100.0
            cap += QUALITY_CHANGE_TOLERANCE_PERCENT
            if abs(float(percent)) > cap:
                self.record(
                    day,
                    code,
                    "change_percent_check",
                    f"{day} 当日涨跌 {float(percent):.2f}%，超出该板块通用幅度 ±{cap:.2f}%"
                    f"（板块 {board or '未判定'}，ST {is_st}）",
                )

        if (
            previous is not None
            and previous.trade_date is not None
            and bar.pre_close is not None
            and previous.close is not None
            and (day - previous.trade_date).days <= 8
        ):
            #: 只在两根线真的挨着时比：中间隔了长假或停牌段时，接不上不是脏数据
            if abs(float(bar.pre_close) - float(previous.close)) > QUALITY_PRE_CLOSE_TOLERANCE:
                self.record(
                    day,
                    code,
                    "pre_close_continuity_check",
                    f"{day} 前收 {bar.pre_close} 接不上 {previous.trade_date} 收盘 {previous.close}",
                )

    def _universe_size(self, days: list[date], start: date, end: date) -> None:
        """一天有行情的股票数与近期水位比。掉一半以上就是源只回了半张表。

        基准取这段区间所有天的中位数：均值会被正在被检查的那一天自己拉跑，中位数不会。
        """
        counts = {
            row[0]: int(row[1])
            for row in self.session.query(
                DailyQuote.trade_date, func.count(func.distinct(DailyQuote.stock_code))
            )
            .filter(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
            .group_by(DailyQuote.trade_date)
            .all()
        }
        baseline = median(list(counts.values())) if counts else 0
        if baseline < 50:
            #: 库里只有几十只（测试、首启还没跑完）时这条没有意义。说清楚比给个假通过值钱。
            logger.info("区间内中位数只有 %s 只，universe_size_check 这一趟跳过", int(baseline))
            return
        floor = baseline * QUALITY_UNIVERSE_MIN_RATIO
        for day in days:
            count = counts.get(day, 0)
            if count < floor:
                self.record(
                    day,
                    "",
                    "universe_size_check",
                    f"{day} 只有 {count} 只股票有行情，低于近期中位数 {int(baseline)} 只的 "
                    f"{int(QUALITY_UNIVERSE_MIN_RATIO * 100)}%（{int(floor)} 只）",
                )


def _ohlc_broken(bar: DailyQuote) -> bool:
    open_, high, low, close = bar.open, bar.high, bar.low, bar.close
    if any(value is None or value <= 0 for value in (open_, high, low, close)):
        return True
    if high < max(open_, close) or low > min(open_, close) or high < low:
        return True
    return False


def run_checks(
    session,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    names: dict[str, str] | None = None,
    max_days: int | None = None,
) -> dict:
    """跑一遍质量检查。只写 `data_quality_errors`，一个行情字段都不改。"""
    result = QualityRunner(session).run(
        start=as_date(start), end=as_date(end), names=names, max_days=max_days
    )
    stamp("last_quality_at", result["ran_at"])
    stamp("last_quality_errors", result["errors"])
    stamp("last_quality_warnings", result["warnings"])
    if result["errors"] or result["warnings"]:
        logger.info(
            "质量检查：%s 天 %s 行，error %s 条、warning %s 条（新写 %s 条，已存在 %s 条）",
            result["scanned_days"],
            result["scanned_rows"],
            result["errors"],
            result["warnings"],
            result["written"],
            result["existing"],
        )
    return result


def as_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def list_errors(
    session,
    *,
    trade_date: date | str | None = None,
    severity: str | None = None,
    check_name: str | None = None,
    limit: int = 200,
) -> list[dict]:
    query = session.query(DataQualityError)
    day = as_date(trade_date)
    if day is not None:
        query = query.filter(DataQualityError.trade_date == day)
    if severity:
        query = query.filter(DataQualityError.severity == severity)
    if check_name:
        query = query.filter(DataQualityError.check_name == check_name)
    rows = (
        query.order_by(
            DataQualityError.trade_date.desc(),
            DataQualityError.stock_code,
            DataQualityError.check_name,
        )
        .limit(int(limit))
        .all()
    )
    return [
        {
            "id": int(row.id),
            "trade_date": row.trade_date.isoformat() if row.trade_date else "",
            "stock_code": row.stock_code,
            "check": row.check_name,
            "check_label": CHECK_LABELS.get(row.check_name, row.check_name),
            "severity": row.severity,
            "detail": row.detail,
        }
        for row in rows
    ]


def counts(session) -> dict:
    """账表的总账：error/warning 各多少条、按检查项各多少、最晚记到哪天。"""
    by_check = {
        row[0]: int(row[1])
        for row in session.query(DataQualityError.check_name, func.count(DataQualityError.id))
        .group_by(DataQualityError.check_name)
        .all()
    }
    latest = session.query(func.max(DataQualityError.trade_date)).scalar()
    settings = read_settings_file()
    return {
        "errors": _count(session, DataQualityError.severity == ERROR),
        "warnings": _count(session, DataQualityError.severity == WARNING),
        "total": sum(by_check.values()),
        "by_check": {name: by_check.get(name, 0) for name in CHECK_NAMES},
        "latest_date": latest.isoformat() if latest else "",
        "last_quality_at": str(settings.get("last_quality_at") or ""),
    }


def _count(session, condition) -> int:
    return int(session.query(func.count(DataQualityError.id)).filter(condition).scalar() or 0)


def error_pairs(session, *, start: date | None = None, end: date | None = None) -> set[tuple[str, date]]:
    """error 级账目指向的「哪只股票哪天」。闸门与脏行清理都吃这个集合。

    没有日期的账（比如名称对不上）不在这里：那种行不是一天的脏数据，按天删它会把主档删掉。
    """
    query = session.query(DataQualityError.stock_code, DataQualityError.trade_date).filter(
        DataQualityError.severity == ERROR
    )
    if start is not None:
        query = query.filter(DataQualityError.trade_date >= start)
    if end is not None:
        query = query.filter(DataQualityError.trade_date <= end)
    return {(code, day) for code, day in query.all() if day is not None and code}


def blocked_codes(session, day: date) -> set[str]:
    """这一天有 error 级脏行的代码。建样本、构造训练集之前问一次这个。"""
    return {code for code, _ in error_pairs(session, start=day, end=day)}


__all__ = [
    "CHECKS",
    "CHECK_LABELS",
    "CHECK_NAMES",
    "CHECK_SEVERITY",
    "ERROR",
    "WARNING",
    "as_date",
    "blocked_codes",
    "counts",
    "duplicate_rows",
    "error_pairs",
    "is_open_day",
    "list_errors",
    "run_checks",
]
