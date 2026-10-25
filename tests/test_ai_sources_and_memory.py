"""档5：多套大模型网关清单 + 每日对账与模型记忆快照。

两条各自要钉死的事：

* 网关清单：**密钥只进不出**，而「切换」必须让三条付费链路当场改口。这两条都只能在
  真接口 + 真解析函数上测——把 `ai_config()` 换成一个假 dict，等于这两条纪律
  随时可以在下一次改动里悄悄失效。
* 每日快照：**只渲染，不造数据**，而且**一分钱不花**。后者用「假网关被挂上但一次都没被敲」
  来证明（把 `httpx.Client` 换成一个会记账的假类，跑完读它记到的数）。

真网关一次都不敲：那是要按 token 付钱的。这一份文件里出现的 `sk-test-…` 全是
编出来专门验证「密钥不会漏进接口/日志/JSON」的假串，不是任何真实凭据。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

from backend.app.core import ai_sources
from backend.app.core.database import session_scope
from backend.app.core.constants import AI_SOURCE_MAX
from backend.app.models.tables import AiPickResult
from backend.app.services import ai_gateway, ai_memory_service, ai_picks
from tests.test_api import _save, _series, weekdays

KEY_A = "sk-test-aaaa111122223333"
KEY_B = "sk-test-bbbb111122223333"
#: 写在路径里的那种自建网关地址：整串都不能出接口。
PATH_TOKEN = "sk-test-ccccdddd1122334455"


@pytest.fixture(autouse=True)
def _forget_sources_cache_between_cases():
    """清单与设置都按「文件路径 + mtime」缓存；用例之间换 tmp_path，进程内那份结论要丢掉。"""
    yield
    ai_sources.invalidate_caches()


# --- 网关清单 ----------------------------------------------------------------


def _created(client, *, name: str = "NAS 那家", key: str = KEY_B, url: str = "") -> dict:
    payload = {
        "name": name,
        "base_url": url or f"https://gateway.test/{PATH_TOKEN}/v1",
        "model": "test-model",
        "api_key": key,
    }
    response = client.post("/api/ai-sources", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    row = next(item for item in body["sources"] if item["name"] == name)
    return {"row": row, "body": body}


def test_settings_row_is_first_builtin_and_stays_editable(client):
    """内置那条排在第一位、画得出「当前」，但不能从这一组接口里改或删。

    它是 `settings.json` 那三格的替身：能删就等于把用户已经配好的密钥删到没有出处，
    能改就等于同一套凭据有两个写入方。
    """
    body = client.get("/api/ai-sources").json()
    first = body["sources"][0]
    assert first["id"] == ai_sources.SETTINGS_SOURCE_ID
    assert first["builtin"] is True
    assert first["active"] is True
    assert first["complete"] is False  # 新库里三格都是空的

    refused = client.put(f"/api/ai-sources/{ai_sources.SETTINGS_SOURCE_ID}", json={"model": "x"})
    assert refused.status_code == 422
    assert "设置页" in refused.json()["detail"]
    refused = client.delete(f"/api/ai-sources/{ai_sources.SETTINGS_SOURCE_ID}")
    assert refused.status_code == 422
    assert "设置页" in refused.json()["detail"]

    #: 「当前」这一格跟着 `PUT /settings` 动：三格配齐了才叫配齐
    client.put("/api/settings", json={"ai_base_url": "https://a.test/v1", "ai_model": "m", "ai_api_key": KEY_A})
    after = client.get("/api/ai-sources").json()["sources"][0]
    assert after["complete"] is True
    assert after["key_present"] is True


def test_key_never_leaves_the_api_but_is_on_disk(client):
    """接口里既没有密钥也没有路径里那把 token，而文件里必须真有——否则是输入被丢了，不是打码生效。

    这一条有两个方向都要落一次：只验「接口里没有」，一个「把 api_key 整列扔掉」的实现
    也能绿；所以同时读盘上的原值。
    """
    created = _created(client)
    text = json.dumps(created["body"], ensure_ascii=False)
    assert KEY_B not in text
    assert PATH_TOKEN not in text
    assert created["row"]["key_present"] is True
    assert "sk-***" in created["row"]["base_url"]

    stored = json.loads(Path(ai_sources.sources_path()).read_text(encoding="utf-8"))
    on_disk = next(item for item in stored["sources"] if item["id"] == created["row"]["id"])
    assert on_disk["api_key"] == KEY_B
    assert PATH_TOKEN in on_disk["base_url"]


def test_url_without_protocol_or_carrying_the_mask_is_refused(client):
    """少了协议、带着 `***` 的地址都不能存：后者是接口回显的那一份，存回去就是每次 401。"""
    no_scheme = client.post(
        "/api/ai-sources", json={"name": "少协议", "base_url": "openai.com/v1", "model": "m", "api_key": KEY_A}
    )
    assert no_scheme.status_code == 422
    assert "要带协议" in no_scheme.json()["detail"]

    masked = client.post(
        "/api/ai-sources",
        json={"name": "抄回显", "base_url": "https://gateway.test/sk-***/v1", "model": "m", "api_key": KEY_A},
    )
    assert masked.status_code == 422
    assert "打码占位符" in masked.json()["detail"]


def test_name_collides_too_long_and_list_full_are_all_refused(client, monkeypatch):
    """名字撞车、名字超长、条数到顶，三种都在落盘之前挡回来。"""
    _created(client, name="第一家")

    duplicate = client.post(
        "/api/ai-sources", json={"name": "第一家", "base_url": "https://b.test/v1", "model": "m", "api_key": KEY_A}
    )
    assert duplicate.status_code == 422
    assert "已经有名叫" in duplicate.json()["detail"]

    long_name = client.post(
        "/api/ai-sources",
        json={"name": "长" * 41, "base_url": "https://b.test/v1", "model": "m", "api_key": KEY_A},
    )
    assert long_name.status_code == 422
    assert "名字最多" in long_name.json()["detail"]

    #: 上限按声明的那个常量灌满，再灌一条才判红。这里把常量压到 2 是为了不在每个用例里
    #: 真发 20 趟请求——判据读的就是「到顶这一条被挡」，与顶是几没有关系的这部分。
    monkeypatch.setattr(ai_sources, "AI_SOURCE_MAX", 2)
    for index in (2, 3):
        full = client.post(
            "/api/ai-sources",
            json={"name": f"第{index}家", "base_url": f"https://b{index}.test/v1", "model": "m", "api_key": KEY_A},
        )
        if index == 2:
            assert full.status_code == 200
        else:
            assert full.status_code == 422
            assert "先删掉一条再加" in full.json()["detail"]

    #: 到顶那一刻占满格子的是**能被删改的那几条**，内置那行不顶格子。
    #: 页面读的必须是这个数：让它自己 `filter(row => !row.builtin)` 数一遍就是第二把尺子，
    #: 而第二把尺子迟早漂成「21 / 20 条」或者「还能加但页面说满了」。
    at_cap = client.get("/api/ai-sources").json()
    assert at_cap["custom_count"] == 2
    assert len(at_cap["sources"]) == 3
    assert at_cap["sources"][0]["id"] == ai_sources.SETTINGS_SOURCE_ID
    assert at_cap["sources"][0]["builtin"] is True


def test_blank_columns_mean_unchanged_and_all_blank_is_refused(client):
    """编辑时留空 = 不改；四列全空是 422，不是「悄悄写一个空串进去」。"""
    row = _created(client)["row"]

    nothing = client.put(f"/api/ai-sources/{row['id']}", json={})
    assert nothing.status_code == 422
    assert "没有需要修改的字段" in nothing.json()["detail"]

    only_model = client.put(f"/api/ai-sources/{row['id']}", json={"model": "换成第二个模型"})
    assert only_model.status_code == 200
    after = next(item for item in only_model.json()["sources"] if item["id"] == row["id"])
    assert after["model"] == "换成第二个模型"
    #: 名字与地址沿用原值——这一条就是「三件套不能各改各的」那一层保护
    assert after["name"] == row["name"]
    assert after["base_url"] == row["base_url"]
    assert after["key_present"] is True


def test_missing_id_and_active_deletion_are_refused(client):
    """id 不在清单里要说清「刷新页面重来」；正在用的那条不许删。"""
    row = _created(client)["row"]
    gone = client.put("/api/ai-sources/s-deadbeef", json={"model": "m"})
    assert gone.status_code == 422
    assert "请刷新页面重来" in gone.json()["detail"]

    switched = client.put(f"/api/ai-sources/{row['id']}/active")
    assert switched.status_code == 200
    assert switched.json()["active_id"] == row["id"]

    refused = client.delete(f"/api/ai-sources/{row['id']}")
    assert refused.status_code == 422
    assert "正在被使用" in refused.json()["detail"]

    #: 切回内置那条之后才允许删——顺序反了就会留下一份没有出处的配置
    client.put(f"/api/ai-sources/{ai_sources.SETTINGS_SOURCE_ID}/active")
    deleted = client.delete(f"/api/ai-sources/{row['id']}")
    assert deleted.status_code == 200
    assert not [item for item in deleted.json()["sources"] if item["id"] == row["id"]]


def test_broken_json_still_renders_and_says_what_broke(client):
    """手改坏的清单不该让整个后端起不来：回落到内置那条，并把坏在哪一层写进 `note`。"""
    _created(client)
    Path(ai_sources.sources_path()).write_text("{这不是 JSON", encoding="utf-8")
    ai_sources.invalidate_caches()

    body = client.get("/api/ai-sources").json()
    assert [item["id"] for item in body["sources"]] == [ai_sources.SETTINGS_SOURCE_ID]
    assert body["note"]
    assert "JSON" in body["note"] or "json" in body["note"]

    #: 负向控制：文件修好后同一次进程里就该读回来，不用重启
    Path(ai_sources.sources_path()).write_text(
        json.dumps(
            {
                "version": 1,
                "active_id": "s-00000001",
                "sources": [
                    {"id": "s-00000001", "name": "手工那条", "base_url": "https://m.test/v1", "model": "mm", "api_key": KEY_A}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ai_sources.invalidate_caches()
    fixed = client.get("/api/ai-sources").json()
    assert fixed["active_id"] == "s-00000001"
    assert [item["name"] for item in fixed["sources"]] == [ai_sources.SETTINGS_SOURCE_NAME, "手工那条"]
    assert fixed["note"] == ""


def test_dangling_active_id_falls_back_and_says_so(client):
    """`active_id` 指着一条不存在的源时，接口上「当前」画的是**真的在用**的那一条。

    不能画成文件里那个指空的 id：那正好是「以为在用 A、实际在敲 B」这件事的画法。
    """
    Path(ai_sources.sources_path()).write_text(
        json.dumps({"version": 1, "active_id": "s-ffffffff", "sources": []}), encoding="utf-8"
    )
    ai_sources.invalidate_caches()
    body = client.get("/api/ai-sources").json()
    assert body["active_id"] == ai_sources.SETTINGS_SOURCE_ID
    assert "s-ffffffff" in body["note"]


def test_three_spend_paths_read_the_active_source(client, monkeypatch):
    """切换当场改口：三条付费链路共用 `ai_config()` 这一只手，所以敲出去的是新那一家。

    两头各落一次：不切换时读到的必须是老那一家（否则这条判据只是在测「配置能不能读」），
    切换后才变成新一家。这里把 `_stream_attempt` 换成一个记录 config 的假函数——
    一次请求都不发，钱一分都不花。
    """
    seen: list[dict] = []

    def fake_attempt(config, body, timeout, deadline, timeout_seconds, on_progress, attempt=1):
        seen.append(dict(config))
        return '{"picks": []}', {"prompt_tokens": 1}

    monkeypatch.setattr(ai_gateway, "_stream_attempt", fake_attempt)

    client.put(
        "/api/settings",
        json={"ai_base_url": "https://first.test/v1", "ai_model": "first-model", "ai_api_key": KEY_A},
    )
    ai_gateway.call_model([{"role": "user", "content": "x"}])
    assert seen[-1]["base_url"] == "https://first.test/v1"
    assert seen[-1]["model"] == "first-model"
    assert seen[-1]["source_id"] == ai_sources.SETTINGS_SOURCE_ID

    row = _created(client, url="https://second.test/v1", key=KEY_B)["row"]
    client.put(f"/api/ai-sources/{row['id']}/active")

    ai_gateway.call_model([{"role": "user", "content": "x"}])
    assert seen[-1]["base_url"] == "https://second.test/v1"
    assert seen[-1]["model"] == "test-model"
    assert seen[-1]["api_key"] == KEY_B
    assert seen[-1]["source_name"] == "NAS 那家"

    #: 另外两条链路的入口也读同一只手：`ai_ready()` 与 `model_name()` 都从 `ai_config()` 出
    assert ai_gateway.ai_ready() is True
    assert ai_gateway.model_name() == "test-model"

    client.put(f"/api/ai-sources/{ai_sources.SETTINGS_SOURCE_ID}/active")
    ai_gateway.call_model([{"role": "user", "content": "x"}])
    assert seen[-1]["base_url"] == "https://first.test/v1"


def test_switch_drops_the_previous_gateways_report(client):
    """切源之后，面板上那份「老网关给的结论」必须消失——否则页面上的模型名和真要敲的那家对不上。"""
    from backend.app.services.ai_analysis_service import board_runner

    row = _created(client, url="https://second.test/v1")["row"]
    client.put(
        "/api/settings",
        json={"ai_base_url": "https://first.test/v1", "ai_model": "first-model", "ai_api_key": KEY_A},
    )
    with board_runner._state_lock:
        board_runner._payloads["board"] = {"ok": True, "model": "first-model"}
        board_runner._stored_at["board"] = time.time()
    assert board_runner.result("board")["source"] == "memory"

    client.put(f"/api/ai-sources/{row['id']}/active")
    assert board_runner.result("board")["source"] == "none"


def test_every_key_in_the_list_is_scrubbed_and_no_overmasking(client):
    """打码名单要带全清单里的钥匙；而上游 429 文案里那种 snake_case 错误码不能被打掉。

    后半句是负向控制：形状层一旦把 `process_rate_limit_exceeded` 也顶成 `process_***`，
    「病因」就糊了，而这一层的全部意义是留下病因、只丢凭据。
    """
    _created(client, key=KEY_B)
    client.put(
        "/api/settings",
        json={"ai_base_url": "https://a.test/v1", "ai_model": "m", "ai_api_key": KEY_A},
    )
    assert KEY_A not in ai_gateway._scrub(f"回显 {KEY_A}")
    assert KEY_B not in ai_gateway._scrub(f"回显 {KEY_B}")
    assert PATH_TOKEN not in ai_gateway._scrub(f"url 里带着 {PATH_TOKEN}")
    assert "process_rate_limit_exceeded" in ai_gateway._scrub(
        '{"code":"process_rate_limit_exceeded","message":"上游并发满了"}'
    )


# --- 每日对账与模型记忆快照 ---------------------------------------------------


def _seed_days(count: int = 6) -> list[date]:
    """行情：600000 从倒数第 3 天起连板到最后一日；万科A 只在最后一天封；第三只从不封。

    连板从倒数第 3 天起头，是为了让倒数第 2 天（样本主体那天）的 `streak_days` 是 2，
    而断点（前一日不封板）落在库里有行的那一格上——这样「确值」与「下界」两种停止
    方式测的是前者。
    """
    days = weekdays(count)
    tail = len(days) - 1
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={tail - 2, tail - 1, tail}))
    _save("000002", "万科A", "SZSE", _series(days, limit_on={tail}, start=9.0))
    _save("600001", "某只观察", "SSE", _series(days, limit_on=set(), start=7.0))
    return days


def _pick(day: date, code: str, *, verdict: str, name: str = "浦发银行", report_id: int = 1) -> None:
    """造一条推荐账，字段按 `reconcile` 定判后真会写的那一份填。

    已定判的行如果漏填 `actual_*`，快照里读到的 null 就是夹具的锅，不是服务的——
    那样这一份文件里「误判库带实际结果」那条断言就什么都验不到了。
    """
    hit = verdict == ai_picks.VERDICT_HIT
    settled = verdict in {ai_picks.VERDICT_HIT, ai_picks.VERDICT_MISS}
    with session_scope() as session:
        session.add(
            AiPickResult(
                trade_date=day,
                stock_code=code,
                stock_name=name,
                source="board",
                report_id=report_id,
                model="test-model",
                recommended_at=f"{day.isoformat()}T17:10:00",
                score=88.0,
                criteria="三日形态 + 未涨停",
                reason="它说：明天会续板",
                counter_evidence="它说：量能不足",
                verdict=verdict,
                at_max_similarity=91.5,
                at_composite_score=72.0,
                at_level="high",
                actual_is_limit_up=hit if settled else None,
                actual_close=11.0 if settled else None,
                actual_change_percent=10.0 if hit else 3.3 if settled else None,
                actual_max_change_percent=10.0 if hit else 5.1 if settled else None,
                actual_source="test" if settled else "",
                checked_at=f"{day.isoformat()}T17:40:00" if settled else "",
                payload={"judge_date": day.isoformat()} if settled else {},
            )
        )


def test_sample_document_pairs_form_with_next_day(client):
    """配对样本的主体：当天的形态特征 + 连板数 + 第二天实际结果，缺的一侧写清为什么缺。"""
    days = _seed_days()
    target = days[-2]
    with session_scope() as session:
        document = ai_memory_service.build_sample_document(session, target)

    assert document["meta"]["kind"] == "model_memory"
    assert document["meta"]["quotes_in_library"] == 3
    codes = [item["code"] for item in document["stocks"]]
    assert codes == ["600000"], "这一天只有 600000 封板"
    sample = document["stocks"][0]
    #: 连板：判据日封了，前一日（T-1）也封了 → 2 板，且是被非涨停行断下来的确值
    assert sample["day"]["streak_days"] == 2
    assert sample["day"]["streak_truncated"] is False
    assert sample["day"]["is_limit_up"] is True
    assert sample["day"]["close"] == pytest.approx(sample["day"]["limit_up_price"])
    #: 前三日行情要一根根带上（喂模型的那一份形状）
    assert sample["prev_days"]["T-1"]["is_limit_up"] is True
    assert sample["prev_days"]["T-3"] is None or sample["prev_days"]["T-3"]["trade_date"]

    #: 第二天结果已经入库 → 填上，并把「第二天又封了」这件事算进市场那一段
    assert sample["next_day"] is not None
    assert sample["next_day"]["is_limit_up"] is True
    #: 最高涨幅 =（最高价 − 前收）/ 前收 × 100，现算的；涨停那天 high 就是 close，
    #: 所以它和收涨幅只差在「涨停价先四舍五入到分」这一步上（10.0321 对 10.0）。
    assert 9.9 <= sample["next_day"]["max_change_percent"] <= 10.1
    assert sample["next_day"]["max_change_percent"] == pytest.approx(
        sample["next_day"]["close_change_percent"], abs=0.05
    )
    assert document["market"]["next_day_settled_samples"] == 1
    assert document["market"]["next_day_limit_up_again"] == 1
    assert document["market"]["next_day_continuation_rate_percent"] == 100.0


def test_next_day_that_has_not_happened_yet_is_null_with_a_reason(client):
    """最新那一天没有「第二天」：那一格是 null，并且 `next_day_status` 说清为什么——不补 0。"""
    days = _seed_days()
    with session_scope() as session:
        document = ai_memory_service.build_sample_document(session, days[-1])
    for item in document["stocks"]:
        assert item["next_day"] is None
        assert "之后还没有任何行情日" in item["next_day_status"]
    assert document["market"]["next_day_settled_samples"] == 0
    #: 一条都没定判时不编一个比例出来
    assert document["market"]["next_day_continuation_rate_percent"] is None


def test_column_entirely_missing_that_day_is_measured_not_assumed(client):
    """`field_gaps` 里那条「这一列整列为空」是**当天实测**出来的，不是写死的一句话。"""
    days = _seed_days()
    latest = days[-1]
    with session_scope() as session:
        from backend.app.models.tables import DailyQuote

        for row in session.query(DailyQuote).filter(DailyQuote.trade_date == latest).all():
            row.amount = None
            row.turnover_rate = None
    with session_scope() as session:
        document = ai_memory_service.build_sample_document(session, latest)
    gaps = " ".join(document["meta"]["field_gaps"])
    assert "amount" in gaps and "turnover_rate" in gaps
    assert f"{latest.isoformat()} 这 3 行里 3 行都是 null" in gaps

    #: 反向：补上数之后那句话必须消失，否则它会变成一句关于事实的假话
    with session_scope() as session:
        from backend.app.models.tables import DailyQuote

        for row in session.query(DailyQuote).filter(DailyQuote.trade_date == latest).all():
            row.amount = 12345.0
            row.turnover_rate = 2.2
    with session_scope() as session:
        after = " ".join(ai_memory_service.build_sample_document(session, latest)["meta"]["field_gaps"])
    assert "这 3 行里 3 行都是 null" not in after


def test_hit_rate_denominator_only_counts_settled_rows(client):
    """误判库的总账：`pending` 永不进分母。没定判的账不是没中，是还不知道。"""
    days = _seed_days()
    day = days[-2]
    _pick(day, "600000", verdict=ai_picks.VERDICT_HIT)
    _pick(day, "000002", verdict=ai_picks.VERDICT_MISS, name="万科A", report_id=2)
    _pick(day, "600001", verdict=ai_picks.VERDICT_PENDING, name="某只观察", report_id=3)
    _pick(day, "600003", verdict=ai_picks.VERDICT_PENDING, name="另一只", report_id=4)

    with session_scope() as session:
        document = ai_memory_service.build_failure_document(session, day)
    summary = document["summary"]
    assert summary["picks"] == 4
    assert (summary["hit"], summary["miss"], summary["pending"]) == (1, 1, 2)
    assert summary["settled"] == 2
    assert summary["hit_rate_percent"] == 50.0
    #: 失败清单里只有那一条 miss，而且带着它当时说的话与实际结果
    assert [item["code"] for item in document["failure_cases"]] == ["000002"]
    case = document["failure_cases"][0]
    assert case["reason"] == "它说：明天会续板"
    assert case["at_recommend_time"]["composite_score"] == 72.0
    assert case["at_recommend_time"]["max_similarity"] == 91.5
    assert case["actual"]["is_limit_up"] is False
    #: 判据日的前收与涨停价账本没存列，是渲染时从行情表现读的那两根原始数
    assert case["actual"]["judge_date"] == day.isoformat()
    assert case["actual"]["pre_close"] == pytest.approx(9.0, abs=1.0)
    assert case["actual"]["limit_up_price"]

    #: 全是 pending 的那天：分母为 0，命中率是 None 而不是 0%
    today = days[-1]
    _pick(today, "600009", verdict=ai_picks.VERDICT_PENDING, name="只待验证")
    with session_scope() as session:
        all_pending = ai_memory_service.build_failure_document(session, today)["summary"]
    assert all_pending["picks"] == 1 and all_pending["settled"] == 0
    assert all_pending["hit_rate_percent"] is None


def test_export_writes_both_files_and_never_touches_other_days(client, tmp_path):
    """一天一个文件、按天攒下去：写新的一天不许动老日子——那里住着的是长期样本库。"""
    days = _seed_days()
    root = tmp_path / "lp-memory"
    root.mkdir()
    #: 预先放一份「不属于本轮」的老文件：它必须活过这一次导出
    stale = root / "model_memory" / "2020-01-01.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"keep": true}', encoding="utf-8")

    with session_scope() as session:
        first = ai_memory_service.export_day(session, days[-2], root=root)
        second = ai_memory_service.export_day(session, days[-1], root=root)

    assert first["error"] == "" and first["memory_samples"] == 1
    assert second["memory_samples"] == 2
    assert Path(first["memory_file"]).is_file() and Path(first["failure_file"]).is_file()
    assert stale.is_file(), "导出把别的日子删掉了"
    assert json.loads(stale.read_text(encoding="utf-8")) == {"keep": True}

    listing = ai_memory_service.list_exported_days(root)
    assert [item["date"] for item in listing["model_memory"]["days"]] == ["2020-01-01", days[-2].isoformat(), days[-1].isoformat()]
    assert listing["failure_logs"]["day_count"] == 2
    assert listing["model_memory"]["total_bytes"] > 0

    #: 幂等：同一导第二次只是重写那一份文件，条数不多、文件不多
    before = listing["model_memory"]["day_count"]
    with session_scope() as session:
        again = ai_memory_service.export_day(session, days[-1], root=root)
    after = ai_memory_service.list_exported_days(root)
    assert again["memory_samples"] == second["memory_samples"]
    assert after["model_memory"]["day_count"] == before


def test_snapshot_files_carry_no_credentials(client, tmp_path):
    """两份 JSON 里不许出现任何一把密钥：它们会被拿去喂别的模型。"""
    days = _seed_days()
    _pick(days[-2], "600000", verdict=ai_picks.VERDICT_MISS)
    _created(client, key=KEY_B)
    client.put(
        "/api/settings",
        json={"ai_base_url": "https://a.test/v1", "ai_model": "m", "ai_api_key": KEY_A},
    )
    root = tmp_path / "lp-secret-scan"
    with session_scope() as session:
        ai_memory_service.export_day(session, days[-2], root=root)
    blobs = [path.read_text(encoding="utf-8") for path in Path(root).rglob("*.json")]
    assert blobs, "两份文件一份都没落盘"
    for text in blobs:
        for secret in (KEY_A, KEY_B, PATH_TOKEN):
            assert secret not in text


def test_reconcile_is_idempotent_and_never_rewrites_a_settled_row(client):
    """已定判的那一行不回改：行情不会自己变，多点两次「当场补对账」不该把账改花。"""
    days = _seed_days()
    earlier = days[-3]
    with session_scope() as session:
        ai_picks.record_picks(
            session,
            {
                "trade_date": earlier.isoformat(),
                "model": "test-model",
                "finished_at": "2026-06-10T17:10:00",
                "criteria": "三日形态",
                "picks": [{"stock_code": "600000", "stock_name": "浦发银行", "score": 80, "reason": "会续板"}],
            },
            source="board",
        )
    with session_scope() as session:
        first = ai_picks.reconcile(session)
    assert first["judged"] == 1
    with session_scope() as session:
        second = ai_picks.reconcile(session)
        row = (
            session.query(AiPickResult)
            .filter(AiPickResult.trade_date == earlier, AiPickResult.stock_code == "600000")
            .one()
        )
    assert second["judged"] == 0
    assert row.verdict in {ai_picks.VERDICT_HIT, ai_picks.VERDICT_MISS}
    assert row.checked_at  # 第一次定判留下的那一刻，第二次没动它


def test_run_walks_all_three_legs_and_reports_each_failure(client, monkeypatch, tmp_path):
    """手动 run 与自动那一拍走同一个函数：对账 → 导当天 → 回填上一交易日，一段都不许少。"""
    days = _seed_days()
    root = tmp_path / "lp-run"
    called: list[str] = []
    original = ai_memory_service.export_day

    def flaky(session, trade_date, *, root=None):
        called.append(trade_date.isoformat())
        if trade_date == days[-1]:
            return {"trade_date": trade_date.isoformat(), "error": "落盘失败：假装这块盘写不进去", "memory_samples": 0, "failure_cases": 0}
        return original(session, trade_date, root=root)

    monkeypatch.setattr(ai_memory_service, "export_day", flaky)
    with session_scope() as session:
        result = ai_memory_service.run_memory(session, days[-1], root=root)

    assert called == [days[-1].isoformat(), days[-2].isoformat()], "回填上一交易日那一段没跑"
    assert result["reconcile"]["judged"] == 0
    assert any("假装这块盘写不进去" in item for item in result["errors"])
    #: 半截状态要说破但不抛：对账成功、导出失败是一种有信息量的结果
    assert Path(result["export_previous"]["memory_file"]).is_file()


def test_a_leg_that_did_not_run_is_null_not_an_empty_object(client, monkeypatch):
    """三段里任何一段没做成，回包里那一格是 null，页面才知道「这一步根本没跑」。

    空对象会被读成「跑了，0 条」——那是一句关于事实的假话，比红字更坏。
    """
    only = _seed_days(count=1)
    with session_scope() as session:
        result = ai_memory_service.run_memory(session, only[0])
    assert result["export_previous"] is None, "库里没有更早的行情日，回填那一段该是 null"
    assert result["export_today"] is not None and result["export_today"]["error"] == ""

    def boom(session):  # noqa: ANN001 - 假装对账这一步炸了
        raise RuntimeError("假装账本读不出来")

    monkeypatch.setattr(ai_picks, "reconcile", boom)
    with session_scope() as session:
        broken = ai_memory_service.run_memory(session, only[0])
    assert broken["reconcile"] is None
    assert any("假装账本读不出来" in item for item in broken["errors"])
    #: 对账炸了不该带崩落盘：那一段照旧做成（这一天两只涨停 → 两条样本）
    assert broken["export_today"] is not None and broken["export_today"]["memory_samples"] == 2


def test_run_endpoint_reports_errors_without_a_500(client, monkeypatch):
    """`POST /api/ai-memory/run` 把 `errors` 原样带出来，页面必须看得见，不许只在控制台留一行日志。"""
    _seed_days()
    monkeypatch.setattr(
        ai_memory_service,
        "export_day",
        lambda session, trade_date, *, root=None: {
            "trade_date": trade_date.isoformat(),
            "error": "渲染失败：故意",
            "memory_file": "",
            "memory_bytes": 0,
            "memory_samples": 0,
            "failure_file": "",
            "failure_bytes": 0,
            "failure_cases": 0,
            "picks": 0,
        },
    )
    response = client.post("/api/ai-memory/run")
    assert response.status_code == 200
    body = response.json()
    assert body["errors"]
    assert body["export"]["model_memory"]["day_count"] == 0

    #: 对比：只导出那一条接口把失败包成 500——「已生成」而目录里什么都没有，比一个红字难查得多
    failed = client.post("/api/ai-memory/export")
    assert failed.status_code == 500
    assert "故意" in failed.json()["detail"]


def test_no_quotes_means_no_run_not_a_zero(client):
    """空库上「跑一遍」是 404 加一句「先同步」，不是回一串看着像结果的零值。"""
    empty = client.post("/api/ai-memory/run")
    assert empty.status_code == 404
    assert "先同步" in empty.json()["detail"]
    exported = client.post("/api/ai-memory/export")
    assert exported.status_code == 404
    summary = client.get("/api/ai-memory/summary").json()
    assert summary["check"]["check"]["total"] == 0
    assert summary["check"]["check"]["hit_rate_percent"] is None
    assert summary["export"]["model_memory"]["day_count"] == 0
    assert summary["field_gaps"]


def test_memory_step_is_part_of_the_daily_sync(client, monkeypatch, tmp_path):
    """每日那一拍真的把对账与快照跑了：同步消息里要写得出落盘条数，目录里要真有文件。

    这一步挂在同步之后（判据读的是判据日那天的 `is_limit_up`），不是另一个开关、
    也不是第二份排期。
    """
    from backend.app.services import sync_service

    days = weekdays(4)

    def kline(code, start, end):
        return _series(days, limit_on={3}, start=12)

    monkeypatch.setattr(ai_memory_service, "memory_dir", lambda root=None: tmp_path / "mm")
    monkeypatch.setattr(ai_memory_service, "failure_dir", lambda root=None: tmp_path / "fl")
    monkeypatch.setattr(ai_memory_service, "memory_file", lambda day, root=None: tmp_path / "mm" / f"{day.isoformat()}.json")
    monkeypatch.setattr(ai_memory_service, "failure_file", lambda day, root=None: tmp_path / "fl" / f"{day.isoformat()}.json")
    sync_service.set_fetcher(lambda: [("600000", "浦发银行")], kline)
    result = sync_service.run_sync_now()

    assert result["progress"] == 100
    assert "已对账并落" in result["message"], result["message"]
    assert (tmp_path / "mm" / f"{days[-1].isoformat()}.json").is_file()
    assert (tmp_path / "fl" / f"{days[-1].isoformat()}.json").is_file()
    #: 上一交易日那份也要回填（只导当天，样本库会永远停在一半）
    assert (tmp_path / "mm" / f"{days[-2].isoformat()}.json").is_file()

    days_read = client.get("/api/ai-memory/days").json()
    assert days_read["model_memory"]["day_count"] == 2
    sync_service.set_fetcher()


def test_the_whole_tier_knocks_no_gateway(client, monkeypatch, tmp_path):
    """这一档一次网关都不敲：把 `httpx.Client` 换成会记账的假类，跑完读它记到的数。

    判「某条路完全不走」要挂一个**会答话**的假服务数请求——静默的 mock 只能证明
    我没敲到真网关，证不了这一条路上根本没有敲门动作。
    """
    import httpx

    hits: list[str] = []

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            hits.append("constructed")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            hits.append("stream")
            raise AssertionError("这一档不该敲网关")

    days = _seed_days()
    _pick(days[-2], "600000", verdict=ai_picks.VERDICT_MISS)
    monkeypatch.setattr(httpx, "Client", RecordingClient)
    monkeypatch.setattr(ai_memory_service, "memory_file", lambda day, root=None: tmp_path / "mm" / f"{day.isoformat()}.json")
    monkeypatch.setattr(ai_memory_service, "failure_file", lambda day, root=None: tmp_path / "fl" / f"{day.isoformat()}.json")

    assert client.post("/api/ai/picks/check").status_code == 200
    assert client.post("/api/ai-memory/run").status_code == 200
    assert client.get("/api/ai-memory/summary").status_code == 200
    assert client.put(f"/api/ai-sources/{ai_sources.SETTINGS_SOURCE_ID}/active").status_code == 200
    assert hits == []
    assert (tmp_path / "mm" / f"{days[-1].isoformat()}.json").is_file()
    assert (tmp_path / "fl" / f"{days[-1].isoformat()}.json").is_file()


def test_ai_sources_document_shape_is_plain_json(client):
    """清单落的是一个普通 JSON 文档，不是数据库表：里面有版本、当前指向、谁改的、什么时候改的。"""
    row = _created(client)["row"]
    document = json.loads(Path(ai_sources.sources_path()).read_text(encoding="utf-8"))
    assert document["version"] == ai_sources.DOCUMENT_VERSION
    assert document["active_id"] == ai_sources.SETTINGS_SOURCE_ID
    assert document["changed_by"] == "settings 页新增 AI 源"
    assert document["updated_at"]
    assert [item["id"] for item in document["sources"]] == [row["id"]]

    listing = client.get("/api/ai-sources").json()
    assert listing["file_path"] == str(ai_sources.sources_path())
    assert listing["max_sources"] == AI_SOURCE_MAX
