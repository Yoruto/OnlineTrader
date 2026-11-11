"""Read-only DSV log capture and post-commit product comparison evidence.

This observer never calls a gateway, imports trades into the ledger, or changes
prices/positions. A text exit decision is not evidence of a closing fill.
"""
from datetime import datetime,timezone,timedelta,date
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.offsets import dsv_startup_profile
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes,content_hash,strict_loads
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.scheduling.reference import clock_observation


def read_dsv_profile(path):
    with Path(path).open('rb') as stream:raw=stream.read(262145)
    if len(raw)>262144:raise TraderError('E_CONFIG','DSV configuration exceeds its supported bound')
    # Private sections are stripped in memory and never exported or logged.
    return DsvProfile.from_dsv_json(raw)


def verify_dsv_profile(path,profile,*,source_profile=None):
    current=read_dsv_profile(path)
    accepted={profile.content_hash}
    if source_profile is not None:
        accepted.update({source_profile.content_hash,dsv_startup_profile(source_profile).content_hash})
    if current.content_hash not in accepted:
        raise TraderError('E_COMPARISON_SETTINGS','DSV business settings differ from the reviewed product profile')
    return current


_CODE=re.compile(r'\(((?:DCE|SHFE|INE|CZCE)\.[A-Za-z]+[0-9]+)\)')
_TIME=re.compile(r'^((?:\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]\s*)+)')
_NUMBER=r'([0-9]+(?:\.[0-9]+)?)'


def parse_dsv_line(line,civil_date,*,directions=None):
    """Only documented business text; retain both cached and inner timestamps."""
    clocks=_TIME.match(line);contract=_CODE.search(line)
    if not clocks or not contract:return None
    stamps=re.findall(r'\[([0-9:]+)\]',clocks[0])
    try:
        at=datetime.fromisoformat(civil_date+'T'+stamps[-1]+'+08:00')
    except ValueError:raise TraderError('E_COMPARISON_LOG','Invalid DSV civil log timestamp') from None
    code=contract[1];directions={} if directions is None else directions
    row={'side':'DSV_KQ','contract_id':code,'source_time_utc':at.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),
         'log_times_local':stamps,'timestamp_precision_ms':1000,'timestamp_source':'DSV_LOG_LAST_LEADING_TIMESTAMP',
         'direction':directions.get(code),'native_identity_available':False}
    order=re.search(r'\[开(long|short)下单\]',line)
    if order:
        directions[code]=order[1].upper();row.update(kind='ENTRY_ORDER',direction=directions[code])
        for field,pattern in (('theoretical_price',r'理论开仓[:：]\s*'+_NUMBER),
                              ('limit_price',r'限价上限\s*'+_NUMBER),('quantity',r'手数[:：]\s*([0-9]+)')):
            m=re.search(pattern,line)
            if m:row[field]=int(m[1]) if field=='quantity' else decimal_text(Decimal(m[1]))
    elif '[成交]' in line and '开仓' in line and '已成交' in line:
        qty=re.search(r'已成交\s*([0-9]+)\s*手',line);price=re.search(r'真实均价\s*'+_NUMBER,line)
        if not qty or not price:return None
        row.update(kind='ENTRY_FILL',quantity=int(qty[1]),price=decimal_text(Decimal(price[1])),aggregation='DSV_LOG_REPORTED_AVERAGE')
        m=re.search(r'理论开仓价\s*'+_NUMBER,line)
        if m:row['theoretical_price']=decimal_text(Decimal(m[1]))
    elif '[平仓]' in line:
        row.update(kind='EXIT_DECISION',direction='SHORT' if '空头' in line else 'LONG' if '多头' in line else None,
                   fill_confirmed=False)
        for field,pattern in (('quantity',r'数量[:：]\s*([0-9]+)'),('reference_price',r'出场[:：]\s*'+_NUMBER)):
            m=re.search(pattern,line)
            if m:row[field]=int(m[1]) if field=='quantity' else decimal_text(Decimal(m[1]))
    elif '[跳过]' in line or '[提示]' in line:
        row.update(kind='ENTRY_FILTER',reason='GEOMETRY' if '几何' in line or '振幅' in line else
                   'ACTIVATION_PASSED' if '激活位' in line else 'PRICE_SLIPPAGE' if '超出上限' in line else 'OTHER')
    else:return None
    return row


class DsvLogTail:
    def __init__(self,directory):
        self.directory=Path(directory);self.offsets={};self.directions={};self.identities={};self.dates={}

    def poll(self):
        rows=[]
        for path in sorted(self.directory.glob('trade_????-??-??.log')):
            m=re.fullmatch(r'trade_([0-9]{4}-[0-9]{2}-[0-9]{2})\.log',path.name)
            if not m:continue
            old=self.offsets.get(path.name,0);stat=path.stat();size=stat.st_size
            identity=str(stat.st_ino)
            if size<old or path.name in self.identities and self.identities[path.name]!=identity:
                rows.append({'kind':'DSV_LOG_GAP','file':path.name,'previous_offset':str(old),'size':str(size)})
                old=0;self.directions.pop(path.name,None);self.dates.pop(path.name,None)
            self.identities[path.name]=identity
            with path.open('rb') as stream:
                stream.seek(old)
                # Bounded work per owner turn; leave partial final lines unread.
                for _ in range(1000):
                    offset=stream.tell();raw=stream.readline(65537)
                    if not raw:break
                    if len(raw)>65536:raise TraderError('E_COMPARISON_LOG','Oversized DSV log line')
                    if not raw.endswith(b'\n'):stream.seek(offset);break
                    try:line=raw.decode('utf-8-sig')
                    except UnicodeError:raise TraderError('E_COMPARISON_LOG','DSV log is not valid UTF-8') from None
                    context=self.dates.setdefault(path.name,{'date':m[1],'last_seconds':None,'rollovers':0})
                    leading=_TIME.match(line);regression=False
                    if leading:
                        stamp=re.findall(r'\[([0-9:]+)\]',leading[0])[-1]
                        parts=[int(p) for p in stamp.split(':')]
                        if not (0<=parts[0]<24 and 0<=parts[1]<60 and 0<=parts[2]<60):
                            raise TraderError('E_COMPARISON_LOG','DSV timestamp has invalid hour/minute/second')
                        seconds=parts[0]*3600+parts[1]*60+parts[2];last=context['last_seconds']
                        if last is not None and last-seconds>12*3600:
                            context['date']=(date.fromisoformat(context['date'])+timedelta(days=1)).isoformat()
                            context['rollovers']+=1
                        elif last is not None and seconds<last:regression=True
                        context['last_seconds']=seconds
                    item=parse_dsv_line(line,context['date'],directions=self.directions.setdefault(path.name,{}))
                    if item is not None:
                        rows.append(item|{'file':path.name,'byte_offset':str(offset),'line_sha256':hashlib.sha256(raw).hexdigest(),
                                          'civil_date_source':'LOG_CHRONOLOGY_MIDNIGHT_INFERENCE' if context['rollovers'] else 'LOG_FILENAME',
                                          'midnight_rollovers_inferred':context['rollovers'],'timestamp_ambiguous':regression,
                                          'cabinet_day_inferred_from_filename':False})
                self.offsets[path.name]=stream.tell()
        return rows


class ComparisonRecorder:
    def __init__(self,kernel,output,profile,calendar,*,dsv_config=None,dsv_log_dir=None):
        self.kernel=kernel;self.profile=profile;self.calendar=calendar
        self.source_profile=getattr(kernel,'_auto_source_profile',None)
        self._last_dsv_profile_hash=None
        self.dsv_config=None if dsv_config is None else Path(dsv_config)
        self.tail=None if dsv_log_dir is None else DsvLogTail(dsv_log_dir)
        self.last_seq=0;self.last_clock=0;self.last_profile_check=0
        self.directory=Path(output)/('comparison-'+uuid.uuid4().hex[:12]);self.directory.mkdir(parents=True)
        self.path=self.directory/'events.jsonl';self.stream=self.path.open('xb');self.closed=False
        self.manifest={'schema_version':'DSV_COMPARISON/1','run_id':self.directory.name,
            'account_key':kernel.account_key,'provider':kernel.simulation.grant.provider,'scope_hash':kernel.simulation.grant.content_hash,
            'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,'kernel_boot_id':kernel.boot_id,
            'contracts':kernel.simulation.grant.value['contracts'],'trading_days':kernel.simulation.grant.trading_days,
            'ledger_path':str(kernel.ledger.path) if hasattr(kernel.ledger,'path') else kernel.simulation.grant.value['ledger_path'],
            'events_file':self.path.name,'entry_authority_at_capture_start':'DISARMED',
            'dsv_settings_attached':self.dsv_config is not None,'dsv_logs_attached':self.tail is not None,
            'started_at_utc':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
            'capture_from_utc':kernel.port.clock.now().isoformat().replace('+00:00','Z'),
            'capture_to_utc':kernel.simulation.grant.value['not_after_utc'],
            'same_host_clock_required':True,'dsv_log_precision_ms':1000,'comparison_is_read_only':True}
        if self.source_profile is not None:
            self.manifest.update(source_profile_hash=self.source_profile.content_hash,
                effective_profile_hash=profile.content_hash,automatic_offsets=True,
                dsv_effective_parameters_verified=False)
        try:
            self.write('RUN_START',self.manifest|{'profile':profile.to_record(),'calendar':calendar.to_record()})
            publish_new_bytes(canonical_bytes(self.manifest),self.directory/'manifest.json')
            if self.source_profile is not None:
                # The original DSV symbol import uses eight columns. Export the
                # exact computed settings for the other side to apply/review.
                from io import StringIO
                from online_trader.configuration.symbol_csv import HEADERS,FIELDS
                symbols=StringIO(newline='');writer=csv.writer(symbols);writer.writerow(HEADERS[:8])
                for row in profile.to_record()['settings']['symbols']:writer.writerow([row.get(k,'') for k in FIELDS[:8]])
                publish_new_bytes(symbols.getvalue().encode('utf-8-sig'),self.directory/'dsv-calculated-symbols.csv')
                publish_new_bytes(profile.to_json().encode('utf-8'),self.directory/'calculated-business-settings.json')
            self.poll()
        except Exception:self.close();raise

    def write(self,kind,data):
        row={'kind':kind,'data':data};row['record_hash']=content_hash(row)
        self.stream.write(canonical_bytes(row)+b'\n');self.stream.flush()

    def market(self,event,account,instrument,decision):
        self.write('PRODUCT_MARKET_DECISION',{'event':event,'account':account,'instrument':instrument,
            'decision':decision,'durable_seq':str(self.kernel._durable_seq),
            'source_epoch':self.kernel.port.source_epoch,'profile_hash':self.profile.content_hash})

    def poll(self):
        k=self.kernel
        for row in k.ledger.query('SELECT event_seq,event_type,payload_json FROM event_journal WHERE event_seq>? AND event_seq<=? ORDER BY event_seq LIMIT 1000',
                                  (self.last_seq,k._durable_seq)):
            self.write('PRODUCT_COMMITTED_EVENT',{'event_seq':str(row['event_seq']),'event_type':row['event_type'],
                'payload':strict_loads(row['payload_json'])});self.last_seq=row['event_seq']
        now=time.monotonic()
        if now-self.last_clock>=1:
            self.write('CLOCK',clock_observation(k.port.clock)|{'source_epoch':k.port.source_epoch,
                'entry_clock_gate_enforced':k.port.broker_connected});self.last_clock=now
        if self.tail is not None:
            for item in self.tail.poll():
                if item.get('source_time_utc'):
                    at=self.calendar.query(item['contract_id'],item['source_time_utc'])
                    item['trading_day']=at.get('trading_day') if at['complete'] and at['is_trading'] else None
                self.write('DSV_LOG',item)
        if self.dsv_config is not None and now-self.last_profile_check>=5:
            self.last_profile_check=now
            current=verify_dsv_profile(self.dsv_config,self.profile,source_profile=self.source_profile)
            if current.content_hash!=self._last_dsv_profile_hash:
                self.write('DSV_PARAMETER_BINDING',{'dsv_saved_profile_hash':current.content_hash,
                    'effective_profile_hash':self.profile.content_hash,
                    'effective_parameters_match':(dsv_startup_profile(current).content_hash==dsv_startup_profile(self.profile).content_hash) if self.source_profile is not None else current.content_hash==self.profile.content_hash,
                    'observed_at_reference_utc':self.kernel.port.clock.now().isoformat().replace('+00:00','Z'),
                    'dsv_startup_correction_matches':self.source_profile is not None and current.content_hash==dsv_startup_profile(self.source_profile).content_hash,
                    'calculation_policy_source_matches':self.source_profile is not None and current.content_hash==self.source_profile.content_hash})
                self._last_dsv_profile_hash=current.content_hash

    def close(self):
        if not self.closed:self.stream.close();self.closed=True


def read_records(path):
    with Path(path).open('rb') as stream:
        for raw in stream:
            if len(raw)>16*1024*1024:raise TraderError('E_COMPARISON','Oversized comparison record')
            try:row=strict_loads(raw)
            except TraderError:raise TraderError('E_COMPARISON','Incomplete or invalid comparison record') from None
            if row.get('record_hash')!=content_hash({key:row[key] for key in ('kind','data')}):
                raise TraderError('E_COMPARISON','Comparison record hash differs')
            yield row


def _product_events(records):
    orders={};native={};confirmed={};out=[];targets={}
    for row in records:
        if row['kind']!='PRODUCT_COMMITTED_EVENT':continue
        material=row['data'];payload=material['payload'];data=payload.get('data',payload);cycle=data.get('cycle')
        if cycle:
            plan=cycle['plan'];candidate=(cycle.get('strategy_metadata') or {}).get('candidate',{})
            state=cycle['protection'];target=state.get('target_remaining_qty');cid=cycle['position_cycle_id']
            if target is not None and target!=targets.get(cid):
                targets[cid]=target
                out.append({'side':'ONLINE_TRADER','kind':'EXIT_DECISION','contract_id':plan['contract_id'],
                    'trading_day':(cycle.get('money_quote') or {}).get('trading_day',plan['trading_day']),
                    'direction':plan['direction'],'quantity':max(0,state['owned_qty']-target),
                    'source_time_utc':(cycle.get('money_quote') or {}).get('observed_at_utc'),
                    'reference_price':(cycle.get('last_quote') or {}).get('price'),
                    'reasons':state.get('exit_reasons',[]),'target_remaining_qty':target,'event_seq':material['event_seq']})
            for order in cycle['orders']:
                child=order['child_order_id']
                if child in orders:continue
                item={'side':'ONLINE_TRADER','kind':'ENTRY_ORDER' if order['effect']=='OPEN' else 'EXIT_ORDER',
                    'contract_id':plan['contract_id'],'trading_day':order.get('native_trading_day',plan['trading_day']),
                    'direction':plan['direction'],'quantity':order['original_qty'],'limit_price':order['limit_price'],
                    'theoretical_price':plan['theoretical_entry_price'],'child_order_id':child,
                    'source_time_utc':candidate.get('source_time_utc') if order['effect']=='OPEN' else None,
                    'event_seq':material['event_seq'],'effect':order['effect']}
                orders[child]=item
                if order['effect']=='OPEN':out.append(item)
        for observation in data.get('orders',[]) if material['event_type']=='ONLINE_ORDER_OBSERVATIONS' else []:
            child=observation['child_order_id']
            for fill in observation.get('fills',[]):
                key=(child,fill['trade_key'])
                if key in native and native[key]!=fill:raise TraderError('E_COMPARISON','Conflicting native fill identity')
                native[key]=fill
        if material['event_type']=='FILL_CONFIRMED':
            fill=data['fill'];key=(fill['child_order_id'],fill['trade_key'])
            if key in confirmed and confirmed[key]!=fill:raise TraderError('E_COMPARISON','Conflicting committed fill identity')
            confirmed[key]=fill
    fills={}
    for key,fill in confirmed.items():
        proof=native.get(key)
        if proof is None or any(fill[k]!=proof.get(k) for k in ('quantity','price','trading_day')):
            raise TraderError('E_COMPARISON','Committed fill and original native evidence differ')
        fills[key]=proof
    for child,order in orders.items():
        owned=[fill for (cid,_),fill in fills.items() if cid==child]
        for day in sorted({fill['trading_day'] for fill in owned}):
            parts=[fill for fill in owned if fill['trading_day']==day]
            qty=sum(fill['quantity'] for fill in parts)
            price=sum(Decimal(fill['price'])*fill['quantity'] for fill in parts)/qty
            stamp=min(int(fill['source_time_ns']) for fill in parts)
            seconds,nanos=divmod(stamp,1_000_000_000)
            at=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds,microseconds=nanos//1000)
            out.append(order|{'kind':'ENTRY_FILL' if order['effect']=='OPEN' else 'EXIT_FILL','trading_day':day,
                'quantity':qty,'price':decimal_text(price),'native_trade_keys':[p['trade_key'] for p in parts],
                'source_time_utc':at.isoformat().replace('+00:00','Z'),'aggregation':'COMMITTED_NATIVE_FILLS_BY_CHILD_AND_FILL_DAY'})
    return out


def native_totals(statement_path,source_account,manifest,product):
    """Cross-account CSV totals, never trade-ID ownership/fee registration."""
    from online_trader.reconciliation import load_statement
    if not source_account:raise TraderError('E_COMPARISON','A DSV native CSV requires an explicit source account')
    statement=load_statement(statement_path);unique={};duplicates=[]
    for row in statement['rows']:
        fields=('account_key','environment','trading_day','trade_key','contract_id','side','effect','quantity','price')
        if row['issues'] or any(row.get(k) is None for k in fields):
            raise TraderError('E_COMPARISON','DSV native CSV has invalid or incomplete rows')
        if (row['account_key']!=source_account or row['environment']!='ONLINE_SIM'
                or row['contract_id'] not in manifest['contracts'] or row['trading_day'] not in manifest['trading_days']):
            raise TraderError('E_SCOPE','DSV native CSV account, environment, day or contract differs from the reviewed comparison')
        key=(row['trading_day'],row['trade_key'])
        economic={k:row[k] for k in ('contract_id','side','effect','quantity','price','time')}
        if key in unique:
            if unique[key]!=economic:raise TraderError('E_COMPARISON','DSV native CSV contains conflicting duplicate trade IDs')
            duplicates.append(row['row_number']);continue
        unique[key]=economic
    left={};right={}
    def add(target,key,qty,price):
        total=target.setdefault(key,{'quantity':0,'value':Decimal(0)})
        total['quantity']+=qty;total['value']+=Decimal(price)*qty
    for (day,_),row in unique.items():
        direction='LONG' if (row['side']=='BUY')==(row['effect']=='OPEN') else 'SHORT'
        kind='ENTRY_FILL' if row['effect']=='OPEN' else 'EXIT_FILL'
        add(left,(day,row['contract_id'],direction,kind),row['quantity'],row['price'])
    for row in product:
        if row['kind'] in {'ENTRY_FILL','EXIT_FILL'}:
            add(right,(row['trading_day'],row['contract_id'],row['direction'],row['kind']),row['quantity'],row['price'])
    rows=[]
    for key in sorted(left.keys()|right.keys()):
        a=left.get(key);b=right.get(key)
        average_a=None if a is None else a['value']/a['quantity'];average_b=None if b is None else b['value']/b['quantity']
        rows.append(dict(zip(('trading_day','contract_id','direction','kind'),key))|{
            'dsv_quantity':None if a is None else a['quantity'],'product_quantity':None if b is None else b['quantity'],
            'quantity_equal':a is not None and b is not None and a['quantity']==b['quantity'],
            'dsv_average_price':None if a is None else decimal_text(average_a),
            'product_average_price':None if b is None else decimal_text(average_b),
            'price_difference':None if a is None or b is None else decimal_text(average_b-average_a)})
    return {'source_sha256':statement['source_sha256'],'source_account_fingerprint':hashlib.sha256(source_account.encode()).hexdigest()[:16],
        'duplicate_rows_ignored':duplicates,'totals':rows,'aggregation':'CABINET_DAY_CONTRACT_DIRECTION_OPEN_CLOSE',
        'ownership_or_fill_time_pairing_claimed':False,'full_selected_day_csv_required':True}


def build_report(manifest_path,output,*,tolerance_ms=2000,dsv_trades=None,dsv_source_account=None):
    if type(tolerance_ms) is not int or not 0<=tolerance_ms<=60000:
        raise TraderError('E_COMPARISON','Explicit comparison tolerance must be within 0..60000 ms')
    manifest_path=Path(manifest_path);manifest=strict_loads(manifest_path.read_bytes())
    if manifest.get('schema_version')!='DSV_COMPARISON/1' or manifest.get('events_file')!='events.jsonl':
        raise TraderError('E_COMPARISON','Expected a product comparison manifest')
    records=list(read_records(manifest_path.parent/'events.jsonl'))
    all_product=_product_events(records);product=all_product;dsv=[r['data'] for r in records if r['kind']=='DSV_LOG']
    start=datetime.fromisoformat(manifest['capture_from_utc'])
    # A late owned close remains visible even after the opening scope expires.
    dsv=[r for r in dsv if not r.get('source_time_utc') or start<=datetime.fromisoformat(r['source_time_utc'])]
    product=[r for r in product if not r.get('source_time_utc') or start<=datetime.fromisoformat(r['source_time_utc'])]
    clocks=[r['data'] for r in records if r['kind']=='CLOCK']
    rows=[];used=set();eligible=[r for r in dsv if r.get('kind') in {'ENTRY_ORDER','ENTRY_FILL','EXIT_DECISION'}]
    def candidates(left):
        if (left.get('timestamp_ambiguous') or not left.get('source_time_utc')
                or left.get('trading_day') is None or left.get('direction') is None):return []
        return [i for i,right in enumerate(product) if all(left.get(k)==right.get(k) for k in ('kind','contract_id','trading_day','direction'))
                and right.get('source_time_utc') and abs((datetime.fromisoformat(left['source_time_utc'])-
                    datetime.fromisoformat(right['source_time_utc'])).total_seconds()*1000)<=tolerance_ms]
    possibilities=[candidates(left) for left in eligible]
    for left,choices in zip(eligible,possibilities):
        unique=len(choices)==1 and sum(choices[0] in other for other in possibilities)==1
        right=product[choices[0]] if unique else None
        row={'status':'MATCHED' if unique else 'AMBIGUOUS' if choices else 'DSV_ONLY',
             'kind':left['kind'],'contract_id':left['contract_id'],'trading_day':left.get('trading_day'),
             'direction':left.get('direction'),'dsv_time':left['source_time_utc'],'product_time':None,
             'dsv_quantity':left.get('quantity'),'product_quantity':None,'quantity_equal':None,
             'time_difference_ms':None,'price_difference':None,'theoretical_price_equal':None}
        if right is not None:
            used.add(choices[0]);row.update(product_time=right['source_time_utc'],product_quantity=right.get('quantity'),
                quantity_equal=left.get('quantity')==right.get('quantity'),
                time_difference_ms=str(round((datetime.fromisoformat(right['source_time_utc'])-
                    datetime.fromisoformat(left['source_time_utc'])).total_seconds()*1000,3)))
            field='price' if left['kind']=='ENTRY_FILL' else 'reference_price' if left['kind']=='EXIT_DECISION' else 'limit_price'
            if left.get(field) is not None and right.get(field) is not None:
                row['price_difference']=decimal_text(Decimal(right[field])-Decimal(left[field]))
            if left.get('theoretical_price') is not None:
                row['theoretical_price_equal']=left['theoretical_price']==right.get('theoretical_price')
        rows.append(row)
    for i,right in enumerate(product):
        if i not in used:rows.append({'status':'PRODUCT_ONLY','kind':right['kind'],'contract_id':right['contract_id'],
            'trading_day':right['trading_day'],'direction':right['direction'],'product_time':right['source_time_utc'],
            'product_quantity':right['quantity']})
    result={'schema_version':'DSV_COMPARISON_REPORT/1','run_id':manifest['run_id'],'profile_hash':manifest['profile_hash'],
        'scope_hash':manifest['scope_hash'],'tolerance_ms':tolerance_ms,'rows':rows,
        'clock_samples':len(clocks),'clock_alignment_enforced':bool(clocks) and all(c['entry_clock_gate_enforced'] for c in clocks),
        'source_coverage':{'product_market_decisions':sum(r['kind']=='PRODUCT_MARKET_DECISION' for r in records),
            'committed_product_events':sum(r['kind']=='PRODUCT_COMMITTED_EVENT' for r in records),
            'recognized_dsv_business_lines_in_window':len(dsv)},
        'maximum_observed_system_skew_ms':str(max((abs(Decimal(c['reference_minus_system_ms'])) for c in clocks),default=Decimal(0))),
        'dsv_exit_decisions':[r for r in dsv if r.get('kind')=='EXIT_DECISION'],
        'dsv_filters':[r for r in dsv if r.get('kind')=='ENTRY_FILTER'],
        'dsv_log_gaps':[r for r in dsv if r.get('kind')=='DSV_LOG_GAP'],
        'complete_native_dsv_trades_available':False,'automatic_correctness_verdict':False,
        'parameter_bindings':[r['data'] for r in records if r['kind']=='DSV_PARAMETER_BINDING'],
        'dsv_native_csv':None if dsv_trades is None else native_totals(dsv_trades,dsv_source_account,manifest,all_product),
        'limits':['DSV text uses second precision; the last leading timestamp is used.',
                  'DSV exit-decision text does not prove closing fills; use the broker export for full close comparison.',
                  'Different-account quantities, funds and fills are reported as differences; no ledger is changed.']}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    publish_new_bytes(canonical_bytes(result),output/'comparison.json')
    keys=('status','kind','contract_id','trading_day','direction','dsv_time','product_time','dsv_quantity',
          'product_quantity','quantity_equal','time_difference_ms','price_difference','theoretical_price_equal')
    with (output/'comparison.csv').open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,keys,extrasaction='ignore');writer.writeheader()
        for row in rows:writer.writerow({key:row.get(key) for key in keys})
    if result['dsv_native_csv'] is not None:
        keys=('trading_day','contract_id','direction','kind','dsv_quantity','product_quantity','quantity_equal',
              'dsv_average_price','product_average_price','price_difference')
        with (output/'native-totals.csv').open('x',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,keys);writer.writeheader();writer.writerows(result['dsv_native_csv']['totals'])
    return result
