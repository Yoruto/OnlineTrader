"""Run the product's real DSV runtime against KQ with broker order calls denied."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from online_trader.accounts import StoredCredential, WindowsVault, account_identity
from online_trader.configuration.commands import parameter_diff
from online_trader.contracts.codec import canonical_bytes
from online_trader.gateway.simnow import credentials
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.runtime import dsv_simulation
from online_trader.runtime.auto_offsets import load_preparation
from online_trader.runtime.comparison import read_dsv_profile


def main():
    def utc(value): return value.isoformat().replace('+00:00', 'Z')
    base = ROOT/'output/simnow/20261110-night-ready'
    reference = base/'credential-reference.json'
    credential_record = json.loads(reference.read_bytes())
    auth = credentials(reference)
    dsv_config = Path(r'C:\Users\xyc\Desktop\trading\config.json')
    raw_config = dsv_config.read_bytes()
    account = json.loads(raw_config)['account']
    if account['mode'] != 'sim' or account['username'] != auth['kq_username']:
        raise RuntimeError('DSV_KQ_IDENTITY_MISMATCH')
    account_key, _ = account_identity(auth['kq_username'])
    target = 'OnlineTrader:SimNow:'+credential_record['account_fingerprint']+':market'
    # Adapt the existing credential reference to the product's KQ credential
    # interface. Credentials stay in the existing per-user vault entry.
    vault_view = SimpleNamespace(vault=WindowsVault(), get=lambda _: {'account_key': account_key},
                                 target=lambda _: target)
    credential = StoredCredential(vault_view, 'current-dsv-kq')
    secrets = tuple(auth.values())
    del auth, account

    class Redactor:
        def __init__(self, stream): self.stream = stream
        def write(self, value):
            for secret in secrets:
                if secret: value = value.replace(secret, '[REDACTED]')
            return self.stream.write(value)
        def __getattr__(self, name): return getattr(self.stream, name)

    sys.stdout, sys.stderr = Redactor(sys.stdout), Redactor(sys.stderr)
    from tqsdk import TqApi
    packet = Path(json.loads((base/'current-comparison-packet.json').read_bytes())['packet'])
    source, profile, calendar = dsv_simulation.load_inputs(packet/'scope.json', packet/'dsv-profile.json', packet/'calendar.json')
    receipt, fixed, effective = load_preparation(source, profile)
    current = read_dsv_profile(dsv_config)
    changes = parameter_diff(effective, current)
    if any(item['path'] != '$.schedule.enabled' for item in changes):
        raise RuntimeError('DSV_STRATEGY_PARAMETERS_CHANGED')
    now = datetime.now(timezone.utc)
    end = now+timedelta(seconds=115)
    codes = fixed.value['contracts']
    sessions = [calendar.query(code, utc(now)) for code in codes]
    if not all(row['is_trading'] and row['trading_day']==fixed.value['trading_day']
               and end < datetime.fromisoformat(row['session']['close_utc']) for row in sessions):
        raise RuntimeError('OBSERVATION_REQUIRES_AN_ACTIVE_COMMON_SESSION')
    out = ROOT/'output/dsv-online'/('kq-product-readonly-'+now.strftime('%Y%m%dT%H%M%SZ'))
    out.mkdir(parents=True)
    def save(name, value): (out/name).write_bytes(canonical_bytes(value))
    scope = fixed.value
    for field in ('trading_days','resume_entries_on_day_change'): scope.pop(field)
    scope.update(schema_version='KQ_DSV_SIM/1', grant_id='kq-observe-'+uuid.uuid4().hex,
                 account_fingerprint=account_key.rsplit(':',1)[-1], ledger_path=str(out/'ledger.db'),
                 profile_hash=current.content_hash, not_before_utc=utc(now), not_after_utc=utc(end),
                 max_order_quantity=1, opening_attempts=1,
                 authorization_reference='USER_TRY_KQ_READONLY_NO_ORDERS_20261111')
    save('scope.json', scope)
    save('profile.json', current.to_record())
    save('calendar.json', calendar.to_record())

    calls, packets = Counter(), Counter()
    api_instances, sdk_closed, native_connections = [], [], []
    original_init, original_close, original_send = TqApi.__init__, TqApi.close, TqApi._send_pack
    def init(api, *args, **kwargs):
        api_instances.append(api)
        return original_init(api,*args,**kwargs)
    def close(api, *args, **kwargs):
        result = original_close(api,*args,**kwargs)
        sdk_closed.append(id(api))
        return result
    def deny(name):
        def blocked(*args,**kwargs):
            calls[name] += 1
            raise RuntimeError('READ_ONLY_ORDER_OPERATION_BLOCKED')
        return blocked
    def send(api, pack):
        aid = pack.get('aid','UNKNOWN')
        if aid not in {'ins_query','subscribe_quote','set_chart','qry_order','req_qry_order'}:
            return deny('unexpected_api_packet:'+str(aid))()
        packets[aid] += 1
        return original_send(api,pack)
    TqApi.__init__, TqApi.close, TqApi._send_pack = init,close,send
    TqApi.insert_order, TqApi.cancel_order = deny('insert_order'), deny('cancel_order')
    connect_original = TqKqDsvPort.connect
    def readonly_connect(cls, cred, grant, clock, *, read_only=False):
        port = connect_original(cred,grant,clock,read_only=True)
        native_connections.append({'requested_read_only':read_only,'effective_read_only':port.read_only,
                                   'broker_connected':port.broker_connected,'clock':clock.record()})
        return port
    TqKqDsvPort.connect = classmethod(readonly_connect)
    TqKqDsvPort.send, TqKqDsvPort.cancel = deny('port_send'), deny('port_cancel')
    prime_original = dsv_simulation.DsvSimulationSession.prime
    def prime(session, **kwargs):
        try:
            return prime_original(session, **kwargs)
        except Exception as error:
            message = str(error)
            for secret in secrets:
                if secret: message = message.replace(secret, '[REDACTED]')
            save('prime-failure.json', {'error_code':getattr(error,'code',type(error).__name__), 'detail':message})
            raise
    dsv_simulation.DsvSimulationSession.prime = prime
    print(json.dumps({'output':str(out),'provider':'TQKQ','mode':'PRODUCT_RUNTIME_DISARMED'},ensure_ascii=False),flush=True)
    code = dsv_simulation.run(out/'scope.json', credential, out/'profile.json', out/'calendar.json', out/'run',
                              arm=False, dsv_config=dsv_config, dsv_log_dir=dsv_config.parent/'logs')
    if not all(id(api) in sdk_closed for api in api_instances):
        for api in api_instances:
            if id(api) not in sdk_closed:
                try: api.close()
                except Exception: pass
    ledger = out/'ledger.db'
    counts = {}
    if ledger.exists():
        with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True) as conn:
            counts = dict(conn.execute('SELECT event_type, count(*) FROM event_journal GROUP BY event_type'))
    phases = [json.loads(path.read_bytes())['phase'] for path in sorted((out/'run').glob('phase-*.json'))]
    evidence = {'exit_code':code,'provider':'TQKQ','scope_path':str(out/'scope.json'),'profile_hash':current.content_hash,
                'same_dsv_saved_profile':read_dsv_profile(dsv_config).content_hash==current.content_hash,
                'differences_from_prior_simnow_profile':changes,'original_calculation_hash':receipt['preparation_hash'],
                'sdk_instances':len(api_instances),'sdk_close_count':len(set(sdk_closed)),
                'native_connections':native_connections,'order_attempts':dict(calls),'api_packet_counts':dict(packets),
                'phases':phases,'ledger_event_counts':counts,'dsv_config_unchanged':dsv_config.read_bytes()==raw_config,
                'final_independent_readonly_login':(out/'run/final-relogin.json').is_file(),
                'runtime_armed':False}
    save('qa-evidence.json',evidence)
    print(json.dumps({'exit_code':code,'output':str(out),'phases':phases,'order_attempts':dict(calls),
                      'sdk_closed':len(set(sdk_closed)),'sdk_instances':len(api_instances)},ensure_ascii=False),flush=True)
    return code if not calls and len(set(sdk_closed))==len(api_instances) else 2


if __name__ == '__main__':
    raise SystemExit(main())
