"""Read the documented QPro local settlement trades endpoint for comparison."""
import json
from datetime import date
from decimal import Decimal
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from online_trader.errors import TraderError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_trades(company, client_id, start_day, end_day, *, port=8815, opener=None):
    try:
        start,end=date.fromisoformat(start_day),date.fromisoformat(end_day)
        if start.isoformat()!=start_day or end.isoformat()!=end_day or end<start:raise ValueError()
    except (TypeError,ValueError) as error:
        raise TraderError('E_SCHEMA','Expected an ordered trading-date range') from error
    if not company.strip() or not client_id.strip() or not 1<=port<=65535:
        raise TraderError('E_SCHEMA','QPro query requires an explicit company, client and local port')
    opener=opener or build_opener(ProxyHandler({}),_NoRedirect())
    identity={'futures_company_name':company,'client_id':client_id}
    items=[];total=None;seen=set()
    while total is None or len(items)<total:
        body={'start_date':start_day,'end_date':end_day,'accounts':[identity],'limit':1000,'offset':len(items)}
        request=Request(f'http://127.0.0.1:{port}/trades/query',data=json.dumps(body).encode('utf-8'),
                        headers={'Content-Type':'application/json; charset=utf-8'},method='POST')
        try:
            with opener.open(request,timeout=5) as response:
                raw=response.read(2_097_153)
            if len(raw)>2_097_152:raise ValueError()
            payload=json.loads(raw,parse_float=Decimal,parse_constant=_reject_constant)
            data=payload['data'];page=data['trades'];count=data['total']
            if (type(payload['code']) is not int or payload['code']!=0 or type(page) is not list
                    or type(count) is not int or not 0<=count<=50000 or (total is not None and count!=total)
                    or len(page)>min(1000,count-len(items)) or (len(items)<count and not page)):
                raise ValueError()
            for row in page:
                day=row['trading_day']
                if (any(row[key]!=value for key,value in identity.items()) or
                        date.fromisoformat(day).isoformat()!=day or not start_day<=day<=end_day):
                    raise ValueError()
                key=tuple(row[field] for field in ('trading_day','exchange','trade_id','direction'))
                if not all(isinstance(value,str) and value for value in key) or key in seen:raise ValueError()
                seen.add(key)
        except (OSError,URLError,ValueError,TypeError,KeyError) as error:
            raise TraderError('E_FACT_BARRIER','QPro historical trade query failed or returned incomplete or mismatched data') from error
        total=count;items.extend(page)
    return {'source':'QPRO_SETTLEMENT_TRADES','usage':'COMPARISON_ONLY','account':identity,
        'start_date':start_day,'end_date':end_day,'total':total,'trades':items}


def _reject_constant(value):
    raise ValueError(value)
