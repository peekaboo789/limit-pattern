"""Independent public HTTP adapters, written from documented schemas (no SDK/code execution)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from html import unescape
import json
import re
import threading
import time
from zoneinfo import ZoneInfo

from . import web_evidence as w

_STOCKS = ({}, 0.0)
_STOCK_LOCK = threading.Lock()


def _request(client, url, *, params=None, data=None, json_body=None):
    if data is None and json_body is None:
        return w._get(client, url, params=params)
    # This POST only searches public disclosures; no login/form/account action.
    from .source_registry import safe_url
    safe_url(url)
    for attempt in (1, 2):
        try:
            response = client.post(url, data=data, json=json_body)
            w._AUDIT.history.append({'url': str(response.url), 'status_code': response.status_code,
                'content_type': response.headers.get('content-type', 'unknown'), 'attempt': attempt})
            if response.status_code in {429, 500, 502, 503, 504} and attempt == 1:
                time.sleep(.25)
                continue
            return response
        except (w.httpx.TimeoutException, w.httpx.NetworkError, w.httpx.RemoteProtocolError) as exc:
            w._AUDIT.history.append({'url': url, 'attempt': attempt, 'error': f'{type(exc).__name__}: {exc}'[:240]})
            if attempt == 2:
                raise
            time.sleep(.25)


def _bars_result(source, url, bars, end, adjustment='none'):
    bars = sorted((bar for bar in bars if w._as_date(bar.get('date')) and bar['date'] <= w._eval_day(end).isoformat()), key=lambda bar:bar['date'])[-30:]
    window = {'kind':'trading_days','end':w._eval_day(end).isoformat(),'requested_trading_days':30,'observed_count':len(bars)}
    if not bars:
        return w._empty('market',source,url,'有效查询未取得评估日及以前日K，不补零',w._now(),window)
    return w._ok('market',source,url,{'bars':bars,'count':len(bars),'adjustment':adjustment,'volume_unit':'股'},w._now(),window)


@w._audited
def fetch_sina_kline(code: str, end: date | None = None):
    source, url = 'sina_kline','https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
    try:
        with w._client() as client:
            rows = w._json(_request(client,url,params={'symbol':('sh' if w._market(code)=='1' else 'sz')+code,'scale':240,'ma':'no','datalen':100}))
        if not isinstance(rows,list):
            raise w.UpstreamError('新浪日K不是数组，不能当空记录')
        bars = [{'date':str(row.get('day',''))[:10],**{key:w._num(row.get(key)) for key in ('open','high','low','close','volume')},
                 'amount':None,'turnover_rate':None} for row in rows]
        return _bars_result(source,url,bars,end)
    except Exception as exc:
        return w._fail('market',source,url,f'{type(exc).__name__}: {exc}',w._now())


@w._audited
def fetch_ths_kline(code: str, end: date | None = None):
    source, url = 'ths_kline',f'https://d.10jqka.com.cn/v6/line/hs_{code}/00/last.js'
    try:
        with w._client() as client:
            client.headers.update({'Referer':'https://stockpage.10jqka.com.cn/'})
            response = _request(client,url)
        if response.status_code == 404:
            result=w._empty('market',source,url,'该股未复权端点HTTP404，数据不可得，不推断停牌/退市或复权价格',w._now())
            result['status']='unavailable';return result
        if response.status_code != 200:
            raise w.UpstreamError(f'HTTP {response.status_code}')
        # Decode a JSONP literal only. Never eval/execute downloaded JavaScript.
        match = re.fullmatch(rf'\s*quotebridge_v6_line_hs_{code}_00_last\((.*)\);?\s*',response.text,re.S)
        if not match:
            raise w.UpstreamError('同花顺非预期JSONP（可能受限），不执行脚本')
        payload = json.loads(match[1])
        if not isinstance(payload,dict) or not isinstance(payload.get('data'),str):
            raise w.UpstreamError('同花顺缺明文data序列，不解密/执行脚本')
        bars=[]
        for line in payload['data'].split(';'):
            p=line.split(',')
            if len(p)<7 or not re.fullmatch(r'\d{8}',p[0]):continue
            bars.append({'date':date.fromisoformat(p[0]).isoformat(),**dict(zip(('open','high','low','close','volume','amount'),map(w._num,p[1:7]))),
                         'turnover_rate':w._num(p[7]) if len(p)>7 else None})
        return _bars_result(source,url,bars,end)
    except Exception as exc:
        return w._fail('market',source,url,f'{type(exc).__name__}: {exc}',w._now())


def _stock_ids(client):
    global _STOCKS
    with _STOCK_LOCK:
        if _STOCKS[0] and time.monotonic()-_STOCKS[1]<21600:
            return _STOCKS[0]
        response=_request(client,'https://www.cninfo.com.cn/new/data/szse_stock.json')
        if len(response.content)>1500000:
            raise w.UpstreamError('巨潮股票映射超过大小上限')
        rows=(w._json(response) or {}).get('stockList')
        if not isinstance(rows,list):raise w.UpstreamError('巨潮缺股票映射，不能臆造orgId')
        ids={str(row.get('code')):str(row.get('orgId')) for row in rows if re.fullmatch(r'\d{6}',str(row.get('code',''))) and re.fullmatch(r'[A-Za-z0-9]{3,30}',str(row.get('orgId','')))}
        _STOCKS=(ids,time.monotonic());return ids


def _cninfo(code,end,reports=False):
    source='cninfo_reports' if reports else 'cninfo_announcements'
    category='financial' if reports else 'announcement'
    url='https://www.cninfo.com.cn/new/hisAnnouncement/query'
    window=w._financial_window(end) if reports else w._window(end)
    try:
        with w._client() as client:
            client.headers.update({'Referer':'https://www.cninfo.com.cn/'})
            org=_stock_ids(client).get(code)
            if not org:
                result=w._empty(category,source,url,'巨潮公开映射未找到股票，无法核验，不猜orgId',w._now(),window);result['status']='unverified';return result
            stop=w._eval_day(end); start=stop-timedelta(days=1100) if reports else stop-timedelta(days=29)
            payload=w._json(_request(client,url,data={'pageNum':1,'pageSize':30,'column':'szse','tabName':'fulltext','plate':'',
                'stock':code+','+org,'searchkey':'','secid':'','trade':'','category':'category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh' if reports else '',
                'seDate':start.isoformat()+'~'+stop.isoformat(),'sortName':'time','sortType':'desc','isHLtitle':'false'}))
        if not isinstance(payload,dict) or 'announcements' not in payload or not isinstance(payload.get('totalAnnouncement'),int):
            raise w.UpstreamError('巨潮返回结构异常，不判无记录')
        rows=payload['announcements'] or []
        if not isinstance(rows,list):raise w.UpstreamError('巨潮announcements不是数组')
        items=[]
        for row in rows:
            if row.get('secCode')!=code:continue
            raw=row.get('announcementTime')
            day=datetime.fromtimestamp(float(raw)/1000,ZoneInfo('Asia/Shanghai')).date() if isinstance(raw,(int,float)) else None
            if day is None or not start<=day<=stop:continue
            items.append({'date':day.isoformat(),'publish_date':day.isoformat(),'title':unescape(re.sub('<[^>]+>','',str(row.get('announcementTitle') or '')))[:400],
                          'url':'https://www.cninfo.com.cn/new/disclosure/detail?stockCode='+code+'&announcementId='+str(row.get('announcementId',''))+'&orgId='+org})
        items.sort(key=lambda row:row['date'],reverse=True)
        if not items:return w._empty(category,source,url,'有效查询在观察范围无公开公告/定期报告记录，不代表数值0',w._now(),window)
        digest={'items':items,'count':len(items),'total_available':payload['totalAnnouncement'],'truncated':bool(payload.get('hasMore'))}
        if reports:
            digest['note']='第一方定期报告披露索引/原文链接；不是已解析完整PDF财务指标，不能冒充数值已核验'
        return w._ok(category,source,url,digest,w._now(),window)
    except Exception as exc:return w._fail(category,source,url,f'{type(exc).__name__}: {exc}',w._now(),window)


@w._audited
def fetch_cninfo_announcements(code,end=None):return _cninfo(code,end)


@w._audited
def fetch_cninfo_reports(code,end=None):return _cninfo(code,end,True)


@w._audited
def fetch_sse_announcements(code,end=None):
    source,url='sse_announcements','https://query.sse.com.cn/security/stock/queryCompanyBulletin.do'
    window=w._window(end)
    if not code.startswith(('6','900')):
        result=w._empty('announcement',source,url,'已核验代码市场非上交所，本源不适用',w._now(),window);result['status']='not_applicable';return result
    try:
        with w._client() as client:
            client.headers.update({'Referer':'https://www.sse.com.cn/'})
            payload=w._json(_request(client,url,params={'isPagination':'true','productId':code,'securityType':'0101,120100,020100,020200,120200',
                'reportType':'ALL','beginDate':window['start'],'endDate':window['end'],'pageHelp.pageSize':30,'pageHelp.pageNo':1}))
        rows=payload.get('result')
        if not isinstance(rows,list):raise w.UpstreamError('上交所缺result数组，不能当空记录')
        items=[]
        unknown_ownership=False
        for row in rows:
            owner=str(row.get('SECURITY_CODE') or row.get('SECURITY_CODE_LIST') or '')
            if code not in re.findall(r'\d{6}',owner):
                unknown_ownership=True
                continue
            day=w._date_str(row.get('SSEDATE'))
            if not w._date_in_natural_window(day,end)[1]:continue
            path=str(row.get('URL') or '')
            items.append({'date':day,'title':str(row.get('TITLE') or '')[:400],'url':path if path.startswith('https://www.sse.com.cn/') else 'https://www.sse.com.cn'+('/' if not path.startswith('/') else '')+path})
        if not items and unknown_ownership:
            result=w._empty('announcement',source,url,'上交所响应的股票归属未核验，不接收其它股票公告',w._now(),window);result['status']='unverified';return result
        return w._ok('announcement',source,url,{'items':items,'count':len(items)},w._now(),window) if items else w._empty('announcement',source,url,'有效查询未返回上交所公告；覆盖是否完整仍需巨潮交叉核验',w._now(),window)
    except Exception as exc:return w._fail('announcement',source,url,f'{type(exc).__name__}: {exc}',w._now(),window)


@w._audited
def fetch_szse_announcements(code,end=None):
    source,url='szse_announcements','https://www.szse.cn/api/disc/announcement/annList'
    window=w._window(end)
    if w._market(code)=='1' or code.startswith(('4','8','92')):
        result=w._empty('announcement',source,url,'已核验代码市场非深交所，本源不适用',w._now(),window);result['status']='not_applicable';return result
    try:
        with w._client() as client:
            client.headers.update({'Referer':'https://www.szse.cn/'})
            payload=w._json(_request(client,url,json_body={'seDate':[window['start'],window['end']],'stock':[code],'channelCode':['listedNotice_disc'],'pageSize':30,'pageNum':1}))
        rows=payload.get('data')
        if not isinstance(rows,list):raise w.UpstreamError('深交所缺data数组，不能当空记录')
        items=[]
        for row in rows:
            day=w._date_str(row.get('publishTime'))
            if code not in (row.get('secCode') or []) or not w._date_in_natural_window(day,end)[1]:continue
            path=str(row.get('attachPath') or '')
            items.append({'date':day,'title':str(row.get('title') or '')[:400],'url':'https://www.szse.cn'+('/' if not path.startswith('/') else '')+path})
        return w._ok('announcement',source,url,{'items':items,'count':len(items),'total_available':payload.get('announceCount')},w._now(),window) if items else w._empty('announcement',source,url,'有效查询在观察区间无深交所公开公告',w._now(),window)
    except Exception as exc:return w._fail('announcement',source,url,f'{type(exc).__name__}: {exc}',w._now(),window)
