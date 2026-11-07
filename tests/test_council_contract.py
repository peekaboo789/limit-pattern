"""Council unit/data contracts. Offline; no paid model or live-source calls."""
from datetime import date, timedelta
import json
import threading

import httpx
import pytest

from backend.app.core.database import session_scope
from backend.app.models.tables import AiCouncilSession
from backend.app.services.council import service, web_evidence as web
from backend.app.services.council.probability import normalize_probability, assess_evidence_quality, apply_evidence_gate


def strong_evidence():
    end = date(2026, 11, 6)
    bars = [{"date": (end-timedelta(days=i)).isoformat(), "open": 10, "high": 11,
             "low": 9, "close": 10, "volume": 100} for i in reversed(range(30))]
    return {"local": {"as_of": end.isoformat()}, "kline": {"merged_bars": bars}, "web": [
        {"status": "ok", "category": "market", "url": "https://push2his.eastmoney.com/api", "digest": {"bars": bars}},
        {"status": "ok", "category": "market", "url": "https://hq.sinajs.cn/list=sh600759", "digest": {"actual_date": end.isoformat(), "price": 10}},
        {"status": "ok", "category": "financial", "url": "https://quotes.sina.cn/report", "digest": {"latest": {
            "report_date": "2026-09-30", "publish_date": "2026-10-31", "revenue": 123}}},
    ]}


@pytest.mark.parametrize('value,unit,expected', [(0.4,'percent',0.4),(0.4,'fraction',40),(0,'percent',0),(100,'percent',100),(1,'fraction',100)])
def test_explicit_unit_only(value, unit, expected):
    result = normalize_probability({'up_probability': value, 'probability_unit': unit})
    assert result['up_probability'] == expected
    assert result['probability_audit']['raw_value'] == value
    assert result['probability_audit']['raw_unit'] == unit
    assert result['probability_unit'] == 'percent'
    assert '未经统计校准' in result['probability_notice']


@pytest.mark.parametrize('value', [.3,.4,40,None])
def test_missing_unit_never_inferred(value):
    result = normalize_probability({'up_probability': value})
    assert result['up_probability'] is None
    assert result['probability_status'] == 'ambiguous_unit'


@pytest.mark.parametrize('value,unit', [(True,'percent'),('0.4','fraction'),(float('nan'),'percent'),
    (float('inf'),'percent'),(-1,'percent'),(101,'percent'),(1.01,'fraction'),(10**400,'percent')])
def test_invalid_never_clamped_or_serialized_nonfinite(value, unit):
    result = normalize_probability({'up_probability': value, 'probability_unit': unit})
    assert result['up_probability'] is None
    assert result['probability_status'] == 'invalid'
    json.dumps(result, allow_nan=False)


def test_gate_and_duplicate_reason_and_financial_metrics():
    conclusion = normalize_probability({'up_probability': .4, 'probability_unit': 'fraction'})
    strong = strong_evidence()
    assert assess_evidence_quality(strong)['eligible_for_probability']
    assert apply_evidence_gate(conclusion, strong)['up_probability'] == 40
    weak = {'local': {'as_of': '2026-11-06'}}
    suppressed = apply_evidence_gate(conclusion, weak)
    assert suppressed['up_probability'] is None and suppressed['confidence'] == 'low'
    assert suppressed['probability_audit']['normalized_candidate_percent'] == 40
    assert apply_evidence_gate(suppressed, weak)['probability_reason'] == suppressed['probability_reason']
    strong['web'][-1]['digest']['latest'].pop('revenue')
    assert not assess_evidence_quality(strong)['eligible_for_probability']


def test_cross_site_not_two_endpoints_same_site_and_disclosure_required():
    evidence = strong_evidence()
    evidence['web'][1]['url'] = 'https://push2.eastmoney.com/api'
    assert assess_evidence_quality(evidence)['price_crosscheck'] == 'unverified'
    evidence = strong_evidence()
    evidence['web'][-1]['digest']['latest']['publish_date'] = '2026-11-20'
    assert not assess_evidence_quality(evidence)['latest_disclosed_financial']
    evidence = strong_evidence()
    evidence['web'][1]['digest']['price'] = 11
    assert assess_evidence_quality(evidence)['price_crosscheck'] == 'conflict'


def test_historical_unitless_probability_not_rewritten(client):
    with session_scope() as db:
        row = AiCouncilSession(stock_code='600759', stock_name='ST洲际', eval_date=date(2026,11,6),
            status='done', participants=[], statements=[], evidence=strong_evidence(),
            conclusion={'up_probability': .3, 'confidence': 'low', 'summary': 'legacy'})
        db.add(row); db.commit(); sid = row.id
    result = client.get(f'/api/council/{sid}')
    assert result.status_code == 200, result.text
    conclusion = result.json()['conclusion']
    assert conclusion['up_probability'] is None
    assert conclusion['probability_status'] == 'ambiguous_unit'
    assert conclusion['probability_audit']['raw_value'] == .3
    with session_scope() as db:
        assert db.get(AiCouncilSession, sid).conclusion['up_probability'] == .3


def mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(web, '_client', lambda: original(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize('code,message,status', [(9501,'DATE排序列不存在','failed'),(9201,'返回数据为空','empty'),(9701,'繁忙','failed')])
def test_datacenter_business_errors_not_empty(monkeypatch, code, message, status):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.params['sortColumns'] == 'TRADE_DATE'
        assert '2026-10-08' in request.url.params['filter']
        return httpx.Response(200, json={'success': False, 'code': code, 'message': message, 'result': None})
    mock_client(monkeypatch, handler)
    result = web.fetch_lhb('600759', date(2026,11,6))
    assert result['status'] == status
    assert len(requests) == (2 if code == 9701 else 1)
    assert result['http_attempts'][0]['status_code'] == 200
    assert 'json' in result['http_attempts'][0]['content_type']
    if status == 'empty':
        assert '不代表数值为0' in result['digest']['note']
    else:
        assert str(code) in result['digest']['reason']


@pytest.mark.parametrize('status,expected', [(403,1),(401,1),(503,2)])
def test_bounded_retry_no_auth_bypass(monkeypatch, status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='upstream error')
    mock_client(monkeypatch, handler)
    assert web.fetch_financial_main('600759', date(2026,11,6))['status'] == 'failed'
    assert len(calls) == expected


def test_non_json_financial_is_fault_not_empty(monkeypatch):
    mock_client(monkeypatch, lambda req: httpx.Response(200, text='<html>restricted</html>', headers={'content-type':'text/html'}))
    result = web.fetch_financial_main('600759', date(2026,11,6))
    assert result['status'] == 'failed'
    assert '非JSON' in result['digest']['reason']
    assert result['http_attempts'][0]['content_type'] == 'text/html'


def test_financial_older_than_window_and_missing_disclosure(monkeypatch):
    rows = [
        {'REPORT_DATE':'2026-09-30','NOTICE_DATE':'2026-11-20','TOTAL_OPERATE_INCOME':999},
        {'REPORT_DATE':'2026-06-30','NOTICE_DATE':'2026-08-25','TOTAL_OPERATE_INCOME':123},
    ]
    def handler(request):
        assert 'NOTICE_DATE' in request.url.params['filter']
        assert '2026-10-08' not in request.url.params['filter']
        return httpx.Response(200, json={'success':True,'result':{'data':rows}})
    mock_client(monkeypatch, handler)
    result = web.fetch_financial_main('600759', date(2026,11,6))
    assert result['digest']['latest']['revenue'] == 123
    assert result['window']['kind'] == 'latest_disclosed_report'
    assert result['digest']['latest']['publish_date'] == '2026-08-25'
    rows[:] = [{'REPORT_DATE':'2026-09-30','TOTAL_OPERATE_INCOME':123}]
    assert web.fetch_financial_main('600759', date(2026,11,6))['status'] == 'unverified'


def test_tencent_unadjusted_independent_prices_and_no_future(monkeypatch):
    def handler(request):
        assert request.url.params['param'] == 'sh600759,day,,2026-11-06,60,'
        return httpx.Response(200, json={'code':0, 'data':{'sh600759':{'day':[
            ['2026-11-06','2.15','2.14','2.15','2.13','544639.38'],
            ['2026-11-09','99','99','99','99','999'],
        ]}}})
    mock_client(monkeypatch, handler)
    result = web.fetch_tencent_kline('600759', date(2026,11,6))
    assert result['status'] == 'ok'
    assert len(result['digest']['bars']) == 1
    bar = result['digest']['bars'][0]
    assert bar['volume'] == pytest.approx(54463938)
    assert bar['amount'] is None and bar['close'] == 2.14


def test_event_no_future_outcome_leakage(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={'success':True,'result':{'data':[
        {'TRADE_DATE':'2026-11-06','CLOSE_PRICE':2.14,'D1_CLOSE_ADJCHRATE':123,'D5_CLOSE_ADJCHRATE':999}]}}))
    result = web.fetch_lhb('600759', date(2026,11,6))
    assert result['status'] == 'ok'
    assert 'D1_' not in json.dumps(result['digest'])
    assert 'D5_' not in json.dumps(result['digest'])


def test_deadline_keeps_completed_sources(monkeypatch, tmp_path):
    from backend.app.services.council import source_registry
    release = threading.Event()
    registry = source_registry.SourceRegistry(tmp_path)
    registry.active['sources'] = [row for row in registry.active['sources'] if row['id'] in {'eastmoney_spot','sina_spot'}]
    monkeypatch.setattr(source_registry, 'get_registry', lambda:registry)
    monkeypatch.setattr(web, 'fetch_spot', lambda *args: {'status':'ok','source':'finished','digest':{}})
    def slow(*args):
        release.wait(.3)
        return {'status':'ok','source':'slow','digest':{}}
    monkeypatch.setattr(web, 'fetch_sina_spot', slow)
    def fake_wait(futures, timeout):
        assert timeout == 32
        first = next(iter(futures))
        first.result(timeout=1)
        return {first}, set(futures)-{first}
    monkeypatch.setattr(web, 'wait', fake_wait)
    try:
        result = web.collect('600759', date(2026,11,6))
        assert result[0]['source'] == 'finished'
        assert result[1]['status'] == 'unavailable'
        assert '32秒' in result[1]['digest']['note']
    finally:
        release.set()


def test_null_quote_data_and_network_retry_are_not_valid_empty(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("server disconnected")
        return httpx.Response(200, json={"rc": 102, "data": None})
    mock_client(monkeypatch, handler)
    result = web.fetch_capital_flow('600759', date(2026,11,6))
    assert len(calls) == 2
    assert result['status'] == 'failed'
    assert '业务错误' in result['digest']['reason']
    assert 'RemoteProtocolError' in result['http_attempts'][0]['error']


def test_local_price_conflict_and_tencent_fallback_provenance():
    evidence = strong_evidence()
    evidence['kline']['merged_bars'][-1]['close'] = 9
    assert assess_evidence_quality(evidence)['price_crosscheck'] == 'conflict'
    bars = strong_evidence()['kline']['merged_bars']
    web_sources = [
        {'source':'eastmoney_kline','status':'failed'},
        {'source':'tencent_kline','status':'ok','digest':{'bars':bars}},
    ]
    result = service.finalize_evidence({'local':{'as_of':'2026-11-06','bars':[]}}, web_sources, date(2026,11,6))
    assert result['kline']['public_source'] == 'tencent_kline'
    assert result['kline']['public_count'] == 30
