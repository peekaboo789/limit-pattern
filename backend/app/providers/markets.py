"""只保留上证 A 股与深证 A 股。按 6 位代码前缀判断，不看首位数字。"""

from __future__ import annotations

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
    "302": "main_board",
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
