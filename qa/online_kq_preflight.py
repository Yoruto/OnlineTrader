"""Explicit user-authorized read-only KQ simulation connectivity probe.

No order or cancel API is called. Credentials are read only from the user's file.
"""
from datetime import datetime, timezone
import hashlib,json,logging,math,re,sys,time
from pathlib import Path


def credentials():
    raw=Path(r"C:\Users\xyc\Desktop\期货模拟账号.txt").read_bytes()
    for encoding in ("utf-8-sig","gb18030"):
        try:lines=raw.decode(encoding).splitlines();break
        except UnicodeError:pass
    else:raise ValueError("Credential file encoding unsupported")
    values={};inside=False
    for line in lines:
        if "快期" in line and "账号" in line:inside=True;continue
        if not inside:continue
        parts=re.split("[:：=]",line,maxsplit=1)
        if len(parts)!=2:continue
        if parts[0].strip()=="用户名":values["username"]=parts[1].strip()
        elif parts[0].strip()=="密码":values["password"]=parts[1].strip()
    if set(values)!={"username","password"} or not all(values.values()):raise ValueError("KQ credential fields are missing")
    return values


def safe_number(value):
    return value if type(value) in {int,float} and math.isfinite(value) else None


def main():
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
    auth=credentials();api=None
    report={"phase":"READ_ONLY_PREFLIGHT","provider":"TqKq","started_utc":datetime.now(timezone.utc).isoformat(),"order_api_called":False,
        "account_fingerprint":hashlib.sha256(auth["username"].encode()).hexdigest()[:16]}
    logging.disable(logging.CRITICAL)
    try:
        from tqsdk import TqApi,TqAuth,TqKq,__version__
        account=TqKq()
        api=TqApi(account=account,auth=TqAuth(auth["username"],auth["password"]),debug=False,web_gui=False,disable_print=True)
        assert type(account) is TqKq
        report["sdk_version"]=__version__
        funds=api.get_account(account=account);positions=api.get_position(account=account)
        orders=api.get_order(account=account);trades=api.get_trade(account=account)
        roots={root:api.get_quote(root) for root in ("KQ.m@DCE.m","KQ.m@DCE.c")}
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            api.wait_update(deadline=time.time()+1)
            if all(quote.underlying_symbol for quote in roots.values()):break
        concrete={q.underlying_symbol:api.get_quote(q.underlying_symbol) for q in roots.values() if q.underlying_symbol}
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:api.wait_update(deadline=time.time()+1)
        report.update(status="CONNECTED",positions=[{"symbol":symbol,"long":p.pos_long,"short":p.pos_short} for symbol,p in positions.items() if p.pos_long or p.pos_short],
            funds={key:safe_number(getattr(funds,key,None)) for key in ("balance","available","margin")},
            order_count=len(orders),trade_count=len(trades),active_order_symbols=sorted({o.exchange_id+"."+o.instrument_id for o in orders.values() if o.status=="ALIVE"}),
            quotes=[{"symbol":symbol,"time":q.datetime,"bid":safe_number(q.bid_price1),"ask":safe_number(q.ask_price1),"tick_size":safe_number(q.price_tick),
                     "last":safe_number(q.last_price),"expired":q.expired,
                     "lower_limit":safe_number(q.lower_limit),"upper_limit":safe_number(q.upper_limit),
                     "bid_volume":safe_number(q.bid_volume1),"ask_volume":safe_number(q.ask_volume1)} for symbol,q in concrete.items()],
            orders=[{key:getattr(order,key,None) for key in ("order_id","exchange_order_id","exchange_id","instrument_id","direction","offset","volume_orign","volume_left","status")}
                for order in orders.values()],
            trades=[{key:getattr(trade,key,None) for key in ("trade_id","order_id","exchange_id","instrument_id","direction","offset","volume","price","trade_date_time")}
                for trade in trades.values()],
            initial_trade_sync_complete=not api._account._get_trade_more_data(api._data))
    except BaseException as error:
        message=str(error)
        for value in auth.values():message=message.replace(value,"[REDACTED]")
        report.update(status="FAILED",error_type=type(error).__name__,error=message[:800])
    finally:
        if api is not None:
            try:api.close()
            except Exception:pass
        report["finished_utc"]=datetime.now(timezone.utc).isoformat()
        with (out/"preflight.json").open("x",encoding="utf-8") as stream:stream.write(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps(report,ensure_ascii=False))
    return 0 if report.get("status")=="CONNECTED" else 2


if __name__=="__main__":raise SystemExit(main())
