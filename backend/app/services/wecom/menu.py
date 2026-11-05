"""企业微信应用底部自定义菜单（3 个一级菜单 × 最多 5 个子菜单，click 类型，点击后回调 Event=click / EventKey）。"""

from __future__ import annotations

from backend.app.services.wecom import client

LAYOUT = [
    ("榜单", [("今日AI榜单", "RANK_TODAY"), ("实时监控", "RANK_LIVE"), ("相似前10", "SIM_TOP"),
              ("次日观察池", "NEXT_POOL"), ("重新生成榜单", "RANK_REGEN")]),
    ("工具", [("一键采集+AI分析", "CAPTURE_AI"), ("切换AI", "AI_SWITCH"), ("采集当前行情", "CAPTURE"),
              ("同步行情", "SYNC"), ("今日总结", "SUMMARY")]),
    ("自选", [("自选行情", "WL_QUOTE"), ("自选异动≥3%", "WL_MOVERS"), ("导出自选", "WL_EXPORT"),
              ("系统状态", "STATUS"), ("帮助", "HELP")]),
]
KEYS = {key for _name, subs in LAYOUT for _n, key in subs}


def flat_items() -> list[tuple[str, str, str]]:
    """把 LAYOUT 扁平化为 (组名, 名称, key)，作为菜单卡片和映射的唯一真相源。"""
    return [(group, name, key) for group, subs in LAYOUT for name, key in subs]


def wechat_group_cmd(text: str) -> str:
    """解析「工具1 / 工具 1 / 榜单2 / 自选3」这类分组名+序号；无法解析返回空字符串。"""
    import re

    s = re.sub(r"\s+", "", str(text or "").strip())
    m = re.match(r"^([榜工自])(?:[单具选])([0-9]+)$", s)
    if not m:
        return ""
    group_name = m.group(1)
    num = int(m.group(2))
    for group, subs in LAYOUT:
        if group.startswith(group_name):
            if 1 <= num <= len(subs):
                return subs[num - 1][1]
            return ""
    return ""


def menu_json() -> dict:
    return {"button": [{"name": name, "sub_button": [{"type": "click", "name": n, "key": k} for n, k in subs]} for name, subs in LAYOUT]}


def sync_menu(cfg: dict | None = None) -> dict:
    cfg = cfg or client.config()
    if not (cfg["corpid"] and cfg["agentid"] and cfg["secret"]):
        raise client.WeComError(None, "底部菜单需要 企业ID + AgentId + Secret（群机器人没有菜单）")
    return client.create_menu(cfg, menu_json())


def wechat_menu_card() -> str:
    """个人微信不支持持久底部菜单，退化为文本菜单卡片；序号与企业微信 LAYOUT 一致。"""
    rows = ["📖 底部菜单（回复序号 1-15 直接执行）："]
    number = 0
    for name, subs in LAYOUT:
        rows.append(f"【{name}】")
        for _n, _key in subs:
            number += 1
            rows.append(f"{number}. {_n}")
    rows += [
        "",
        "命令与「帮助」一致，也可直接发 榜单 / 采集分析 / 自选 等文字；",
        "也可发 工具1 / 榜单2 / 自选3 这类「分组名+序号」；切换AI 后请直接回复模型序号。",
    ]
    return "\n".join(rows)


def wechat_menu_cmd(number: str) -> str:
    """把菜单卡片序号映射到 commands.MENU 的菜单 key。越界返回空。"""
    try:
        num = int(str(number or "").strip())
    except (TypeError, ValueError):
        return ""
    flat = flat_items()
    return flat[num - 1][2] if 1 <= num <= len(flat) else ""


def autosync_in_background() -> None:
    """版本变化后启动时自动同步一次菜单（设置里可关）。"""
    import os
    import threading

    from backend.app.core.logging import get_logger

    if os.environ.get("LP_SCHEDULER", "1") == "0":  # 测试 / 无定时任务模式不联网
        return
    raw = client.read_settings_file()
    cfg = client.config(raw)
    if not (cfg["enabled"] and raw.get("wecom_menu_autosync") is not False and cfg["corpid"] and cfg["agentid"] and cfg["secret"]):
        return

    def run():
        try:
            sync_menu(cfg)
        except Exception as exc:  # noqa: BLE001
            get_logger("limit_pattern.wecom").warning("自动同步企业微信菜单失败：%s", exc)

    threading.Thread(target=run, name="wecom-menu", daemon=True).start()
