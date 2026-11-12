"""Review and launch a finite DSV simulation scope after explicit authorization."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import subprocess

from online_trader import __version__
from online_trader.contracts.codec import content_hash,canonical_bytes,strict_loads
from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError
from online_trader.runtime.comparison import verify_dsv_profile
from online_trader.runtime.dsv_simulation import load_inputs
from online_trader.runtime.executable import bundled_cli


class SessionLauncher:
    def __init__(self,store,account_id,scope,profile,calendar):
        self.store=store;self.account_id=account_id
        self.paths=[Path(path).resolve() for path in (scope,profile,calendar)]
        self.packet=None

    @classmethod
    def for_simnow(cls,packet):
        packet=Path(packet).resolve()
        launcher=cls(None,None,*(packet/name for name in ('scope.json','profile.json','calendar.json')))
        launcher.packet=packet
        return launcher

    @staticmethod
    def _record(path):
        with path.open('rb') as stream:raw=stream.read(8193)
        if len(raw)>8192:raise TraderError('E_ONLINE_SCOPE','Desktop launch reference exceeds its size bound')
        return strict_loads(raw)

    def _simnow_binding(self,grant,profile):
        reference=self._record(self.packet/'simnow-credentials.json')
        if (grant.provider!='TQ_SIMNOW' or grant.manual_workbench or grant.automatic_offsets
                or reference!={'schema_version':'SIMNOW_CREDENTIAL_REFERENCE/1',
                               'account_fingerprint':grant.value['account_fingerprint']}
                or Path(grant.value['ledger_path']).resolve()!=self.packet/'ledger.db'):
            raise TraderError('E_SCOPE','Desktop SimNow packet must bind its own ledger, reference and fixed strategy settings')
        comparison=self._record(self.packet/'dsv-comparison.json')
        if (type(comparison) is not dict or set(comparison)!={'config','log_dir'}
                or any(type(path) is not str or not Path(path).is_absolute() for path in comparison.values())):
            raise TraderError('E_COMPARISON_LOG','Desktop comparison needs explicit DSV config and log paths')
        verify_dsv_profile(comparison['config'],profile)
        if not Path(comparison['log_dir']).is_dir():raise TraderError('E_COMPARISON_LOG','DSV log directory is missing')
        return {'pipe':'OnlineTrader.SimNow.'+reference['account_fingerprint'],
                'dsv_config':comparison['config'],'dsv_log_dir':comparison['log_dir']}

    def review(self):
        grant,profile,calendar=load_inputs(*self.paths)
        if self.packet is not None:binding=self._simnow_binding(grant,profile)
        else:
            row=self.store.get(self.account_id)
            if self.store.read()['selected_id']!=self.account_id:
                raise TraderError('E_SCOPE','Only the selected account can be launched from this workbench')
            if row['account_key']!=grant.account_key or self.store.ledger_path(row)!=Path(grant.value['ledger_path']).resolve():
                raise TraderError('E_SCOPE','Account/ledger differs from the exact finite scope')
            binding={'pipe':self.store.pipe_name(row)}
        value=grant.value
        login_from=datetime.fromisoformat(value['not_before_utc'])
        if self.packet is not None:login_from-=timedelta(minutes=15)
        result={'account_key':grant.account_key,'scope_hash':grant.content_hash,'profile_hash':profile.content_hash,
            'calendar_hash':calendar.content_hash,'contracts':value['contracts'],'trading_day':value['trading_day'],
            'not_before_utc':value['not_before_utc'],'not_after_utc':value['not_after_utc'],
            'login_from_utc':login_from.isoformat(),
            'opening_attempts':value['opening_attempts'],'max_order_quantity':value['max_order_quantity'],
            'scope_schema':value['schema_version'],'trading_days':grant.trading_days,
            'manual_workbench':grant.manual_workbench,
            'resume_entries_on_day_change':value.get('resume_entries_on_day_change',False),
            'entry_authority':'DISARMED','broker_connected':False}|binding
        if self.packet is not None:
            settings=profile.to_record()['settings']
            result['schedule']=settings['schedule']
            result['auto_start_on_launch']=settings['trade']['auto_start_on_launch']
        return result|{'review_hash':content_hash(result)}

    def scheduled_trigger(self, *, expected_review_hash, now):
        """Apply DSV's local minute and near-session rules inside a reviewed scope."""
        if self.packet is None or now.tzinfo is None:
            raise TraderError('E_ONLINE_SCOPE','Timed desktop launch needs an exact paired scope and clock')
        review=self.review()
        if review['review_hash']!=expected_review_hash:
            raise TraderError('E_REVISION_CONFLICT','The scheduled scope or reviewed inputs changed')
        if now>=datetime.fromisoformat(review['not_after_utc']):
            raise TraderError('E_ONLINE_SCOPE','The scheduled scope has expired')
        if now<datetime.fromisoformat(review['login_from_utc']):
            return None
        grant,profile,calendar=load_inputs(*self.paths)
        if (grant.content_hash,profile.content_hash,calendar.content_hash)!=(
                review['scope_hash'],review['profile_hash'],review['calendar_hash']):
            raise TraderError('E_REVISION_CONFLICT','The scheduled scope or reviewed inputs changed')
        schedule=review['schedule']
        local=now.astimezone(timezone(timedelta(hours=8)))
        minute=local.hour*60+local.minute
        if schedule['enabled']:
            for label in ('day','night'):
                hour,minutes=map(int,schedule[f'{label}_start_time'].split(':'))
                target=hour*60+minutes
                if target<=minute<=target+schedule['catch_up_minutes']:
                    return f'{local.date()}_{label}'
        if review['auto_start_on_launch']:
            at=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
            for contract in review['contracts']:
                context=calendar.query(contract,at)
                if not context['complete']:
                    continue
                session=context['session'] or context['next_session']
                if session is not None and grant.permits_day(session['trading_day']) and (
                        context['is_trading'] or datetime.fromisoformat(session['open_utc'])-now<=timedelta(minutes=30)):
                    return 'AUTO_START_ON_LAUNCH'
        return None

    def launch(self,python,output,*,expected_review_hash,arm=False,now=None):
        review=self.review()
        if review['review_hash']!=expected_review_hash:raise TraderError('E_REVISION_CONFLICT','Scope or inputs changed after review')
        instant=now or datetime.now(timezone.utc)
        if instant.tzinfo is None or not datetime.fromisoformat(review['login_from_utc'])<=instant<datetime.fromisoformat(review['not_after_utc']):
            raise TraderError('E_ONLINE_SCOPE','This finite scope is outside its explicit startup window')
        if type(arm) is not bool:raise TraderError('E_SCHEMA','Initial entry authority must be explicit')
        if arm and review['manual_workbench']:raise TraderError('E_ONLINE_SCOPE','Arm manual entry explicitly from the attached workbench')
        executable=Path(python).resolve()
        if not executable.is_file():raise TraderError('E_DEPENDENCY','Select the installed product SDK interpreter')
        packaged=bundled_cli()
        use_bundled=packaged is not None and executable==packaged.resolve()
        prefix=[str(executable)]
        probe_args=prefix+(['--verify-runtime',__version__] if use_bundled else ['-I','-c',
            "import online_trader,importlib.metadata;assert online_trader.__version__=="+repr(__version__)+";assert importlib.metadata.version('tqsdk')=='3.10.4'"])
        # Probe only installed identities; no network, credentials or ledger.
        try:
            probe=subprocess.run(probe_args,
                capture_output=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.TimeoutExpired):raise TraderError('E_DEPENDENCY','Selected interpreter could not complete its offline identity check') from None
        if probe.returncode:raise TraderError('E_DEPENDENCY','Selected interpreter lacks this exact product version and pinned SDK')
        grant,profile,calendar=load_inputs(*self.paths)
        if (grant.content_hash!=review['scope_hash'] or profile.content_hash!=review['profile_hash'] or calendar.content_hash!=review['calendar_hash']
                or self.review()['review_hash']!=expected_review_hash):
            raise TraderError('E_REVISION_CONFLICT','Reviewed inputs or selected account changed during interpreter verification')
        directory=Path(output).resolve();directory.mkdir(parents=True,exist_ok=False)
        # Launch from the exact reviewed, validated material. Later edits to
        # the user's source files cannot expand the child process's scope.
        frozen=[]
        for name,record in (('scope',grant.value),('profile',profile.to_record()),('calendar',calendar.to_record())):
            frozen.append(publish_new_bytes(canonical_bytes(record),directory/('reviewed-'+name+'.json')))
        publish_new_bytes(canonical_bytes(review),directory/'reviewed-launch.json')
        if self.packet is not None:
            reference={'schema_version':'SIMNOW_CREDENTIAL_REFERENCE/1','account_fingerprint':grant.value['account_fingerprint']}
            credentials=publish_new_bytes(canonical_bytes(reference),directory/'reviewed-credential-reference.json')
            connection=['simnow','run','--credentials',str(credentials),'--dsv-config',review['dsv_config'],'--dsv-log-dir',review['dsv_log_dir']]
        else:connection=['dsv','run','--account-store',str(self.store.directory),'--account-id',self.account_id]
        argv=prefix+([] if use_bundled else ['-I','-X','utf8','-m','online_trader'])+[*connection,
            '--scope',str(frozen[0]),'--profile',str(frozen[1]),'--calendar',str(frozen[2]),
            '--output',str(directory),'--pipe',review['pipe']]
        if arm:argv.append('--arm')
        with (directory/'executor.log').open('xb') as stream:
            child=subprocess.Popen(argv,cwd=directory,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return {'status':'PROCESS_LAUNCHED_NOT_CONNECTED','pid':child.pid,'pipe':review['pipe'],
            'review_hash':review['review_hash'],'entry_arm_requested':arm,'broker_connected':False}
