"""Discussion estimates are explicit-unit, uncalibrated opinions, never score inputs."""
from __future__ import annotations

import math
from urllib.parse import urlparse

CONTRACT_VERSION = 2
PROBABILITY_NOTICE = "仅为模型主观估计，未经统计校准，不是实测上涨频率；不计入综合评分，不是投资建议。"


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def normalize_probability(raw: dict) -> dict:
    value = raw.get("up_probability")
    unit = raw.get("probability_unit")
    number = finite_number(value)
    reason = ""
    status = "estimated"
    normalized = None
    if not isinstance(unit, str) or unit not in {"percent", "fraction"}:
        status, reason = "ambiguous_unit", "原始输出没有明确percent（0–100）或fraction（0–1）单位，不能推断或擅自乘100"
    elif value is None:
        status, reason = "not_estimable", "模型未给出数值，不能从文字或默认值补造概率"
    elif number is None:
        status, reason = "invalid", "概率必须是有限JSON数值；不接受布尔、字符串、NaN或Infinity"
    elif not 0 <= number <= (100 if unit == "percent" else 1):
        status, reason = "invalid", f"数值超出{unit}规定范围，不做钳制或猜测修正"
    else:
        normalized = number if unit == "percent" else number * 100
    safe_raw = value if number is not None or value is None or isinstance(value, (str, bool)) else repr(value)
    return {
        "up_probability": normalized,
        "probability_unit": "percent",
        "probability_contract_version": CONTRACT_VERSION,
        "probability_status": status,
        "probability_reason": reason,
        "probability_validation_reason": reason,
        "probability_audit": {"raw_value": safe_raw, "raw_unit": unit if isinstance(unit, str) or unit is None else repr(unit),
                              "normalized_candidate_percent": normalized,
                              "normalization": "explicit_fraction_times_100" if normalized is not None and unit == "fraction" else ("explicit_percent_unchanged" if normalized is not None else "not_normalized")},
        "probability_notice": PROBABILITY_NOTICE,
    }


def _site(item):
    host = (urlparse(str(item.get("url") or "")).hostname or "").lower()
    for domain in ("eastmoney.com", "sina.cn", "sina.com.cn", "sinajs.cn", "gtimg.cn", "qq.com"):
        if host == domain or host.endswith("." + domain):
            return "sina" if "sina" in domain else "tencent" if domain in {"gtimg.cn", "qq.com"} else "eastmoney"
    return host or str(item.get("source") or "unknown")


def assess_evidence_quality(evidence: dict) -> dict:
    local = evidence.get("local") or {}
    as_of = str(local.get("as_of") or (evidence.get("kline") or {}).get("as_of") or "")[:10]
    bars = (evidence.get("kline") or {}).get("merged_bars") or local.get("bars_30d") or local.get("bars") or []
    valid = {str(bar.get("date"))[:10]: bar for bar in bars if bar.get("date") and str(bar["date"])[:10] <= as_of
             and all(finite_number(bar.get(key)) is not None and bar[key] > 0 for key in ("open", "high", "low", "close"))}
    observations = []
    financial = False
    counts = {}
    web = evidence.get("web") or []
    for item in web:
        status = item.get("status", "failed")
        counts[status] = counts.get(status, 0) + 1
        if status != "ok":
            continue
        digest = item.get("digest") or {}
        if not isinstance(digest, dict):
            continue
        for bar in digest.get("bars") or []:
            price = finite_number(bar.get("close"))
            if str(bar.get("date"))[:10] == as_of and price is not None:
                observations.append((_site(item), price))
        price = finite_number(digest.get("price"))
        if str(digest.get("actual_date"))[:10] == as_of and price is not None:
            observations.append((_site(item), price))
        latest = digest.get("latest") or {}
        disclosed = str(latest.get("publish_date") or latest.get("notice_date") or "")[:10]
        report = str(latest.get("report_date") or "")[:10]
        has_metrics = any(finite_number(latest.get(key)) is not None for key in
                          ("revenue", "net_profit", "eps", "total_assets", "operating_cash_flow"))
        has_metrics = has_metrics or any(isinstance(value, dict) and finite_number(value.get("value")) is not None
                                         for value in (latest.get("metrics") or {}).values())
        if item.get("category") == "financial" and has_metrics and report and disclosed and report <= as_of and disclosed <= as_of:
            financial = True
    sites = {site for site, _ in observations}
    matching = False
    conflict = False
    for site, price in observations:
        for other_site, other in observations:
            if site != other_site:
                if abs(price-other) <= max(.011, min(price, other)*.001):
                    matching = True
                else:
                    conflict = True
    local_price = finite_number((valid.get(as_of) or {}).get("close"))
    if local_price is not None and any(abs(local_price-price) > max(.011, min(local_price, price)*.001) for _, price in observations):
        conflict = True
    missing = []
    if not as_of or len(valid) < 20:
        missing.append(f"评估日前有效日K不足20根（现有{len(valid)}根）")
    if as_of not in valid:
        missing.append("缺评估日有效收盘行情")
    if not matching:
        missing.append("缺至少两家独立公开来源的同日价格一致核对")
    if conflict:
        missing.append("独立来源同日价格存在差异，需先核验")
    if not financial:
        missing.append("缺披露日明确且不晚于评估日的最近财报")
    return {"eligible_for_probability": not missing, "critical_missing": missing,
            "valid_bar_count": len(valid), "as_of": as_of, "price_sites": sorted(sites),
            "price_crosscheck": "conflict" if conflict else "matched" if matching else "unverified",
            "latest_disclosed_financial": financial, "source_status_counts": counts,
            "source_count": len(web), "successful_source_count": counts.get("ok", 0),
            "policy": "保守展示门槛：至少20根有效K线、评估日收盘、两家独立同日价格一致、已披露财报。通过也不代表统计校准。"}


def apply_evidence_gate(conclusion: dict, evidence: dict) -> dict:
    result = dict(conclusion)
    quality = assess_evidence_quality(evidence)
    result["evidence_quality"] = quality
    result["probability_notice"] = PROBABILITY_NOTICE
    if not quality["eligible_for_probability"]:
        result["up_probability"] = None
        previous = result.get("probability_validation_reason") or ""
        result["probability_status"] = "insufficient_evidence"
        result["probability_reason"] = (previous + "；" if previous else "") + "证据不足：" + "；".join(quality["critical_missing"])
        result["confidence"] = "low"
    return result
