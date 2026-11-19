"""QA-only offline owner for exercising Runtime public IPC without an account connection."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from hashlib import sha256
import json
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.dsv_simulation import DsvSimulationSession
from online_trader.scheduling.calendar import TradingCalendar
from qa.dsv_native_facts import FactClock, NativeFactApi


CONTRACT = 'DCE.p2701'
TRADING_DAY = '2026-11-10'


def _calendar():
    return TradingCalendar.from_record({
        'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'runtime-offline-day',
        'calendar_version': '1', 'complete': True,
        'coverage_from_utc': '2026-11-10T00:00:00Z', 'coverage_to_utc': '2026-11-10T08:00:00Z',
        'contracts': [{'contract_id': CONTRACT, 'sessions': [{
            'session_id': 'day', 'trading_day': TRADING_DAY,
            'open_utc': '2026-11-10T01:00:00Z', 'close_utc': '2026-11-10T07:00:00Z',
            'is_opening_session': True, 'force_close_at_end': True,
        }]}],
    })


def _profile(quantity):
    settings = load_confirmed_baseline()[0].to_settings()
    settings['trade'].update(protect_80_enabled=False, half_close_enabled=True, half_stage2=True,
                             half_breakeven=True, trailing_activate_ratio=0,
                             entry_slip_abort=False, no_chase_after_act=False)
    next(row for row in settings['symbols'] if row['code'] == CONTRACT)['order_volume'] = quantity
    return DsvProfile.from_settings(settings)


def _grant(path, profile, calendar):
    value = {
        'schema_version': 'KQ_DSV_SIM/1', 'grant_id': 'runtime-offline-' + uuid.uuid4().hex,
        'account_fingerprint': uuid.uuid4().hex[:16], 'ledger_path': str(path.resolve()),
        'contracts': [CONTRACT], 'trading_day': TRADING_DAY,
        'not_before_utc': '2026-11-10T01:00:00Z', 'not_after_utc': '2026-11-10T07:00:00Z',
        'max_order_quantity': 10, 'opening_attempts': 20, 'risk_budget': '1000000',
        'profile_hash': profile.content_hash, 'calendar_hash': calendar.content_hash,
        'authorization_reference': 'OFFLINE_RUNTIME_QA_NO_ACCOUNT_AUTHORITY',
    }
    return DsvSimulationGrant.parse(canonical_bytes(value))


class OfflineRuntimeFixture:
    """Create native-shaped facts; product assertions must use Runtime public clients."""

    def __init__(self, root, quantity=5):
        self.root = Path(root)
        self.path = self.root / 'ledger.db'
        self.clock = FactClock()
        self.calendar = _calendar()
        self.profile = _profile(quantity)
        self.grant = _grant(self.path, self.profile, self.calendar)
        self.api = NativeFactApi(self.clock, (CONTRACT,))
        self.port = TqKqDsvPort(self.api, self.api._account, self.grant, self.clock)
        # This QA-only adapter represents a completed local broker sync. No network is opened.
        self.port.broker_connected = True
        self.port.configure_calendar(self.calendar)
        self.kernel = Kernel(self.path, self.grant.account_key, port=self.port,
                             simulation_grant=self.grant, risk_budget='1000000', max_quote_age_ms=30_000)
        self.kernel.simulation.initialize()
        self.session = DsvSimulationSession(self.kernel, self.root / 'owner', profile=self.profile,
                                            calendar=self.calendar, clock=self.clock, emit=lambda _event: None)
        self.api.tick(CONTRACT, at='2026-11-10T01:00:00Z', last=1040)
        self.api.tick(CONTRACT, last=1040)
        self.session.prime()
        self.kernel.simulation.arm()
        self.kernel.wait_idle()

    @property
    def account_key(self):
        return self.grant.account_key

    def quote(self, price=1055, milliseconds=1000, **values):
        self.clock.advance(milliseconds)
        self.api.tick(CONTRACT, last=price, **values)
        self.session.step()

    def fill(self, order_id, quantity, price, *, finish=True):
        self.api.fill(order_id, quantity, price, finish=finish)
        self.session.step()

    def operator(self, operations):
        token = token_urlsafe(48)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
        private = self.root / 'private'
        private.mkdir(exist_ok=True)
        record = {'schema_version': 'ONLINE_TRADER_OPERATORS/1', 'operators': [{
            'id': 'runtime-qa', 'token_sha256': sha256(token.encode()).hexdigest(),
            'account_keys': [self.account_key], 'operations': sorted(operations),
            'expires_at_utc': expiry,
        }]}
        (private / 'operators.json').write_text(json.dumps(record), encoding='utf-8')
        return 'runtime-qa', token

    def close(self):
        self.session.close()
        self.kernel.close()
