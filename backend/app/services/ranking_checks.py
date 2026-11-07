"""核对 AI 写的标签/理由和本地日线算出的事实是否一致，不一致的在榜单上标 ⚠ 核对。

只核对能从日线明确判断的说法：均线多头、连续收涨/收阳、接近 20 日高点、放量/缩量、连板。
说法里没提到的不核对；数据不够的也不判错。
"""

from __future__ import annotations

import re

CN_NUM = {"两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
NEAR_HIGH_PCT = -3.0  # 距 20 日最高不超过 3% 才算"接近高点"


def _ma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def facts_from_bars(bars) -> dict:
    """bars 按日期升序，最后一根是评估日；元素有 open/high/low/close/volume 属性。"""
    rows = [b for b in bars if getattr(b, "close", None) is not None]
    if len(rows) < 2:
        return {}
    closes = [b.close for b in rows]
    ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
    up_streak = 0
    for i in range(len(rows) - 1, 0, -1):
        if rows[i].close > rows[i - 1].close:
            up_streak += 1
        else:
            break
    yang_streak = 0
    for b in reversed(rows):
        if b.open is not None and b.close > b.open:
            yang_streak += 1
        else:
            break
    part = rows[-20:]
    highs = [b.high for b in part if b.high is not None]
    dist20 = (closes[-1] / max(highs) - 1) * 100 if highs else None
    prev_vols = [b.volume for b in rows[-6:-1] if b.volume]
    vr = rows[-1].volume / (sum(prev_vols) / len(prev_vols)) if rows[-1].volume and prev_vols else None
    limit_streak = 0
    for b in reversed(rows):
        if getattr(b, "is_limit_up", False):
            limit_streak += 1
        else:
            break
    return {
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma_bull": (ma5 > ma10 > ma20) if None not in (ma5, ma10, ma20) else None,
        "up_streak": up_streak,
        "yang_streak": yang_streak,
        "dist20": dist20,
        "vr": vr,
        "limit_streak": limit_streak,
        "last_pct": (closes[-1] / closes[-2] - 1) * 100,
    }


def _count(text: str | None, default: int) -> int:
    if not text:
        return default
    if text.isdigit():
        return int(text)
    return CN_NUM.get(text, default)


def _f(value, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def check_claims(tags: list[str], reason: str, facts: dict) -> list[dict]:
    """返回不一致的说法列表 [{claim, fact}]；空列表表示没发现问题。"""
    if not facts:
        return []
    text = " ".join([*(tags or []), reason or ""])
    issues: list[dict] = []

    def flag(claim: str, fact: str) -> None:
        issues.append({"claim": claim, "fact": fact})

    if re.search(r"均线多头|多头排列|MA5/10/20\s*(保持|呈)?\s*多头|5/10/20\s*日?均线多头", text) and facts.get("ma_bull") is False:
        flag(
            "均线多头",
            f"MA5 {_f(facts['ma5'])} / MA10 {_f(facts['ma10'])} / MA20 {_f(facts['ma20'])}，不是 MA5>MA10>MA20",
        )
    m = re.search(r"([两二三四五六七八九十\d])\s*[日天]\s*连续\s*(收涨|上涨|收红)|连续\s*([两二三四五六七八九十\d])?\s*[日天]?\s*(收涨|上涨|走高)|连续多日(上涨|收涨)|连涨", text)
    if m:
        need = _count(m.group(1) or m.group(3), 2)
        if facts.get("up_streak", 0) < need:
            flag(m.group(0), f"截至评估日连续收涨 {facts.get('up_streak', 0)} 天（评估日涨跌 {_f(facts.get('last_pct'))}%）")
    m = re.search(r"连续\s*([两二三四五六七八九十\d])?\s*[日天根]?\s*收阳|([两二三四五六七八九十\d])\s*连阳", text)
    if m:
        need = _count(m.group(1) or m.group(2), 2)
        if facts.get("yang_streak", 0) < need:
            flag(m.group(0), f"截至评估日连续收阳 {facts.get('yang_streak', 0)} 根")
    if re.search(r"(接近|靠近|逼近|临近)\s*(20\s*日|近期|区间)?\s*(新?高点|高位|前高)|距离?近期高点较近|创\s*20\s*日新高", text):
        dist = facts.get("dist20")
        if dist is not None and dist < NEAR_HIGH_PCT:
            flag("接近 20 日高点", f"距 20 日最高 {_f(dist, 1)}%")
    vr = facts.get("vr")
    if vr is not None:
        if re.search(r"(?<!缩量后)(温和放量|放量突破|明显放量|放量上涨|放量)", text) and not re.search(r"缩量", text) and vr < 1.0:
            flag("放量", f"评估日量比 {_f(vr)}（低于前 5 日均量）")
        if re.search(r"缩量", text) and not re.search(r"放量", text) and vr > 1.2:
            flag("缩量", f"评估日量比 {_f(vr)}（高于前 5 日均量）")
    if re.search(r"[二两三四五六七八九\d]\s*连板|连板", text) and facts.get("limit_streak", 0) < 2:
        flag("连板", f"截至评估日连续涨停 {facts.get('limit_streak', 0)} 天")
    return issues


def check_ranked(session, day, ranked: list[dict]) -> None:
    from backend.app.services.ai_context import load_bars

    for row in ranked:
        try:
            facts = facts_from_bars(load_bars(session, row["code"], day, 30))
        except Exception:
            facts = {}
        row["checks"] = check_claims(row.get("tags") or [], row.get("reason") or "", facts)
