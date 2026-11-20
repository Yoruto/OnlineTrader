"""QA-only offline owner for exercising Runtime public IPC without an account connection."""

from pathlib import Path
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


def _calendar(contract=CONTRACT):
    return TradingCalendar.from_record({
        'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'runtime-offline-day',
        'calendar_version': '1', 'complete': True,
        'coverage_from_utc': '2026-11-10T00:00:00Z', 'coverage_to_utc': '2026-11-10T08:00:00Z',
        'contracts': [{'contract_id': contract, 'sessions': [{
            'session_id': 'day', 'trading_day': TRADING_DAY,
            'open_utc': '2026-11-10T01:00:00Z', 'close_utc': '2026-11-10T07:00:00Z',
            'is_opening_session': True, 'force_close_at_end': True,
        }]}],
    })


def _profile(quantity, contract=CONTRACT, symbol_changes=None):
    settings = load_confirmed_baseline()[0].to_settings()
    settings['trade'].update(protect_80_enabled=False, half_close_enabled=True, half_stage2=True,
                             half_breakeven=True, trailing_activate_ratio=0,
                             entry_slip_abort=False, no_chase_after_act=False)
    symbol = next(row for row in settings['symbols'] if row['code'] == contract)
    symbol['order_volume'] = quantity
    if symbol_changes:
        symbol.update(symbol_changes)
    return DsvProfile.from_settings(settings)


def _grant(path, profile, calendar, contract=CONTRACT):
    value = {
        'schema_version': 'KQ_DSV_SIM/1', 'grant_id': 'runtime-offline-' + uuid.uuid4().hex,
        'account_fingerprint': uuid.uuid4().hex[:16], 'ledger_path': str(path.resolve()),
        'contracts': [contract], 'trading_day': TRADING_DAY,
        'not_before_utc': '2026-11-10T01:00:00Z', 'not_after_utc': '2026-11-10T07:00:00Z',
        'max_order_quantity': 10, 'opening_attempts': 20, 'risk_budget': '1000000',
        'profile_hash': profile.content_hash, 'calendar_hash': calendar.content_hash,
        'authorization_reference': 'OFFLINE_RUNTIME_QA_NO_ACCOUNT_AUTHORITY',
    }
    return DsvSimulationGrant.parse(canonical_bytes(value))


class OfflineRuntimeFixture:
    """Create native-shaped facts; product assertions must use Runtime public clients."""

    def __init__(self, root, quantity=5, *, contract=CONTRACT, symbol_changes=None,
                 initial_price=1040, initial_high=1090, initial_low=1000):
        self.root = Path(root)
        self.path = self.root / 'ledger.db'
        self.contract = contract
        self.clock = FactClock()
        self.calendar = _calendar(contract)
        self.profile = _profile(quantity, contract, symbol_changes)
        self.grant = _grant(self.path, self.profile, self.calendar, contract)
        self.api = NativeFactApi(self.clock, (contract,))
        self.port = TqKqDsvPort(self.api, self.api._account, self.grant, self.clock)
        # This QA-only adapter represents a completed local broker sync. No network is opened.
        self.port.broker_connected = True
        self.port.configure_calendar(self.calendar)
        self.kernel = Kernel(self.path, self.grant.account_key, port=self.port,
                             simulation_grant=self.grant, risk_budget='1000000', max_quote_age_ms=30_000)
        self.kernel.simulation.initialize()
        self.session = DsvSimulationSession(self.kernel, self.root / 'owner', profile=self.profile,
                                            calendar=self.calendar, clock=self.clock, emit=lambda _event: None)
        initial = {'last': initial_price, 'high': initial_high, 'low': initial_low}
        self.api.tick(contract, at='2026-11-10T01:00:00Z', **initial)
        self.api.tick(contract, **initial)
        self.session.prime()
        self.kernel.simulation.arm()
        self.kernel.wait_idle()

    @property
    def account_key(self):
        return self.grant.account_key

    def quote(self, price=1055, milliseconds=1000, **values):
        self.clock.advance(milliseconds)
        self.api.tick(self.contract, last=price, **values)
        self.session.step()

    def fill(self, order_id, quantity, price, *, finish=True):
        self.api.fill(order_id, quantity, price, finish=finish)
        self.session.step()

    def close(self):
        self.session.close()
        self.kernel.close()
