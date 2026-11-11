"""Bounded, explicitly authorized online KQ simulation probe; outside product.

One buy-open lot at most. No real-money account class or local matching account.
One owner journals each intent durably before SDK sends. Unknown sends never
release occupancy; only terminal/fill-consistent close orders may be replaced.
"""
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
import hashlib,json,logging,math,os,sqlite3,sys,time,uuid
from pathlib import Path

if __package__:
    from .online_kq_preflight import credentials,safe_number
    from .online_clock_reference import measure_clock
else:
    from online_kq_preflight import credentials,safe_number
    from online_clock_reference import measure_clock


UTC8=timezone(timedelta(hours=8))
def utc():return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,at_utc TEXT NOT NULL,monotonic_ns INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL)")
        self.db.commit()
        last=self.db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        self.previous=last[0] if last else "0"*64

    def append(self,kind,payload):
        at=utc();now=time.monotonic_ns()
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
        digest=hashlib.sha256((self.previous+at+str(now)+kind+raw).encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT INTO events(at_utc,monotonic_ns,kind,payload,previous_hash,event_hash) VALUES(?,?,?,?,?,?)",(at,now,kind,raw,self.previous,digest))
        self.previous=digest
        return {"kind":kind,"at_utc":at,"monotonic_ns":now,"hash":digest}

    def close(self):self.db.close()


class RoundTrip:
    def __init__(self,journal):
        self.journal=journal;self.open_id=None;self.symbol=None;self.open_qty=0;self.close_qty=0
        self.first_fill_ns=None;self.deadline_ns=None;self.close_id=None;self.close_terminal=False
        self.close_reported=0;self.close_traded=0;self.open_terminal=False

    def prepare_open(self,symbol,price,*,position_long,position_short,active_orders):
        if self.open_id is not None or position_long or position_short or active_orders:
            raise ValueError("Opening allowance consumed or selected contract already occupied")
        identity="ot1m"+uuid.uuid4().hex
        self.journal.append("OPEN_INTENT",{"order_id":identity,"symbol":symbol,"direction":"BUY","offset":"OPEN","quantity":1,"limit_price":str(price)})
        self.open_id,self.symbol=identity,symbol
        return identity

    def observe_open(self,*,traded,reported,terminal,now_ns):
        if self.open_id is None or traded not in (0,1) or reported not in (0,1):raise ValueError("Invalid owned entry observation")
        if traded<self.open_qty:raise ValueError("Owned entry fills cannot regress")
        if traded and not self.open_qty:
            self.journal.append("OPEN_FILL_CONFIRMED",{"order_id":self.open_id,"quantity":1,"observed_ns":now_ns,"exit_due_ns":now_ns+60_000_000_000})
            self.first_fill_ns=now_ns;self.deadline_ns=now_ns+60_000_000_000
        self.open_qty=traded
        self.open_terminal=bool(terminal and reported==traded)

    def prepare_close(self,price,*,now_ns,position_long,position_short):
        if (self.open_qty-self.close_qty!=1 or not self.open_terminal or self.deadline_ns is None or now_ns<self.deadline_ns
                or position_long!=1 or position_short!=0 or self.close_id is not None and not self.close_terminal):
            raise ValueError("Owned exit not due, position mismatch, or prior close occupancy unresolved")
        identity="ot1c"+uuid.uuid4().hex
        self.journal.append("CLOSE_INTENT",{"order_id":identity,"symbol":self.symbol,"direction":"SELL","offset":"CLOSE","quantity":1,"limit_price":str(price),"entry_order_id":self.open_id,"due_ns":self.deadline_ns})
        self.close_id=identity;self.close_terminal=False;self.close_reported=0;self.close_traded=0
        return identity

    def observe_close(self,*,traded,reported,terminal):
        if self.close_id is None or traded not in (0,1) or reported not in (0,1):raise ValueError("Invalid owned close observation")
        if traded<self.close_traded:raise ValueError("Owned close fills cannot regress")
        if traded>self.close_traded:
            self.journal.append("CLOSE_FILL_CONFIRMED",{"order_id":self.close_id,"quantity":traded-self.close_traded})
            self.close_qty+=traded-self.close_traded
        self.close_traded=traded;self.close_reported=reported
        self.close_terminal=bool(terminal and reported==traded)

    @property
    def closed(self):return self.open_qty==self.close_qty==1 and self.close_terminal


def order_record(order):
    return {key:getattr(order,key,None) for key in ("order_id","exchange_order_id","exchange_id","instrument_id","direction","offset","volume_orign","volume_left","status","last_msg")}


def fills(order):
    return [{key:getattr(trade,key,None) for key in ("trade_id","order_id","exchange_id","instrument_id","direction","offset","volume","price","trade_date_time")} for trade in order.trade_records.values()]


def fresh_quote(quote,now=None):
    try:
        at=datetime.fromisoformat(quote.datetime).replace(tzinfo=UTC8)
        now=datetime.now(UTC8) if now is None else now
        values=(quote.bid_price1,quote.ask_price1,quote.price_tick,quote.lower_limit,quote.upper_limit)
        return (not quote.expired and -1<=(now-at).total_seconds()<=5
                and all(math.isfinite(v) and v>0 for v in values)
                and quote.lower_limit<=quote.bid_price1<=quote.ask_price1<=quote.upper_limit
                and quote.bid_volume1>=1 and quote.ask_volume1>=1)
    except (ValueError,TypeError,AttributeError):return False


def limit_price(quote,direction):
    tick=Decimal(str(quote.price_tick))
    if direction=="BUY":
        value=min(Decimal(str(quote.ask_price1))+tick*2,Decimal(str(quote.upper_limit)))
        return (value/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    value=max(Decimal(str(quote.bid_price1))-tick*2,Decimal(str(quote.lower_limit)))
    return (value/tick).to_integral_value(rounding=ROUND_CEILING)*tick


def snapshot(api,account):
    funds=api.get_account(account=account)
    return {"at_utc":utc(),"trade_sync_complete":not api._account._get_trade_more_data(api._data),
        "funds":{key:safe_number(getattr(funds,key,None)) for key in ("balance","available","margin")},
        "positions":[{"symbol":symbol,"long":p.pos_long,"short":p.pos_short} for symbol,p in api.get_position(account=account).items() if p.pos_long or p.pos_short],
        "orders":[order_record(o) for o in api.get_order(account=account).values()],
        "trades":[{key:getattr(t,key,None) for key in ("trade_id","order_id","exchange_id","instrument_id","direction","offset","volume","price","trade_date_time")} for t in api.get_trade(account=account).values()]}


def run(directory):
    out=Path(directory);auth=credentials();api=None;journal=Journal(out/"probe-ledger.db")
    report={"provider":"TqKq","scope":"Independent online simulated probe; not OnlineTrader product acceptance","started_utc":utc(),"max_open_lots":1,"hold_seconds":60}
    state=RoundTrip(journal);opening=None;closing=None;close_orders=[];lock=None
    def write(name,value):
        with (out/name).open("x",encoding="utf-8") as stream:stream.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    def progress(kind,**values):print(json.dumps({"phase":kind,"at_utc":utc(),**values},ensure_ascii=False),flush=True)
    logging.disable(logging.CRITICAL)
    try:
        # This lock serializes only our probes. It does not alter any other app.
        import msvcrt
        fingerprint=hashlib.sha256(auth["username"].encode()).hexdigest()[:16]
        lock_path=out.parent/("kq-probe-"+fingerprint+".lock")
        lock=lock_path.open("a+b");lock.seek(0)
        if lock_path.stat().st_size==0:lock.write(b"0");lock.flush();lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        if journal.db.execute("SELECT COUNT(*) FROM events WHERE kind='OPEN_INTENT'").fetchone()[0]:raise ValueError("Existing opening intent requires reconciliation, never a new opening")
        clock=measure_clock();write("clock-reference.json",clock.record());journal.append("CLOCK_REFERENCE",clock.record())
        report["clock_offset_seconds"]=clock.offset_seconds
        from tqsdk import TqApi,TqKq,TqAuth,__version__
        account=TqKq();assert type(account) is TqKq
        api=TqApi(account=account,auth=TqAuth(auth["username"],auth["password"]),debug=False,web_gui=False,disable_print=True)
        report.update(sdk_version=__version__,account_fingerprint=fingerprint)
        before=snapshot(api,account);write("before.json",before);journal.append("BASELINE",before)
        if not before["trade_sync_complete"]:raise ValueError("Initial full trading state incomplete")
        roots=[api.get_quote(symbol) for symbol in ("KQ.m@DCE.m","KQ.m@DCE.c")]
        candidates={};fresh_seen={};until=time.monotonic()+600;last_progress=0
        while time.monotonic()<until and opening is None:
            if time.monotonic()-clock.monotonic_anchor>120:
                clock=measure_clock();journal.append("CLOCK_REFERENCE_REFRESH",clock.record())
            api.wait_update(deadline=time.time()+0.5)
            for root in roots:
                if root.underlying_symbol and root.underlying_symbol not in candidates:
                    if not root.underlying_symbol.startswith(("DCE.m","DCE.c")):continue
                    candidates[root.underlying_symbol]=api.get_quote(root.underlying_symbol)
            for symbol,quote in candidates.items():
                if not fresh_quote(quote,clock.now()):continue
                seen=fresh_seen.setdefault(symbol,set());seen.add(quote.datetime)
                if len(seen)<2:continue
                position=api.get_position(symbol,account=account)
                live=[o for o in api.get_order(account=account).values() if o.exchange_id+"."+o.instrument_id==symbol and o.status=="ALIVE"]
                if position.pos_long or position.pos_short or live:continue
                available=api.get_account(account=account).available
                if not math.isfinite(available) or available<10_000:raise ValueError("Insufficient verified simulated available funds")
                initial=snapshot(api,account);write("before-send.json",initial);journal.append("READY",{"symbol":symbol,"quote_time":quote.datetime,"bid":quote.bid_price1,"ask":quote.ask_price1,"position_long":0,"position_short":0})
                price=limit_price(quote,"BUY")
                oid=state.prepare_open(symbol,price,position_long=position.pos_long,position_short=position.pos_short,active_orders=len(live))
                progress("OPEN_INTENT_COMMITTED",symbol=symbol,quantity=1,limit_price=str(price),order_id=oid)
                # An exception here leaves the durable intent occupied. Never retry.
                opening=api.insert_order(symbol=symbol,direction="BUY",offset="OPEN",volume=1,limit_price=float(price),order_id=oid,account=account)
                submitted=time.monotonic();report["symbol"]=symbol
                break
            if opening is None and time.monotonic()-last_progress>30:
                progress("WAITING_FRESH_QUOTES",quotes=[{"symbol":symbol,"time":q.datetime,"bid":safe_number(q.bid_price1),"ask":safe_number(q.ask_price1)} for symbol,q in candidates.items()]);last_progress=time.monotonic()
        if opening is None:raise TimeoutError("No fresh unoccupied simulated contract during preflight window")
        open_cancelled=False;last_hold=-1;close_started=None;cancelled_close_ids=set();last_signature=None
        while True:
            if time.monotonic()-clock.monotonic_anchor>120:
                clock=measure_clock();journal.append("CLOCK_REFERENCE_REFRESH",clock.record())
            api.wait_update(deadline=time.time()+0.2)
            now=time.monotonic();now_ns=time.monotonic_ns();entry_fills=fills(opening)
            entry_volume=sum(t["volume"] for t in entry_fills)
            state.observe_open(traded=entry_volume,reported=opening.volume_orign-opening.volume_left,terminal=opening.status=="FINISHED",now_ns=now_ns)
            if state.first_fill_ns is not None and "opening_confirmed_utc" not in report:
                report.update(opening_confirmed_utc=utc(),opening_fills=entry_fills,entry_order_id=state.open_id,
                    hold_started_ns=state.first_fill_ns,exit_due_ns=state.deadline_ns)
                journal.append("HOLDING",{"symbol":state.symbol,"owned_quantity":1,"due_ns":state.deadline_ns})
                progress("OPEN_FILLED_HOLDING",symbol=state.symbol,quantity=1,exit_after_seconds=60)
            if not entry_volume and opening.status=="FINISHED" and state.open_terminal:
                report.update(status="OPEN_NOT_FILLED",opening=order_record(opening));break
            if not entry_volume and now-submitted>15 and not open_cancelled:
                journal.append("OPEN_CANCEL_INTENT",{"order_id":state.open_id});api.cancel_order(opening,account=account);open_cancelled=True
            if state.open_qty and state.first_fill_ns is not None:
                remaining=max((state.deadline_ns-now_ns)/1e9,0)
                bucket=int(remaining)//15
                if bucket!=last_hold and closing is None:
                    progress("HOLDING",seconds_remaining=round(remaining,1));last_hold=bucket
            position=api.get_position(state.symbol,account=account);quote=candidates[state.symbol]
            if closing is not None:
                owned_fills=fills(closing)
                state.observe_close(traded=sum(t["volume"] for t in owned_fills),reported=closing.volume_orign-closing.volume_left,terminal=closing.status=="FINISHED")
                if state.closed and position.pos_long==0 and position.pos_short==0:
                    report.update(status="CLOSED",closing_confirmed_utc=utc(),closing_fills=owned_fills,
                        close_order_id=state.close_id,observed_hold_seconds=(time.monotonic_ns()-state.first_fill_ns)/1e9)
                    progress("CLOSE_FILLED",symbol=state.symbol,quantity=1,observed_hold_seconds=report["observed_hold_seconds"])
                    break
                if closing.status=="ALIVE" and now-close_started>10 and closing.order_id not in cancelled_close_ids:
                    journal.append("CLOSE_CANCEL_INTENT",{"order_id":closing.order_id});api.cancel_order(closing,account=account);cancelled_close_ids.add(closing.order_id)
            due=state.deadline_ns is not None and now_ns>=state.deadline_ns
            if due and state.open_terminal and state.open_qty-state.close_qty==1 and (closing is None or state.close_terminal):
                if not fresh_quote(quote,clock.now()):
                    if now-last_progress>15:progress("EXIT_WAITING_FRESH_QUOTE");last_progress=now
                    continue
                if position.pos_long!=1 or position.pos_short!=0:
                    raise RuntimeError("Owned close position mismatch; no aggregate or foreign position will be flattened")
                price=limit_price(quote,"SELL")
                oid=state.prepare_close(price,now_ns=now_ns,position_long=position.pos_long,position_short=position.pos_short)
                progress("CLOSE_INTENT_COMMITTED",order_id=oid,quantity=1,limit_price=str(price))
                closing=api.insert_order(symbol=state.symbol,direction="SELL",offset="CLOSE",volume=1,limit_price=float(price),order_id=oid,account=account)
                close_orders.append(closing);close_started=now
            signature=(opening.status,opening.volume_left,entry_volume,None if closing is None else closing.status,None if closing is None else closing.volume_left)
            if signature!=last_signature:
                journal.append("ORDER_OBSERVATION",{"opening":order_record(opening),"opening_fills":entry_fills,
                    "closing":None if closing is None else order_record(closing),"closing_fills":[] if closing is None else fills(closing)})
                last_signature=signature
        end=time.monotonic()+3
        while time.monotonic()<end:api.wait_update(deadline=time.time()+0.5)
        after=snapshot(api,account);write("after-stream.json",after)
        report["owned_final_flat"]=not any(p["symbol"]==state.symbol and (p["long"] or p["short"]) for p in after["positions"])
        report["owned_active_orders"]=[o["order_id"] for o in after["orders"] if o["order_id"] in {state.open_id,*[o.order_id for o in close_orders]} and o["status"]=="ALIVE"]
        if report.get("status")=="CLOSED":
            report["server_fill_interval_seconds"]=(report["closing_fills"][0]["trade_date_time"]-report["opening_fills"][0]["trade_date_time"])/1e9
            if not report["owned_final_flat"] or report["owned_active_orders"]:raise RuntimeError("Final test-owned activity did not settle")
        journal.append("FINISHED",report)
    except BaseException as error:
        message=str(error)
        for value in auth.values():message=message.replace(value,"[REDACTED]")
        report.update(status="FAILED_NEEDS_RECONCILIATION" if state.open_id else "PREFLIGHT_FAILED",error_type=type(error).__name__,error=message[:800],
            entry_order_id=state.open_id,close_order_id=state.close_id,confirmed_open_quantity=state.open_qty,confirmed_close_quantity=state.close_qty)
        journal.append("FAILURE",report)
        # Keep any exposure explicitly visible; do not invent fills or retry an unknown send.
        progress("FAILURE",status=report["status"],error_type=type(error).__name__)
    finally:
        if api is not None:
            try:api.close()
            except Exception:pass
        if lock is not None:
            try:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            except Exception:pass
            lock.close()
        report["finished_utc"]=utc();write("result.json",report);journal.close()
        print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report.get("status")=="CLOSED" and report["owned_final_flat"] else 2


if __name__=="__main__":raise SystemExit(run(sys.argv[1]))
