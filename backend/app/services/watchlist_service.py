"""v0.4.8 自选股：增删、按代码 / 汉字 / 拼音首字母查找、一键导入导出、实时监控（复用监控行情源和报警时段判断）。

列表存在 data/watchlist.json（重启保留）。科创板（688/689）允许加入自选，但标「科创板，不在相似度样本池」。"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time as _time
from datetime import datetime

from backend.app.core.clock import SHANGHAI, now
from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.watchlist")
_LOCK = threading.Lock()
MAX_ITEMS = 500
STAR_PREFIXES = ("688", "689")

# ---------- 拼音首字母（GB2312 一级汉字区间，离线、零依赖） ----------
_BOUNDS = [
    (-20319, "a"), (-20283, "b"), (-19775, "c"), (-19218, "d"), (-18710, "e"), (-18526, "f"), (-18239, "g"),
    (-17922, "h"), (-17417, "j"), (-16474, "k"), (-16212, "l"), (-15640, "m"), (-15165, "n"), (-14922, "o"),
    (-14914, "p"), (-14630, "q"), (-14149, "r"), (-14090, "s"), (-13318, "t"), (-12838, "w"), (-12556, "x"),
    (-11847, "y"), (-11055, "z"),
]
# 二级字库和常见多音 / 生僻字的补充
_EXTRA = {"琼": "q", "瑞": "r", "鑫": "x", "璞": "p", "锂": "l", "钴": "g", "钛": "t", "镍": "n", "钼": "m", "硅": "g",
          "珀": "p", "晶": "j", "鹏": "p", "泸": "l", "邦": "b", "霖": "l", "昊": "h", "旻": "m", "晟": "s", "昇": "s",
          "赣": "g", "皖": "w", "沪": "h", "渝": "y", "滇": "d", "黔": "q", "闽": "m", "湘": "x", "鄂": "e", "豫": "y",
          "冀": "j", "晋": "j", "陕": "s", "甘": "g", "桂": "g", "粤": "y", "浙": "z", "苏": "s", "辽": "l", "吉": "j",
          "藏": "z", "芯": "x", "瓴": "l", "铖": "c", "埃": "a", "禾": "h", "翔": "x", "钒": "f", "铂": "b", "铟": "y",
          "铼": "l", "锗": "z", "镓": "j", "煜": "y", "璟": "j", "珈": "j", "炬": "j", "韦": "w", "蔚": "w", "曦": "x",
          "长": "c", "重": "c", "行": "h", "乐": "l", "朝": "z", "厦": "x", "藏": "z", "单": "d", "解": "x", "曾": "z"}


def initials(text: str) -> str:
    out = []
    for ch in str(text or ""):
        if ch.isascii():
            if ch.isalnum():
                out.append(ch.lower())
            continue
        if ch in _EXTRA:
            out.append(_EXTRA[ch])
            continue
        try:
            raw = ch.encode("gb2312")
        except UnicodeEncodeError:
            continue
        if len(raw) != 2:
            continue
        code = raw[0] * 256 + raw[1] - 65536
        letter = ""
        for bound, value in _BOUNDS:
            if code >= bound:
                letter = value
            else:
                break
        if letter and code <= -10247:
            out.append(letter)
    return "".join(out)


# ---------- 本地股票表 ----------
def _stocks(session) -> list[dict]:
    from backend.app.models.tables import Stock

    rows = session.query(Stock.code, Stock.name).all()
    return [{"code": c, "name": n or "", "py": initials(n or "")} for c, n in rows]


def board_label(code: str) -> str:
    if code.startswith(STAR_PREFIXES):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"


_CODE_RE = re.compile(r"^(?:(?:sh|sz|bj)[.\-]?)?(\d{6})(?:[.\-]?(?:sh|sz|bj|ss))?$", re.I)


def normalize_code(token: str) -> str | None:
    m = _CODE_RE.match(str(token or "").strip())
    return m.group(1) if m else None


def search(session, query: str, limit: int = 10, stocks: list[dict] | None = None) -> list[dict]:
    """代码（可带 sh/sz/bj 前缀或 .SH 后缀）/ 代码前缀 / 汉字名称（含部分）/ 拼音首字母。"""
    q = str(query or "").strip()
    if not q:
        return []
    stocks = stocks if stocks is not None else _stocks(session)
    code = normalize_code(q)
    if code:
        hit = [s for s in stocks if s["code"] == code]
        if hit:
            return [_result(s) for s in hit]
        if code.startswith(STAR_PREFIXES) or True:
            return [_result({"code": code, "name": ""})]  # 本地没有（如科创板）也允许按代码加
    ql = q.lower()
    scored = []
    for s in stocks:
        name = s["name"]
        if q.isdigit():
            if s["code"].startswith(q):
                scored.append((0, s))
        elif name == q:
            scored.append((0, s))
        elif name.startswith(q):
            scored.append((1, s))
        elif q in name:
            scored.append((2, s))
        elif ql.isascii() and ql.isalpha() and s["py"].startswith(ql):
            scored.append((3 if s["py"] != ql else 1, s))
        elif ql.isascii() and ql.isalpha() and len(ql) >= 2 and ql in s["py"]:
            scored.append((4, s))
    scored.sort(key=lambda x: (x[0], len(x[1]["name"]), x[1]["code"]))
    return [_result(s) for _r, s in scored[:limit]]


def _result(s: dict) -> dict:
    code = s["code"]
    return {"code": code, "name": s.get("name", ""), "board": board_label(code), "star": code.startswith(STAR_PREFIXES)}


# ---------- 存储 ----------
def _path():
    return data_dir() / "watchlist.json"


def load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return [dict(x) for x in data.get("items") or [] if normalize_code(x.get("code"))]
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def add(entries: list[dict]) -> dict:
    """entries: [{code, name?, note?}]；已在列表里的跳过并报告。"""
    with _LOCK:
        items = load()
        have = {x["code"] for x in items}
        added, dup = [], []
        for e in entries:
            code = normalize_code(e.get("code"))
            if not code:
                continue
            if code in have:
                dup.append(code)
                continue
            if len(items) >= MAX_ITEMS:
                break
            item = {"code": code, "name": e.get("name") or "", "note": str(e.get("note") or "")[:200],
                    "added_at": datetime.now(SHANGHAI).isoformat(timespec="seconds")}
            items.append(item)
            have.add(code)
            added.append(code)
        _save(items)
    return {"added": added, "duplicates": dup, "items": items}


def remove(codes: list[str] | None = None, clear: bool = False) -> list[dict]:
    with _LOCK:
        items = [] if clear else [x for x in load() if x["code"] not in set(codes or [])]
        _save(items)
    return items


def update(code: str, note: str | None = None) -> list[dict]:
    with _LOCK:
        items = load()
        for x in items:
            if x["code"] == code and note is not None:
                x["note"] = str(note)[:200]
        _save(items)
    return items


def reorder(codes: list[str]) -> list[dict]:
    with _LOCK:
        items = load()
        pos = {c: i for i, c in enumerate(codes)}
        old = {x["code"]: i for i, x in enumerate(items)}
        items.sort(key=lambda x: pos.get(x["code"], len(pos) + old[x["code"]]))
        _save(items)
    return items


# ---------- 一键导入 ----------
_SPLIT = re.compile(r"[\s,，;；、|]+")
_HEADER_WORDS = {"代码", "名称", "股票代码", "股票名称", "证券代码", "证券名称", "code", "name", "序号", "现价", "涨幅", "涨跌幅"}


def _json_codes(text: str) -> list[str] | None:
    """外部 AI 的 JSON（items / stocks 列表）也能直接导入：取 code，没有 code 再用 name。"""
    if "{" not in text and "[" not in text:
        return None
    try:
        from backend.app.services.ai_export import _entries, _loads_loose

        entries, _meta = _entries(_loads_loose(text))
    except Exception:
        return None
    out = []
    for e in entries:
        if isinstance(e, dict):
            token = str(e.get("code") or e.get("代码") or e.get("symbol") or e.get("name") or e.get("名称") or "").strip()
        else:
            token = str(e).strip()
        if token:
            out.append(token)
    return out


def parse_import(session, text: str) -> dict:
    """把粘贴文本 / TXT / CSV（同花顺、东方财富导出等）拆成代码或名称，逐个解析。
    返回 matched / ambiguous（多个候选，让用户挑）/ not_found / duplicates（列表里已有或文本里重复）。"""
    stocks = _stocks(session)
    by_code = {s["code"]: s for s in stocks}
    by_name = {s["name"]: s for s in stocks if s["name"]}
    existing = {x["code"] for x in load()}
    text = str(text or "").lstrip("\ufeff")
    tokens: list[str] = []
    json_tokens = _json_codes(text)
    for line in ([] if json_tokens is not None else text.splitlines()):
        parts = [p.strip().strip('"\'') for p in _SPLIT.split(line) if p.strip().strip('"\'')]
        # 「600132 重庆啤酒」这种：紧跟在代码后面、正好是它本地名称的那一格只用来核对，不再单独解析
        prev = None
        for part in parts:
            code = normalize_code(part)
            if code:
                tokens.append(part)
                prev = code
                continue
            if part.lower() in _HEADER_WORDS or re.fullmatch(r"[-+]?\d+(\.\d+)?%?", part):
                continue
            if prev and by_code.get(prev, {}).get("name", "").replace(" ", "") == part.replace(" ", ""):
                prev = None
                continue
            prev = None
            tokens.append(part)
    if json_tokens is not None:
        tokens = json_tokens
    matched, ambiguous, not_found, duplicates = [], [], [], []
    seen: set[str] = set()

    def take(code: str, name: str, token: str):
        if code in seen or code in existing:
            duplicates.append({"token": token, "code": code, "name": name, "reason": "已在自选" if code in existing else "文本里重复"})
            return
        seen.add(code)
        matched.append({"token": token, **_result({"code": code, "name": name})})

    for token in tokens:
        code = normalize_code(token)
        if code:
            s = by_code.get(code)
            if s or code.startswith(STAR_PREFIXES):
                take(code, s["name"] if s else "", token)
            else:
                not_found.append({"token": token, "reason": "本地股票列表里没有这个代码"})
            continue
        if token in by_name:
            take(by_name[token]["code"], token, token)
            continue
        cands = search(session, token, limit=8, stocks=stocks)
        cands = [c for c in cands if c["name"]]
        if len(cands) == 1:
            take(cands[0]["code"], cands[0]["name"], token)
        elif cands:
            ambiguous.append({"token": token, "candidates": cands})
        else:
            not_found.append({"token": token, "reason": "找不到匹配的股票名称"})
    return {"matched": matched, "ambiguous": ambiguous, "not_found": not_found, "duplicates": duplicates, "token_count": len(tokens)}


def export_text(fmt: str) -> tuple[str, str, str]:
    items = load()
    day = now().date().isoformat()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["代码", "名称", "板块", "备注", "加入时间"])
        for x in items:
            w.writerow([x["code"], x.get("name", ""), board_label(x["code"]), x.get("note", ""), x.get("added_at", "")])
        return "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8", f"自选股_{day}.csv"
    lines = [f"{x['code']}\t{x.get('name', '')}" + (f"\t{x['note']}" if x.get("note") else "") for x in items]
    return "\ufeff" + "\r\n".join(lines) + "\r\n", "text/plain; charset=utf-8", f"自选股_{day}.txt"


# ---------- 实时监控 ----------
_QCACHE: dict = {"key": None, "at": 0.0, "quotes": {}, "source": "", "error": ""}


def _quotes(codes: list[str], ttl: float) -> dict:
    from backend.app.services import monitor_service as ms

    key = tuple(sorted(codes))
    with _LOCK:
        if _QCACHE["key"] == key and _time.monotonic() - _QCACHE["at"] < ttl:
            return dict(_QCACHE)
    fetch = ms._fetcher or ms._default_fetch
    try:
        rows, source = fetch(list(codes)) if codes else ([], "")  # 一次批量请求
        quotes, error = {str(r.get("code")): r for r in rows if r.get("code")}, ""
        star = [c for c in codes if c.startswith(STAR_PREFIXES) and c not in quotes]
        if star and ms._fetcher is None:  # 通用行情源会滤掉科创板：单独用新浪补一次
            try:
                from backend.app.providers import sina

                quotes.update({str(r["code"]): r for r in sina.fetch_spot(star, allow_star=True) if r.get("code")})
            except Exception as exc:
                logger.info("科创板自选行情没拉到：%s", exc)
    except Exception as exc:
        logger.warning("自选股行情没有拉到：%s", exc)
        quotes, source, error = dict(_QCACHE["quotes"]), _QCACHE["source"], str(exc)
    with _LOCK:
        _QCACHE.update(key=key, at=_time.monotonic(), quotes=quotes, source=source, error=error)
        return dict(_QCACHE)


def _limit_ratio(code: str, name: str) -> float:
    if "ST" in (name or "").upper():
        return 0.05
    if code.startswith(("300", "301") + STAR_PREFIXES):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _scores(session) -> dict[str, dict]:
    """相似度 / 综合分：取最近的手动快照结果和最近 AI 榜单里已有的值，取不到就 —。"""
    out: dict[str, dict] = {}
    try:
        from backend.app.services.ranking_service import get_ranking

        ranking = get_ranking(session) or {}
        for it in ranking.get("items") or []:
            out[it["code"]] = {"max_similarity": it.get("max_similarity"), "composite_score": it.get("composite_score"), "from": f"AI 榜单 {ranking.get('date')}"}
    except Exception:
        pass
    try:
        from backend.app.services import manual_capture

        snap = manual_capture.load_snapshot() or {}
        for it in (snap.get("result") or {}).get("items") or []:
            out.setdefault(it["code"], {"max_similarity": it.get("max_similarity"), "composite_score": it.get("composite_score"), "from": snap.get("label")})
    except Exception:
        pass
    return out


def _avg_volume(session, codes: list[str], days: int = 5) -> dict[str, float]:
    from sqlalchemy import func

    from backend.app.models.tables import DailyQuote
    from backend.app.services.intraday_service import PROVISIONAL_SOURCES
    from backend.app.services.retention import quote_dates

    dates = [d for d in quote_dates(session)][-days - 1:]
    if not codes or not dates:
        return {}
    rows = (
        session.query(DailyQuote.stock_code, DailyQuote.trade_date, DailyQuote.volume)
        .filter(DailyQuote.stock_code.in_(codes), DailyQuote.trade_date.in_(dates),
                ~DailyQuote.source.in_(PROVISIONAL_SOURCES) | DailyQuote.source.is_(None))
        .all()
    )
    per: dict[str, list[tuple]] = {}
    for c, d, v in rows:
        if v:
            per.setdefault(c, []).append((d, float(v)))
    return per  # type: ignore[return-value]


def snapshot(session, moment: datetime | None = None) -> dict:
    from backend.app.services import monitor_service as ms
    from backend.app.services.intraday_service import elapsed_minutes, volume_fraction

    moment = moment or now()
    local = moment.astimezone(SHANGHAI)
    day = local.date()
    phase = ms.alert_phase(moment)
    enabled = read_settings_file().get("watchlist_alerts") is not False
    active = phase["alerts_active"] and enabled
    items = load()
    codes = [x["code"] for x in items]
    cache = _quotes(codes, ms.OPEN_TTL if phase["phase"] == "trading" else ms.CLOSED_TTL)
    scores = _scores(session)
    vols = _avg_volume(session, codes)
    fraction = 1.0
    if phase["phase"] == "trading":
        fraction = max(volume_fraction(elapsed_minutes(moment)), 0.02)
    rows = []
    for x in items:
        code = x["code"]
        q = cache["quotes"].get(code) or {}
        name = x.get("name") or q.get("name") or ""
        pct = ms._pct(q) if q else None
        pre, close = q.get("pre_close"), q.get("close")
        limit_up = bool(pre and close and float(close) >= round(float(pre) * (1 + _limit_ratio(code, name)), 2) - 0.001)
        quote_day = str(q.get("trade_date") or "")[:10]
        history = [v for d, v in sorted(vols.get(code, [])) if d.isoformat() != quote_day][-5:]
        ratio = None
        if history and q.get("volume"):
            ratio = round(float(q["volume"]) / fraction / (sum(history) / len(history)), 2)
        level = ms.level_for(pct)
        sc = scores.get(code) or {}
        row = {
            "code": code, "name": name, "note": x.get("note", ""), "board": board_label(code),
            "star": code.startswith(STAR_PREFIXES),
            "price": close, "pre_close": pre, "pct": pct, "high": q.get("high"), "low": q.get("low"), "open": q.get("open"),
            "volume_ratio": ratio, "is_limit_up": limit_up,
            "max_similarity": None if code.startswith(STAR_PREFIXES) else sc.get("max_similarity"),
            "composite_score": None if code.startswith(STAR_PREFIXES) else sc.get("composite_score"),
            "score_from": sc.get("from"),
            "level": level if active else "normal",
            "raw_level": level,
        }
        rows.append(row)
    # 本地没有名称的（如科创板）用行情里的名称补上并保存
    named = {r["code"]: r["name"] for r in rows if r["name"]}
    if any(not x.get("name") and named.get(x["code"]) for x in items):
        with _LOCK:
            fresh = load()
            for x in fresh:
                if not x.get("name") and named.get(x["code"]):
                    x["name"] = named[x["code"]]
            _save(fresh)
    out = {
        "date": day.isoformat(), "phase": phase["phase"], "alerts_active": active, "alerts_enabled": enabled,
        "status_text": phase["text"] if enabled else "自选股报警已关闭", "market_open": ms.market_open(moment),
        "thresholds": {"warn": ms.WARN_PCT, "alert": ms.ALERT_PCT},
        "source": cache["source"], "error": cache["error"], "updated_at": moment.isoformat(timespec="seconds"),
        "items": rows,
        "alert_count": sum(1 for r in rows if r["level"] == "alert"),
        "warn_count": sum(1 for r in rows if r["level"] == "warn"),
    }
    if not active:
        quote_day = next((str(q.get("trade_date"))[:10] for q in cache["quotes"].values() if q.get("trade_date")), "")
        if phase["phase"] == "lunch":
            out["price_label"] = "午休前最后行情（报警暂停）"
        elif quote_day and quote_day == day.isoformat():
            out["price_label"] = f"今日 {quote_day[5:]} 最后行情（报警暂停）" if phase["phase"] != "trading" else "自选股报警已关闭"
        elif quote_day:
            out["price_label"] = f"上一交易日 {quote_day[5:]} 收盘（报警暂停）"
        else:
            out["price_label"] = "最后行情（报警暂停）"
    return out
