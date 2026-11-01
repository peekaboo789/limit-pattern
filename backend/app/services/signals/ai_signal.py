"""AI 信号：规则 + AI 信号，固定提示词版本 + 温度，结构化 JSON，记模型版本。

复用 ai_client.chat + summary_service 的 _extract_json / salvage_objects，绝不硬编码密钥。
AI 置信度不是概率，只是「模型自评的把握」，最终要靠 calibration_report 校准。
"""

from __future__ import annotations

from datetime import date

from backend.app.core.config import read_settings_file
from backend.app.services.summary_service import AiFailed, AiNotConfigured, _extract_json, ai_ready, call_chat

SIGNAL_PROMPT_VERSION = "v1"
SIGNAL_TEMPERATURE = 0.2

SYSTEM_PROMPT = (
    "你是一名资深 A 股短线交易与涨停板研究员，多年复盘涨停板、连板、首板、炸板、分歧转一致、龙头股和情绪周期。"
    "现在根据用户给出的、由本地日线算好的候选特征，判断哪些是值得跟踪的短线信号。只用给出的数字，不得编造。"
    "confidence 是你自评的把握（0–100），不是上涨概率。不要预测涨跌，不要给买卖建议。\n"
    '只返回 JSON：{"signals":[{"code":"600000","confidence":72,"reason":"60字以内"}]}'
)


def _parse_signals(text: str, partial: bool = False) -> list[dict]:
    from backend.app.services.summary_service import salvage_objects

    try:
        payload = _extract_json(text)
        raw = payload.get("signals")
        if not isinstance(raw, list):
            raise AiFailed("AI 没有返回 signals 列表", "JSON 格式不对")
    except AiFailed:
        if not partial:
            raise
        raw = salvage_objects(text)
        if not raw:
            raise
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()[-6:]
        if not code:
            continue
        try:
            confidence = float(entry.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        out.append(
            {
                "code": code,
                "confidence": None if confidence is None else round(max(0.0, min(100.0, confidence)), 1),
                "reason": str(entry.get("reason") or "").strip()[:60],
            }
        )
    if not out:
        raise AiFailed("AI 没有给出有效信号", "JSON 格式不对")
    return out


def ai_signals(
    day: date,
    candidates: list[dict],
    *,
    chat=None,
    settings: dict | None = None,
    market: str = "",
) -> list[dict]:
    """对一组候选让 AI 挑信号。candidates: [{code, name?, line?}]。

    整次运行固定模型 + 温度；返回带 model / prompt_version / temperature 的结果。
    """
    settings = settings if settings is not None else read_settings_file()
    if chat is None and not ai_ready():
        raise AiNotConfigured("未配置AI接口")
    lines = [f"评估日 {day.isoformat()}，候选 {len(candidates)} 只。", market or "市场环境：-"]
    lines.extend(
        c.get("line") or f"{c['code']} {c.get('name') or ''}" for c in candidates
    )
    prompt = "\n".join(lines)
    if chat is not None:
        text = chat(prompt)
    else:
        text = call_chat(prompt, system=SYSTEM_PROMPT, settings=settings, thinking_default=False)
    signals = _parse_signals(text, partial=False)
    model = str(settings.get("ai_model") or "") if chat is None else ""
    for signal in signals:
        signal["model"] = model
        signal["prompt_version"] = SIGNAL_PROMPT_VERSION
        signal["temperature"] = SIGNAL_TEMPERATURE
    return signals
