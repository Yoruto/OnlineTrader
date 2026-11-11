"""Dated local night packet: credential reference, exact DSV settings, no login."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import importlib.metadata
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.offsets import STOPS
from online_trader.contracts.codec import canonical_bytes,strict_loads
from online_trader.domain.values import decimal_text
from online_trader.gateway.simnow import credentials,account_fingerprint
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.comparison import read_dsv_profile
from online_trader.runtime.dsv_simulation import prepare
from online_trader.scheduling.calendar import TradingCalendar


def main():
    parser=argparse.ArgumentParser()
    for field in ('credential-reference','input-packet','dsv-config','output'):parser.add_argument('--'+field,type=Path,required=True)
    args=parser.parse_args()
    if any(args.credential_reference.parent.rglob('ledger.db')):
        raise RuntimeError('An existing night ledger requires its original scope; preparation cannot renew it')
    auth=credentials(args.credential_reference)
    fingerprint=account_fingerprint(auth['simnow_username']);del auth
    reference=strict_loads(args.credential_reference.read_bytes())
    if reference['account_fingerprint']!=fingerprint:raise RuntimeError('Credential reference identity differs')
    profile=read_dsv_profile(args.dsv_config);settings=profile.to_record()['settings']
    calendar=TradingCalendar.from_record(strict_loads((args.input_packet/'calendar.json').read_bytes()))
    provenance=strict_loads((args.input_packet/'preparation.json').read_bytes())
    multipliers={'DCE.p2701':10,'DCE.l2701':5,'SHFE.bu2701':10,'CZCE.OI701':10,'SHFE.ag2702':15}
    symbols=settings['symbols'];codes=[r['code'] for r in symbols]
    if set(codes)!=set(multipliers):raise RuntimeError('This dated input covers the reviewed five contracts only')
    trade=settings['trade'];maximum=trade['max_volume'];per_contract_orders=trade['max_daily_orders']
    if maximum<=0 or per_contract_orders<=0:raise RuntimeError('An unbounded DSV limit needs an explicit finite scope')
    # Nonbinding ceiling implied by one occupied cycle per contract and saved
    # maximum lots. Actual sizing still uses DSV equity * risk_per_trade_ratio.
    components=[{'contract_id':r['code'],'stop_distance':str(STOPS.get(r['name'],15)),'multiplier':multipliers[r['code']],
                 'maximum_lots':maximum,'nominal_stop_exposure':decimal_text(Decimal(STOPS.get(r['name'],15))*multipliers[r['code']]*maximum)} for r in symbols]
    budget=decimal_text(sum((Decimal(r['nominal_stop_exposure']) for r in components),Decimal(0)))
    if Decimal(budget)<=0:raise RuntimeError('No configured positive stop exposure is ready')
    sdk_site=ROOT/'output/online/sdk-venv/Lib/site-packages'
    sdk_versions={d.metadata['Name'].lower():d.version for d in importlib.metadata.distributions(path=[str(sdk_site)])}
    if sdk_versions.get('tqsdk')!='3.10.4':raise RuntimeError('The reviewed SDK is not available')
    args.output.mkdir(parents=True,exist_ok=False)
    scope={'schema_version':'SIMNOW_DSV_AUTO/1','grant_id':'simnow-night-'+uuid.uuid4().hex,'account_fingerprint':fingerprint,
        'ledger_path':str((args.output/'ledger.db').resolve()),'contracts':codes,'trading_day':'2026-11-11',
        'trading_days':['2026-11-11'],'resume_entries_on_day_change':False,
        'not_before_utc':provenance['suggested_comparison_window_utc']['from'],
        'not_after_utc':provenance['suggested_comparison_window_utc']['to'],
        'max_order_quantity':maximum,'opening_attempts':per_contract_orders*len(codes),'risk_budget':budget,
        'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,
        'authorization_reference':'USER_AUTO_CALC_PREPARE_ONLY_20261110_NO_CONNECTION',
        'auto_offsets':{'schema_version':'DSV_START_OFFSETS/1','as_of_date':'2026-11-11','last_completed_day':'2026-11-10'}}
    grant=DsvSimulationGrant.parse(canonical_bytes(scope))
    for name,value in [('scope.json',scope),('dsv-profile.json',profile.to_record()),('calendar.json',calendar.to_record())]:
        publish_new_bytes(canonical_bytes(value),args.output/name)
    result=prepare(args.output/'scope.json',args.output/'dsv-profile.json',args.output/'calendar.json',args.output/'review',
                   dsv_config=args.dsv_config,dsv_log_dir=args.dsv_config.parent/'logs')
    metadata={'status':'LOCAL_PREPARATION_COMPLETE_NOT_CONNECTED','account_key':grant.account_key,
        'scope_hash':grant.content_hash,'credential_reference':str(args.credential_reference.resolve()),
        'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,'sdk_version':'3.10.4',
        'max_order_quantity_source':'DSV max_volume','opening_attempts_source':'DSV max_daily_orders per contract times contract count',
        'nominal_risk_ceiling_source':'Sum of DSV calculated fixed stop distance * contract multiplier * saved max_volume',
        'nominal_risk_ceiling':budget,'nominal_risk_components':components,'automatic_sizing_preserved':True,
        'risk_per_trade_ratio':trade['risk_per_trade_ratio'],'broker_balance_queried':False,
        'zero_stop_contracts':[r['code'] for r in symbols if Decimal(r['stop_offset'])==0],
        'automatic_offsets':result['automatic_offsets'],
        'zero_stop_interpretation':'Saved source value; startup calculation resolves the DSV fixed stop before entry',
        'scope_prepared':True,'connection_authorized':False,'broker_connected':False,'entry_authority':'DISARMED',
        'created_at_system_utc':datetime.now(timezone.utc).isoformat(),
        'multiplier_sources':['https://www.dce.com.cn/dalianshangpin/resource/cms/2016/11/%E7%8E%89%E7%B1%B3%E6%B7%80%E7%B2%89%E6%9C%9F%E8%B4%A7%E5%90%88%E7%BA%A6%E8%AE%BE%E8%AE%A1%E8%AF%B4%E6%98%8E.pdf',
          'https://www.shfe.com.cn/products/futures/energyandchemical/bu_f/',
          'https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/',
          'https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207144928864.pdf']}
    publish_new_bytes(canonical_bytes(metadata),args.output/'readiness.json')
    script="""param([switch]$Connect,[switch]$Arm)
$ErrorActionPreference='Stop'
if ($Arm -and -not $Connect) { throw 'Arming needs an explicit connection instruction.' }
$packetRoot=$PSScriptRoot
$reviewId=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6)
$argsForOwner=@{Scope=(Join-Path $packetRoot 'scope.json');Profile=(Join-Path $packetRoot 'dsv-profile.json');Calendar=(Join-Path $packetRoot 'calendar.json');Output=(Join-Path $packetRoot ('run-'+$reviewId));Credentials='CREDENTIAL_REF';DsvConfig='DSV_CONFIG';DsvLogDir='DSV_LOGS'}
if ($Connect) { $argsForOwner.Run=$true }
if ($Arm) { $argsForOwner.Arm=$true }
& 'E:\\online-trader\\scripts\\simnow.ps1' @argsForOwner
"""
    for token,value in [('CREDENTIAL_REF',str(args.credential_reference.resolve())),('DSV_CONFIG',str(args.dsv_config.resolve())),('DSV_LOGS',str(args.dsv_config.parent/'logs'))]:
        script=script.replace(token,value.replace("'","''"))
    publish_new_bytes(script.encode('utf-8'),args.output/'Start-Night.ps1')
    report_script="""param([string]$DsvTrades,[string]$DsvSourceAccount)
& 'E:\\online-trader\\scripts\\compare.ps1' -RunRoot $PSScriptRoot -DsvTrades $DsvTrades -DsvSourceAccount $DsvSourceAccount
exit $LASTEXITCODE
"""
    publish_new_bytes(report_script.encode('utf-8'),args.output/'Report-Now.ps1')
    print(str(args.output.resolve()))


if __name__=='__main__':main()
