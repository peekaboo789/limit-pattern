"""每日 AI 固定榜：把「备好输入」和「排出名次」拆成两笔账。

拆开的理由很实在——凑一份 100 行的输入是本系统自己算的，一分钱不花；
敲一次网关要按 token 付钱，而且按老工程实测排 20 名要 978.5 秒。
所以页面上是两颗按钮，每一次结果都带回 `request_sent`（请求交没交给网关）与
`billed`（这一趟按 token 计了费没有）：429 那种被上游挡在做结论之前的失败是「发了、没花」，
跟「根本没敲网关」和「花了钱没结论」是三件事，必须一句话能问出来。

名次归模型（它自己给的分排出第 1 到最后一名，本系统不替它重排），
综合评分与等级归本系统按 `FORMULA_TEXT` 那个算式算，两者并排显示、差也一起给。

五道校验闸的口径与盘中那份同源，只多了一道 `rejected`：固定榜每一行都要有名次，
缺分或越界的行排不进去，剔掉并计数（不是惩罚它，是为了让「第 N 名」仍对得上一个真实的分）；
`unverified` 那道**只盖章不删行**——判据是启发式的，删掉一条付过钱的推荐不等于那条是假的。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from backend.app.core import clock
from backend.app.core.clock import SHANGHAI
from backend.app.core.constants import (
    AI_RAW_EXCERPT_LIMIT,
    DAILY_FEED_LIMIT,
    DAILY_PICK_COUNT,
    DAILY_RANK_RETENTION_DAYS,
    DAILY_RANK_RUN_TIMEOUT_SECONDS,
)
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.services import ai_evidence, ai_gateway, ai_picks
from backend.app.services.ai_analysis_service import (
    AiFailure,
    criteria_of,
    evidence_with_gap,
    json_object_of,
    model_score_of,
    text_of,
)
from backend.app.services.ai_analysis_store import cost_of
from backend.app.services.realtime_tracker import DEFAULT_METRIC, RANK_METRICS
from backend.app.services.score_model import FORMULA_TEXT

logger = get_logger("ai_daily_rank")

STATUS_PREPARED = "prepared"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"

#: 固定榜按哪个口径取前 100 行——「相似榜」就是默认口径，跟着 `realtime_tracker` 那个默认值走，
#: 哪天默认口径改了，两条路不能一个改了一个没改。
DAILY_METRIC = DEFAULT_METRIC

#: 理由里的数字（含小数）。只用来做「追不追得回出处」的盖章，见 `_unverified_codes()`。
_CITATION_RE = re.compile(r"\d+(?:\.\d+)?")

TASK_RANK = (
    "输入是 {trade_date} 按「{metric_label}」选出的相似榜前 {feed_rows} 行{prepared_note}。"
    "任务：从这 {feed_rows} 只里挑出你认为「更值得看」的最多 {pick_count} 只，"
    "按你从好到差的顺序放进 picks 数组——数组第一个就是你认为最好的那只，"
    "名次就是数组顺序（本系统不替你重排，最多按你给的分裁在 {pick_count} 只以内）。"
    "每只必须给一个 0~100 的相对判断分（不是涨停概率）：名次靠这个分对账，"
    "缺分或越界的行会被当成无效行剔除，那一行就白写了。"
    "这一份榜的名次归你这个分，输入里 formula 那个综合评分归本系统算、只作为这一行的读数，"
    "两者可以不同；不同就把差的原因写进这只股票的理由里，不要为了对上它而改你的分。"
    "每只再各给一句话理由、一句话反证（它哪里不行）。"
    "另外用一段话写清你这一次是按什么逻辑排的（criteria）：这套标准原样展示给用户看，"
    "写你自己真正的取舍，不要复述字段清单，也不要写成免责话术。"
    "必须只输出一个 JSON 对象，形如 "
    '{{"criteria":"...","picks":[{{"stock_code":"600072","score":78.5,'
    '"reason":"...","counter_evidence":"..."}}]}}，'
    "stock_code 必须原样来自输入，不要输出别的文字，不要用 markdown 代码块包裹。"
)


def daily_formula(*, metric_label: str) -> str:
    """这一份榜在页面上的口径说明。

    做成函数而不是模块常量：选榜口径的中文名来自运行时的后端，在源码里抄一遍
    「按最高相似度」就成了两条会各自过时的字符串。
    这一版程序没有「每天 11:25 自动备榜」那种定时档位（备输入是收盘后那一轮同步之后
    由程序自己发起，或者用户在页面上点一次），所以这句里不写任何具体时刻。
    """
    return (
        f"输入是「按{metric_label or '默认相似度'}口径选出的相似榜前 {DAILY_FEED_LIMIT} 行」，"
        f"由本系统当场凑好并落库（这一步不花钱）；页面上点一次「排出前 {DAILY_PICK_COUNT} 名」"
        f"才会敲一次外部大模型，它按自己声明的标准（见「它的判断标准」）从好到差排出最多 "
        f"{DAILY_PICK_COUNT} 名。{FORMULA_TEXT}"
        "名次按模型自报的相对分排（本系统不替它重排），综合评分与等级按上面这个算式由本系统算，"
        "两者并排显示、模型那一分与公式分的差也一起显示。"
        "名次、模型那一分与理由都是模型的输出：没有回测校准、没有历史命中率，不是涨停概率。"
    )


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _brief(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


# --- 落库的那一行：输入与结论合住一天一行 -------------------------------------


def get_board(session, day: str | date | None = None):
    """某一天的固定榜那一行。不给日子就取库里最近一天。"""
    from backend.app.models.tables import AiDailyRankBoard

    query = session.query(AiDailyRankBoard)
    parsed = _as_date(day)
    if parsed is not None:
        query = query.filter(AiDailyRankBoard.trade_date == parsed)
    return query.order_by(AiDailyRankBoard.trade_date.desc()).first()


def upsert_prepared(session, *, trade_date: str, base_date: str, window_mode: str, metric: str,
                    metric_label: str, model: str, board_rows: int, feed_rows: int,
                    feed: list[dict], extra: dict) -> Any:
    """落一份备好的输入（`status=prepared`），回那一行。

    当天已经跑过排名（`done`/`failed`）时这一句会把那一行压回 `prepared` 并丢掉结论：
    输入换了，旧名次就不再是这份输入的名次。留着旧名次，页面上「第 1 名」说的是
    上一份输入里的第 1 名——那是个假的对应关系，比空着更坏。
    """
    from backend.app.models.tables import AiDailyRankBoard

    row = session.query(AiDailyRankBoard).filter(
        AiDailyRankBoard.trade_date == _as_date(trade_date)
    ).one_or_none()
    if row is None:
        row = AiDailyRankBoard(trade_date=_as_date(trade_date))
        session.add(row)
    payload = dict(row.payload or {})
    payload.pop("result", None)
    payload["prepared"] = {
        "feed": feed,
        "extra": extra,
        "prepared_at": _now_iso(),
    }
    row.status = STATUS_PREPARED
    row.base_date = _as_date(base_date)
    row.window_mode = window_mode
    row.metric = metric
    row.metric_label = metric_label[:64]
    row.model = model[:64]
    row.board_rows = int(board_rows)
    row.feed_rows = int(feed_rows)
    row.prepared_at = _now_iso()
    row.payload = payload
    row.error = ""
    row.started_at = ""
    row.finished_at = ""
    row.latency_seconds = None
    row.picks_count = 0
    row.picks_beyond_limit = 0
    row.dropped_codes_count = 0
    row.rejected_count = 0
    row.unverified_citations = 0
    row.cost_text = ""
    session.flush()
    return row


# --- 备输入（不花钱的那一半）--------------------------------------------------


def prepare_from_tracker(session, *, metric: str = DAILY_METRIC,
                        top_n: int = DAILY_FEED_LIMIT) -> dict:
    """从监控轮次那份**已经跑完的全市场打分结果**里选前 N 行，凑成输入并落库。

    用上一轮留下的 `full_rows` 而不是当场再取一次价：这样「固定榜的输入」与
    「用户屏幕上那份相似榜」是同一批打分结果、同一个选榜代码，不存在第二套。
    上一轮还没跑出来时才同步跑一轮——调用方是收盘后的定时任务或点了按钮的人，
    不是 2 秒一次的刷榜请求。
    """
    from backend.app.services.realtime_tracker import tracker

    board = tracker.snapshot(metric=metric, top_n=top_n)
    return prepare_board(session, board, selection={
        "metric": metric,
        "metric_label": str(board.get("metric_label") or ""),
        "top_n": top_n,
        "window_mode": str(board.get("window_mode") or ""),
    })


def prepare_board(session, board: dict, *, selection: dict | None = None) -> dict:
    """把一张榜凑成 100 行证据并落 `status=prepared`。一分钱不花、一次网关都不敲。"""
    extra = selection or {}
    trade_date = str(board.get("trade_date") or "")
    rows = [*(board.get("limit_up_rows") or []), *(board.get("pending_rows") or [])]
    if not trade_date:
        #: 这一句**不猜原因**：榜为什么是空的只有取榜那一步知道（预热中、还是取价报错），
        #: 它把状态词一起带回来。写死一句「首轮扫描可能还没跑出来」会把排查带去没病的路。
        return {"ok": False, "status": "", "trade_date": "", "board_rows": len(rows),
                "feed_rows": 0, "error": "这一份榜没有 trade_date（取榜那一步没给出当日那张榜），没有落库"}

    today = clock.today().isoformat()
    if trade_date != today:
        #: 休市日的坑（周末/节假日）：数据源那天回的还是**上一个交易日**的价格，
        #: 于是 `trade_date` 指回周五。照原样落库等于拿周五收盘后的整张榜覆盖掉周五那份输入，
        #: 顺手把已经花钱排好的名次压回 `prepared`。那是把一天的固定榜改成另一天的口径。
        return {"ok": False, "status": "", "trade_date": trade_date, "board_rows": len(rows),
                "feed_rows": 0,
                "error": (f"榜上的交易日是 {trade_date}，而今天是 {today}：今天不是交易日"
                          "（或行情源还没回到今天的价）。没有落库——把上一天的输入和名次覆盖掉不是「备榜」")}

    if not rows:
        return {"ok": False, "status": "", "trade_date": trade_date, "board_rows": 0,
                "feed_rows": 0, "error": f"{trade_date} 的榜是空的：没有可备的输入，没有落库"}

    bundle = ai_evidence.build_evidence(session, board, row_limit=DAILY_FEED_LIMIT)
    if not bundle["rows"]:
        return {"ok": False, "status": "", "trade_date": trade_date,
                "board_rows": bundle["board_rows"], "feed_rows": 0,
                "error": f"榜上 {bundle['board_rows']} 行里没有一行凑得出最高相似度（跳过 {bundle['skipped_rows']} 行），没有落库"}

    row = upsert_prepared(
        session,
        trade_date=trade_date,
        base_date=str(board.get("base_date") or ""),
        window_mode=str(board.get("window_mode") or ""),
        metric=str(extra.get("metric") or DAILY_METRIC),
        metric_label=str(extra.get("metric_label") or ""),
        model=ai_gateway.model_name(),
        board_rows=bundle["board_rows"],
        feed_rows=bundle["feed_rows"],
        feed=bundle["rows"],
        extra={key: value for key, value in bundle.items() if key != "rows"},
    )
    logger.info("每日 AI 固定榜已备好输入：%s（榜 %s 行 / 喂 %s 行，口径 %s，不花钱）",
                trade_date, bundle["board_rows"], bundle["feed_rows"], row.metric)
    return {"ok": True, "status": STATUS_PREPARED, "trade_date": trade_date,
            "board_rows": bundle["board_rows"], "feed_rows": bundle["feed_rows"],
            "prepared_at": row.prepared_at, "error": ""}


# --- 提示词 ------------------------------------------------------------------


def build_ranking_messages(feed: list[dict], *, meta: dict) -> list[dict[str, str]]:
    """前 100 行的输入 + 「排出前 20 名」的输出形状。

    与盘中那份共用同四段（角色、字段来源、给定评分模型、防编造），只有任务那句不一样。
    这里有一点是这一条链路特有的：名次照模型自己给的分排，本系统不重排，
    所以必须说开「formula 那个数是本系统的读数」——不然它会以为自己在替本系统抄那个数，
    一份 100 行的输入能变成 100 行重复的 formula。
    """
    prepared_at = str(meta.get("prepared_at") or "")
    system = (
        ai_evidence.system_prompt()
        + TASK_RANK.format(
            trade_date=str(meta.get("trade_date") or ""),
            metric_label=str(meta.get("metric_label") or "") or DAILY_METRIC,
            feed_rows=len(feed),
            pick_count=DAILY_PICK_COUNT,
            prepared_note=f"（{prepared_at} 定格，之后的行情不在里面）" if prepared_at else "",
        )
    )
    user = (
        f"相似榜前 {len(feed)} 行（{meta.get('trade_date')}）：\n"
        + json.dumps([ai_evidence.prompt_row_of(item) for item in feed], ensure_ascii=False)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --- 校验闸：把它的回包变成一份能被追问的名次表 -------------------------------


def parse_ranking(content: str, feed: list[dict]) -> dict:
    """把模型原文变成名次表，并**只认输入里的代码**。

    与 `parse_picks()` 的差别只有一处，而且是这份榜的性质决定的：盘中那份允许
    「没给分」的推荐（页面上那一格显示「未给分」），而固定榜每一行都要有名次，
    一个没有分的行排不进去，所以缺分/越界的行在这里被剔掉并计数（`rejected`）。
    排序用它自己给的分，并列时按输入里的位次（相似度榜更靠前的那只在前）。
    """
    known = {str(item.get("stock_code") or ""): item for item in feed}
    blob = json_object_of(content)
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError as error:
        raise AiFailure(f"回包里的 JSON 解析失败：{error}；原文摘要 {_brief(content)}") from error
    if not isinstance(payload, dict):
        raise AiFailure(f"回包的顶层不是对象：{_brief(content)}")
    raw_picks = payload.get("picks")
    if not isinstance(raw_picks, list):
        raise AiFailure(f"回包里没有 picks 数组：{_brief(content)}")

    picked: list[dict] = []
    dropped: list[str] = []
    duplicates: list[str] = []
    rejected: list[str] = []
    truncated = False
    seen: set[str] = set()
    for item in raw_picks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stock_code") or "").strip()
        row = known.get(code)
        if row is None:
            if code:
                dropped.append(code)
            continue
        if code in seen:
            duplicates.append(code)
            continue
        score = model_score_of(item.get("score"))
        if score is None:
            #: 没有合法分就排不出名次：计数并丢掉，**不补一个 0 分**，也不夹到量程内
            rejected.append(code)
            continue
        seen.add(code)
        reason, cut_reason = text_of(item.get("reason"))
        counter, cut_counter = text_of(item.get("counter_evidence"))
        truncated = truncated or cut_reason or cut_counter
        evidence = evidence_with_gap(row, score)
        picked.append(
            {
                "stock_code": code,
                "stock_name": str(row.get("stock_name") or ""),
                "score": score,
                "reason": reason,
                "counter_evidence": counter,
                "feed_rank": int(row.get("feed_rank") or 0) or None,
                "formula": evidence.get("formula") or {},
                "indicator_scores": dict(row.get("indicator_scores") or {}),
                "evidence": evidence,
                #: 数字追不回出处的盖章在 `_unverified_codes()` 里判，这里先占个位
                "unverified_citation": False,
            }
        )

    picked.sort(key=lambda item: (-(item["score"] or 0.0), item["feed_rank"] or 0, item["stock_code"]))
    beyond_limit = max(0, len(picked) - DAILY_PICK_COUNT)
    kept = picked[:DAILY_PICK_COUNT]
    flagged = _unverified_codes(kept, known)
    for item in kept:
        if item["stock_code"] in flagged:
            item["unverified_citation"] = True
    criteria, criteria_truncated = criteria_of(payload.get("criteria"))
    return {
        "picks": [{**item, "rank": index + 1} for index, item in enumerate(kept)],
        "criteria": criteria,
        "criteria_truncated": criteria_truncated,
        "text_truncated": truncated,
        "dropped": dropped,
        "duplicates": duplicates,
        "rejected": rejected,
        "beyond_limit": beyond_limit,
        "unverified": flagged,
    }


def _numbers_of(value: Any, into: set[float]) -> set[float]:
    """把一份证据里所有**数字**收成集合（bool 不算：True 会变成 1，凭空多一个可对上的数）。"""
    if isinstance(value, bool) or value is None:
        return into
    if isinstance(value, (int, float)):
        number = float(value)
        if number == number:
            into.add(round(number, 4))
        return into
    if isinstance(value, dict):
        for item in value.values():
            _numbers_of(item, into)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _numbers_of(item, into)
    elif isinstance(value, str):
        for token in _CITATION_RE.findall(value):
            into.add(round(float(token), 4))
    return into


def _unverified_codes(picks: list[dict], known: dict[str, dict]) -> list[str]:
    """哪些推荐的理由里引用了输入里追不回来的数字（只盖章，绝不删行）。

    判据是启发式的，所以它**只能加标签、不能掐结论**：一句「3 家券商看涨」里的数字
    本来就不在输入里，删掉那条推荐等于把用户付过钱的东西弄丢。反过来，一句不带数字的
    理由（纯定性）不算违规——它没什么可对账的。

    规则：一只推荐的理由 + 反证里出现的每个数，只要有一个能在**它自己那一行证据**
    （含 `ai_history`、`feed_rank`）里对上（±0.01 容差，覆盖四舍五入的写法），
    这一只就不盖章；一个都对不上才进清单。代码里的数字不参与对账。

    可比对的数字来自 `prompt_row_of(row)`——也就是**它被展示过的那一行**，
    而不是落库的完整证据：模型压根没看见的项，不能拿来当「有出处」给它放行。
    """
    flagged: list[str] = []
    for item in picks:
        code = str(item.get("stock_code") or "")
        row = known.get(code) or {}
        text = f"{item.get('reason') or ''} {item.get('counter_evidence') or ''}"
        cited = [float(token) for token in _CITATION_RE.findall(text)]
        if not cited:
            continue
        own = _numbers_of(ai_evidence.prompt_row_of(row), set())
        try:
            own.add(float(code))
        except (TypeError, ValueError):
            pass
        if any(abs(round(value, 4) - number) <= 0.01 for number in cited for value in own):
            continue
        flagged.append(code)
    return flagged


# --- 花钱的那一半 ------------------------------------------------------------


def rank_now(session, *, trade_date: str, on_progress: Callable[[int, int], None] | None = None) -> dict:
    """读落库的那份输入、敲一次网关、把名次算出来（**本函数不落库**，落库交给调用方）。

    返回的那一份永远带 `request_sent`：钱有没有花，页面上必须一句话就能问出来。
    「没有备好的输入」与「网关没回话」是两种失败，前者一次请求都没发。

    与盘中分析同一个纪律：证据一读齐就 `session.close()`，那一段等待（上限
    `DAILY_RANK_RUN_TIMEOUT_SECONDS`）绝不占着连接池——把一次外部等待算进事务里，
    等于让页面、监控轮次、落库一起排那十条连接的队。
    """
    model = ai_gateway.model_name()
    if not ai_gateway.ai_ready():
        return failed_outcome(
            ai_gateway.not_configured_message(),
            request_sent=False, billed=False, model=model, trade_date=trade_date,
        )
    row = get_board(session, trade_date)
    if row is None:
        return failed_outcome(
            f"{trade_date} 没有备好的榜（这一天的输入还没凑过，或那天不是交易日）。"
            "可以先点「备好输入」，一分钱不花",
            request_sent=False, billed=False, model=model, trade_date=trade_date,
        )
    payload = row.payload if isinstance(row.payload, dict) else {}
    prepared = payload.get("prepared") if isinstance(payload.get("prepared"), dict) else {}
    feed = prepared.get("feed") if isinstance(prepared.get("feed"), list) else []
    meta = {
        "row_id": int(row.id),
        "trade_date": row.trade_date.isoformat() if row.trade_date else trade_date,
        "base_date": row.base_date.isoformat() if row.base_date else "",
        "window_mode": row.window_mode or "",
        "metric": row.metric or "",
        "metric_label": row.metric_label or "",
        "prepared_at": row.prepared_at or "",
        "board_rows": int(row.board_rows or 0),
        "feed_rows": int(row.feed_rows or 0),
    }
    if not feed:
        return failed_outcome(
            f"{meta['trade_date']} 那一行的输入是空的（备榜时一行都没凑出来），这一次没有发请求",
            request_sent=False, billed=False, model=model, **meta,
        )

    messages = build_ranking_messages(feed, meta=meta)
    session.close()

    started = time.monotonic()
    started_at = _now_iso()
    use_format = True
    try:
        content, usage = ai_gateway.call_model(
            messages,
            timeout_seconds=DAILY_RANK_RUN_TIMEOUT_SECONDS,
            on_progress=on_progress,
            use_response_format=True,
        )
    except ai_gateway.AiGatewayError as error:
        if error.rejects_response_format:
            logger.info("上游不接受 response_format（每日固定榜），去掉该参数重发一次：%s", _brief(str(error)))
            use_format = False
            try:
                content, usage = ai_gateway.call_model(
                    messages,
                    timeout_seconds=DAILY_RANK_RUN_TIMEOUT_SECONDS,
                    on_progress=on_progress,
                    use_response_format=False,
                )
            except ai_gateway.AiGatewayError as retry_error:
                return failed_outcome(
                    f"不带 response_format 重发后仍然失败：{retry_error}",
                    request_sent=True, billed=bool(getattr(retry_error, "billed", True)),
                    model=model, started_at=started_at,
                    latency=round(time.monotonic() - started, 1), raw=str(retry_error), **meta,
                )
        else:
            return failed_outcome(
                str(error), request_sent=True, billed=bool(getattr(error, "billed", True)),
                model=model, started_at=started_at,
                latency=round(time.monotonic() - started, 1), **meta,
            )

    latency = round(time.monotonic() - started, 1)
    try:
        answer = parse_ranking(content, feed)
    except (AiFailure, ai_gateway.AiGatewayError) as error:
        return failed_outcome(
            str(error), request_sent=True, model=model, started_at=started_at,
            latency=latency, raw=content, **meta,
        )
    if not answer["picks"]:
        return failed_outcome(
            f"模型没有给出任何可用的名次（池外代码 {len(answer['dropped'])} 个、"
            f"缺分或越界 {len(answer['rejected'])} 个），这一次花钱没有换来结论",
            request_sent=True, model=model, started_at=started_at, latency=latency, raw=content, **meta,
        )

    logger.info(
        "每日 AI 固定榜完成：%s 行进提示词（榜 %s 行），名次 %s 条，池外 %s、重复 %s、缺分 %s、"
        "截在 %s 名之外 %s，数字追不回出处 %s 只，耗时 %ss，tokens=%s",
        len(feed), meta["board_rows"], len(answer["picks"]), len(answer["dropped"]),
        len(answer["duplicates"]), len(answer["rejected"]), DAILY_PICK_COUNT,
        answer["beyond_limit"], len(answer["unverified"]), latency, usage,
    )
    return {
        "ok": True,
        "status": STATUS_DONE,
        "request_sent": True,
        "billed": True,
        "row_id": meta["row_id"],
        "trade_date": meta["trade_date"],
        "model": model,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "latency_seconds": latency,
        "error": "",
        "result": {
            #: 钱这两笔跟着结论一起落库：重启之后页面还能说清上一趟花没花，
            #: 不至于只剩一句 429 让人猜。
            "request_sent": True,
            "billed": True,
            "criteria": answer["criteria"],
            "criteria_truncated": answer["criteria_truncated"],
            "text_truncated": answer["text_truncated"],
            "picks": answer["picks"],
            "picks_count": len(answer["picks"]),
            "beyond_limit": answer["beyond_limit"],
            "dropped_codes": answer["dropped"],
            "dropped_codes_count": len(answer["dropped"]),
            "duplicate_codes": answer["duplicates"],
            "rejected_codes": answer["rejected"],
            "rejected_count": len(answer["rejected"]),
            "unverified_codes": answer["unverified"],
            "unverified_citations": len(answer["unverified"]),
            "usage": usage,
            "cost_text": cost_of(usage),
            "response_format": use_format,
            "raw_excerpt": content[:AI_RAW_EXCERPT_LIMIT],
            "feed_rows": len(feed),
            "board_rows": meta["board_rows"],
            "formula": daily_formula(metric_label=str(meta["metric_label"] or "")),
            "disclaimer": ai_evidence.AI_DISCLAIMER,
        },
    }


def failed_outcome(error: str, *, request_sent: bool, model: str, started_at: str = "",
                   latency: float = 0.0, raw: str = "", billed: bool = True,
                   **meta: Any) -> dict:
    return {
        "ok": False,
        "status": STATUS_FAILED,
        "request_sent": bool(request_sent),
        #: 「请求交没交给网关」与「这一趟按 token 计了费没有」是两件事：
        #: 上游回 429/5xx 那种挡在做结论之前的失败，是发出去了但没花钱。
        "billed": bool(billed),
        "row_id": meta.get("row_id"),
        "trade_date": str(meta.get("trade_date") or ""),
        "model": model,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "latency_seconds": latency,
        "error": str(error)[:1000],
        "result": {
            "request_sent": bool(request_sent),
            "billed": bool(billed),
            "criteria": "",
            "picks": [],
            "picks_count": 0,
            "beyond_limit": 0,
            "dropped_codes": [],
            "dropped_codes_count": 0,
            "duplicate_codes": [],
            "rejected_codes": [],
            "rejected_count": 0,
            "unverified_codes": [],
            "unverified_citations": 0,
            "usage": {},
            "cost_text": "",
            "raw_excerpt": raw[:AI_RAW_EXCERPT_LIMIT],
            "feed_rows": int(meta.get("feed_rows") or 0),
            "board_rows": int(meta.get("board_rows") or 0),
            "disclaimer": ai_evidence.AI_DISCLAIMER,
        },
    }


def persist_outcome(session, outcome: dict) -> dict:
    """把名次落到那一行，并把推荐进对账账本。回一份给页面的读值。

    落库的异常不吞：这一条链路的结论就住在这张表里，写不进去必须让页面知道
    （盘中那一份有内存兜底，这一份没有）。
    """
    from backend.app.models.tables import AiDailyRankBoard

    row = get_board(session, outcome.get("trade_date"))
    if row is None:
        row = AiDailyRankBoard(trade_date=_as_date(outcome.get("trade_date")))
        session.add(row)
    result = dict(outcome.get("result") or {})
    payload = dict(row.payload or {})
    if result:
        payload["result"] = result
    else:
        payload.pop("result", None)
    row.status = STATUS_DONE if outcome.get("ok") else STATUS_FAILED
    row.payload = payload
    row.model = str(outcome.get("model") or "")[:64]
    row.started_at = str(outcome.get("started_at") or "")
    row.finished_at = str(outcome.get("finished_at") or "")
    row.latency_seconds = outcome.get("latency_seconds")
    row.error = str(outcome.get("error") or "")[:1000]
    row.picks_count = int(result.get("picks_count") or 0)
    row.picks_beyond_limit = int(result.get("beyond_limit") or 0)
    row.dropped_codes_count = int(result.get("dropped_codes_count") or 0)
    row.rejected_count = int(result.get("rejected_count") or 0)
    row.unverified_citations = int(result.get("unverified_citations") or 0)
    row.cost_text = str(result.get("cost_text") or "")[:120]
    session.flush()
    recorded = 0
    if outcome.get("ok"):
        recorded = ai_picks.record_picks(
            session,
            {
                "trade_date": outcome.get("trade_date"),
                "model": outcome.get("model"),
                "finished_at": outcome.get("finished_at"),
                "criteria": result.get("criteria"),
                "metric": row.metric,
                "report_id": int(row.id),
                "picks": result.get("picks") or [],
            },
            source="daily_rank",
        )
    # 90 天以外的固定榜整行删掉：那一天的输入有 100 行证据，留着只会把库撑大。
    # 拿日期比日期：`trade_date` 是 Date 列，跟 datetime 比会退化成字符串比较。
    deadline = clock.today() - timedelta(days=DAILY_RANK_RETENTION_DAYS)
    stale = session.query(AiDailyRankBoard).filter(AiDailyRankBoard.trade_date < deadline).all()
    for stale_row in stale:
        session.delete(stale_row)
    logger.info("每日固定榜已落库（%s / %s，名次 %s 条，进账本 %s 条，清掉 %s 天旧榜）",
                row.trade_date, row.status, row.picks_count, recorded, len(stale))
    return board_view(row)


def feed_of(row) -> list[dict]:
    payload = dict(row.payload or {})
    prepared = payload.get("prepared") if isinstance(payload.get("prepared"), dict) else {}
    feed = prepared.get("feed") if isinstance(prepared.get("feed"), list) else []
    return [{key: item.get(key) for key in (
        "feed_rank", "stock_code", "stock_name", "board", "at_limit", "last_price", "change_percent",
        "to_limit_percent", "max_similarity", "avg_similarity", "high_sim_count", "sample_count", "band")}
        for item in feed if isinstance(item, dict)]



def _cost_text(row) -> str:
    """结果里那份是全的；列上那份老版本截在 40 个字，「其中思考 N」正好被截掉。"""
    payload = row.payload if isinstance(row.payload, dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return str(result.get("cost_text") or "") or (row.cost_text or "")


def board_view(row) -> dict:
    """给页面的一行固定榜。`feed` 只带页面要的那几列，完整证据留在 `payload` 里。"""
    payload = dict(row.payload or {})
    result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    prepared = payload.get("prepared") if isinstance(payload.get("prepared"), dict) else {}
    metric_item = RANK_METRICS.get(str(row.metric or "")) or RANK_METRICS[DEFAULT_METRIC]
    return {
        "row_id": int(row.id),
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "base_date": row.base_date.isoformat() if row.base_date else None,
        "status": row.status,
        #: 「成功了吗」必须从 `status` 推。后台线程那边读的是这一份视图，
        #: 而它自己那份 `ok` 是 `rank_now` 的返回值、落库之后就留在这几个列里——
        #: 视图里没有这一项时 `.get("ok")` 永远回 None：名次排好了，日志与状态却说「失败」。
        "ok": str(row.status or "") == STATUS_DONE,
        "model": row.model or "",
        "prepared_at": row.prepared_at or "",
        "started_at": row.started_at or "",
        "finished_at": row.finished_at or "",
        "latency_seconds": row.latency_seconds,
        "board_rows": int(row.board_rows or 0),
        "feed_rows": int(row.feed_rows or 0),
        "window_mode": row.window_mode or "",
        "metric": row.metric or metric_item["key"],
        "metric_label": row.metric_label or metric_item["label"],
        "picks_count": int(row.picks_count or 0),
        "picks_beyond_limit": int(row.picks_beyond_limit or 0),
        "dropped_codes_count": int(row.dropped_codes_count or 0),
        "rejected_count": int(row.rejected_count or 0),
        "unverified_citations": int(row.unverified_citations or 0),
        "cost_text": _cost_text(row),
        "error": row.error or "",
        "result": result,
        "feed": feed_of(row),
        "extra": prepared.get("extra") if isinstance(prepared.get("extra"), dict) else {},
        "formula": daily_formula(metric_label=str(row.metric_label or "")),
        "disclaimer": ai_evidence.AI_DISCLAIMER,
    }


# --- 后台执行 ---------------------------------------------------------------


class DailyRankRunner:
    """排名那一次花钱的调用放在线程里：POST 起线程，GET 轮询，槽位非阻塞抢占。

    与盘中那条链路同一个跑法，但是槽位只有一个（一天一份榜），
    而且它排的是**落库的那份输入**而不是当场抓的榜——所以备输入与排名是两次独立的操作，
    备输入不进线程（本系统自己算，一秒级），排名进线程。
    """

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        #: 槽位与进度合用一本账：`_current` 在不在，就是「这一天有没有正在排名」。
        #: 以前另有一把锁，两本账交接的那一瞬会露出「没在跑、又说这一份还在跑」的空档。
        self._current: dict[str, Any] | None = None
        self._last: dict[str, Any] = {}

    def start(self, trade_date: str) -> dict:
        if not ai_gateway.ai_ready():
            raise ai_gateway.not_configured()
        started_at = _now_iso()
        with self._state_lock:
            if self._current is not None:
                #: 幂等：已经有一份在跑就接着等那一份，不重复花钱
                waiting = dict(self._current)
                waiting.pop("since", None)
                waiting.pop("thread", None)
                return {"accepted": False, "already_running": True, **waiting}
            self._current = {"trade_date": trade_date, "started_at": started_at,
                             "since": time.monotonic(), "answer_chars": 0, "thinking_chars": 0}
        try:
            threading.Thread(target=self._guarded, args=(trade_date,), name="ai-daily-rank", daemon=True).start()
        except BaseException:
            #: 线程根本没起来就不该继续占着这一天，否则「排名」这颗按钮要到重启才再按得动
            with self._state_lock:
                self._retire()
            raise
        return {"accepted": True, "already_running": False, "trade_date": trade_date, "started_at": started_at}

    def status(self) -> dict:
        with self._state_lock:
            current = dict(self._current) if self._current else None
            last = dict(self._last)
        if current is None:
            return {"running": False, "last": last}
        mark = current.pop("since", None)
        current.pop("thread", None)
        if mark is not None:
            current["running_seconds"] = round(time.monotonic() - mark, 1)
        return {"running": True, "last": last, **current}

    def reset(self) -> None:
        with self._state_lock:
            self._current = None
            self._last = {}

    def run_once(self, trade_date: str) -> dict:
        """同步跑完整一次排名并落库。线程走这一条，测试也走这一条。"""
        outcome: dict = {}
        try:
            with session_scope() as session:
                outcome = rank_now(session, trade_date=trade_date, on_progress=self._progress)
                view = persist_outcome(session, outcome)
        except Exception as error:  # noqa: BLE001 - 后台线程绝不能静默死掉
            logger.exception("每日固定榜这一趟崩了：%s", error)
            view = {"ok": False, "error": f"{type(error).__name__}: {_brief(str(error))}",
                    "trade_date": trade_date, "status": STATUS_FAILED}
        self._finish(trade_date, view, outcome)
        return view

    def _finish(self, trade_date: str, view: dict, outcome: dict | None = None) -> None:
        """落库之后，把「这一趟的结果」和「不再在跑」写进同一本账。

        库里那份已经提交、进度却还挂着，轮询就只会一直等；反过来先清进度再记账，
        页面上会出现「没在跑、可名次还是备输入那一份」——花钱排好的名次得等重进页面才出现。

        「请求发出去了没」与「这一趟花钱了没」必须抄 `rank_now` 那份账，不能从 `status` 倒推：
        上游回 429 时库里那份确实是 `failed`，但请求明明交出去了、而一个 token 都没生成，
        照状态推就会写成「请求没有发出，一分钱没花」——前半句是假的。
        """
        outcome = outcome or {}
        with self._state_lock:
            self._last = {"trade_date": trade_date, "finished_at": _now_iso(),
                          "ok": bool(view.get("ok")),
                          "request_sent": bool(outcome.get("request_sent", outcome_sent(view))),
                          #: 兜底那一路宁可回「说不准」也别回「没花钱」：`rank_now` 整个抛出来时，
                          #: 崩点可能在敲网关之后（解析、落库那几步），那一个 token 也许已经生成。
                          "billed": bool(outcome.get("billed", True))}
            self._retire()

    def _retire(self) -> None:
        """只交属于本线程那一趟的槽位。调用方必须已经握着 `_state_lock`。"""
        if self._current is not None and self._current.get("thread") in (None, threading.get_ident()):
            self._current = None

    def _progress(self, answer_chars: int, thinking_chars: int) -> None:
        with self._state_lock:
            if self._current is not None:
                self._current["answer_chars"] = answer_chars
                self._current["thinking_chars"] = thinking_chars

    def _guarded(self, trade_date: str) -> None:
        with self._state_lock:
            if self._current is not None:
                #: 从这一行起，这一天的账就归本线程了
                self._current["thread"] = threading.get_ident()
        try:
            view = self.run_once(trade_date)
            logger.info("每日固定榜线程结束：%s", "成功" if view.get("ok") else f"失败（{str(view.get('error'))[:120]}）")
        finally:
            #: 正常那一路 `_finish` 已经交回过了，这里是空操作；留这一手是给 `_finish` 之前
            #: 就被打断的退出路径（BaseException）兜底，不让那一天永远占着槽位。
            with self._state_lock:
                self._retire()


def outcome_sent(view: dict) -> bool:
    """兜底用的旧推法：`done` 一定敲过网关，`prepared` 一定没敲过。

    现在只有 `rank_now` 那份账都拿不到时（线程在敲网关之前就崩了）才落到这里——
    那种情况下「failed」说不准花没花钱，照实回 False 好过替它圆。
    正常那一路读的是 `rank_now` 自己写的 `request_sent/billed`，别再用这个推。
    """
    return str(view.get("status") or "") == STATUS_DONE


#: 进程内单例
daily_rank_runner = DailyRankRunner()


def state(session, day: str | None = None) -> dict:
    """页面读的那一份：某一天的榜（没备过就回最近一天）+ 有没有在跑。

    先问在不在跑、再读库里那一份。反过来读会撞上一个真空档：库里还是「备好了、没排名」，
    而后台线程刚好在这一瞬跑完并清掉了进度——页面于是显示「没在跑、也还没排名」，
    把已经花钱排好的那一份藏到用户重进页面之后才出现。
    """
    running = daily_rank_runner.status()
    row = get_board(session, day)
    view = board_view(row) if row is not None else None
    return {"board": view, "asked_date": str(day or ""), "runner": running}


def history(session, limit: int = 15) -> dict:
    from backend.app.models.tables import AiDailyRankBoard

    rows = session.query(AiDailyRankBoard).order_by(AiDailyRankBoard.trade_date.desc()).limit(
        max(1, min(60, int(limit)))
    ).all()
    return {
        "items": [
            {
                "trade_date": row.trade_date.isoformat() if row.trade_date else None,
                "status": row.status,
                "metric_label": row.metric_label,
                "model": row.model,
                "prepared_at": row.prepared_at,
                "finished_at": row.finished_at,
                "latency_seconds": row.latency_seconds,
                "feed_rows": int(row.feed_rows or 0),
                "picks_count": int(row.picks_count or 0),
                "rejected_count": int(row.rejected_count or 0),
                "unverified_citations": int(row.unverified_citations or 0),
                "cost_text": _cost_text(row),
                "error": (row.error or "")[:200],
            }
            for row in rows
        ],
        "retention_days": DAILY_RANK_RETENTION_DAYS,
        "feed_limit": DAILY_FEED_LIMIT,
        "pick_count": DAILY_PICK_COUNT,
    }


__all__ = [
    "DAILY_FEED_LIMIT",
    "DAILY_METRIC",
    "DAILY_PICK_COUNT",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PREPARED",
    "DailyRankRunner",
    "board_view",
    "build_ranking_messages",
    "daily_formula",
    "daily_rank_runner",
    "failed_outcome",
    "get_board",
    "history",
    "parse_ranking",
    "persist_outcome",
    "prepare_board",
    "prepare_from_tracker",
    "rank_now",
    "state",
]
