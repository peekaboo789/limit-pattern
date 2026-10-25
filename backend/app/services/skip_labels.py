"""跳过账原因的中文说法。

`skipped_records.reason` 存的是机器码（`insufficient_window` 之类），页面上直接露出来就是一串英文。
各阶段的原因码分散在各自的服务里，这里只做汇总、不另起第二套说法：每个阶段的那句话仍由
那个阶段的服务定义。同一个码在不同阶段意思可能不同（样本的「三根」在涨停日之前，
扫描的「三根」含评估日当天），所以按阶段查；不带阶段时才退到一句两边都对的通用说法。
"""

from __future__ import annotations

import re

from backend.app.services import match_service, pattern_service, training_service
from backend.app.services.feature_service import SKIP_NO_BAR

BY_STAGE: dict[str, dict[str, str]] = {
    "feature": {SKIP_NO_BAR: "区间内没有可用的收盘K线"},
    "pattern": dict(pattern_service.REASON_LABELS),
    "match": {
        match_service.INSUFFICIENT_WINDOW: match_service.REASONS[match_service.INSUFFICIENT_WINDOW],
        match_service.QUALITY_ERROR: match_service.REASONS[match_service.QUALITY_ERROR],
        match_service.EMPTY_POOL: match_service.REASONS[match_service.EMPTY_POOL],
    },
    "training": dict(training_service.REASON_LABELS),
}

#: 几个阶段共用一个码、说法又不一样时，不带阶段的那一眼用这句。
GENERIC: dict[str, str] = {
    "insufficient_window": "凑不齐形态那三根K线",
    "data_quality_error": "命中 error 级数据质量异常",
}


_MACHINE_CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")


def label(reason: str, stage: str | None = None) -> str:
    if stage and reason in BY_STAGE.get(stage, {}):
        return BY_STAGE[stage][reason]
    if reason in GENERIC:
        return GENERIC[reason]
    for labels in BY_STAGE.values():
        if reason in labels:
            return labels[reason]
    return reason


def annotate(page: dict, stage: str | None) -> dict:
    """给 `skip_page` 的回包补上中文：每行一句 `reason_label`，计数那组一份 `reason_labels`。"""
    for item in page.get("items") or []:
        stage_of = str(item.get("stage") or "") or None
        item["reason_label"] = label(str(item.get("reason") or ""), stage_of)
        # 旧版本把窗口函数的原因码直接写进了说明列（`insufficient_window`）。库里留着的那些行
        # 不重建就一直在，读的时候换成中文，别让页面露英文。
        detail = str(item.get("detail") or "")
        if _MACHINE_CODE.fullmatch(detail):
            item["detail"] = label(detail, stage_of)
    page["reason_labels"] = {reason: label(reason, stage) for reason in page.get("by_reason") or {}}
    return page


__all__ = ["BY_STAGE", "GENERIC", "annotate", "label"]
