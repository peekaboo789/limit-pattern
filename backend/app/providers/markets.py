"""只保留上证 A 股与深证 A 股。按 6 位代码前缀判断，不看首位数字。

302 段是创业板：300114 中航电测自 2025-02-17 起改码为 302132 中航成飞，
仍适用《深圳证券交易所创业板交易特别规定》的 20% 涨跌幅。
"""

from __future__ import annotations

from backend.app.core.logging import get_logger

logger = get_logger("markets")

SSE_BOARDS = {
    "600": "main_board",
    "601": "main_board",
    "603": "main_board",
    "605": "main_board",
    "688": "star",
    "689": "star",
}

SZSE_BOARDS = {
    "000": "main_board",
    "001": "main_board",
    "002": "main_board",
    "003": "main_board",
    "300": "chinext",
    "301": "chinext",
    "302": "chinext",
}

# 北交所。默认不在范围内，只有 SUPPORTED_EXCHANGES 写了 BSE 才放行。
BSE_PREFIXES = {
    "430",
    "920",
    *(f"83{i}" for i in range(10)),
    "870",
    "871",
    "872",
    "873",
    *(f"88{i}" for i in range(10)),
}

BOARD_LABELS = {
    "main_board": "主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
    "unknown": "未知",
}


def normalize_code(raw: str) -> str:
    text = (raw or "").strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def classify(code: str) -> tuple[str | None, str | None]:
    """返回 (交易所, 板块)。对不上就是 (None, None)。"""
    code = normalize_code(code)
    if len(code) != 6:
        return None, None
    head = code[:3]
    if head in SSE_BOARDS:
        return "SSE", SSE_BOARDS[head]
    if head in SZSE_BOARDS:
        return "SZSE", SZSE_BOARDS[head]
    if head in BSE_PREFIXES:
        return "BSE", "bse"
    return None, None


def _tagged_exchange(raw: str) -> str | None:
    text = (raw or "").strip().lower()
    if text.startswith("sh"):
        return "SSE"
    if text.startswith("sz"):
        return "SZSE"
    if text.startswith("bj"):
        return "BSE"
    return None


def accept_instrument(raw_symbol: str, code: str = "", name: str = "") -> tuple[str, str] | None:
    """返回 (6 位代码, 交易所)。前缀表优先于行情源自带的 sh/sz。

    sh000001 这类是上证指数，和深市股票 000001 撞号，直接丢掉。
    sz600519 这种标错市场的，仍按代码前缀当成上证股票，并记一条日志。
    """
    code = normalize_code(code or raw_symbol)
    exchange, _board = classify(code)
    if exchange not in {"SSE", "SZSE"}:
        return None
    tagged = _tagged_exchange(raw_symbol)
    if tagged == "SSE" and code[:3] in {"000", "001", "002", "003"}:
        logger.info("丢弃 %s %s：这是上证指数代码，不是深市 A 股", raw_symbol, name)
        return None
    if "指数" in (name or "") and code.startswith("000"):
        logger.info("丢弃 %s %s：名称是指数", raw_symbol, name)
        return None
    if tagged and tagged != exchange:
        logger.warning(
            "%s 标成 %s，与代码 %s 的前缀不一致，按交易所前缀规则视为 %s",
            raw_symbol,
            tagged,
            code,
            exchange,
        )
    return code, exchange


def symbol_of(code: str) -> str:
    code = normalize_code(code)
    exchange, _board = classify(code)
    if exchange == "SSE":
        return "sh" + code
    if exchange == "BSE":
        return "bj" + code
    return "sz" + code


def reject_reason(code: str, allowed: set[str] | None = None) -> str | None:
    if allowed is None:
        from backend.app.core.config import get_settings

        allowed = get_settings().exchange_set()
    exchange, _board = classify(code)
    if exchange is None:
        return "不是沪深 A 股"
    if exchange not in allowed:
        return f"{exchange} 不在纳入范围"
    return None


def keep_code(code: str, allowed: set[str] | None = None) -> bool:
    return reject_reason(code, allowed) is None
