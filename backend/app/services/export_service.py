"""把排行、涨停池和每日总结导出成记事本能打开的 UTF-8 TXT。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import quote

from backend.app.core.clock import SHANGHAI
from backend.app.core.config import exports_dir, public_settings
from backend.app.core.constants import SIMILARITY_DISCLAIMER
from backend.app.models.tables import DailyQuote, Stock
from backend.app.providers.markets import classify, is_in_scope
from backend.app.services.similarity_service import pool_dates, scan_market, similar_to
from backend.app.services.summary_service import get_summary, streak_of

COLUMNS = ["排名", "代码", "名称", "总分", "形态分", "量能分", "量比分", "最相似涨停股", "涨停日期"]


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _header(day: date, pool: list[str], pool_size: int) -> list[str]:
    settings = public_settings()
    source = settings.get("active_source") or settings.get("data_source") or ""
    now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    return [
        f"生成时间\t{now}",
        f"数据日期\t{day.isoformat()}",
        f"对比池日期\t{','.join(pool)}",
        f"对比池数量\t{pool_size}",
        f"数据源\t{source}",
        f"免责声明\t{SIMILARITY_DISCLAIMER}",
        "",
    ]


def _table(rows: list[list]) -> str:
    lines = ["\t".join(COLUMNS)]
    for row in rows:
        lines.append("\t".join(_cell(item) for item in row))
    return "\n".join(lines)


def _scan_rows(payload: dict) -> list[list]:
    rows = []
    for index, item in enumerate(payload.get("items") or [], start=1):
        match = item.get("best_match") or {}
        best = match.get("code") or ""
        if match.get("name"):
            best = f"{best} {match['name']}".strip()
        rows.append(
            [
                index,
                item.get("code"),
                item.get("name"),
                item.get("score"),
                item.get("shape_score"),
                item.get("volume_score"),
                item.get("volume_pct_score"),
                best,
                match.get("limit_date"),
            ]
        )
    return rows


def _similarity_rows(payload: dict) -> list[list]:
    rows = []
    for index, item in enumerate(payload.get("matches") or [], start=1):
        rows.append(
            [
                index,
                item.get("code"),
                item.get("name"),
                item.get("score"),
                item.get("shape_score"),
                item.get("volume_score"),
                item.get("volume_pct_score"),
                f"{item.get('code') or ''} {item.get('name') or ''}".strip(),
                item.get("limit_date"),
            ]
        )
    return rows


def _pool_rows(session, day: date) -> tuple[list[str], list[list]]:
    dates = pool_dates(session, day)
    names = {row.code: row.name for row in session.query(Stock)}
    rows: list[list] = []
    rank = 1
    for limit_day in dates:
        quotes = (
            session.query(DailyQuote)
            .filter(DailyQuote.trade_date == limit_day, DailyQuote.is_limit_up.is_(True))
            .order_by(DailyQuote.stock_code)
            .all()
        )
        for quote in quotes:
            if not is_in_scope(quote.stock_code):
                continue
            exchange, _board = classify(quote.stock_code)
            rows.append(
                [
                    rank,
                    quote.stock_code,
                    names.get(quote.stock_code, ""),
                    "",
                    "",
                    "",
                    "",
                    "",
                    limit_day.isoformat(),
                    "SH" if exchange == "SSE" else "SZ",
                    quote.close,
                    quote.change_percent,
                    _streak_text(*streak_of(session, quote.stock_code, limit_day)),
                ]
            )
            rank += 1
    return [item.isoformat() for item in dates], rows


def _streak_text(height: int, at_least: bool) -> str:
    """数到库里最早那天还没断的连板只能写下界，和页面上的「≥N」同一个读法。"""
    return f"≥{height}" if at_least else str(height)


def render_export(session, kind: str, day: date, code: str | None, top: int) -> tuple[str, str]:
    pool: list[str] = []
    pool_size = 0
    body: list[str] = []
    suffix = ""
    if kind == "scan":
        payload = scan_market(session, day, top, 0)
        pool = payload.get("pool_dates") or []
        pool_size = int(payload.get("pool_size") or 0)
        body = _header(day, pool, pool_size)
        body.append(_table(_scan_rows(payload)))
        if payload.get("skipped"):
            body.append("")
            body.append("未参加打分")
            for item in payload["skipped"]:
                body.append(f"{item.get('code')}\t{item.get('name')}\t{item.get('limit_date')}\t{item.get('reason')}")
    elif kind == "similarity":
        if not code:
            raise ValueError("导出相似度需要股票代码")
        payload = similar_to(session, code, day, top)
        pool = payload.get("pool_dates") or []
        pool_size = int(payload.get("pool_size") or 0)
        body = _header(day, pool, pool_size)
        target = payload.get("target") or {}
        body.append(f"查询股票\t{target.get('code') or code}\t{target.get('name') or ''}")
        body.append("")
        body.append(_table(_similarity_rows(payload)))
        suffix = f"_{code}"
    elif kind == "pool":
        pool, rows = _pool_rows(session, day)
        pool_size = len(rows)
        body = _header(day, pool, pool_size)
        columns = COLUMNS + ["板块", "收盘价", "涨跌幅", "连板"]
        lines = ["\t".join(columns)]
        for row in rows:
            lines.append("\t".join(_cell(item) for item in row))
        body.append("\n".join(lines))
    elif kind == "summary":
        payload = get_summary(session, day)
        if payload is None:
            raise LookupError("这一天还没有总结")
        body = _header(day, [], int(payload.get("limitup_count") or 0))
        body.append(f"标题\t{payload.get('headline') or ''}")
        body.append(f"来源\t{payload.get('source') or ''}")
        if payload.get("model"):
            body.append(f"模型\t{payload.get('model')}")
        body.append("要点")
        for point in payload.get("key_points") or []:
            body.append(f"- {point}")
        if payload.get("ai_error"):
            body.append(f"AI\t{payload.get('ai_error')}")
        body.append("")
        body.append("板块")
        for item in payload.get("sectors") or []:
            body.append(f"{item.get('name')}\t{item.get('count')}")
        body.append("")
        body.append("连板")
        for item in payload.get("ladder") or []:
            body.append(f"{item.get('height')}\t{item.get('count')}\t{','.join(item.get('codes') or [])}")
        body.append("")
        body.append("原始JSON")
        body.append(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        raise ValueError("导出类型只能是 scan、similarity、pool、summary")
    filename = f"{kind}_{day.isoformat()}{suffix}.txt"
    return "\n".join(body) + "\n", filename


def save_copy(filename: str, content: bytes) -> None:
    path = exports_dir() / filename
    path.write_bytes(content)


def content_disposition(filename: str) -> str:
    """下载头。真名字走 `filename*`，`filename` 那段只是老浏览器的兜底，必须是纯 ASCII。

    中文直接写进 `filename="…"` 会让响应头编不出 latin-1——整个导出接口 500，
    页面上点「导出 CSV」只得到一个服务器错（`matches_…_截断.csv` 实测踩过）。
    """
    if not filename.isascii():
        stem, dot, ext = filename.rpartition(".")
        stem = re.sub(r"-{2,}", "-", "".join(_fallback_char(char) for char in stem))
        filename_stem = stem.strip("-_") or "export"
        fallback = f"{filename_stem}{dot}{ext}" if dot else filename_stem
    else:
        fallback = filename
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _fallback_char(char: str) -> str:
    return char if char.isascii() and char not in '"\\;' else "-"
