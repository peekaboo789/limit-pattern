"""Versioned, data-only council source registry. No plugins, arbitrary URLs or executable updates."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx

UPDATE_URL = 'https://raw.githubusercontent.com/peekaboo789/limit-pattern/cursor/limit-pattern-system-8abb/backend/app/services/council/data_sources.json'
REPOSITORY = 'https://github.com/peekaboo789/limit-pattern'
MAX_MANIFEST_BYTES = 65536
BUILTIN = Path(__file__).with_name('data_sources.json')
ALLOWED_HOSTS = frozenset({'push2.eastmoney.com','push2his.eastmoney.com','datacenter-web.eastmoney.com',
    'search-api-web.eastmoney.com','np-anotice-stock.eastmoney.com','reportapi.eastmoney.com',
    'hq.sinajs.cn','quotes.sina.cn','vip.stock.finance.sina.com.cn','web.ifzq.gtimg.cn',
    'd.10jqka.com.cn','www.cninfo.com.cn','query.sse.com.cn','www.szse.cn'})

# Tuple metadata is code-reviewed, never supplied by an online manifest.
OLD = [('spot','market'),('sina_spot','market'),('kline','market'),('tencent_kline','market'),('intraday','market_intraday'),
       ('capital_flow','capital'),('sina_capital','capital'),('lhb','capital'),('margin','capital'),('northbound','capital'),
       ('block_trade','trade'),('repurchase','trade'),('holder_change','trade'),('unlock','trade'),('news','news'),
       ('announcements','announcement'),('research','research'),('financial_main','financial'),('financial_zy','financial'),
       ('sina_financial','financial'),('financial_balance','financial'),('financial_cashflow','financial')]
ALIASES = {'spot':'eastmoney_spot','kline':'eastmoney_kline','intraday':'eastmoney_trends','capital_flow':'eastmoney_capital_flow',
           'sina_capital':'sina_capital_flow'}
NEW = [('sina_kline','sina','market'),('ths_kline','ths','market'),('cninfo_announcements','cninfo','announcement'),
       ('cninfo_reports','cninfo','financial'),('sse_announcements','sse','announcement'),('szse_announcements','szse','announcement')]
PROVIDERS = {'eastmoney':'东方财富','sina':'新浪财经','tencent':'腾讯证券','ths':'同花顺','cninfo':'巨潮资讯','sse':'上海证券交易所','szse':'深圳证券交易所'}


def catalog():
    from . import web_evidence as web, public_sources
    items={}
    for name,category in OLD:
        sid=ALIASES.get(name, name if name.startswith(('sina_','tencent_')) else 'eastmoney_'+name)
        provider='sina' if name.startswith('sina_') else 'tencent' if name.startswith('tencent_') else 'eastmoney'
        items[sid]={'fn':getattr(web,'fetch_'+name),'provider':provider,'category':category}
    for name,provider,category in NEW:items[name]={'fn':getattr(public_sources,'fetch_'+name),'provider':provider,'category':category}
    return items


def safe_url(url: str, *, updater=False):
    parsed=urlsplit(url)
    allowed={'raw.githubusercontent.com'} if updater else ALLOWED_HOSTS
    if (parsed.scheme!='https' or parsed.hostname not in allowed or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.fragment or '\\' in url or (updater and url!=UPDATE_URL)):
        raise ValueError('仅允许代码审核过的HTTPS域名/固定模板，无凭据、重定向或自定义URL')
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:raise ValueError('禁止本地/私网地址')
    except ValueError:
        # Hostnames are exact static allowlist entries, not untrusted suffixes.
        if parsed.hostname not in allowed:raise
    return url


def _version(text):
    if not isinstance(text,str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}',text):raise ValueError('版本必须是三段数字')
    return tuple(map(int,text.split('.')))


def _canonical(payload):return json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def seal(payload):
    result=deepcopy(payload);result.pop('content_sha256',None)
    result['content_sha256']=hashlib.sha256(_canonical(result)).hexdigest();return result


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('清单禁止重复JSON字段')
        result[key]=value
    return result


def validate(data: bytes, app_version=None):
    from backend.app import __version__
    if len(data)>MAX_MANIFEST_BYTES:raise ValueError('清单超过64KiB')
    p=json.loads(data.decode('utf-8'),object_pairs_hook=_unique,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('禁止非有限数值')))
    keys={'format','schema_version','version','min_app_version','repository','sources','content_sha256'}
    if not isinstance(p,dict) or set(p)!=keys:raise ValueError('清单字段不符合严格schema，禁止URL/代码/凭据字段')
    if p['format']!='limit-pattern-council-sources' or type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('清单schema不兼容，需软件更新')
    if p['repository']!=REPOSITORY:raise ValueError('清单仓库来源不可信')
    _version(p['version'])
    if _version(p['min_app_version'])>_version(app_version or __version__):raise ValueError('来源清单需要较新软件版本，保留当前可用配置')
    expected=seal(p)['content_sha256']
    if not isinstance(p['content_sha256'],str) or p['content_sha256']!=expected:raise ValueError('清单完整性SHA256不匹配')
    if not isinstance(p['sources'],list) or not 1<=len(p['sources'])<=40:raise ValueError('来源数超限')
    known=catalog();seen=set()
    for row in p['sources']:
        if not isinstance(row,dict) or set(row)!={'id','enabled','priority','timeout_seconds','cache_ttl','min_interval','endpoint'}:raise ValueError('来源参数字段不符合schema')
        if not isinstance(row['id'],str) or row['id'] not in known or row['id'] in seen:raise ValueError('未知/重复解析器，必须正常更新软件')
        seen.add(row['id'])
        if type(row['enabled']) is not bool or row['endpoint']!='primary':raise ValueError('仅允许已知固定HTTPS端点模板，不接受自定义地址')
        for key,lo,hi in [('priority',1,100),('timeout_seconds',2,5),('cache_ttl',30,3600),('min_interval',1,5)]:
            if type(row[key]) is not int or not lo<=row[key]<=hi:raise ValueError('来源参数范围错误：'+key)
    return p


def _atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,allow_nan=False);stream.flush()
        import os
        os.fsync(stream.fileno())
    temp.replace(path)


class SourceRegistry:
    def __init__(self, directory: Path):
        self.directory=Path(directory);self.lock=threading.RLock();self.cache=OrderedDict();self.guards={};self.last_start={};self.candidate=None;self.load_error=''
        self.builtin=validate(BUILTIN.read_bytes());self.active=deepcopy(self.builtin);self.previous=None;self.preferences={};self.health={}
        path=self.directory/'state.json'
        if path.exists():
            try:
                if path.stat().st_size>200000:raise ValueError('本地来源状态过大')
                state=json.loads(path.read_text())
                self.active=validate(_canonical(state['active']))
                if state.get('previous'):self.previous=validate(_canonical(state['previous']))
                self.preferences=state.get('preferences',{});self.health=state.get('health',{})
                if not isinstance(self.preferences,dict) or not isinstance(self.health,dict) or any(not isinstance(v,dict) for v in self.health.values()):raise ValueError('状态格式无效')
                for patch in self.preferences.values():self._valid_patch(patch)
                for health in self.health.values():
                    if type(health.get('open_until',0)) not in (int,float) or not 0 <= health.get('open_until',0) < 100000000000 or type(health.get('consecutive_failures',0)) is not int:raise ValueError('健康状态数值无效')
            except Exception as exc:self.load_error=f'本地清单无效，使用内置清单：{exc}';self.active=deepcopy(self.builtin);self.preferences={};self.health={};self.previous=None

    def _save(self):
        _atomic(self.directory/'state.json',{'active':self.active,'previous':self.previous,'preferences':self.preferences,'health':self.health})

    def _config(self):
        return sorted(({**row,**self.preferences.get(row['id'],{})} for row in self.active['sources']),key=lambda row:(row['priority'],row['id']))

    def view(self):
        with self.lock:
            known=catalog();items=[]
            for row in self._config():
                meta=known[row['id']];items.append({**row,'provider':meta['provider'],'provider_name':PROVIDERS[meta['provider']],
                    'category':meta['category'],'health':deepcopy(self.health.get(row['id'],{}))})
            candidate=self.candidate[0] if self.candidate and time.monotonic()-self.candidate[1]<600 else None
            return {'version':self.active['version'],'builtin_version':self.builtin['version'],'schema_version':1,'origin':REPOSITORY,
                'update_url':UPDATE_URL,'content_sha256':self.active['content_sha256'],'rollback_available':self.previous is not None,
                'candidate_version':candidate['version'] if candidate else None,'items':items,'load_error':self.load_error,
                'policy':'仅手动检查；清单只配置已审核解析器的启停/优先级/缓存/超时，不执行代码，不新增任意URL。新解析器/模板须软件更新。',
                'limits':{'workers':4,'total_seconds':32,'failure_threshold':3,'circuit_seconds':60,'cache_entries':256}}

    @staticmethod
    def _valid_patch(patch):
        if not isinstance(patch,dict) or not patch or not set(patch)<={'enabled','priority'}:raise ValueError('仅可调整启用和优先级')
        if 'enabled' in patch and type(patch['enabled']) is not bool:raise ValueError('enabled必须为布尔值')
        if 'priority' in patch and (type(patch['priority']) is not int or not 1<=patch['priority']<=100):raise ValueError('优先级须1–100')

    def configure(self,sid,patch):
        self._valid_patch(patch)
        with self.lock:
            if sid not in {row['id'] for row in self.active['sources']}:raise ValueError('未知来源')
            old=deepcopy(self.preferences)
            self.preferences[sid]={**self.preferences.get(sid,{}),**patch}
            try:self._save()
            except Exception:self.preferences=old;raise
            return self.view()

    def check_update(self):
        safe_url(UPDATE_URL,updater=True)
        with self.lock:self.candidate=None
        def download():
            deadline=time.monotonic()+8
            # Reject host-file/DNS mappings to private/local networks. Fixed host + TLS,
            # no redirects, no cookies/auth or model/user data ever enter this request.
            addresses=socket.getaddrinfo('raw.githubusercontent.com',443,type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):raise ValueError('清单DNS解析到非公网地址，拒绝访问')
            data=bytearray()
            with httpx.Client(timeout=httpx.Timeout(6,connect=3),follow_redirects=False,trust_env=False) as client:
                with client.stream('GET',UPDATE_URL,headers={'Accept':'application/json','User-Agent':'LimitPattern-Council-Manifest/1'}) as response:
                    if response.status_code!=200:raise ValueError(f'清单HTTP {response.status_code}，保留内置/当前版本（不跟随重定向）')
                    for chunk in response.iter_bytes():
                        if time.monotonic()>deadline:raise TimeoutError('清单下载总预算到期')
                        data.extend(chunk)
                        if len(data)>MAX_MANIFEST_BYTES:raise ValueError('清单响应超过64KiB')
            return bytes(data)
        from .service import _bounded_call
        data=_bounded_call(download,10)
        candidate=validate(bytes(data))
        with self.lock:
            self.candidate=None
            if candidate['version']==self.active['version'] and candidate['content_sha256']!=self.active['content_sha256']:raise ValueError('同版本清单内容变化，发布者须递增版本')
            if _version(candidate['version'])>_version(self.active['version']):self.candidate=(candidate,time.monotonic())
            return self.view()

    def apply_update(self):
        with self.lock:
            if not self.candidate or time.monotonic()-self.candidate[1]>=600:raise ValueError('没有已验证且未过期的候选清单，请先检查')
            candidate=validate(_canonical(self.candidate[0]))
            if _version(candidate['version'])<=_version(self.active['version']):raise ValueError('不接受清单降级/重复应用')
            old_active,old_previous=self.active,self.previous
            self.previous=deepcopy(self.active);self.active=candidate
            try:self._save()
            except Exception:self.active,self.previous=old_active,old_previous;raise
            self.candidate=None;self.cache.clear();return self.view()

    def rollback(self):
        with self.lock:
            if self.previous is None:raise ValueError('没有上一可用清单')
            old_active,old_previous=self.active,self.previous
            self.active=validate(_canonical(self.previous));self.previous=deepcopy(old_active)
            try:self._save()
            except Exception:self.active,self.previous=old_active,old_previous;raise
            self.candidate=None;self.cache.clear();return self.view()

    def fetch(self,row,meta,code,end,version):
        sid=row['id'];key=(version,sid,code,str(end));now=time.time()
        with self.lock:
            cached=self.cache.get(key)
            if cached and now-cached[0]<row['cache_ttl']:
                self.cache.move_to_end(key);result=deepcopy(cached[1]);result['cache']={'hit':True,'age_seconds':round(now-cached[0],2)};return result
            health=self.health.get(sid,{})
            if now<health.get('open_until',0):return self._unavailable(sid,meta,'来源熔断60秒冷却中，不重复请求',version)
            guard=self.guards.setdefault(sid,threading.Lock())
        if not guard.acquire(blocking=False):return self._unavailable(sid,meta,'来源正在处理其它请求，有限回退不排队阻塞',version)
        try:
            with self.lock:delay=max(0,self.last_start.get(sid,0)+row['min_interval']-time.monotonic())
            if delay:time.sleep(delay)
            with self.lock:self.last_start[sid]=time.monotonic()
            from . import web_evidence as web
            web._AUDIT.request_timeout=row['timeout_seconds']
            try:result=meta['fn'](code,end)
            except Exception as exc:result=web._fail(meta['category'],sid,'',f'{type(exc).__name__}: {exc}',web._now())
            finally:web._AUDIT.request_timeout=4
            result['registry']={'version':version,'parser':sid,'parser_version':1,'provider':meta['provider']};result['cache']={'hit':False}
            with self.lock:
                failed=result.get('status')=='failed';old=self.health.get(sid,{})
                streak=old.get('consecutive_failures',0)+1 if failed else 0
                self.health[sid]={'status':result.get('status'),'stock_code':code,'eval_date':str(end),'checked_at':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':result.get('elapsed_seconds'),
                    'consecutive_failures':streak,'open_until':time.time()+60 if streak>=3 else 0,
                    'reason':str((result.get('digest') or {}).get('reason') or (result.get('digest') or {}).get('note') or '')[:240]}
                if result.get('status') in {'ok','empty','not_applicable'}:
                    self.cache[key]=(time.time(),deepcopy(result));self.cache.move_to_end(key)
                    while len(self.cache)>256:self.cache.popitem(last=False)
                try:self._save()
                except OSError:pass  # Evidence remains usable if diagnostics disk is read-only.
            return result
        finally:guard.release()

    @staticmethod
    def _unavailable(sid,meta,reason,version):
        return {'source':sid,'category':meta['category'],'status':'unavailable','url':'','digest':{'note':reason},
                'registry':{'version':version,'parser':sid,'parser_version':1,'provider':meta['provider']}}

    def collect(self,code,end=None):
        if not re.fullmatch(r'\d{6}',str(code)) or (end is not None and not isinstance(end,date)):raise ValueError('采集股票/日期格式无效')
        with self.lock:rows=[deepcopy(row) for row in self._config() if row['enabled']];version=self.active['version']
        if not rows:return []
        known=catalog();executor=ThreadPoolExecutor(max_workers=4)
        futures={executor.submit(self.fetch,row,known[row['id']],code,end,version):i for i,row in enumerate(rows)}
        # Keep the existing wait surface mockable; bounded collection remains 32s.
        from . import web_evidence as web
        done,pending=web.wait(futures,timeout=32.0);results={}
        for future in done:
            try:results[futures[future]]=future.result()
            except Exception as exc:
                row=rows[futures[future]];results[futures[future]]=self._unavailable(row['id'],known[row['id']],str(exc)[:240],version)
        for future in pending:
            future.cancel();row=rows[futures[future]]
            results[futures[future]]=self._unavailable(row['id'],known[row['id']],'采集总预算32秒到期，保留其它完成来源',version)
        executor.shutdown(wait=False,cancel_futures=True)
        return [results[i] for i in range(len(rows))]


_REGISTRIES={}
_REGISTRY_LOCK=threading.Lock()

def get_registry():
    from backend.app.core.config import data_dir
    directory=data_dir()/'council_sources'
    with _REGISTRY_LOCK:
        key=str(directory.resolve())
        if key not in _REGISTRIES:_REGISTRIES[key]=SourceRegistry(directory)
        return _REGISTRIES[key]
