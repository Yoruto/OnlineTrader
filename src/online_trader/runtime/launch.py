"""Review and explicit launch of an existing finite DSV simulation scope.

No default grant, account lookup, SDK connection or automatic arming occurs.
The child process uses the original product run/owner/lock and survives GUI close.
"""
from datetime import datetime,timezone
from pathlib import Path
import subprocess

from online_trader import __version__
from online_trader.contracts.codec import content_hash,canonical_bytes
from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError
from online_trader.runtime.dsv_simulation import load_inputs


class SessionLauncher:
    def __init__(self,store,account_id,scope,profile,calendar):
        self.store=store;self.account_id=account_id
        self.paths=[Path(path).resolve() for path in (scope,profile,calendar)]

    def review(self):
        row=self.store.get(self.account_id);grant,profile,calendar=load_inputs(*self.paths)
        if self.store.read()['selected_id']!=self.account_id:
            raise TraderError('E_SCOPE','Only the selected account can be launched from this workbench')
        if row['account_key']!=grant.account_key or self.store.ledger_path(row)!=Path(grant.value['ledger_path']).resolve():
            raise TraderError('E_SCOPE','Account/ledger differs from the exact finite scope')
        value=grant.value
        result={'account_key':row['account_key'],'scope_hash':grant.content_hash,'profile_hash':profile.content_hash,
            'calendar_hash':calendar.content_hash,'contracts':value['contracts'],'trading_day':value['trading_day'],
            'not_before_utc':value['not_before_utc'],'not_after_utc':value['not_after_utc'],
            'opening_attempts':value['opening_attempts'],'max_order_quantity':value['max_order_quantity'],
            'scope_schema':value['schema_version'],'trading_days':grant.trading_days,
            'resume_entries_on_day_change':value.get('resume_entries_on_day_change',False),
            'pipe':self.store.pipe_name(row),'entry_authority':'DISARMED','broker_connected':False}
        return result|{'review_hash':content_hash(result)}

    def launch(self,python,output,*,expected_review_hash,arm=False,now=None):
        review=self.review()
        if review['review_hash']!=expected_review_hash:raise TraderError('E_REVISION_CONFLICT','Scope or inputs changed after review')
        instant=now or datetime.now(timezone.utc)
        if instant.tzinfo is None or not datetime.fromisoformat(review['not_before_utc'])<=instant<=datetime.fromisoformat(review['not_after_utc']):
            raise TraderError('E_ONLINE_SCOPE','This finite scope is outside its explicit startup window')
        if type(arm) is not bool:raise TraderError('E_SCHEMA','Initial entry authority must be explicit')
        executable=Path(python).resolve()
        if not executable.is_file():raise TraderError('E_DEPENDENCY','Select the installed product SDK interpreter')
        # Probe only installed identities; no network, credentials or ledger.
        try:
            probe=subprocess.run([str(executable),'-I','-c',
                "import online_trader,importlib.metadata;assert online_trader.__version__=="+repr(__version__)+";assert importlib.metadata.version('tqsdk')=='3.10.4'"],
                capture_output=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.TimeoutExpired):raise TraderError('E_DEPENDENCY','Selected interpreter could not complete its offline identity check') from None
        if probe.returncode:raise TraderError('E_DEPENDENCY','Selected interpreter lacks this exact product version and pinned SDK')
        grant,profile,calendar=load_inputs(*self.paths)
        if (grant.content_hash!=review['scope_hash'] or profile.content_hash!=review['profile_hash'] or calendar.content_hash!=review['calendar_hash']
                or self.store.read()['selected_id']!=self.account_id):
            raise TraderError('E_REVISION_CONFLICT','Reviewed inputs or selected account changed during interpreter verification')
        directory=Path(output).resolve();directory.mkdir(parents=True,exist_ok=False)
        # Launch from the exact reviewed, validated material. Later edits to
        # the user's source files cannot expand the child process's scope.
        frozen=[]
        for name,record in (('scope',grant.value),('profile',profile.to_record()),('calendar',calendar.to_record())):
            frozen.append(publish_new_bytes(canonical_bytes(record),directory/('reviewed-'+name+'.json')))
        publish_new_bytes(canonical_bytes(review),directory/'reviewed-launch.json')
        argv=[str(executable),'-I','-X','utf8','-m','online_trader','dsv','run',
            '--account-store',str(self.store.directory),'--account-id',self.account_id,
            '--scope',str(frozen[0]),'--profile',str(frozen[1]),'--calendar',str(frozen[2]),
            '--output',str(directory),'--pipe',review['pipe']]
        if arm:argv.append('--arm')
        with (directory/'executor.log').open('xb') as stream:
            child=subprocess.Popen(argv,cwd=directory,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return {'status':'PROCESS_LAUNCHED_NOT_CONNECTED','pid':child.pid,'pipe':review['pipe'],
            'review_hash':review['review_hash'],'entry_arm_requested':arm,'broker_connected':False}
