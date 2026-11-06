"""多模型讨论验证：先做本地证据 + 联网核对，再按顺序发言，最后一位主席总结。

只新增讨论会结论，不改综合评分公式，不写回榜单，不改变 ai_active_profile。
"""

from __future__ import annotations

import json
import re
import threading
import time as _time
from datetime import date, datetime

from backend.app.core.clock import today
from backend.app.core.config import read_settings_file, write_settings_file
from backend.app.core.database import session_scope
from backend.app.models.tables import AiCouncilSession, DailyQuote, Stock
from backend.app.providers.markets import classify, is_in_scope, normalize_code, reject_reason
from backend.app.services import watchlist_service as ws
from backend.app.services.ai_context import load_bars, market_context, stock_metrics
from backend.app.services.score_model import QUANT_STANDARD_TEXT
from backend.app.services.ranking_service import get_ranking
from backend.app.services.similarity_service import similar_to
from backend.app.core import ai_profiles

DISCLAIMER = "这是讨论会估计，不计入系统综合评分，不是确定预测，不是投资建议。"

CONFIDENCE_SET = {"low", "medium", "high"}
STATUS_ORDER = ("collecting_local", "verifying_web", "speaking", "chairman", "done", "failed")

_PROGRESS: dict = {
    "running": False,
    "phase": "idle",
    "session_id": None,
    "stock": None,
    "stage": "空闲",
    "current_model": "",
    "done": 0,
    "total": 0,
    "last_status": "",
    "error": "",
}
_LOCK = threading.RLock()
_START_LOCK = threading.Lock()


def progress() -> dict:
    return dict(_PROGRESS)


def _now_dt() -> datetime:
    return datetime.utcnow()


def _valid_profiles(profile_ids: list[str] | None) -> list[dict]:
    pub = ai_profiles.public()
    by_id = {p["id"]: p for p in pub["items"]}
    if profile_ids is not None:
        profiles = []
        for pid in profile_ids:
            prof = by_id.get(str(pid))
            if prof is None:
                raise ValueError(f"模型配置 {pid} 不存在")
            if not prof["api_key_set"]:
                raise ValueError(f"模型「{prof['name'] or prof['model']}」还没填密钥")
            profiles.append(prof)
    else:
        raw = read_settings_file()
        saved = raw.get("ai_council_profile_ids")
        if isinstance(saved, list) and saved:
            profiles = []
            for pid in saved:
                prof = by_id.get(str(pid))
                if prof is None or not prof["api_key_set"]:
                    continue
                profiles.append(prof)
        else:
            profiles = [p for p in pub["items"] if p["api_key_set"]]
    return profiles


def profile_order() -> dict:
    raw = read_settings_file()
    return {"profile_ids": list(raw.get("ai_council_profile_ids") or [])}


def save_profile_order(profile_ids: list[str]) -> dict:
    profile_ids = [str(pid) for pid in (profile_ids or [])]
    profiles = _valid_profiles(profile_ids)  # 校验至少都有密钥；空数组是清空
    with open_settings_lock():
        raw = read_settings_file()
        raw["ai_council_profile_ids"] = profile_ids
        write_settings_file(raw)
    return profile_order()


def open_settings_lock():
    from backend.app.core.config import _SETTINGS_LOCK

    return _SETTINGS_LOCK


def _parse_code_or_name(value: str) -> str:
    """复用自选 search 的解析；找不到代码/名称会抛 ValueError。"""
    query = str(value or "").strip()
    if not query:
        raise ValueError("请提供股票代码或名称")
    code = normalize_code(query)
    if len(code) == 6 and code.isdigit():
        return code
    with session_scope() as session:
        hits = ws.search(session, query, limit=5)
        exact = [h for h in hits if h["code"] == ws.normalize_code(query) or h.get("name") == query]
        hit = exact[0] if exact else (hits[0] if len(hits) == 1 else None)
        if hit is None:
            if hits:
                raise ValueError("匹配到多只股票，请写完整代码")
            raise ValueError("没找到这只股票")
        return hit["code"]


def _reject_out_of_scope(code: str) -> None:
    reason = reject_reason(code)
    if reason == "科创板不在纳入范围":
        raise ValueError("科创板（688/689）不在讨论验证范围内")
    if reason:
        raise ValueError(reason)
    if classify(code)[0] not in {"SSE", "SZSE"}:
        raise ValueError("只支持沪深 A 股")


def _stock_name(session, code: str) -> str:
    row = session.query(Stock).filter(Stock.code == code).first()
    if row and row.name:
        return row.name
    return ws._result({"code": code, "name": ""}).get("name", "")


def _local_bars(session, code: str, day: date, count: int = 6) -> list[dict]:
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date <= day)
        .order_by(DailyQuote.trade_date.desc())
        .limit(count)
        .all()
    )
    out = []
    for row in reversed(rows):
        out.append({
            "date": row.trade_date.isoformat(),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "pre_close": row.pre_close,
            "pct_chg": row.change_percent,
            "volume": row.volume,
            "amount": row.amount,
            "turnover_rate": row.turnover_rate,
            "is_limit_up": bool(row.is_limit_up),
        })
    return out


def _local_similarity(session, code: str, day: date) -> dict:
    try:
        return similar_to(session, code, day, top=3)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _local_ranking(session, day: date) -> dict | None:
    try:
        return get_ranking(session, day)
    except Exception:  # noqa: BLE001
        return None


def _rank_item(payload: dict, code: str) -> dict | None:
    for item in payload.get("items") or []:
        if item.get("code") == code:
            return item
    return None


def build_evidence(session, code: str, name: str, day: date) -> dict:
    exchange, board = classify(code)
    metrics = stock_metrics(load_bars(session, code, day, 70))
    market = market_context(session, day)
    sim = _local_similarity(session, code, day)
    ranking = _local_ranking(session, day)
    rank_item = _rank_item(ranking, code) if ranking else None
    formula = (rank_item or {}).get("formula") or {}
    local_bars = _local_bars(session, code, day)
    return {
        "evaluated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "local": {
            "code": code,
            "name": name,
            "exchange": exchange,
            "board": board,
            "as_of": day.isoformat(),
            "bars": local_bars,
            "metrics": metrics,
            "similarity": sim,
            "in_ranking": bool(rank_item),
            "formula": formula,
        },
        "market": market,
        "is_limit_up": bool(local_bars and local_bars[-1].get("is_limit_up")),
    }


def _evidence_text(code: str, name: str, day: date, evidence: dict, web: list[dict]) -> str:
    from backend.app.services.ai_context import LEGEND, stock_line

    local = evidence.get("local") or {}
    metrics = local.get("metrics") or {}
    sim = local.get("similarity") or {}
    line = stock_line({
        "code": code,
        "name": name,
        "board_name": local.get("board"),
        "top_matches": (sim.get("matches") or [])[:3],
        "max_similarity": (sim.get("stats") or {}).get("max_similarity"),
        "avg_similarity": (sim.get("stats") or {}).get("avg_similarity"),
        "high_similarity_count": (sim.get("stats") or {}).get("high_similarity_count"),
        "sample_count": (sim.get("stats") or {}).get("sample_count"),
        "band": (sim.get("stats") or {}).get("band"),
        "formula": local.get("formula"),
        "is_limit_up": bool(evidence.get("is_limit_up")),
    }, metrics)

    # 榜单/相似度给不出时不要硬塞“今日”，用 stock_line 的 is_limit_up 由公式决定。
    lines = [
        f"评估日 {day.isoformat()}，股票 {code} {name}（{local.get('exchange')} {local.get('board') or ''}）。",
        LEGEND,
        line,
    ]
    if evidence.get("market"):
        from backend.app.services.ai_context import market_line

        lines.append(market_line(evidence["market"]))
    else:
        lines.append("市场环境：-")
    lines.append("\n[本地相似度] " + json.dumps(sim, ensure_ascii=False))
    lines.append("\n[联网核对]")
    for item in web:
        lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "你是一名与会分析师，只依据系统给出的证据包和此前发言发表看法。"
        "不得编造输入中没有的估值、市盈率、盈利、股东、北向资金或基本面数据；不得给买入/卖出建议或目标价；不得宣称必然涨停。"
        "发现本地行情与联网快照不一致要明确指出。"
        + QUANT_STANDARD_TEXT
    )


def _chair_system_prompt() -> str:
    return (
        "你是讨论会主席，负责总结并估算下一交易日收盘相对评估日收盘上涨的概率。"
        "只依据证据包和全部发言；不得编造输入中没有的数据；不得给买入/卖出建议或目标价；不得宣称必然涨停。"
        + QUANT_STANDARD_TEXT
    )


def _speaker_prompt(evidence_text: str, prior: list[dict], profile: dict, index: int, total: int) -> str:
    prior_text = ""
    for item in prior:
        prior_text += f"\n[{item['speaker']}] {item['content']}\n"
    role = "你是主席，最后总结。" if index == total - 1 else f"你是第 {index + 1} 位与会分析师。"
    return (
        f"{role}\n"
        f"这是第 {index + 1}/{total} 位发言。\n\n"
        f"===== 证据包 =====\n{evidence_text}\n\n"
        f"===== 此前发言 =====\n{prior_text or '（还没有前人发言）'}\n"
        + ("请输出结构化 JSON 结论，字段：up_probability, confidence, verified, conflicts_or_unverified, agreements, disagreements, bullish, bearish, risks, cne6_notes, summary, disclaimer。"
           if index == total - 1 else "请发表分析意见，可以同意、反驳或指出数据矛盾。")
    )


def _extract_json(text: str) -> dict:
    import json as _json

    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    candidates = re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, flags=re.S)
    if candidates:
        text = next((c for c in candidates if "{" in c), candidates[0])
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有 JSON")
    for attempt in (text[start : end + 1], re.sub(r",\s*([}\]])", r"\1", text[start : end + 1])):
        try:
            payload = _json.loads(attempt)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("JSON 解析失败")


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def normalize_conclusion(raw: dict) -> dict:
    """主席输出契约：概率钳制 0–100，缺字段降级说明，不编造。"""
    probability = _num(raw.get("up_probability"))
    if probability is None:
        raise ValueError("主席结论缺少 up_probability")
    probability = max(0.0, min(100.0, probability))
    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in CONFIDENCE_SET:
        confidence = "low"
    return {
        "up_probability": round(probability, 2),
        "confidence": confidence,
        "verified": _str_list(raw.get("verified")),
        "conflicts_or_unverified": _str_list(raw.get("conflicts_or_unverified")),
        "agreements": _str_list(raw.get("agreements")),
        "disagreements": _str_list(raw.get("disagreements")),
        "bullish": _str_list(raw.get("bullish")),
        "bearish": _str_list(raw.get("bearish")),
        "risks": _str_list(raw.get("risks")),
        "cne6_notes": _str_list(raw.get("cne6_notes")),
        "summary": str(raw.get("summary") or "").strip(),
        "disclaimer": str(raw.get("disclaimer") or DISCLAIMER).strip(),
    }


def _public_participants(profiles: list[dict]) -> list[dict]:
    return [
        {"id": p["id"], "name": p.get("name") or "", "model": p.get("model") or ""}
        for p in profiles
    ]


def _chat_for(profile: dict):
    from backend.app.core.ai_client import chat

    settings = ai_profiles.settings_for(profile["id"])

    def run(system: str, prompt: str) -> str:
        return chat(system, prompt, settings=settings, min_tokens=1000)

    return run


def start(code_or_name: str, day: date | None = None, profile_ids: list[str] | None = None) -> dict:
    if not _START_LOCK.acquire(blocking=False):
        raise RuntimeError("已经有一场讨论在跑，请稍后再试")
    try:
        code = _parse_code_or_name(code_or_name)
        _reject_out_of_scope(code)
        profiles = _valid_profiles(profile_ids)
        if len(profiles) < 2:
            raise ValueError("至少需要两个已填密钥的模型才能开会，请到 设置 → AI 模型 配置")
        if day is None:
            with session_scope() as session:
                from backend.app.services.retention import quote_dates

                days = quote_dates(session)
                if not days:
                    raise LookupError("还没有行情")
                day = days[-1]
        if day > today():
            raise ValueError("不能用未来的日期")

        with session_scope() as session:
            name = _stock_name(session, code)
            evidence = build_evidence(session, code, name, day)
            session.add(AiCouncilSession(
                stock_code=code,
                stock_name=name,
                eval_date=day,
                status="running",
                participants=_public_participants(profiles),
                evidence=evidence,
                statements=[],
                conclusion=None,
                error="",
            ))
            session.commit()
            session_id = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first().id

        with _LOCK:
            _PROGRESS.update(running=True, phase="collecting_local", session_id=session_id, stock=code, stage="搜集本地证据",
                             current_model="", done=0, total=len(profiles), last_status="已开始", error="")

        thread = threading.Thread(target=_run, args=(session_id, code, name, day, profiles, evidence), daemon=True)
        thread.start()
        return {"session_id": session_id, "started": True, "participants": _public_participants(profiles)}
    finally:
        _START_LOCK.release()


def _update_progress(**kwargs) -> None:
    with _LOCK:
        _PROGRESS.update(kwargs)


def _run(session_id: int, code: str, name: str, day: date, profiles: list[dict], evidence: dict) -> None:
    try:
        _update_progress(phase="verifying_web", stage="联网核对", last_status="正在联网核对公开行情/新闻/公告")
        from backend.app.services.council import web_evidence

        web = web_evidence.collect(code)
        evidence = dict(evidence)
        evidence["web"] = web
        evidence_text = _evidence_text(code, name, day, evidence, web)

        statements: list[dict] = []
        total = len(profiles)
        for index, profile in enumerate(profiles):
            is_chair = index == total - 1
            _update_progress(phase="chairman" if is_chair else "speaking", stage="主席总结中" if is_chair else f"第 {index + 1} 位模型发言中",
                             current_model=profile.get("name") or profile.get("model"), done=index, total=total,
                             last_status=f"正在等待 {profile.get('name') or profile.get('model')} 发言")
            system = _chair_system_prompt() if is_chair else _system_prompt()
            prompt = _speaker_prompt(evidence_text, statements, profile, index, total)
            try:
                content = _chat_for(profile)(system, prompt)
                statements.append({"index": index, "speaker": profile.get("name") or profile.get("model"), "model": profile.get("model"), "content": content, "error": ""})
                if is_chair:
                    conclusion = normalize_conclusion(_extract_json(content))
            except Exception as exc:  # noqa: BLE001
                message = str(exc)[:240]
                statements.append({"index": index, "speaker": profile.get("name") or profile.get("model"), "model": profile.get("model"), "content": "", "error": message})
                if is_chair:
                    message = f"主席发言失败：{message}"
                    statements[-1]["error"] = message
                    _update_progress(error=message)
                    raise
            # 持续保存中途记录
            with session_scope() as session:
                row = session.query(AiCouncilSession).filter(AiCouncilSession.id == session_id).one()
                row.evidence = evidence
                row.statements = list(statements)
                row.status = "running"
                session.commit()

        conclusion = normalize_conclusion(_extract_json(statements[-1]["content"]))
        with session_scope() as session:
            row = session.query(AiCouncilSession).filter(AiCouncilSession.id == session_id).one()
            row.status = "done"
            row.conclusion = conclusion
            row.statements = list(statements)
            row.finished_at = _now_dt()
            session.commit()
        _update_progress(running=False, phase="done", stage="完成", current_model="", done=total, total=total,
                         last_status="讨论完成", error="")
        return None
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:240]
        with session_scope() as session:
            row = session.query(AiCouncilSession).filter(AiCouncilSession.id == session_id).first()
            if row:
                row.status = "failed"
                row.error = message
                row.statements = list(statements)
                row.finished_at = _now_dt()
                session.commit()
        _update_progress(running=False, phase="failed", stage="失败", current_model="", last_status="讨论失败", error=message)


def _session_payload(row: AiCouncilSession) -> dict:
    return {
        "id": row.id,
        "stock_code": row.stock_code,
        "stock_name": row.stock_name,
        "eval_date": row.eval_date.isoformat(),
        "status": row.status,
        "participants": row.participants,
        "evidence": row.evidence,
        "statements": row.statements,
        "conclusion": row.conclusion,
        "error": row.error,
        "created_at": row.created_at.isoformat(timespec="seconds") + "Z",
        "finished_at": row.finished_at.isoformat(timespec="seconds") + "Z" if row.finished_at else None,
    }


def latest() -> dict | None:
    with session_scope() as session:
        row = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first()
        return _session_payload(row) if row else None


def get(session_id: int) -> dict | None:
    with session_scope() as session:
        row = session.query(AiCouncilSession).filter(AiCouncilSession.id == session_id).first()
        return _session_payload(row) if row else None


def history(code: str | None = None, day: date | None = None, limit: int = 100) -> list[dict]:
    with session_scope() as session:
        query = session.query(AiCouncilSession)
        if code:
            query = query.filter(AiCouncilSession.stock_code == code)
        if day:
            query = query.filter(AiCouncilSession.eval_date == day)
        rows = query.order_by(AiCouncilSession.id.desc()).limit(min(limit, 500)).all()
        return [_session_payload(row) for row in rows]
