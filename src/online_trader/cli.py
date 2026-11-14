"""Engineering console with a separate explicitly granted online simulation command."""

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import uuid

from online_trader.errors import TraderError


def main(argv=None):
    parser=argparse.ArgumentParser(description="OnlineTrader 内核、参数与状态控制台；在线模拟需要独立显式授权")
    commands=parser.add_subparsers(dest="action",required=True)
    serve=commands.add_parser("serve",help="启动无交易适配器的独立内核")
    serve.add_argument("--data-dir",required=True,type=Path)
    serve.add_argument("--account-key",required=True)
    serve.add_argument("--pipe",required=True)
    serve.add_argument("--ready-file",type=Path)
    serve.add_argument("--shadow-worker",action="store_true",help="启动只有状态读取权的G1策略进程骨架")
    for action in ("status","pause","desktop","workbench-status"):
        command=commands.add_parser(action);command.add_argument("--pipe",required=True)
    workbench=commands.add_parser('workbench',help='完整工作台；只附着原内核，不建立交易连接')
    workbench.add_argument('--pipe');workbench.add_argument('--account-store',type=Path);workbench.add_argument('--account-id')
    workbench.add_argument('--dsv-config',type=Path,help='读取原 DSV 已保存的业务参数')
    workbench.add_argument('--offline-pipe', help='附着 DSV 离线重构执行器')
    offline=commands.add_parser('offline-dsv',help='运行或操作 DSV 离线执行器')
    offline_actions=offline.add_subparsers(dest='offline_action',required=True)
    for name in ('serve','request'):
        mode=offline_actions.add_parser(name)
        mode.add_argument('--pipe',required=True)
        mode.add_argument('--input',type=Path,required=True)
        if name=='serve':
            mode.add_argument('--db',type=Path,required=True)
            mode.add_argument('--ready-file',type=Path)
    desktop_start=commands.add_parser('desktop-start',help='执行桌面确认的本次模拟策略启动')
    desktop_start.add_argument('--request',type=Path,required=True)
    desktop_start.add_argument('--output',type=Path,required=True)
    qpro=commands.add_parser('qpro-history',help='从本机快期专业版查询历史成交，仅用于对照')
    for field in ('company','client-id','start-day','end-day'):qpro.add_argument('--'+field,required=True)
    qpro.add_argument('--port',type=int,default=8815)
    qpro.add_argument('--output',required=True,type=Path)
    accounts=commands.add_parser('accounts',help='本地账户档案，Windows凭据库；不登录')
    account_actions=accounts.add_subparsers(dest='account_action',required=True)
    for name in ('list','add','edit','select','delete','desktop'):
        command=account_actions.add_parser(name);command.add_argument('--store',type=Path,required=True)
        if name not in {'list','desktop'}:command.add_argument('--expected-revision',type=int,required=True)
        if name in {'edit','select','delete'}:command.add_argument('--account-id',required=True)
        if name in {'add','edit'}:
            command.add_argument('--alias',required=True)
            command.add_argument('--broker',default=None if name=='add' else '快期模拟')
            command.add_argument('--line',default=None if name=='add' else 'TqKq')
        if name=='add':
            command.add_argument('--username',required=True)
            command.add_argument('--provider',choices=('TQKQ','TQ_SIMNOW'),default='TQKQ')
    data=commands.add_parser('data',help='本地参考历史/日历导入，不改变当前执行源')
    data_actions=data.add_subparsers(dest='data_action',required=True)
    for name in ('daily-import','daily-export','calendar-import','calendar-review','desktop'):
        command=data_actions.add_parser(name);command.add_argument('--store',type=Path,required=True)
        if name in {'daily-import','calendar-import','calendar-review'}:command.add_argument('--input',type=Path,required=True)
        if name in {'calendar-import','calendar-review'}:command.add_argument('--as-of-utc',required=True)
        if name=='daily-export':
            command.add_argument('--hash',required=True);command.add_argument('--output',type=Path,required=True)
    from online_trader.configuration.commands import add_parameter_parser
    add_parameter_parser(commands)
    simulation=commands.add_parser('simulation',help='显式的一手快期在线模拟测试；默认内核仍禁开')
    simulation_actions=simulation.add_subparsers(dest='simulation_action',required=True)
    for name in ('prepare','run'):
        command=simulation_actions.add_parser(name)
        command.add_argument('--scope',type=Path,required=True)
        command.add_argument('--credentials',type=Path,required=True)
        command.add_argument('--output',type=Path,required=True)
        if name=='run':command.add_argument('--pipe')
    for provider in ('dsv','simnow'):
        dsv=commands.add_parser(provider,help='独立DSV策略范围；prepare不联网，run才连接指定模拟账户')
        dsv_actions=dsv.add_subparsers(dest='dsv_action',required=True)
        if provider=='simnow':
            command=dsv_actions.add_parser('credentials',help='交互式保存Windows凭据及无密码引用文件；不登录')
            command.add_argument('--output',type=Path,required=True)
        command=dsv_actions.add_parser('scope',help='按明确额度、账户档案和时间生成独立范围；不连接')
        for field in ('profile','calendar','ledger','output'):
            command.add_argument('--'+field,type=Path,required=True)
        command.add_argument('--contract',action='append',required=True)
        command.add_argument('--trading-day',action='append',required=True)
        command.add_argument('--not-before-utc',required=True);command.add_argument('--not-after-utc',required=True)
        command.add_argument('--max-order-quantity',type=int,required=True)
        command.add_argument('--opening-attempts',type=int,required=True);command.add_argument('--risk-budget',required=True)
        command.add_argument('--authorization-reference',required=True)
        command.add_argument('--manual-workbench',action='store_true',help='生成独立人工交易与外部持仓接管范围')
        command.add_argument('--allow-external-allocation',action='store_true',help='允许在工作台明确复核并登记外部持仓归属调整')
        if provider=='dsv':
            command.add_argument('--account-store',type=Path,required=True)
            command.add_argument('--account-id',required=True)
            command.add_argument('--resume-entries-on-day-change',action='store_true')
        else:
            account_source=command.add_mutually_exclusive_group(required=True)
            account_source.add_argument('--credential-reference',type=Path)
            account_source.add_argument('--account-store',type=Path)
            command.add_argument('--account-id')
            command.add_argument('--resume-entries-on-day-change',action='store_true')
            command.add_argument('--automatic-offsets',action='store_true',help='按已绑定DSV公式在首次开仓前自动计算；当前仅支持一个明确柜台日')
            command.add_argument('--last-completed-day',help='自动计算明确使用的最后完整日线交易日')
            command=dsv_actions.add_parser('profile',help='只读提取DSV业务设置，不导出账户密码')
            command.add_argument('--dsv-config',type=Path,required=True);command.add_argument('--output',type=Path,required=True)
        for name in ('prepare','run'):
            command=dsv_actions.add_parser(name)
            for field in ('scope','profile','calendar','output'):command.add_argument('--'+field,type=Path,required=True)
            command.add_argument('--dsv-config',type=Path,help='诊断原 DSV 业务设置是否与当前运行参数一致')
            command.add_argument('--dsv-log-dir',type=Path,help='只读跟踪DSV原日志，自动记录至本次对比目录')
            if name=='run':
                credential_source=command.add_mutually_exclusive_group(required=True)
                credential_source.add_argument('--credentials',type=Path)
                credential_source.add_argument('--account-store',type=Path)
                command.add_argument('--account-id')
                command.add_argument('--pipe')
                command.add_argument('--arm',action='store_true',help='仅首次新范围显式开启策略；重启只管理原退出')
    compare=commands.add_parser('compare',help='双侧对比记录和时钟核对；不登录交易账户')
    compare_actions=compare.add_subparsers(dest='compare_action',required=True)
    command=compare_actions.add_parser('report')
    command.add_argument('--manifest',type=Path,required=True);command.add_argument('--output',type=Path,required=True)
    command.add_argument('--tolerance-ms',type=int,default=2000)
    command.add_argument('--dsv-trades',type=Path,help='可选：DSV快期原生成交CSV，比较完整交易日开/平总量及均价')
    command.add_argument('--dsv-source-account',help='明确CSV来源账户；不同账户只对照，不登记本地成交')
    command=compare_actions.add_parser('clock',help='显式发起只读NTP校时检查，不修改Windows时间')
    command.add_argument('--output',type=Path,required=True)
    reports=commands.add_parser('reports',help='只读资金盈亏报表与导出（未连接柜台）')
    report_actions=reports.add_subparsers(dest='report_action',required=True)
    for action in ('show','export','desktop'):
        command=report_actions.add_parser(action)
        command.add_argument('--data-dir',type=Path,required=action!='desktop')
        command.add_argument('--account-key',required=action!='desktop')
        command.add_argument('--as-of-utc')
        command.add_argument('--checkpoint-event-seq')
        if action=='desktop':command.add_argument('--pipe')
        if action=='export':
            command.add_argument('--output',type=Path,required=True)
            command.add_argument('--table',choices=('trades','by_day','by_contract','cycles','equity','nominal'),default='trades')
            command.add_argument('--format',choices=('json','csv'))
    reconcile=commands.add_parser('reconcile',help='外部成交对照、导出与离线匹配费用登记')
    reconcile_actions=reconcile.add_subparsers(dest='reconcile_action',required=True)
    for action in ('preview','export','register','desktop'):
        command=reconcile_actions.add_parser(action)
        command.add_argument('--data-dir',type=Path,required=action!='desktop')
        command.add_argument('--account-key',required=action!='desktop')
        if action=='desktop':command.add_argument('--pipe')
        else:
            command.add_argument('--input',type=Path,required=True)
            command.add_argument('--environment',choices=('ONLINE_SIM','LIVE'),default='ONLINE_SIM')
            command.add_argument('--trading-day',required=True)
            command.add_argument('--source-account')
        if action in ('preview','export'):command.add_argument('--checkpoint-event-seq')
        if action=='register':command.add_argument('--preview-hash',required=True)
        if action=='export':
            command.add_argument('--output',type=Path,required=True)
            command.add_argument('--format',choices=('csv','json','xlsx'))
    args=parser.parse_args(argv)
    try:
        if args.action=='desktop-start':
            from online_trader.runtime.desktop_bootstrap import run_desktop_start
            return run_desktop_start(args.request,args.output)
        if args.action=='qpro-history':
            from online_trader.configuration.files import publish_new_bytes
            from online_trader.gateway.qpro_history import query_trades
            report=query_trades(args.company,args.client_id,args.start_day,args.end_day,port=args.port)
            publish_new_bytes(json.dumps(report,ensure_ascii=False,default=str,indent=2).encode('utf-8'),args.output)
            print(json.dumps({'total':report['total'],'usage':report['usage'],'output':str(args.output)}));return 0
        if args.action=='accounts':
            from online_trader.accounts import AccountStore
            store=AccountStore(args.store)
            if args.account_action=='desktop':
                from PySide6.QtWidgets import QApplication
                from online_trader.desktop.accounts import AccountDialog
                app=QApplication.instance() or QApplication([]);window=AccountDialog(store);window.show();return app.exec()
            if args.account_action=='list':result=store.read()
            else:
                fields={key:getattr(args,key) for key in ('account_id','alias','username','broker','line','provider') if hasattr(args,key)}
                if args.account_action=='add':
                    fields['broker']=fields['broker'] or ('SimNow' if args.provider=='TQ_SIMNOW' else '快期模拟')
                    fields['line']=fields['line'] or ('TqAccount' if args.provider=='TQ_SIMNOW' else 'TqKq')
                result=store.change(args.account_action,expected_revision=args.expected_revision,**fields)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        elif args.action=='data':
            from online_trader.marketdata import ReferenceStore,bounded_json,review_calendar
            store=ReferenceStore(args.store)
            if args.data_action=='desktop':
                from online_trader.desktop.data import run
                return run(store)
            if args.data_action=='daily-import':result=store.import_daily(args.input)
            elif args.data_action=='daily-export':result={'exported':str(store.daily_csv(args.hash,args.output))}
            elif args.data_action=='calendar-import':result=store.import_calendar(args.input,as_of_utc=args.as_of_utc)
            else:result=review_calendar(bounded_json(args.input),as_of_utc=args.as_of_utc)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        elif args.action=='workbench':
            if args.offline_pipe is not None:
                if any(value is not None for value in (args.pipe,args.account_store,args.account_id,args.dsv_config)):
                    raise TraderError('E_DSV_SCOPE','离线工作台使用独立管道和账户')
                from online_trader.desktop.offline_workbench import run as run_offline
                return run_offline(args.offline_pipe)
            store=None;account_id=args.account_id;pipe=args.pipe
            if args.account_store is not None:
                from online_trader.accounts import AccountStore
                store=AccountStore(args.account_store);account_id=account_id or store.read()['selected_id']
                if account_id is not None:pipe=pipe or store.pipe_name(store.get(account_id))
            if pipe is None and store is None:raise TraderError('E_ACCOUNT','Select an account store or supply an existing owner pipe')
            from online_trader.desktop.workbench import run
            return run(pipe,store=store,account_id=account_id,dsv_config=args.dsv_config)
        elif args.action=='offline-dsv':
            from online_trader.dsv_runtime.offline_endpoint import cli
            return cli(args)
        elif args.action=='compare':
            if args.compare_action=='report':
                from online_trader.runtime.comparison import build_report
                result=build_report(args.manifest,args.output,tolerance_ms=args.tolerance_ms,
                    dsv_trades=args.dsv_trades,dsv_source_account=args.dsv_source_account)
            else:
                from online_trader.scheduling.reference import measure_reference,clock_observation
                from online_trader.configuration.files import publish_new_bytes
                from online_trader.contracts.codec import canonical_bytes
                result=clock_observation(measure_reference())
                publish_new_bytes(canonical_bytes(result),args.output)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 2 if args.compare_action=='clock' and not result['system_aligned'] else 0
        elif args.action in {'dsv','simnow'}:
            from online_trader.runtime import dsv_simulation
            if args.dsv_action=='credentials':
                from online_trader.gateway.simnow import save_credential_reference
                print(json.dumps(save_credential_reference(args.output),ensure_ascii=False,indent=2));return 0
            if args.dsv_action=='scope':
                from online_trader.online_scope import DsvSimulationGrant
                from online_trader.configuration.dsv import DsvProfile
                from online_trader.scheduling.calendar import TradingCalendar
                from online_trader.contracts.codec import canonical_bytes,strict_loads
                from online_trader.configuration.files import publish_new_bytes
                if args.action=='simnow' and args.account_store is None:
                    reference=strict_loads(args.credential_reference.read_bytes())
                    if (type(reference) is not dict or reference.keys()!={'schema_version','account_fingerprint'}
                            or reference['schema_version']!='SIMNOW_CREDENTIAL_REFERENCE/1'):
                        raise TraderError('E_CREDENTIALS','Scope preparation reads only a password-free SimNow credential reference')
                    fingerprint=reference['account_fingerprint']
                    schema='SIMNOW_WORKBENCH_SIM/1' if args.manual_workbench else 'SIMNOW_DSV_SIM/1'
                else:
                    from online_trader.accounts import AccountStore
                    store=AccountStore(args.account_store)
                    account=store.get(args.account_id or store.read()['selected_id'])
                    expected_provider='TQ_SIMNOW' if args.action=='simnow' else 'TQKQ'
                    if account['provider']!=expected_provider or store.ledger_path(account)!=args.ledger.resolve():
                        raise TraderError('E_SCOPE','The scope ledger must be the selected account archive ledger')
                    fingerprint=account['account_key'].split(':')[-1]
                    if args.action=='simnow':
                        schema='SIMNOW_WORKBENCH_SIM/1' if args.manual_workbench else 'SIMNOW_DSV_SIM/1'
                    else:schema='KQ_WORKBENCH_SIM/1' if args.manual_workbench else 'KQ_DSV_SIM/2'
                profile=DsvProfile.from_record(strict_loads(args.profile.read_bytes()))
                calendar=TradingCalendar.from_record(strict_loads(args.calendar.read_bytes()))
                value={'schema_version':schema,'grant_id':args.action+'-'+uuid.uuid4().hex,
                    'account_fingerprint':fingerprint,'ledger_path':str(args.ledger.resolve()),
                    'contracts':args.contract,'trading_day':args.trading_day[0],'trading_days':args.trading_day,
                    'resume_entries_on_day_change':args.resume_entries_on_day_change,
                    'not_before_utc':args.not_before_utc,'not_after_utc':args.not_after_utc,
                    'max_order_quantity':args.max_order_quantity,'opening_attempts':args.opening_attempts,
                    'risk_budget':args.risk_budget,'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,
                    'authorization_reference':args.authorization_reference}
                if args.allow_external_allocation:value['allow_external_allocation']=True
                if getattr(args,'automatic_offsets',False):
                    if args.manual_workbench:raise TraderError('E_ONLINE_SCOPE','Startup auto calculation needs a strategy-only scope')
                    if args.last_completed_day is None:raise TraderError('E_AUTO_OFFSETS','An explicit last completed daily date is required')
                    value.update(schema_version='SIMNOW_DSV_AUTO/1',auto_offsets={'schema_version':'DSV_START_OFFSETS/1',
                        'as_of_date':args.trading_day[0],'last_completed_day':args.last_completed_day})
                elif getattr(args,'last_completed_day',None) is not None:
                    raise TraderError('E_AUTO_OFFSETS','The completed daily date belongs to an explicit automatic calculation scope')
                grant=DsvSimulationGrant.parse(canonical_bytes(value))
                dsv_simulation.validate_inputs(grant,profile,calendar)
                publish_new_bytes(canonical_bytes(value),args.output)
                print(json.dumps({'scope_hash':grant.content_hash,'account_key':grant.account_key,'broker_connected':False,
                    'entry_authority':'DISARMED','output':str(args.output)},ensure_ascii=False,indent=2));return 0
            if args.dsv_action=='profile':
                from online_trader.runtime.comparison import read_dsv_profile
                from online_trader.configuration.files import publish_new_bytes
                from online_trader.contracts.codec import canonical_bytes
                profile=read_dsv_profile(args.dsv_config)
                publish_new_bytes(canonical_bytes(profile.to_record()),args.output)
                print(json.dumps({'profile_hash':profile.content_hash,'output':str(args.output),'broker_connected':False}));return 0
            grant,_,_=dsv_simulation.load_inputs(args.scope,args.profile,args.calendar)
            if (args.action=='simnow')!=(grant.provider=='TQ_SIMNOW'):
                raise TraderError('E_ONLINE_SCOPE','Use the connection command matching this scope provider')
            if args.dsv_action=='prepare':
                result=dsv_simulation.prepare(args.scope,args.profile,args.calendar,args.output,
                    dsv_config=args.dsv_config,dsv_log_dir=args.dsv_log_dir)
                print(json.dumps(result,ensure_ascii=False,indent=2));return 0
            credential=args.credentials;pipe=args.pipe
            if getattr(args,'account_store',None) is not None:
                from online_trader.accounts import AccountStore
                store=AccountStore(args.account_store)
                account_id=args.account_id or store.read()['selected_id']
                if store.read()['selected_id']!=account_id:raise TraderError('E_SCOPE','Only the selected account may use this saved-account connection entry')
                row=store.get(account_id);grant,_,_=dsv_simulation.load_inputs(args.scope,args.profile,args.calendar)
                if (row['provider']!=grant.provider or row['account_key']!=grant.account_key
                        or store.ledger_path(row)!=Path(grant.value['ledger_path']).resolve()):
                    raise TraderError('E_SCOPE','Selected account and original ledger differ from the explicit scope')
                credential=store.credential_reference(account_id)
                if grant.manual_workbench and pipe is None:pipe=store.pipe_name(row)
            return dsv_simulation.run(args.scope,credential,args.profile,args.calendar,args.output,pipe=pipe,arm=args.arm,
                dsv_config=args.dsv_config,dsv_log_dir=args.dsv_log_dir)
        elif args.action=='simulation':
            from online_trader.runtime.simulation import run_session
            return run_session(args.scope,args.credentials,args.output,pipe=getattr(args,'pipe',None),prepare=args.simulation_action=='prepare')
        elif args.action=="parameters":
            from online_trader.configuration.commands import run_parameters
            return run_parameters(args)
        elif args.action=='reports':
            path=None if args.data_dir is None else args.data_dir/'ledger.db'
            if args.report_action=='desktop':
                from online_trader.desktop.reports import run
                return run(path,args.account_key,pipe_name=args.pipe)
            from online_trader.reporting import read_report,export_report
            report=read_report(path,args.account_key,as_of_utc=args.as_of_utc,checkpoint_event_seq=args.checkpoint_event_seq)
            if args.report_action=='export':
                exported=export_report(report,args.output,table=args.table,format=args.format)
                result={'exported':str(exported),'checkpoint_event_seq':report['checkpoint_event_seq'],'broker_connected':False}
            else:result=report
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        elif args.action=='reconcile':
            path=None if args.data_dir is None else args.data_dir/'ledger.db'
            if args.reconcile_action=='desktop':
                from online_trader.desktop.reconciliation import run
                return run(path,args.account_key,pipe_name=args.pipe)
            from online_trader.statement_workflow import preview_file,register_fees
            scope={'environment':args.environment,'trading_day':args.trading_day,'source_account':args.source_account}
            if args.reconcile_action=='register':
                result=register_fees(args.input,path,args.account_key,**scope,expected_preview_hash=args.preview_hash)
            else:
                result=preview_file(args.input,path,args.account_key,**scope,checkpoint_event_seq=args.checkpoint_event_seq)
                if args.reconcile_action=='export':
                    from online_trader.reconciliation_export import export_comparison
                    destination=export_comparison(result,args.output,format=args.format)
                    result={'exported':str(destination),'preview_hash':result['preview_hash'],'trading_authority':False}
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        elif args.action=="serve":
            from online_trader.kernel.engine import Kernel
            from online_trader.kernel.server import ControlServer
            args.data_dir.mkdir(parents=True,exist_ok=True)
            with Kernel(args.data_dir/"ledger.db",args.account_key) as kernel:
                server=ControlServer(kernel,args.pipe)
                worker=None
                if args.shadow_worker:
                    instance="shadow-"+uuid.uuid4().hex
                    from online_trader.runtime.executable import bundled_cli
                    packaged=bundled_cli()
                    prefix=[str(packaged),'--strategy-worker'] if packaged else [sys.executable,'-m','online_trader.strategy.worker']
                    worker=subprocess.Popen([*prefix,"--pipe",args.pipe,"--instance-id",instance,
                        "--parent-pid",str(os.getpid()),"--report-file",str(args.data_dir/"worker-status.json")],creationflags=subprocess.CREATE_NO_WINDOW)
                    server.register_peer(instance,worker.pid)
                if args.ready_file:
                    args.ready_file.parent.mkdir(parents=True,exist_ok=True)
                    args.ready_file.write_text(json.dumps({"pipe":args.pipe,"pid":os.getpid(),"kernel_boot_id":kernel.boot_id,"shadow_launcher_pid":None if worker is None else worker.pid}),encoding="utf-8")
                print("G1 内核已启动：未接入交易通道，新增交易未授权。",flush=True)
                try:
                    try:server.serve()
                    except KeyboardInterrupt:pass
                finally:
                    if worker is not None and worker.poll() is None:
                        worker.terminate();worker.wait(timeout=5)
        elif args.action=="desktop":
            from online_trader.desktop.app import run
            return run(args.pipe)
        else:
            from online_trader.desktop.client import StatusClient
            client=StatusClient(args.pipe)
            result=client.workbench() if args.action=='workbench-status' else client.snapshot() if args.action=="status" else client.pause()
            print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except TraderError as exc:
        print(f"{exc.code}: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
