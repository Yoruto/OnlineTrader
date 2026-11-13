"""Local account catalogue and Windows user credential vault; never logs in.

Catalogue revisions contain masked identities only. Passwords live in Windows
Credential Manager, outside the append-only journal and configuration exports.
"""
from contextlib import ExitStack
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path
import re
import sqlite3
import uuid

from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock


def _error(message, code='E_ACCOUNT'):
    raise TraderError(code, message)


def _label(value):
    if type(value) is not str or not 1 <= len(value.strip()) <= 64 or any(ord(c) < 32 for c in value):
        _error('Expected a short nonempty display label')
    return value.strip()


def account_identity(username, provider='TQKQ'):
    if type(username) is not str or not username or username != username.strip() or len(username) > 256 or any(ord(c)<32 for c in username):
        _error('Expected an explicit account login identity')
    if provider not in {'TQKQ','TQ_SIMNOW'}:_error('Unsupported simulation account provider')
    source = 'simnow\0' + username if provider == 'TQ_SIMNOW' else username
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    prefix = 'SIM:SIMNOW:' if provider == 'TQ_SIMNOW' else 'SIM:KQ:'
    return prefix + digest, '*' * max(4, len(username)-2) + username[-2:]


def _password(value):
    if type(value) is not str or not 1 <= len(value) <= 1024:
        _error('A nonempty bounded password is required','E_CREDENTIALS')
    return value


class WindowsVault:
    """Generic per-user credentials, persisted locally; no machine/service fallback."""
    class Credential(ctypes.Structure):
        _fields_ = [('Flags',wintypes.DWORD),('Type',wintypes.DWORD),('TargetName',wintypes.LPWSTR),
            ('Comment',wintypes.LPWSTR),('LastWritten',wintypes.FILETIME),('CredentialBlobSize',wintypes.DWORD),
            ('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',wintypes.DWORD),
            ('AttributeCount',wintypes.DWORD),('Attributes',ctypes.c_void_p),('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]

    def __init__(self):
        self.api=ctypes.WinDLL('advapi32',use_last_error=True)
        self.api.CredWriteW.argtypes=[ctypes.POINTER(self.Credential),wintypes.DWORD]
        self.api.CredWriteW.restype=wintypes.BOOL
        self.api.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(self.Credential))]
        self.api.CredReadW.restype=wintypes.BOOL
        self.api.CredDeleteW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD]
        self.api.CredDeleteW.restype=wintypes.BOOL
        self.api.CredFree.argtypes=[ctypes.c_void_p]

    def put(self, target, username, password):
        if type(password) is not str or not password or len(password)>1024:
            _error('A nonempty bounded password is required','E_CREDENTIALS')
        raw=canonical_bytes({'username':username,'password':password})
        blob=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
        item=self.Credential();item.Type=1;item.TargetName=target;item.UserName='OnlineTrader'
        item.CredentialBlobSize=len(raw);item.CredentialBlob=blob;item.Persist=2
        if not self.api.CredWriteW(ctypes.byref(item),0):
            _error('Cannot save credentials for the current Windows user','E_CREDENTIALS')

    def get(self,target):
        pointer=ctypes.POINTER(self.Credential)()
        if not self.api.CredReadW(target,1,0,ctypes.byref(pointer)):
            _error('Saved credentials are unavailable for this Windows user','E_CREDENTIALS')
        try:
            if not 1<=pointer.contents.CredentialBlobSize<=8192:_error('Invalid stored credential size','E_CREDENTIALS')
            return strict_loads(ctypes.string_at(pointer.contents.CredentialBlob,pointer.contents.CredentialBlobSize))
        finally:self.api.CredFree(pointer)

    def delete(self,target):
        if not self.api.CredDeleteW(target,1,0) and ctypes.get_last_error()!=1168:
            _error('Cannot forget credentials for this Windows user','E_CREDENTIALS')


@dataclass(frozen=True,repr=False)
class StoredCredential:
    store: object
    account_id: str

    def __repr__(self):return 'StoredCredential(<masked>)'

    def resolve(self):
        row=self.store.get(self.account_id)
        if row['provider']!='TQKQ':_error('A KQ credential reference cannot read another provider','E_CREDENTIALS')
        value=self.store.vault.get(self.store.target(row))
        if (type(value) is not dict or set(value)!={'username','password'} or type(value['password']) is not str
                or not 1<=len(value['password'])<=1024 or account_identity(value['username'])[0]!=row['account_key']):
            _error('Saved credentials do not match the selected account','E_CREDENTIALS')
        return value


@dataclass(frozen=True,repr=False)
class StoredSimNowCredential:
    store: object
    account_id: str

    def __repr__(self):return 'StoredSimNowCredential(<masked>)'

    def resolve(self):
        row=self.store.get(self.account_id)
        if row['provider']!='TQ_SIMNOW':_error('A SimNow credential reference cannot read another provider','E_CREDENTIALS')
        try:
            trade=self.store.vault.get(self.store.target(row)+':trade')
            market=self.store.vault.get(self.store.target(row)+':market')
            for item in (trade,market):
                if type(item) is not dict or set(item)!={'username','password'}:raise ValueError()
                account_identity(item['username']);_password(item['password'])
            if account_identity(trade['username'],'TQ_SIMNOW')[0]!=row['account_key']:raise ValueError()
            return {'simnow_username':trade['username'],'simnow_password':trade['password'],
                    'kq_username':market['username'],'kq_password':market['password']}
        except (KeyError,TypeError,ValueError,TraderError):
            _error('Saved SimNow credentials do not match the selected account','E_CREDENTIALS')


class AccountStore:
    """CAS append-only catalogue. Active execution owners block edit/delete/switch.

    Account directories and pipe names are derived from immutable identities.
    Removing a profile never removes its ledgers, reports or past evidence.
    """
    def __init__(self,directory,*,vault=None):
        self.directory=Path(directory).resolve()
        self.vault=vault if vault is not None else WindowsVault()

    def read(self):
        state={'revision':0,'previous_hash':'0'*64,'accounts':[],'selected_id':None}
        if (self.directory/'catalogue').exists() and not (self.directory/'catalogue').is_dir():_error('Account catalogue path is not a directory')
        paths=sorted((self.directory/'catalogue').glob('????????.json'))
        if len(paths)>4096:_error('Account revision limit exceeded')
        digest='0'*64
        try:
            for i,path in enumerate(paths,1):
                if path.name!=f'{i:08}.json' or path.stat().st_size>131072:_error('Account revision gap or size limit')
                row=strict_loads(path.read_bytes())
                if (type(row) is not dict or set(row)!={'revision','previous_hash','accounts','selected_id'}
                        or type(row['revision']) is not int or row['revision']!=i or row['previous_hash']!=digest):_error('Account history is inconsistent')
                self._validate(row);state=row;digest=content_hash(row)
        except OSError:_error('Cannot read the selected account catalogue')
        return state|{'journal_hash':digest}

    def _validate(self,state):
        rows=state['accounts']
        if type(rows) is not list or len(rows)>64:_error('Account capacity exceeded')
        keys=set();ids=set()
        for row in rows:
            if (type(row) is not dict or set(row)!={'account_id','alias','provider','environment','account_key','masked_login','broker','line'}
                    or any(type(v) is not str for v in row.values())
                    or not re.fullmatch(r'account-[0-9a-f]{32}',row.get('account_id',''))
                    or row['provider'] not in {'TQKQ','TQ_SIMNOW'} or row['environment']!='ONLINE_SIM'
                    or not re.fullmatch(r'SIM:' + ('SIMNOW' if row['provider']=='TQ_SIMNOW' else 'KQ') + r':[0-9a-f]{16}',row['account_key'])
                    or not re.fullmatch(r'\*{4,254}.{1,2}',row['masked_login'])):_error('Invalid account profile')
            for name in ('alias','broker','line'):_label(row[name])
            if row['account_id'] in ids or row['account_key'] in keys:_error('Duplicate account identity')
            ids.add(row['account_id']);keys.add(row['account_key'])
        if state['selected_id'] is not None and state['selected_id'] not in ids:_error('Selected account is missing')

    def get(self,account_id):
        row=next((r for r in self.read()['accounts'] if r['account_id']==account_id),None)
        if row is None:_error('Selected account profile does not exist')
        return row

    def target(self,row):
        if row['provider']=='TQ_SIMNOW':return 'OnlineTrader:SimNow:'+row['account_key'].split(':')[-1]
        namespace=hashlib.sha256(str(self.directory).casefold().encode()).hexdigest()[:24]
        return 'OnlineTrader/'+namespace+'/'+row['account_id']

    def ledger_path(self,row):
        base=self.directory/'data'
        if row['provider']=='TQ_SIMNOW':base=base/'simnow'
        return base/row['account_key'].split(':')[-1]/'ledger.db'
    def pipe_name(self,row):return 'OnlineTrader.Account.'+hashlib.sha256(str(self.ledger_path(row)).casefold().encode()).hexdigest()[:24]

    def _settled(self,row):
        path=self.ledger_path(row)
        if not path.exists():return
        try:
            connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
            try:
                connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
                scope=connection.execute('SELECT account_key FROM ledger_scope').fetchall()
                if scope!=[(row['account_key'],)]:_error('Account ledger identity differs','E_SCOPE')
                unsettled=connection.execute("SELECT 1 FROM position_cycles WHERE lifecycle!='CLOSED' OR remaining_qty!=0 LIMIT 1").fetchone()
                unknown=connection.execute("SELECT 1 FROM send_attempts WHERE send_state IN ('UNKNOWN','SEND_POSSIBLE') LIMIT 1").fetchone()
                reservations=connection.execute("SELECT 1 FROM reservations WHERE status IN ('HELD','UNKNOWN') LIMIT 1").fetchone()
                if unsettled or unknown or reservations:_error('Original positions, orders or reservations need reconciliation before switching','E_FACT_BARRIER')
            finally:connection.close()
        except sqlite3.Error:_error('Account ledger cannot prove a safe switch','E_FACT_BARRIER')

    def change(self,operation,*,expected_revision,account_id=None,alias=None,username=None,broker=None,line=None,provider='TQKQ'):
        # Catalogue mutex serializes profile and vault operations; each account
        # mutex additionally excludes its current execution owner across sessions.
        if operation=='edit' and username is not None:_error('Account identity is immutable; create a separate profile')
        with AccountLock('CATALOGUE:'+str(self.directory).casefold()),ExitStack() as locks:
            current=self.read()
            if type(expected_revision) is not int or current['revision']!=expected_revision:
                _error('Account catalogue changed; reload before applying','E_REVISION_CONFLICT')
            rows=current['accounts'];selected=current['selected_id'];row=None
            if operation=='add':
                key,masked=account_identity(username,provider)
                row={'account_id':'account-'+uuid.uuid4().hex,'alias':_label(alias),'provider':provider,'environment':'ONLINE_SIM',
                     'account_key':key,'masked_login':masked,
                     'broker':_label(broker if broker is not None else ('SimNow' if provider=='TQ_SIMNOW' else '快期模拟')),
                     'line':_label(line if line is not None else ('TqAccount / SimNow' if provider=='TQ_SIMNOW' else 'TqKq'))}
                locks.enter_context(AccountLock(key));rows.append(row)
            else:
                row=next((r for r in rows if r['account_id']==account_id),None)
                if row is None:_error('Selected account profile does not exist')
                locks.enter_context(AccountLock(row['account_key']))
                if operation=='select':
                    old=next((r for r in rows if r['account_id']==selected),None)
                    if old is not None and old['account_key']!=row['account_key']:
                        locks.enter_context(AccountLock(old['account_key']));self._settled(old)
                    selected=account_id
                elif operation=='edit':row.update(alias=_label(alias),broker=_label(broker if broker is not None else row['broker']),line=_label(line if line is not None else row['line']))
                elif operation=='delete':
                    self._settled(row)
                    for target in self._credential_targets(row):self.vault.delete(target)
                    rows.remove(row)
                    if selected==account_id:selected=None
                else:_error('Unsupported account catalogue operation')
            record={'revision':current['revision']+1,'previous_hash':current['journal_hash'],'accounts':rows,'selected_id':selected}
            self._validate(record)
            publish_new_bytes(canonical_bytes(record),self.directory/'catalogue'/f"{record['revision']:08}.json")
            return self.read()

    def _credential_targets(self,row):
        target=self.target(row)
        return (target+':trade',target+':market') if row['provider']=='TQ_SIMNOW' else (target,)

    def _credential_values(self,row,username,password,market_username=None,market_password=None):
        if account_identity(username,row['provider'])[0]!=row['account_key']:_error('Credentials belong to another account','E_CREDENTIALS')
        _password(password)
        if row['provider']=='TQ_SIMNOW':
            account_identity(market_username);_password(market_password)
            return [(self.target(row)+':trade',username,password),(self.target(row)+':market',market_username,market_password)]
        if market_username is not None or market_password is not None:_error('KQ credentials cannot include another account','E_CREDENTIALS')
        return [(self.target(row),username,password)]

    def _save_vault_values(self,values):
        """Validate before writing; restore existing entries on a partial failure."""
        original={};changed=[]
        for target,_,_ in values:
            try:original[target]=self.vault.get(target)
            except TraderError as error:
                if error.code!='E_CREDENTIALS':raise
                original[target]=None
        try:
            for target,username,password in values:
                if original[target]=={'username':username,'password':password}:continue
                self.vault.put(target,username,password);changed.append(target)
        except Exception:
            for target in reversed(changed):
                before=original[target]
                if before is None:self.vault.delete(target)
                else:self.vault.put(target,before['username'],before['password'])
            raise

    def save_credentials(self,account_id,username,password,*,market_username=None,market_password=None):
        with AccountLock('CATALOGUE:'+str(self.directory).casefold()):
            row=self.get(account_id)
            with AccountLock(row['account_key']):
                self._save_vault_values(self._credential_values(row,username,password,market_username,market_password))

    def forget_credentials(self,account_id):
        with AccountLock('CATALOGUE:'+str(self.directory).casefold()):
            row=self.get(account_id)
            with AccountLock(row['account_key']):
                for target in self._credential_targets(row):self.vault.delete(target)

    def credential_reference(self,account_id):
        row=self.get(account_id)
        reference=StoredSimNowCredential if row['provider']=='TQ_SIMNOW' else StoredCredential
        return reference(self,account_id)

    def initialize_simulation_accounts(self,values):
        """Explicit local import of two identities; never reads files or connects.

        Existing profiles and selection are retained. Only a catalogue without
        a selected account chooses SimNow; repeated imports append no revision.
        """
        fields={'simnow_username','simnow_password','kq_username','kq_password'}
        if type(values) is not dict or set(values)!=fields:_error('Expected explicit SimNow and KQ credential fields','E_CREDENTIALS')
        for field in ('simnow','kq'):
            account_identity(values[field+'_username'],'TQ_SIMNOW' if field=='simnow' else 'TQKQ')
            _password(values[field+'_password'])
        with AccountLock('CATALOGUE:'+str(self.directory).casefold()),ExitStack() as locks:
            current=self.read();rows=list(current['accounts']);imported=[];writes=[]
            for provider,prefix,alias,broker,line in (
                    ('TQ_SIMNOW','simnow','SimNow 模拟账号','SimNow','TqAccount / SimNow'),
                    ('TQKQ','kq','快期模拟账号','快期模拟','TqKq')):
                username=values[prefix+'_username'];key,masked=account_identity(username,provider)
                locks.enter_context(AccountLock(key))
                row=next((item for item in rows if item['account_key']==key),None)
                if row is None:
                    row={'account_id':'account-'+uuid.uuid4().hex,'alias':alias,'provider':provider,'environment':'ONLINE_SIM',
                         'account_key':key,'masked_login':masked,'broker':broker,'line':line}
                    rows.append(row)
                imported.append(row)
                writes.extend(self._credential_values(row,username,values[prefix+'_password'],
                    values['kq_username'] if provider=='TQ_SIMNOW' else None,
                    values['kq_password'] if provider=='TQ_SIMNOW' else None))
            selected=current['selected_id'] or imported[0]['account_id']
            record={'revision':current['revision']+1,'previous_hash':current['journal_hash'],'accounts':rows,'selected_id':selected}
            self._validate(record)
            self._save_vault_values(writes)
            if rows!=current['accounts'] or selected!=current['selected_id']:
                publish_new_bytes(canonical_bytes(record),self.directory/'catalogue'/f"{record['revision']:08}.json")
            return self.read()
