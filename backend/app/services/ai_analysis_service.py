"""盘中整榜分析与单股直评：两条路共用同一次「凑证据 → 敲网关 → 校验回包 → 成报告」。

四条红线是从老工程一路带过来的，这里一条都不松：

1. **算式只写一处**。综合评分、等级、模型分与公式分的差，全部由 `score_model` 与
   `_evidence_with_gap()` 算，页面上不许再抄一遍公式。
2. **本地分不进模型**。喂给它的 `formula` 是本系统的读数，提示词里明写「不要重算、不要改数」，
   它自报的那一分不参与上面那个加权。
3. **校验闸只盖章不删行**（删行只在「池外代码」这一种情况发生）：池外代码是幻觉，
   留下来等于我们自己编了一只股票；而一句追不回出处的理由只是可疑，删掉它
   等于把用户付过钱的东西弄丢。
4. **空档不显示 0**。模型没给分就是 `null`，`has_signal` 为假的公式分就是 `null`——
   0 分是一种读数，「无从谈起」不是。

跑法与 `realtime_tracker` 同一个量级：一次分析要几十秒到几分钟，绝不能压在
2 秒一次的刷榜请求里，所以 POST 起线程、GET 轮询，槽位非阻塞抢占，
抢不到就照实说「那一份还在跑」，第二条 POST 不排队也不重复花钱。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Callable

from backend.app.core.clock import SHANGHAI, in_session
from backend.app.core.constants import (
    AI_RAW_EXCERPT_LIMIT,
    AI_REPORT_TTL_SECONDS,
    AI_RUN_TIMEOUT_SECONDS,
    CRITERIA_LIMIT,
    RECOMMEND_COUNT,
    TEXT_LIMIT,
)
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.services import ai_evidence, ai_gateway, ai_picks
from backend.app.services.ai_analysis_store import cost_of
from backend.app.services.ai_analysis_store import latest_report as archived_report
from backend.app.services.ai_analysis_store import persist_report, trim
from backend.app.services.score_model import formula_text, with_gap

logger = get_logger("ai_analysis")

CHAIN_BOARD = "realtime_board"
CHAIN_SINGLE = "single"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_RUNNING = "running"

TASK_BOARD = (
    "任务：从池子里挑出你认为「明天更值得看」的最多{limit}只，"
    "给出 0~100 的相对判断分（不是涨停概率）、一句话理由、一句话反证（它哪里不行）。"
    "你的分与本系统算好的综合评分可以不同，不同就把差的原因写进理由里；"
    "另外用一段话写清你这一次是按什么逻辑挑的（criteria）：这套标准原样展示给用户看，"
    "写你自己真正的取舍，不要复述字段清单，也不要写成免责话术。"
)

TASK_SINGLE = (
    "任务：只对这一只股票给一个 0~100 的相对判断分（不是涨停概率），"
    "picks 数组里只放它一条，再给一句话理由与一句话反证（它哪里不行）。"
    "你的分与本系统算好的综合评分可以不同，不同就把差的原因写进理由里；"
    "另外用一段话写清你这一次是按什么逻辑判的（criteria）：这套标准原样展示给用户看，"
    "写你自己真正的取舍，不要复述字段清单，也不要写成免责话术。"
)

OUTPUT_SHAPE = (
    "必须只输出一个 JSON 对象，形如 "
    '{"criteria":"...","picks":[{"stock_code":"600072","score":78.5,'
    '"reason":"...","counter_evidence":"..."}]}，'
    "stock_code 必须原样来自输入，不要输出别的文字，不要用 markdown 代码块包裹。"
)

SCORE_MIN = 0.0
SCORE_MAX = 100.0


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _brief(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def json_object_of(content: str) -> str:
    """剥掉 markdown 代码块与前后闲字，截出第一个 `{` 到最后一个 `}`。

    实测这类网关的 `content` 前面会带两个换行（不带 `response_format` 时），
    有时还会被 ```json 包住；这两种都不该让一次好端端的分析判失败。
    """
    text = str(content or "").strip()
    if text.startswith("```"):
        _, _, rest = text.partition("\n")
        text = rest.rsplit("```", 1)[0].strip()
    low, high = text.find("{"), text.rfind("}")
    if low >= 0 and high > low:
        return text[low : high + 1]
    return text


def text_of(value: Any, limit: int = TEXT_LIMIT) -> tuple[str, bool]:
    """(文字, 是否被截断)。空文字就是空，不替模型补一句「未提供」。"""
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit], True
    return text, False


def criteria_of(value: Any) -> tuple[str, bool]:
    """它自述的判断标准：只收文字，其它形状一律算「没自述」。

    给进来的是对象/数组时不做 `str()` 兜底——那会把 `{'a': 1}` 这种结构印到页面上
    冒充「它的逻辑」，而把一段数组拼成一句话又是我们的拼法。
    """
    if isinstance(value, str):
        return text_of(value, CRITERIA_LIMIT)
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if isinstance(part, str)]
        parts = [part for part in parts if part]
        if parts:
            return text_of("；".join(parts), CRITERIA_LIMIT)
        return "", False
    if value is None:
        return "", False
    logger.warning("回包里的 criteria 不是文字（%s），按「它没自述判断标准」处理", type(value).__name__)
    return "", False


def model_score_of(value: Any) -> float | None:
    """模型分只收 0~100 的数字，其它一律 None（页面上显示「未给分」而不是 0）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN 到不了量程判断
        return None
    if not SCORE_MIN <= number <= SCORE_MAX:
        return None
    return round(number, 2)


def evidence_with_gap(row: dict, model_score: float | None) -> dict:
    """这一行的证据原值 + 模型自报分与公式分的差。**差值只在这里算一次**。

    算在后端而不是页面：页面上「差 X 分」和「公式分 Y」必须是同一份账，
    挪到前端就等于前端抄了一遍公式——那正是本系统一直避免的第二套口径。
    模型没给分时 `model_gap` 也是 `None`，不是 0：0 会被读成「它完全同意公式」。
    基准取的是 `read_composite()`：五项加权全没数时那个 `composite_score` 是 0.0，
    而那个 0 是「无从谈起」不是分数，拿它当基准就会写出「与公式差 66.5」这种没有对象的差。
    """
    formula = row.get("formula")
    merged: dict[str, Any] = {}
    if isinstance(formula, dict):
        merged = with_gap(formula, model_score)
    merged["model_score"] = model_score
    evidence = {key: value for key, value in row.items() if key != "formula"}
    evidence["formula"] = merged
    return evidence


# --- 提示词 ------------------------------------------------------------------


def build_messages(bundle: dict, *, task: str = TASK_BOARD, limit: int = RECOMMEND_COUNT) -> list[dict[str, str]]:
    """角色 + 输入是什么 + 给定的评分模型 + 防编造 + 要回什么形状。

    交代的是**算式与字段出处**，不是「该怎么想」：分已经由本系统算好了，
    它要做的是在给定权重下解读这些数，并写清自己的读法。
    """
    system = (
        ai_evidence.system_prompt()
        + task.replace("{limit}", str(limit))
        + OUTPUT_SHAPE
    )
    label = "盘中监控池" if limit > 1 else "这一只股票的证据"
    user = (
        f"{label}（本系统监控榜当前排序的前 {bundle['feed_rows']} 行）：\n"
        if limit > 1
        else "这一只股票的证据：\n"
    )
    market = bundle.get("market_high_similarity") or {}
    if market.get("high_rate_percent") is not None:
        #: 市场级那一句只说数与出处，不加解释：它没有逐行落进 `ai_evidence._FIELD_DOC`，
        #: 写多了就成了第二处口径。没跑过扫描时这一句整条不出现，不写「0%」。
        user += (
            f"当天全市场落库匹配汇总 {market.get('targets')} 只里，"
            f"相似度不低于 {market.get('high_threshold')} 的样本占比 "
            f"{market.get('high_rate_percent')}%（出处 match_summaries）。\n"
        )
    rows = [ai_evidence.prompt_row_of(row) for row in bundle["rows"]]
    user += json.dumps(rows, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def board_meta(board: dict, selection: dict | None = None) -> dict:
    """把「这一份分析对应哪一轮榜」固定成一组字段，成功与失败报告都带同一份。"""
    extra = selection or {}
    return {
        "trade_date": str(board.get("trade_date") or ""),
        "base_date": str(board.get("base_date") or ""),
        "window_mode": str(extra.get("window_mode") or board.get("window_mode") or ""),
        "metric": str(extra.get("metric") or board.get("metric") or ""),
        "metric_label": str(extra.get("metric_label") or board.get("metric_label") or ""),
        "top_n": int(extra.get("top_n") or board.get("top_n") or 0),
        "source": str(board.get("source") or ""),
        "pool_size": int(board.get("pool_size") or 0),
    }


# --- 校验回包 ----------------------------------------------------------------


#: 解析与调用侧共用同一个异常名：`ai_gateway` 里那个类管网关，这里管「回包不成形状」。
class AiFailure(RuntimeError):
    """回包读不懂。与网关失败一样收成一份 `ok=False` 的报告，不抛给页面。"""


def parse_picks(content: str, known: dict[str, dict], *, pick_limit: int = RECOMMEND_COUNT) -> dict:
    """把模型原文变成推荐清单 + 它自述的判断标准，并且**只认池内代码**。

    整体不是 JSON 时抛 `AiFailure`：宁可整份分析判失败，
    也不从一段散文里抠一个像代码的字符串出来。

    截到 `pick_limit` 用的是**它自己给的 `score`**（并列时按代码），这是它逻辑的一部分，
    不是本系统的权衡；顺序只影响展示，不改它的任何结论。
    """
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

    picks: list[dict] = []
    dropped: list[str] = []
    duplicates: list[str] = []
    truncated = False
    seen: set[str] = set()
    for item in raw_picks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stock_code") or "").strip()
        row = known.get(code)
        if row is None:
            #: 池外代码就是幻觉。丢掉并计数，绝不「帮忙」猜一个相近的代码。
            if code:
                dropped.append(code)
            continue
        if code in seen:
            duplicates.append(code)
            continue
        seen.add(code)
        reason, cut_reason = text_of(item.get("reason"))
        counter, cut_counter = text_of(item.get("counter_evidence"))
        truncated = truncated or cut_reason or cut_counter
        score = model_score_of(item.get("score"))
        picks.append(
            {
                "stock_code": code,
                "stock_name": str(row.get("stock_name") or ""),
                "score": score,
                "reason": reason,
                "counter_evidence": counter,
                "feed_rank": row.get("feed_rank"),
                "evidence": evidence_with_gap(row, score),
            }
        )
    picks.sort(key=lambda pick: (-(pick["score"] or 0.0), pick["stock_code"]))
    criteria, criteria_truncated = criteria_of(payload.get("criteria"))
    return {
        "picks": picks[:max(1, int(pick_limit))],
        "dropped": dropped,
        "duplicates": duplicates,
        "text_truncated": truncated,
        "beyond_limit": max(0, len(picks) - max(1, int(pick_limit))),
        "criteria": criteria,
        "criteria_truncated": criteria_truncated,
    }


# --- 一次完整分析 ------------------------------------------------------------


def run_analysis(
    session,
    board: dict,
    *,
    selection: dict | None = None,
    chain: str = CHAIN_BOARD,
    pick_limit: int = RECOMMEND_COUNT,
    row_limit: int = ai_evidence.DEFAULT_ROW_LIMIT,
    on_progress: Callable[[int, int], None] | None = None,
    messages_for: Callable[[dict], list[dict[str, str]]] | None = None,
) -> dict:
    """凑证据 → 调模型 → 校验回包 → 出一份带现场报告的结果。

    这里**不抛异常**：任何上游/解析问题都变成一份 `ok=False` 的报告，里面写清是哪一步不行、
    耗时多少、模型原文摘要是什么。页面显示失败报告比显示一个 500 有用得多，
    而失败报告里绝不带任何编出来的推荐。

    `messages_for` 是**唯一**允许换的东西：提示词的拼法。单股直评喂的是一行的榜，
    任务那句必须跟着改，而凑证据、还连接、降级重发、校验回包这一整段阶梯只有一份实现——
    抄第二份的话，「等网关时不占连接」这种修过的病会在另一条路上复发。
    """
    model = ai_gateway.model_name()
    if not ai_gateway.ai_ready():
        return failed_report(
            ai_gateway.not_configured_message(),
            model=model,
            request_sent=False,
            billed=False,
            **board_meta(board, selection),
            chain=chain,
        )

    meta = board_meta(board, selection)
    started = time.monotonic()
    started_at = _now_iso()
    bundle = ai_evidence.build_evidence(session, board, row_limit=row_limit)
    #: 证据表一凑齐就**再也不读库**：后面每一步都在等外部网关回包，最长 `AI_RUN_TIMEOUT_SECONDS`。
    #: 连接池只有 5+10 条，把一次外部等待算进事务里，等于让页面、监控轮次、落库一起排这条队。
    #: `close()` 只归还连接、不销毁 session；落库走 `archive()` 自己开的会话。
    session.close()
    if not bundle["rows"]:
        return failed_report(
            f"榜上没有任何可分析的行（榜 {bundle['board_rows']} 行，"
            f"其中 {bundle['skipped_rows']} 行取不到最高相似度），这一次没有发请求",
            model=model,
            request_sent=False,
            billed=False,
            started_at=started_at,
            board=bundle,
            chain=chain,
            **meta,
        )

    messages = (messages_for or build_messages)(bundle)
    known = {str(row.get("stock_code") or ""): row for row in bundle["rows"]}

    use_format = True
    sent = False
    try:
        content, usage = ai_gateway.call_model(
            messages,
            timeout_seconds=AI_RUN_TIMEOUT_SECONDS,
            on_progress=on_progress,
            use_response_format=True,
        )
        sent = True
    except ai_gateway.AiGatewayError as error:
        sent = sent or bool(getattr(error, "sent", True))
        if not error.rejects_response_format:
            return failed_report(
                str(error),
                model=model,
                request_sent=sent,
                billed=bool(getattr(error, "billed", True)),
                started_at=started_at,
                latency=round(time.monotonic() - started, 1),
                board=bundle,
                chain=chain,
                **meta,
            )
        #: 网关明确不认 `response_format`：去掉它再发一次，这是特性不是保证
        logger.info("上游不接受 response_format，去掉该参数重发一次：%s", _brief(str(error)))
        use_format = False
        try:
            content, usage = ai_gateway.call_model(
                messages,
                timeout_seconds=AI_RUN_TIMEOUT_SECONDS,
                on_progress=on_progress,
                use_response_format=False,
            )
        except (ai_gateway.AiGatewayError, AiFailure) as retry_error:
            return failed_report(
                f"不带 response_format 重发后仍然失败：{retry_error}",
                model=model,
                request_sent=True,
                billed=bool(getattr(retry_error, "billed", True)),
                started_at=started_at,
                latency=round(time.monotonic() - started, 1),
                board=bundle,
                chain=chain,
                **meta,
            )

    latency = round(time.monotonic() - started, 1)
    try:
        answer = parse_picks(content, known, pick_limit=pick_limit)
    except (AiFailure, ai_gateway.AiGatewayError) as error:
        return failed_report(
            str(error),
            model=model,
            request_sent=True,
            started_at=started_at,
            latency=latency,
            raw=content,
            board=bundle,
            chain=chain,
            **meta,
        )
    if not answer["picks"]:
        return failed_report(
            f"模型没有给出任何池内股票（被丢掉的池外代码 {len(answer['dropped'])} 个），"
            "这一次分析没有可用结论",
            model=model,
            request_sent=True,
            started_at=started_at,
            latency=latency,
            dropped=answer["dropped"],
            raw=content,
            board=bundle,
            chain=chain,
            **meta,
        )

    logger.info(
        "%s完成（口径 %s）：%s 只进提示词（榜 %s 行），推荐 %s 只，池外 %s 个，判断标准 %s 字，"
        "response_format=%s，耗时 %ss，tokens=%s",
        "AI 单股直评" if pick_limit == 1 else "AI 综合分析",
        meta.get("metric") or "-",
        bundle["feed_rows"],
        bundle["board_rows"],
        len(answer["picks"]),
        len(answer["dropped"]),
        len(answer["criteria"]),
        use_format,
        latency,
        usage,
    )
    finished_at = _now_iso()
    return {
        "ok": True,
        "status": STATUS_OK,
        "chain": chain,
        "request_sent": True,
        "billed": True,
        "model": model,
        "error": "",
        "started_at": started_at,
        "finished_at": finished_at,
        "latency_seconds": latency,
        "picks": answer["picks"],
        "picks_beyond_limit": answer["beyond_limit"],
        "dropped_codes": answer["dropped"],
        "duplicate_codes": answer["duplicates"],
        "text_truncated": answer["text_truncated"],
        "criteria": answer["criteria"],
        "criteria_truncated": answer["criteria_truncated"],
        "usage": usage,
        "cost_text": cost_of(usage),
        "response_format": use_format,
        "raw_excerpt": content[:AI_RAW_EXCERPT_LIMIT],
        "board_rows": bundle["board_rows"],
        "feed_rows": bundle["feed_rows"],
        "skipped_rows": bundle["skipped_rows"],
        "history_missing_rows": bundle["history_missing_rows"],
        "feature_missing_rows": bundle["feature_missing_rows"],
        "board_generated_at": bundle["board_generated_at"],
        # 技术/量能/风险那三项吃的是哪一根日线：回包说清，页面上「评今天的板」与
        # 「指标是昨天的」就不可能是两件事。
        "indicator_date": bundle.get("indicator_date", ""),
        "indicator_date_source": bundle.get("indicator_date_source", ""),
        "formula": formula_text(),
        "disclaimer": ai_evidence.AI_DISCLAIMER,
        "report_id": None,
        "store_error": "",
        **meta,
    }


def failed_report(
    error: str,
    *,
    model: str,
    request_sent: bool,
    billed: bool = True,
    started_at: str = "",
    latency: float = 0.0,
    dropped: list[str] | None = None,
    raw: str = "",
    board: dict | None = None,
    chain: str = CHAIN_BOARD,
    **meta: Any,
) -> dict:
    """失败也出完整一份：`request_sent` 说请求交没交给网关，`billed` 说这一趟按 token 计了费没有，
    `feed_rows=0` 说清这次没敲网关。上游在做出结论之前就把这趟挡回来时，
    是 `request_sent=True` 而 `billed=False`——两件事不能合成一个布尔。"""
    bundle = board or {}
    return {
        "ok": False,
        "status": STATUS_FAILED,
        "chain": chain,
        "request_sent": bool(request_sent),
        "billed": bool(billed),
        "model": model,
        "error": str(error)[:1000],
        "started_at": started_at,
        "finished_at": _now_iso(),
        "latency_seconds": latency,
        "picks": [],
        "picks_beyond_limit": 0,
        "dropped_codes": list(dropped or []),
        "duplicate_codes": [],
        "text_truncated": False,
        "criteria": "",
        "criteria_truncated": False,
        "usage": {},
        "cost_text": "",
        "raw_excerpt": raw[:AI_RAW_EXCERPT_LIMIT],
        "board_rows": int(bundle.get("board_rows") or 0),
        "feed_rows": int(bundle.get("feed_rows") or 0),
        "skipped_rows": int(bundle.get("skipped_rows") or 0),
        "history_missing_rows": int(bundle.get("history_missing_rows") or 0),
        "feature_missing_rows": int(bundle.get("feature_missing_rows") or 0),
        "indicator_date": str(bundle.get("indicator_date") or ""),
        "indicator_date_source": str(bundle.get("indicator_date_source") or ""),
        "formula": formula_text(),
        "disclaimer": ai_evidence.AI_DISCLAIMER,
        "report_id": None,
        "store_error": "",
        **{key: value for key, value in meta.items()},
    }


# --- 落库 -------------------------------------------------------------------


def archive(report: dict) -> dict:
    """把这一次落进历史存档，并把「存好了（第 #N 份）」或「没存进去（原因）」写回报告。

    **`feed_rows == 0` 的报告不进历史**：那意味着连一行证据都没凑出来，也就一次网关都没敲过
    （未配置、榜还是空的）。那种「失败」没有成本也没有结论，存进历史只会让列表被开机时
    那几次误点占满——真正要留住的是敲过网关的那几份。

    落库的异常在这里吞成一句原因：分析已经跑完、钱已经花了，页面此刻必须能看到结论；
    把「没存进历史」报成「分析失败」，等于用一次落库故障把结论也毁掉。
    吞的同时必须留话——原因进 `store_error` 显示在面板上。
    """
    if not report.get("feed_rows"):
        return report
    try:
        with session_scope() as session:
            report_id = persist_report(session, report)
            written = ai_picks.record_picks(session, {**report, "report_id": report_id}, source=report["chain"])
            trimmed = trim(session)
    except Exception as error:  # noqa: BLE001 - 见上面那段
        logger.warning("AI 分析的结论没能存进历史（这一次分析本身不受影响）：%s", error)
        return {**report, "report_id": None, "store_error": f"{type(error).__name__}: {_brief(str(error))}"}
    logger.info("AI 分析已存档（第 #%s 份，推荐 %s 只进账本，清掉 %s 份旧档）", report_id, written, trimmed)
    return {**report, "report_id": int(report_id), "store_error": "", "picks_recorded": written}


# --- 单股直评的输入：一只股票的一行榜 -----------------------------------------


def one_row_board(session, code: str) -> dict:
    """只装这一只股票的一行榜。取不到就抛，让调用方说清是哪一只、为什么。

    盘中走实时快照，盘后走 `settled` 回放——和监控页同一把尺子，
    不然「直评的那一行」和「榜上的那一行」会长成两个数。
    """
    from backend.app.services import realtime_service

    mode = realtime_service.MODE_AUTO if in_session() else realtime_service.MODE_SETTLED
    result = realtime_service.scan(session, mode=mode, codes=[code])
    rows = list(result.get("rows") or [])
    if not rows:
        raise realtime_service.RealtimeUnavailable(f"{code} 这一轮没有打分结果（不在监控范围、或窗口/样本凑不齐）")
    row = rows[0]
    board = {key: value for key, value in result.items() if key != "rows"}
    board["limit_up_rows"] = [row] if row["is_limit_up"] else []
    board["pending_rows"] = [] if row["is_limit_up"] else [row]
    board["row_count"] = 1
    return board


def single_messages(bundle: dict) -> list[dict[str, str]]:
    return build_messages(bundle, task=TASK_SINGLE, limit=1)


# --- 后台执行：一个槽位一份，POST 触发 / GET 轮询 ----------------------------


class AiAnalysisRunner:
    """把「一次要几十秒到几分钟的调用」和「2 秒一次的刷榜」彻底隔开。

    非阻塞抢槽位，抢不到就不排队，直接告诉调用方「那一份还在跑」：
    两次分析各花一份真金白银，而页面上也只能显示一份结论。

    `board_provider` 由调用方给（接口层给的是取榜那一步），这样本模块不需要认识监控轮次单例，
    测试也能直接喂一张固定的榜。槽位按 `key` 分：整榜一个槽，每只股票各占一个槽，
    互不排队——直评一只股票不该把整榜那份挤掉。

    结果除了留在内存里给轮询，也从存档兜底：程序重启或 TTL 过了，
    页面读到的还是同一份结论，而不是「什么都没有」。
    """

    def __init__(self, *, label: str, thread_name: str, chain: str, pick_limit: int, row_limit: int) -> None:
        self.label = label
        self.thread_name = thread_name
        self.chain = chain
        self.pick_limit = pick_limit
        self.row_limit = row_limit
        self._state_lock = threading.Lock()
        #: 「哪一格正在跑」与「那一格的进度」合用一本账。
        #: 以前是分开的两本（一把 per-key 的锁 + 一个进度字典），两本账交接的那一瞬
        #: 会露出一个空档：进度已经清了、锁还没还——刚跑完那一刻再点一次，
        #: 回的是「上一份还在跑」，而状态那一格明明写着没在跑。
        self._running: set[str] = set()
        self._current: dict[str, dict] = {}
        self._payloads: dict[str, dict] = {}
        self._stored_at: dict[str, float] = {}

    def start(self, key: str, board_provider: Callable[[], dict], *, selection: dict | None = None) -> dict:
        """抢占这一格的槽位并起线程。没配密钥直接抛 `AiNotConfigured`，一次请求都不发。"""
        if not ai_gateway.ai_ready():
            raise ai_gateway.not_configured()
        started_at = _now_iso()
        with self._state_lock:
            if key in self._running:
                #: 幂等：已经有一份在跑就接着等那一份，不重复花钱
                waiting = dict(self._current.get(key) or {})
                waiting.pop("since", None)
                waiting.pop("thread", None)
                return {"accepted": False, "already_running": True, "label": self.label, **waiting}
            self._running.add(key)
            self._current[key] = {
                "started_at": started_at,
                "since": time.monotonic(),
                "answer_chars": 0,
                "thinking_chars": 0,
                "key": key,
                **(selection or {}),
            }
        try:
            threading.Thread(
                target=self._guarded,
                args=(key, board_provider, selection or {}),
                name=f"{self.thread_name}-{key[:8]}" if key else self.thread_name,
                daemon=True,
            ).start()
        except BaseException:
            #: 线程根本没起来，那一格就不该继续占着：否则这个槽位要一直挂到重启为止
            with self._state_lock:
                self._retire(key)
            raise
        return {"accepted": True, "already_running": False, "label": self.label, "started_at": started_at, "key": key}

    def status(self, key: str = "") -> dict:
        """当前有没有在跑、跑到第几秒、已经收到多少字。钱花到哪儿了，屏幕上得看得见。"""
        with self._state_lock:
            current = dict(self._current.get(key) or {}) if key else next(iter(self._current.values()), {})
            running = key in self._running if key else bool(self._running)
        if not current or not running:
            return {"running": running, "chain": self.chain}
        mark = current.pop("since", None)
        current.pop("thread", None)
        if mark is not None:
            current["running_seconds"] = round(time.monotonic() - mark, 1)
        return {"running": True, "chain": self.chain, **current}

    def result(self, key: str = "") -> dict:
        """轮询读的那一份：内存里有就读内存，过期或没跑过就读当天存档。"""
        with self._state_lock:
            stored = self._payloads.get(key)
            mark = self._stored_at.get(key)
        fresh = bool(stored) and mark is not None and (time.time() - mark) <= AI_REPORT_TTL_SECONDS
        if fresh:
            return {"source": "memory", "report": stored, "age_seconds": round(time.time() - mark, 1)}
        fallback = None
        code = key if self.chain == CHAIN_SINGLE else ""
        try:
            with session_scope() as session:
                fallback = archived_report(session, chain=self.chain, code=code)
        except Exception as error:  # noqa: BLE001 - 读存档失败不该把整条链路判死
            logger.warning("读 AI 存档失败：%s", error)
        if fallback:
            return {"source": "archive", "report": fallback, "memory_expired": bool(stored)}
        return {"source": "none", "report": stored if stored else None, "memory_expired": False}

    def run_once(self, key: str, board_provider: Callable[[], dict], *, selection: dict | None = None) -> dict:
        """同步跑完整一次，先落历史存档、再落内存。线程走这一条，测试也走这一条。

        落库排在给内存之前：页面读的那一份是内存里那份，它带上 `report_id` 与 `store_error`，
        用户才能当场看见「这笔钱存下来了（第 #N 份）」还是「没存进去，原因是 …」。
        """
        selection = {**(selection or {}), "key": key}
        try:
            board = board_provider()
        except ai_gateway.AiNotConfigured:
            raise
        except Exception as error:  # noqa: BLE001 - 输入侧的失败是一句真话，不是一次 500
            #: 单独一只手抓 `board_provider()`，因为「输入凑不出来」和「分析这一步崩了」
            #: 是两句完全不同的话：前者一次网关都没敲（`feed_rows=0`，`archive` 因此不落库），
            #: 后者钱可能已经花了。单股直评尤其靠这一句：那只股票今天没有行情、窗口不够长、
            #: 样本库是空的，都得当场说清是哪一只、为什么。
            report = failed_report(
                f"输入凑不出来，这一次没有发请求：{type(error).__name__}: {_brief(str(error))}",
                model=ai_gateway.model_name(),
                request_sent=False,
                billed=False,
                chain=self.chain,
                **selection,
            )
            logger.warning("%s的输入侧失败（未敲网关）：%s", self.label, report["error"])
        else:
            try:
                with session_scope() as session:
                    report = self._analyze(session, board, selection)
            except Exception as error:  # noqa: BLE001 - 后台线程绝不能静默死掉
                report = failed_report(
                    f"分析这一步撞上了没预料到的异常：{type(error).__name__}: {_brief(str(error))}",
                    model=ai_gateway.model_name(),
                    request_sent=False,
                    chain=self.chain,
                    **board_meta(board, selection),
                )
        report = archive(report)
        return self._publish(key, report)

    def _publish(self, key: str, report: dict) -> dict:
        """结论落地与「这一份不再在跑、槽位也交回去了」必须是同一个动作。

        分成两步写的话，轮询能读到一份自相矛盾的账：先读结论（那一刻还没写）、
        再读进度（这一刻已经清掉），页面于是停在「没在跑、也没有结论」那一格——
        等于把一次已经跑完的分析藏起来了，只能靠重进页面才看得见。
        """
        report["chain"] = self.chain
        report["key"] = key
        with self._state_lock:
            self._payloads[key] = report
            self._stored_at[key] = time.time()
            self._retire(key)
        return report

    def _retire(self, key: str) -> None:
        """交回这一格的槽位与进度——只交属于本线程那一趟的。调用方必须已经握着 `_state_lock`。

        所有权这一道不是多余的：`_publish` 之后同一格可能已经挂上新一轮的账
        （用户紧接着又点了一次），老那一趟收尾时无条件一清，就会把新那一份的进度抹掉，
        页面于是显示「没在跑」，而新那一份正在花钱。
        """
        entry = self._current.get(key)
        if entry is not None and entry.get("thread") not in (None, threading.get_ident()):
            return
        self._current.pop(key, None)
        self._running.discard(key)

    def _analyze(self, session, board: dict, selection: dict) -> dict:
        key = str(selection.get("key") or "")

        def progress(answer_chars: int, thinking_chars: int) -> None:
            with self._state_lock:
                current = self._current.get(key)
                if current is not None:
                    current["answer_chars"] = answer_chars
                    current["thinking_chars"] = thinking_chars

        return run_analysis(
            session,
            board,
            selection=selection,
            chain=self.chain,
            pick_limit=self.pick_limit,
            row_limit=self.row_limit,
            on_progress=progress,
            messages_for=single_messages if self.chain == CHAIN_SINGLE else build_messages,
        )

    def _guarded(self, key: str, board_provider: Callable[[], dict], selection: dict) -> None:
        selection = {**selection, "key": key}
        with self._state_lock:
            entry = self._current.get(key)
            if entry is not None:
                #: 从这一行起，这一格的账就归本线程了
                entry["thread"] = threading.get_ident()
        try:
            try:
                report = self.run_once(key, board_provider, selection=selection)
            except Exception as error:  # noqa: BLE001 - run_once 不该抛，真抛了也要留下一份能读的账
                #: 静默死掉的线程会让页面永远停在「没在跑、也没有结论」：
                #: 进度那一格已经被 finally 清掉，内存里却什么都没有，用户只能重进页面碰运气。
                #: 这里把异常本身变成一句能读到的结论，`request_sent=False` 说的是「这次的钱不一定花了」，
                #: 敲没敲网关只有报告自己知道，不能替它圆。
                report = self._publish(
                    key,
                    failed_report(
                        f"后台线程没跑完：{type(error).__name__}: {_brief(str(error))}",
                        model=ai_gateway.model_name(),
                        request_sent=False,
                        chain=self.chain,
                        **selection,
                    ),
                )
                logger.exception("%s线程异常退出（已留下一份可读的失败结论）：%s", self.label, error)
                return
            logger.info(
                "%s线程结束：%s，耗时 %ss",
                self.label,
                "成功" if report["ok"] else f"失败（{report['error'][:120]}）",
                report.get("latency_seconds"),
            )
        finally:
            #: 正常那一路 `_publish` 已经交回过了，这里是空操作；
            #: 留这一手是为了 `_publish` 之前就被打断的退出路径（BaseException）
            #: 不会把「正在跑」永远挂在状态上、槽位也一起还回去。
            with self._state_lock:
                self._retire(key)

    def reset(self) -> None:
        """测试收尾：把「正在跑」与内存里那一份都清掉。不杀线程，也杀不掉。"""
        with self._state_lock:
            self._current.clear()
            self._payloads.clear()
            self._stored_at.clear()


#: 两条链路，两个进程内单例
board_runner = AiAnalysisRunner(
    label="AI 综合分析", thread_name="ai-analysis", chain=CHAIN_BOARD,
    pick_limit=RECOMMEND_COUNT, row_limit=ai_evidence.DEFAULT_ROW_LIMIT,
)
single_runner = AiAnalysisRunner(
    label="AI 单股直评", thread_name="ai-single", chain=CHAIN_SINGLE,
    pick_limit=1, row_limit=1,
)


def single_selection(code: str) -> dict:
    """直评的选榜口径。`metric` 写成 `single`：它排的不是榜，是一只股票，
    混进整榜那个 `score` 会让存档里两种报告看起来是同一件事。"""
    return {"metric": "single", "metric_label": "单股直评", "top_n": 1, "code": code}


def board_selection(metric: str, metric_label: str, top_n: int) -> dict:
    return {"metric": metric, "metric_label": metric_label, "top_n": top_n}


__all__ = [
    "CHAIN_BOARD",
    "CHAIN_SINGLE",
    "RECOMMEND_COUNT",
    "AiAnalysisRunner",
    "archive",
    "board_meta",
    "board_runner",
    "board_selection",
    "build_messages",
    "criteria_of",
    "evidence_with_gap",
    "failed_report",
    "json_object_of",
    "model_score_of",
    "one_row_board",
    "parse_picks",
    "run_analysis",
    "single_messages",
    "single_runner",
    "single_selection",
    "text_of",
]
