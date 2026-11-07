"""v0.5.4：菜单新增 一键采集+AI分析 / 切换AI；回复数字切换模型；手机端模型列表与切换接口。"""

from __future__ import annotations

import time

from backend.app.services.wecom import commands, menu
from tests.test_config_models_052 import KEY_B, _seed
from tests.test_wecom import write_settings
from tests.test_wecom_051 import _click, _post, mock  # noqa: F401


def _text(user, content):
    return (f"<xml><ToUserName>x</ToUserName><FromUserName>{user}</FromUserName><MsgType>text</MsgType>"
            f"<Content><![CDATA[{content}]]></Content><AgentID>1000002</AgentID></xml>")


def _two_profiles(client):
    _seed(client)
    client.post("/api/ai/profiles", json={"name": "DeepSeek", "base_url": "https://b.example/v1", "model": "model-b", "api_key": KEY_B})


def _wait(mock, n):
    for _ in range(100):
        if len(mock.sent) >= n:
            return
        time.sleep(0.05)


def test_menu_layout():
    names = [(b["name"], [s["name"] for s in b["sub_button"]]) for b in menu.menu_json()["button"]]
    assert names[1] == ("工具", ["一键采集+AI分析", "切换AI", "采集当前行情", "同步行情", "今日总结"])
    assert "帮助" in names[2][1] and "添加说明" not in str(names)
    assert commands.MENU["CAPTURE_AI"] == "采集分析" and commands.MENU["AI_SWITCH"] == "切换AI"


def test_text_aliases():
    for t in ("采集分析", "一键采集+AI分析", "采集AI"):
        assert commands.parse(t)[0] == "采集分析"
    for t in ("切换AI", "切换模型", "模型", "AI列表"):
        assert commands.parse(t)[0] == "切换AI"
    assert commands.parse("采集")[0] == "采集"


def test_switch_by_number_over_callback(client, mock):
    _two_profiles(client)
    write_settings(client)
    _post(client, _click("zhangsan", "AI_SWITCH"))
    _wait(mock, 1)
    body = mock.sent[-1]["text"]["content"]
    assert "1. ✅" in body and "2. ▫️ DeepSeek" in body and "回复数字切换" in body and KEY_B not in body
    _post(client, _text("zhangsan", "5"))
    _wait(mock, 2)
    assert "1~2" in mock.sent[-1]["text"]["content"]
    _post(client, _text("zhangsan", "2"))
    _wait(mock, 3)
    assert "已切换AI" in mock.sent[-1]["text"]["content"] and "model-b" in mock.sent[-1]["text"]["content"]
    assert client.get("/api/ai/models").json()["active_name"] == "DeepSeek"
    # 选完后数字不再当成切换（没有待选列表）
    assert commands.pending_pick("zhangsan") is False
    # 不在白名单的人发数字 / 切换AI 都被拒
    _post(client, _text("wangwu", "切换AI"))
    _wait(mock, 4)
    assert mock.sent[-1]["touser"] == "wangwu" and "白名单" in mock.sent[-1]["text"]["content"]


def test_pick_expires(client, monkeypatch):
    _two_profiles(client)
    commands.ai_list("lisi")
    assert commands.pending_pick("lisi")
    monkeypatch.setattr(commands, "PICK_TTL", -1)
    commands.ai_list("lisi")
    assert not commands.pending_pick("lisi")
    assert "过期" in commands.pick("lisi", "1")[1]


def test_pick_non_ascii_digit_is_friendly(client):
    _two_profiles(client)
    commands.ai_list("zhaoliu")
    assert commands.pending_pick("zhaoliu")
    title, body = commands.pick("zhaoliu", "²")  # Unicode 上标，isdigit() 为真但 int() 会炸
    assert title == "🤖 切换AI" and "1~2" in body
    assert commands.pending_pick("zhaoliu") is True  # 没真正切走，列表仍待选


def test_pick_empty_and_out_of_range_prompt(client):
    _two_profiles(client)
    commands.ai_list("sunqi")
    for text in ("", "0", "3", "abc"):
        assert "1~2" in commands.pick("sunqi", text)[1]


def test_handle_message_superscript_falls_through(client):
    from backend.app.api.wecom_routes import handle_message

    _two_profiles(client)
    cfg = {"admins": ["zhangsan"]}
    commands.ai_list("zhangsan")
    assert commands.pending_pick("zhangsan")
    info = handle_message(cfg, _text("zhangsan", "²"))
    assert info["cmd"] == "unknown"  # 不当成「选AI」去 int()，落到正常解析
    info = handle_message(cfg, _text("zhangsan", "1"))
    assert info["cmd"] == "选AI" and info["arg"] == "1"


def test_models_api(client):
    _two_profiles(client)
    got = client.get("/api/ai/models").json()
    assert [i["index"] for i in got["items"]] == [1, 2] and got["items"][0]["active"] and KEY_B not in str(got)
    got = client.post("/api/ai/models/active", json={"index": 2}).json()
    assert got["active_name"] == "DeepSeek"
    got = client.post("/api/ai/models/active", json={"id": got["items"][0]["id"]}).json()
    assert got["items"][0]["active"]
    assert client.post("/api/ai/models/active", json={"index": 9}).status_code == 422
    assert client.post("/api/ai/models/active", json={"id": "nope"}).status_code == 404


def test_capture_ai_flow(monkeypatch):
    from backend.app.services import manual_capture, ranking_service, summary_service

    monkeypatch.setattr(summary_service, "ai_ready", lambda: True)
    board = {"date": "2026-10-30", "trigger": "manual", "snapshot": {"kind": "daily"}, "ai_error": "",
             "items": [{"rank": i, "code": f"6000{i:02d}", "name": f"股{i}", "composite_score": 90 - i, "model_score": 80 - i, "reason": "形态接近"} for i in range(1, 31)]}

    def nodata():
        raise manual_capture.NoDataYet("今天不是交易日")
    monkeypatch.setattr(manual_capture, "capture", nodata)
    monkeypatch.setattr(ranking_service, "generate_ranking", lambda *a, **k: dict(board))
    out = commands.capture_ai_job()
    title, body = out[0]
    assert "前20" in title and "20. 股20" in body and "21. 股21" not in body
    assert "收盘" in body and "今天不是交易日" in body and "不是上涨概率" in body


def test_capture_ai_no_key_and_concurrency(monkeypatch):
    from backend.app.services import summary_service

    monkeypatch.setattr(summary_service, "ai_ready", lambda: False)
    assert "没有配置好" in commands.start_capture_ai(None)[1]
    assert commands._CAP_AI.acquire(blocking=False)
    try:
        assert "还没做完" in commands.start_capture_ai(lambda t, b: None)[1]
    finally:
        commands._CAP_AI.release()


def test_capture_ai_click_replies_started(client, mock, monkeypatch):
    write_settings(client)
    monkeypatch.setattr(commands, "capture_ai_job", lambda: [("🤖 采集+AI分析 前20（共1只）", "1. 股1")])
    _post(client, _click("zhangsan", "CAPTURE_AI"))
    _wait(mock, 2)
    texts = [m["text"]["content"] for m in mock.sent]
    assert "已开始一键采集+AI分析" in texts[0] and "采集+AI分析 前20" in texts[1]
