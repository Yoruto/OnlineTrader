"""SimNow through the pinned TqSdk trade service, with the same TQ market feed.

There is deliberately no configurable real broker, front or URL. No SDK import,
credential access or connection happens until explicit connect().
"""
import hashlib
from importlib.metadata import version,PackageNotFoundError
from pathlib import Path
import time

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import number
from online_trader.gateway.tqkq_dsv import TqKqDsvPort


def account_fingerprint(username):
    if (type(username) is not str or not 1<=len(username)<=256 or not username.strip()
            or username!=username.strip() or any(ord(c)<32 for c in username)):
        raise TraderError('E_CREDENTIALS','An exact SimNow account identity is required')
    return hashlib.sha256(('simnow\0'+username).encode()).hexdigest()[:16]


def credentials(path):
    """Explicit external JSON only. Never include its values in errors/reports."""
    try:
        from online_trader.accounts import StoredSimNowCredential
        if type(path) is StoredSimNowCredential:value=path.resolve()
        else:
            with Path(path).open('rb') as stream:raw=stream.read(16385)
            if len(raw)>16384:raise ValueError()
            value=strict_loads(raw)
        if type(value) is dict and value.get('schema_version')=='SIMNOW_CREDENTIAL_REFERENCE/1':
            if value.keys()!={'schema_version','account_fingerprint'}:raise ValueError()
            from online_trader.accounts import WindowsVault
            import re
            fingerprint=value['account_fingerprint']
            if type(fingerprint) is not str or not re.fullmatch('[0-9a-f]{16}',fingerprint):raise ValueError()
            vault=WindowsVault();prefix='OnlineTrader:SimNow:'+fingerprint
            simnow=vault.get(prefix+':trade');tq=vault.get(prefix+':market')
            value={'simnow_username':simnow['username'],'simnow_password':simnow['password'],
                   'kq_username':tq['username'],'kq_password':tq['password']}
        fields={'simnow_username','simnow_password','kq_username','kq_password'}
        if (type(value) is not dict or value.keys()!=fields
                or any(type(v) is not str or not 1<=len(v)<=1024 for v in value.values())):raise ValueError()
        account_fingerprint(value['simnow_username'])
        return value
    except (OSError,ValueError,TypeError,KeyError,TraderError):
        raise TraderError('E_CREDENTIALS','Expected the four explicit external SimNow/TQ credential fields') from None


def save_credential_reference(output):
    """Explicit local setup: getpass and Windows vault, with no SDK/login."""
    from getpass import getpass
    from online_trader.accounts import WindowsVault
    from online_trader.configuration.files import publish_new_bytes
    from online_trader.contracts.codec import canonical_bytes
    output=Path(output)
    if output.exists():raise TraderError('E_CREDENTIALS','Credential reference output already exists')
    username=getpass('SimNow账号（仅本地保存，不登录）: ')
    fingerprint=account_fingerprint(username)
    password=getpass('SimNow密码: ');market_user=getpass('快期行情用户名: ');market_password=getpass('快期行情密码: ')
    if not market_user:raise TraderError('E_CREDENTIALS','An explicit TQ market login identity is required')
    vault=WindowsVault();prefix='OnlineTrader:SimNow:'+fingerprint
    vault.put(prefix+':trade',username,password);vault.put(prefix+':market',market_user,market_password)
    value={'schema_version':'SIMNOW_CREDENTIAL_REFERENCE/1','account_fingerprint':fingerprint}
    publish_new_bytes(canonical_bytes(value),output)
    return value|{'broker_connected':False,'credential_values_exported':False}


class SimNowDsvPort(TqKqDsvPort):
    provider='TQ_SIMNOW'
    trade_prefix='sn-'

    @classmethod
    def connect(cls,path,grant,clock,*,read_only=False):
        if getattr(grant,'provider',None)!='TQ_SIMNOW':
            raise TraderError('E_ONLINE_SCOPE','SimNow requires its own finite strategy scope')
        auth=credentials(path)
        if account_fingerprint(auth['simnow_username'])!=grant.value['account_fingerprint']:
            raise TraderError('E_SCOPE','SimNow identity differs from its reviewed scope')
        try:
            if version('tqsdk')!='3.10.4':raise TraderError('E_DEPENDENCY','SimNow requires the reviewed TqSdk 3.10.4')
        except PackageNotFoundError:
            raise TraderError('E_DEPENDENCY','The optional pinned simulation SDK is required') from None
        try:
            from tqsdk import TqApi,TqAccount,TqAuth
        except ImportError:
            raise TraderError('E_DEPENDENCY','The optional pinned simulation SDK is required') from None
        api=None
        try:
            # The SDK resolves its supported SimNow service. Never accept a
            # caller-supplied broker/front/server or fall back to KQ/TqSim.
            account=TqAccount('simnow',auth['simnow_username'],auth['simnow_password'])
            if type(account) is not TqAccount or account._broker_id!='simnow':
                raise TraderError('E_SCOPE','Only the SDK SimNow broker is permitted')
            api=TqApi(account=account,auth=TqAuth(auth['kq_username'],auth['kq_password']),
                      debug=False,web_gui=False,disable_print=True)
            result=cls(api,account,grant,clock);result.read_only=read_only
            deadline=time.monotonic()+25
            while (not result.complete() or number(getattr(api.get_account(account=account),'balance',None)) is None
                    or any(number(getattr(q,'price_tick',None)) is None for q in result.quotes.values())):
                if time.monotonic()>=deadline:raise TraderError('E_FACT_BARRIER','SimNow full synchronization is incomplete')
                result.poll_updates(.2)
            result.broker_connected=True
            return result
        except Exception:
            if api is not None:
                try:api.close()
                except Exception:pass
            raise TraderError('E_GATEWAY_CONNECT','SimNow login or synchronization failed; credentials are not echoed') from None


def strategy_port(grant):
    return SimNowDsvPort if grant.provider=='TQ_SIMNOW' else TqKqDsvPort
