"""盘中整榜与单股直评的存档。存的是「那一次花钱换来了什么」，不是缓存。

页面上读的那一份优先在内存里（`ai_analysis_service` 的槽位），内存没有就从这里读当天
最后一份——重开程序、或者十分钟到了，读数都不该变成空白。老工程用 Redis 当那层缓存，
这一版没有 Redis，所以存档就是唯一能跨重启的那只手。
"""

from __future__ import annotations

from datetime import date, timedelta

from backend.app.core.clock import now_naive
from backend.app.core.constants import AI_REPORT_MAX, AI_REPORT_RETENTION_DAYS
from backend.app.models.tables import AiAnalysisReport


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def cost_of(usage: dict) -> str:
    """上游给了什么就写什么，一个都没给就是空串——不写「0 tokens」。"""
    if not isinstance(usage, dict):
        return ""
    parts: list[str] = []
    if usage.get("prompt_tokens") is not None:
        parts.append(f"输入 {usage['prompt_tokens']} tokens")
    if usage.get("completion_tokens") is not None:
        parts.append(f"输出 {usage['completion_tokens']} tokens")
    if usage.get("reasoning_tokens") is not None:
        parts.append(f"其中思考 {usage['reasoning_tokens']}")
    if usage.get("cost") not in (None, ""):
        parts.append(f"成本 {usage['cost']}")
    return " · ".join(parts)


def persist_report(session, report: dict) -> int:
    """落一份存档，回它的编号。榜上没一行证据的报告不该走到这里（调用方已经拦了）。"""
    usage = report.get("usage") or {}
    row = AiAnalysisReport(
        trade_date=_as_date(report.get("trade_date")),
        base_date=_as_date(report.get("base_date")),
        status="ok" if report.get("ok") else "failed",
        model=str(report.get("model") or "")[:128],
        payload=report,
        chain=str(report.get("chain") or ""),
        metric=str(report.get("metric") or ""),
        metric_label=str(report.get("metric_label") or "")[:64],
        window_mode=str(report.get("window_mode") or ""),
        started_at=str(report.get("started_at") or ""),
        finished_at=str(report.get("finished_at") or ""),
        latency_seconds=report.get("latency_seconds"),
        board_rows=int(report.get("board_rows") or 0),
        feed_rows=int(report.get("feed_rows") or 0),
        picks_count=len(report.get("picks") or []),
        picks_beyond_limit=int(report.get("picks_beyond_limit") or 0),
        text_truncated=bool(report.get("text_truncated")),
        recommended_codes=[str(pick.get("stock_code") or "") for pick in report.get("picks") or []],
        cost_text=cost_of(usage)[:40],
    )
    session.add(row)
    session.flush()
    return int(row.id)


def view_of(row: AiAnalysisReport) -> dict:
    """存档里读回来的一份报告。`payload` 是当时算好的那一份，列上的字段只做筛选。"""
    payload = dict(row.payload or {})
    payload["report_id"] = int(row.id)
    payload.setdefault("chain", row.chain)
    payload["archived_at"] = row.created_at.isoformat(timespec="seconds") if row.created_at else ""
    payload["stored_cost_text"] = row.cost_text or cost_of(payload.get("usage") or {})
    return payload


def latest_report(session, *, chain: str = "", code: str = "") -> dict | None:
    """最近一份存档。单股直评按代码找，别把整榜的结论顶成这只股票的答案。

    代码筛是在 Python 里往回翻着找的：`recommended_codes` 是一列 JSON，
    SQLite 上没有可靠的「JSON 数组里含这个字符串」查询，硬凑一个 LIKE 会把
    `600072` 匹配进 `["6000721"]` 这种不存在的代码上，也会漏掉带空格的写法。
    """
    query = session.query(AiAnalysisReport)
    if chain:
        query = query.filter(AiAnalysisReport.chain == chain)
    rows = query.order_by(AiAnalysisReport.id.desc()).limit(500).all()
    if code:
        for row in rows:
            if code in [str(item) for item in (row.recommended_codes or [])]:
                return view_of(row)
        return None
    return view_of(rows[0]) if rows else None


def report_detail(session, report_id: int) -> dict | None:
    row = session.query(AiAnalysisReport).filter(AiAnalysisReport.id == int(report_id)).one_or_none()
    return view_of(row) if row else None


def list_reports(session, *, chain: str = "", limit: int = 20) -> dict:
    query = session.query(AiAnalysisReport)
    if chain:
        query = query.filter(AiAnalysisReport.chain == chain)
    rows = query.order_by(AiAnalysisReport.id.desc()).limit(max(1, min(100, int(limit)))).all()
    items = [
        {
            "report_id": int(row.id),
            "trade_date": row.trade_date.isoformat() if row.trade_date else None,
            "chain": row.chain,
            "status": row.status,
            "model": row.model,
            "metric": row.metric,
            "metric_label": row.metric_label,
            "window_mode": row.window_mode,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "latency_seconds": row.latency_seconds,
            "board_rows": int(row.board_rows or 0),
            "feed_rows": int(row.feed_rows or 0),
            "picks_count": int(row.picks_count or 0),
            "recommended_codes": list(row.recommended_codes or []),
            "cost_text": row.cost_text or cost_of((row.payload or {}).get("usage") or {}),
            "error": str((row.payload or {}).get("error") or "")[:200],
        }
        for row in rows
    ]
    return {"items": items, "retention_days": AI_REPORT_RETENTION_DAYS, "max_reports": AI_REPORT_MAX}


def trim(session) -> int:
    """按天数与条数两道各删一次。整榜那一条链路十分钟一份的话，90 天早就不止 1000 份。"""
    #: `created_at` 存的是东八区挂钟，裁剪的基准必须是同一个挂钟——用 UTC 会把保留期拉长八小时。
    deadline = now_naive() - timedelta(days=AI_REPORT_RETENTION_DAYS)
    stale = [
        row.id
        for row in session.query(AiAnalysisReport).filter(AiAnalysisReport.created_at < deadline).all()
    ]
    keep = (
        session.query(AiAnalysisReport)
        .order_by(AiAnalysisReport.id.desc())
        .offset(AI_REPORT_MAX)
        .all()
    )
    ids = set(stale) | {int(row.id) for row in keep}
    if not ids:
        return 0
    session.query(AiAnalysisReport).filter(AiAnalysisReport.id.in_(ids)).delete(synchronize_session=False)
    return len(ids)
