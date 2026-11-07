"""Council-only source registry, hot-update security and independent adapter contracts."""
from copy import deepcopy
from datetime import date
import json
import socket

import httpx
import pytest

_REAL_CLIENT = httpx.Client

from backend.app.services.council import source_registry as mod, public_sources as public, web_evidence as web


def raw(payload):return json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()


@pytest.fixture
def registry(tmp_path):return mod.SourceRegistry(tmp_path/'sources')


def next_manifest(registry):
    p=deepcopy(registry.active);p['version']='1.0.1';p['sources'][0]['priority']=99
    return mod.seal(p)


def mock_update(monkeypatch,payload,status=200):
    original=_REAL_CLIENT
    def handler(request):
        assert str(request.url)==mod.UPDATE_URL
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(status,content=payload,headers={'content-type':'application/json','location':'http://127.0.0.1/steal'})
    monkeypatch.setattr(mod.socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('185.199.108.133',443))])
    monkeypatch.setattr(mod.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


def test_manifest_valid_and_compatibility(registry):
    p=mod.validate(mod.BUILTIN.read_bytes());assert len(p['sources'])==28
    assert {m['provider'] for m in mod.catalog().values()}=={'eastmoney','sina','tencent','ths','cninfo','sse','szse'}
    with pytest.raises(ValueError,match='较新软件'):mod.validate(mod.BUILTIN.read_bytes(),app_version='0.7.13')


@pytest.mark.parametrize('field,value', [('repository','https://github.com/evil/repo'),('schema_version',2),('version','x'),('content_sha256','0'*64)])
def test_bad_manifest_header(registry,field,value):
    p=next_manifest(registry);p[field]=value
    if field!='content_sha256':p=mod.seal(p)
    with pytest.raises(ValueError):mod.validate(raw(p))


@pytest.mark.parametrize('patch', [ {'id':'execute_python'}, {'endpoint':'http://127.0.0.1/'}, {'endpoint':'https://example.com/'},
 {'url':'https://raw.githubusercontent.com/evil/file.py'}, {'api_key':'secret'}, {'enabled':1}, {'priority':False},
 {'priority':0}, {'timeout_seconds':600}, {'cache_ttl':0}, {'min_interval':0}])
def test_reject_untrusted_parameters(registry,patch):
    p=next_manifest(registry);p['sources'][0].update(patch);p=mod.seal(p)
    with pytest.raises(ValueError):mod.validate(raw(p))


def test_duplicate_json_large_nan_and_duplicate_source(registry):
    for data in [b'{"x":1,"x":2}',b' '*65537,b'{"x":NaN}']:
        with pytest.raises(ValueError):mod.validate(data)
    p=next_manifest(registry);p['sources'].append(p['sources'][0]);p=mod.seal(p)
    with pytest.raises(ValueError):mod.validate(raw(p))


@pytest.mark.parametrize('url', ['http://www.cninfo.com.cn/a','https://127.0.0.1','https://[::1]/','https://169.254.169.254/latest/meta-data',
 'https://localhost/','https://user:pass@www.cninfo.com.cn/a','https://www.cninfo.com.cn.evil.com/a','https://www.cninfo.com.cn:8000/a','file:///etc/passwd'])
def test_ssrf_urls_rejected(url):
    with pytest.raises(ValueError):mod.safe_url(url)


def test_update_apply_rollback_restart_and_preferences(registry,monkeypatch):
    registry.configure('sina_kline',{'enabled':False,'priority':3})
    candidate=next_manifest(registry);mock_update(monkeypatch,raw(candidate))
    assert registry.check_update()['candidate_version']=='1.0.1'
    assert registry.view()['version']=='1.0.0'  # Checking never applies silently.
    assert registry.apply_update()['version']=='1.0.1'
    assert mod.SourceRegistry(registry.directory).view()['version']=='1.0.1'
    assert registry.rollback()['version']=='1.0.0'
    restored=mod.SourceRegistry(registry.directory)
    row=next(r for r in restored.view()['items'] if r['id']=='sina_kline')
    assert not row['enabled'] and row['priority']==3
    assert restored.view()['version']=='1.0.0'


@pytest.mark.parametrize('status', [302,403,500])
def test_update_http_refusal_keeps_active(registry,monkeypatch,status):
    mock_update(monkeypatch,b'{}',status)
    with pytest.raises(ValueError):registry.check_update()
    assert registry.view()['version']=='1.0.0' and registry.previous is None


def test_update_private_dns_and_size_and_hash(registry,monkeypatch):
    mock_update(monkeypatch,raw(next_manifest(registry)))
    monkeypatch.setattr(mod.socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError,match='非公网'):registry.check_update()
    mock_update(monkeypatch,b'x'*65537)
    with pytest.raises(ValueError,match='64KiB'):registry.check_update()
    p=next_manifest(registry);p['content_sha256']='0'*64;mock_update(monkeypatch,raw(p))
    with pytest.raises(ValueError,match='SHA256'):registry.check_update()
    assert registry.active==registry.builtin


def test_same_version_mutation_downgrade_and_expired_candidate(registry,monkeypatch):
    p=next_manifest(registry);p['version']='1.0.0';p=mod.seal(p);mock_update(monkeypatch,raw(p))
    with pytest.raises(ValueError,match='同版本'):registry.check_update()
    p['version']='0.9.0';p=mod.seal(p);mock_update(monkeypatch,raw(p))
    assert registry.check_update()['candidate_version'] is None
    registry.candidate=(next_manifest(registry),mod.time.monotonic()-601)
    with pytest.raises(ValueError,match='过期'):registry.apply_update()


def test_atomic_disk_failure_keeps_previous(registry,monkeypatch):
    registry.candidate=(next_manifest(registry),mod.time.monotonic())
    def fail(*a):raise OSError('read only')
    monkeypatch.setattr(mod,'_atomic',fail)
    with pytest.raises(OSError):registry.apply_update()
    assert registry.active==registry.builtin and registry.previous is None
    with pytest.raises(OSError):registry.configure('sina_kline',{'enabled':False})
    assert registry.preferences=={}


def test_corrupt_local_config_uses_builtin(tmp_path):
    (tmp_path/'state.json').write_text('not json')
    r=mod.SourceRegistry(tmp_path)
    assert r.view()['version']=='1.0.0' and '内置' in r.load_error


def fetch_row(registry):
    row=deepcopy(registry.active['sources'][0]);row['min_interval']=0
    return row


def test_cache_expiration_key_and_provenance(registry,monkeypatch):
    row=fetch_row(registry);calls=[];clock=[1000.0]
    monkeypatch.setattr(mod.time,'time',lambda:clock[0])
    def fn(code,end):calls.append(code);return {'status':'ok','source':row['id'],'digest':{'price':10},'fetched_at':'2026-11-06T07:00:00Z'}
    meta={'fn':fn,'provider':'eastmoney','category':'market'}
    first=registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    second=registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    assert first['fetched_at']==second['fetched_at'] and second['cache']['hit'] and len(calls)==1
    assert second['registry']['version']=='1.0.0'
    registry.fetch(row,meta,'000001',date(2026,11,6),'1.0.0');assert len(calls)==2
    clock[0]+=301;registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0');assert len(calls)==3


def test_circuit_breaker_and_empty_not_failure(registry):
    row=fetch_row(registry);row['cache_ttl']=0;calls=[]
    def fn(*a):calls.append(1);return {'status':'failed','source':row['id'],'digest':{'reason':'timeout'}}
    meta={'fn':fn,'provider':'eastmoney','category':'market'}
    for _ in range(3):registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    result=registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    assert result['status']=='unavailable' and len(calls)==3
    registry.health.clear();meta['fn']=lambda *a:{'status':'empty','source':row['id'],'digest':{'note':'valid empty'}}
    for _ in range(4):registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    assert registry.health[row['id']]['consecutive_failures']==0


def test_disable_priority_api_and_no_code_fields(client):
    response=client.get('/api/council/sources');assert response.status_code==200,response.text
    assert len(response.json()['items'])==28
    assert client.put('/api/council/sources/sina_kline',json={'enabled':False,'priority':1}).status_code==200
    assert client.put('/api/council/sources/sina_kline',json={'url':'http://127.0.0.1'}).status_code==422
    assert client.put('/api/council/sources/sina_kline',json={'priority':True}).status_code==422
    assert client.post('/api/council/sources/apply-update').status_code==422


def test_new_parsers_offline(monkeypatch):
    original=httpx.Client
    def handler(request):
        url=str(request.url)
        if '10jqka' in url:return httpx.Response(200,text='quotebridge_v6_line_hs_000001_00_last({"data":"20261106,11.62,11.85,11.57,11.81,164027640,1927307600,0.845"})')
        if 'sina' in url:return httpx.Response(200,json=[{'day':'2026-11-06','open':'2.15','high':'2.15','low':'2.13','close':'2.14','volume':'54463938'},{'day':'2026-11-09','close':99}])
        if 'szse_stock' in url:return httpx.Response(200,json={'stockList':[{'code':'000001','orgId':'gssz0000001'}]})
        if 'cninfo' in url:return httpx.Response(200,json={'totalAnnouncement':1,'announcements':[{'secCode':'000001','announcementTime':1792771200000,'announcementTitle':'第三季度报告','announcementId':'123'}]})
        if 'szse.cn' in url:return httpx.Response(200,json={'announceCount':1,'data':[{'secCode':['000001'],'publishTime':'2026-10-24','title':'公告','attachPath':'/disc/test.PDF'}]})
        raise AssertionError(url)
    monkeypatch.setattr(web,'_client',lambda:original(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(public,'_STOCKS',({},0.0))
    assert public.fetch_sina_kline('600759',date(2026,11,6))['digest']['count']==1
    ths=public.fetch_ths_kline('000001',date(2026,11,6))
    assert ths['digest']['adjustment']=='none' and ths['digest']['bars'][0]['volume']==164027640
    assert public.fetch_cninfo_announcements('000001',date(2026,11,6))['digest']['count']==1
    assert public.fetch_cninfo_reports('000001',date(2026,11,6))['window']['kind']=='latest_disclosed_report'
    assert public.fetch_szse_announcements('000001',date(2026,11,6))['digest']['count']==1
    assert public.fetch_szse_announcements('600759',date(2026,11,6))['status']=='not_applicable'
    assert public.fetch_sse_announcements('000001',date(2026,11,6))['status']=='not_applicable'


def test_jsonp_not_executable_and_404_unavailable(monkeypatch):
    original=httpx.Client
    monkeypatch.setattr(web,'_client',lambda:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='alert("execute")'))))
    assert public.fetch_ths_kline('000001',date(2026,11,6))['status']=='failed'
    monkeypatch.setattr(web,'_client',lambda:original(transport=httpx.MockTransport(lambda r:httpx.Response(404))))
    assert public.fetch_ths_kline('600759',date(2026,11,6))['status']=='unavailable'


def test_update_total_timeout_and_old_subset_compatibility(registry,monkeypatch):
    from backend.app.services.council import service
    def timeout(call,seconds):
        assert seconds==10
        raise TimeoutError('total deadline')
    monkeypatch.setattr(service,'_bounded_call',timeout)
    registry.candidate=(next_manifest(registry),mod.time.monotonic())
    with pytest.raises(TimeoutError):registry.check_update()
    assert registry.candidate is None and registry.active==registry.builtin
    p=deepcopy(registry.active);p['version']='0.9.0';p['min_app_version']='0.7.13';p['sources']=p['sources'][:2]
    assert len(mod.validate(raw(mod.seal(p)))['sources'])==2


def test_rate_limit_busy_fallback_and_cache_size(registry,monkeypatch):
    import threading
    row=fetch_row(registry);row['min_interval']=2
    sleeps=[]
    monkeypatch.setattr(mod.time,'monotonic',lambda:100.0)
    monkeypatch.setattr(mod.time,'sleep',lambda seconds:sleeps.append(seconds))
    meta={'fn':lambda *args:{'status':'ok','source':row['id'],'digest':{}},'provider':'eastmoney','category':'market'}
    registry.fetch(row,meta,'600759',date(2026,11,6),'1.0.0')
    registry.fetch(row,meta,'000001',date(2026,11,6),'1.0.0')
    assert sleeps==[2.0]
    guard=registry.guards[row['id']];guard.acquire()
    try:assert registry.fetch(row,meta,'600519',date(2026,11,6),'1.0.0')['status']=='unavailable'
    finally:guard.release()
    row['min_interval']=0
    for i in range(260):registry.fetch(row,meta,f'{i:06d}',date(2026,11,6),'1.0.0')
    assert len(registry.cache)==256


def test_sse_ownership_must_match_requested_stock(monkeypatch):
    original=_REAL_CLIENT
    rows=[{'SECURITY_CODE':'600519','SSEDATE':'2026-11-06','TITLE':'另一只股票公告','URL':'/disclosure/other.pdf'}]
    monkeypatch.setattr(web,'_client',lambda:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'result':rows}))))
    assert public.fetch_sse_announcements('600759',date(2026,11,6))['status']=='unverified'
    rows[0]['SECURITY_CODE']='600759'
    assert public.fetch_sse_announcements('600759',date(2026,11,6))['digest']['count']==1


def test_trusted_manifest_live_on_public_ci(tmp_path, capsys):
    """Real product transport on the normal CI network; never relax private-DNS guards."""
    import os
    if not (os.getenv('GITHUB_ACTIONS') == 'true' and os.getenv('GITHUB_EVENT_NAME') == 'push'
            and os.getenv('GITHUB_REPOSITORY') == 'peekaboo789/limit-pattern'):
        pytest.skip('real public-manifest acceptance runs only on this repository push CI')
    r=mod.SourceRegistry(tmp_path/'live-manifest')
    first=r.check_update()
    payload=r.candidate[0] if r.candidate else r.active
    remote_version=payload['version'];remote_sha=payload['content_sha256']
    # Isolated simulated older local state; do not claim it was a released version.
    r.active=mod.seal({**r.active,'version':'0.0.0'})
    assert r.check_update()['candidate_version']==remote_version
    assert r.apply_update()['version']==remote_version
    assert r.rollback()['version']=='0.0.0'
    assert mod.SourceRegistry(r.directory).view()['version']=='0.0.0'
    with capsys.disabled():
        print(f'REAL_PUBLIC_MANIFEST_ACCEPTANCE version={remote_version} sha256={remote_sha}; actual HTTPS/schema/apply/rollback/restart passed on CI; older state simulated')
