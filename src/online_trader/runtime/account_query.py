"""One-shot simulation login and account read, independent of execution."""

from datetime import datetime, timezone
import time

from online_trader.accounts import account_identity
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import broker_account_facts, number
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.runtime.startup_metadata import _sdk_factory


def _connect_read_only(credential, row, deadline):
    try:
        from tqsdk import TqAccount, TqKq
    except ImportError:
        raise TraderError('E_DEPENDENCY', 'The optional pinned simulation SDK is required') from None

    secrets = credential.resolve()
    provider = row['provider']
    simnow = provider == 'TQ_SIMNOW'
    username = secrets['simnow_username'] if simnow else secrets['username']
    if account_identity(username, provider)[0] != row['account_key']:
        raise TraderError('E_SCOPE', 'Login credentials differ from the selected simulation account')
    try:
        if simnow:
            account = TqAccount('simnow', username, secrets['simnow_password'])
            if type(account) is not TqAccount or account._broker_id != 'simnow':
                raise TraderError('E_SCOPE', 'The selected account requires the SDK SimNow broker')
            auth = {'username': secrets['kq_username'], 'password': secrets['kq_password']}
        else:
            account = TqKq()
            if type(account) is not TqKq:
                raise TraderError('E_SCOPE', 'The selected account requires the SDK TqKq broker')
            auth = secrets
        api = _sdk_factory(auth, deadline=deadline, account=account)
    except TraderError:
        raise
    except Exception:
        raise TraderError('E_GATEWAY_CONNECT', 'Read-only simulated login or initial synchronization failed') from None
    return api, account


def _read_synchronized(api, account, deadline):
    while time.monotonic() < deadline:
        funds = api.get_account(account=account)
        if (api._account._get_trade_more_data(api._data) is False
                and number(getattr(funds, 'balance', None)) is not None):
            facts = broker_account_facts(api, account)
            if time.monotonic() < deadline:
                return facts
            break
        api.wait_update(deadline=time.time() + min(.2, max(0, deadline - time.monotonic())))
    raise TraderError('E_FACT_BARRIER', 'The simulated account snapshot did not synchronize in time')


def read_current_account(root, *, account=None):
    """Read a stopped runtime's current native account snapshot through its SDK."""
    store = PortableAccountStore(root, account)
    row = store.get(store.read()['selected_id'])
    with AccountLock(row['account_key']):
        deadline = time.monotonic() + 30
        api, native_account = _connect_read_only(store.credential_reference(row['account_id']), row, deadline)
        try:
            facts = _read_synchronized(api, native_account, deadline)
            observed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        except TraderError:
            raise
        except Exception:
            raise TraderError('E_FACT_BARRIER', 'Cannot read the synchronized simulated account snapshot') from None
        finally:
            try:
                api.close()
            except Exception:
                raise TraderError('E_GATEWAY_CLOSE', 'The read-only simulated connection did not close normally') from None
    return {'schema_version': 'ONLINE_TRADER_ACCOUNT_OBSERVATION/1', 'account_key': row['account_key'],
            'environment': row['environment'], 'provider': row['provider'], 'masked_login': row['masked_login'],
            'observed_at_utc': observed_at, 'trading_day': None, 'complete': True,
            'coverage': 'CURRENT_BROKER_SNAPSHOT', **facts}
