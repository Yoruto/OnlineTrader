from datetime import datetime, timedelta
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.contracts.validation import validate_message
from online_trader.desktop.runtime_logs import JsonLogTail, executor_log_path, resolve_run
from online_trader.desktop.workbench_layout import display_quote_delay
from online_trader.kernel.workbench import workbench_monitor_view
from online_trader.runtime.market_monitor import DsvMarketMonitor, append_latency
from online_trader.transport.status_client import StatusClient
from qa.dsv_native_facts import FactClock
from qa.replay_dsv_archive import latency_summary
from tests import test_online_dsv


class MarketMonitorTests(unittest.TestCase):
    def setUp(self):
        self.wall = datetime.fromisoformat('2026-11-10T01:06:03.500000Z')
        self.first, self.second = 'DCE.p2701', 'DCE.l2701'
        self.monitor = DsvMarketMonitor((self.first, self.second), test_online_dsv.day_calendar(), diagnostic=True)
        def quote(stamp):
            return SimpleNamespace(datetime=stamp, margin=1_200, last_price=1_000, volume_multiple=10)
        self.port = SimpleNamespace(source_epoch=1, fault=False, clock=FactClock(),
                                    quotes={self.first: quote('2026-11-10 09:06:02.400'),
                                            self.second: quote('2026-11-10 09:06:01.900')})
        self.ns = 0

    def poll(self, seconds=0):
        self.ns += int(seconds * 1_000_000_000)
        self.wall += timedelta(seconds=seconds)
        self.monitor.observe(self.port, returned_ns=self.ns, wall=self.wall)
        return self.monitor.latest['quote_delay_seconds']

    def test_last_changed_contract_wins_then_quiet_interval_is_published(self):
        self.assertEqual(self.poll(), '2.500000')
        self.assertEqual(self.monitor.margin_rates[self.first], '0.12')
        self.assertEqual(self.poll(3), '3.000000')
        self.port.quotes[self.first].datetime = '2026-11-10 09:06:07.100'
        self.port.quotes[self.first].margin = 0
        self.assertEqual(self.poll(), '3.000000')
        self.assertIsNone(self.monitor.margin_rates[self.first])
        self.assertEqual(self.monitor.margin_rates[self.second], '0.12')

    def test_fault_and_new_source_clear_old_cached_quotes(self):
        self.poll()
        self.port.fault = True
        self.assertIsNone(self.poll(1))
        self.assertEqual(self.monitor.margin_rates, {})
        self.port.source_epoch += 1
        self.port.fault = False
        self.assertIsNone(self.poll(1))
        self.port.quotes[self.first].datetime = '2026-11-10 09:06:06.001'
        self.assertEqual(self.poll(1), '0.500000')
        self.assertEqual(self.monitor.margin_rates, {self.first:'0.12'})

    def test_quote_display_boundaries_and_invalid_sample(self):
        self.assertEqual(display_quote_delay(None), '--（需启动策略）')
        for lag, expected in (('3','正常'),('3.000001','偏慢'),('10','偏慢'),
                              ('10.000001','很慢/疑似断流'),('120','很慢/疑似断流'),('120.000001','休市/无行情')):
            with self.subTest(lag=lag):
                self.assertIn(expected, display_quote_delay(lag))
        self.poll()
        self.port.quotes[self.first].datetime = 'bad-time'
        self.poll(1)
        self.assertEqual(self.monitor.latest['quote_status'], '未知')
        self.assertNotIn(self.first, self.monitor.margin_rates)

    def test_closing_protection_is_not_a_lunch_break_and_survives_the_session_end(self):
        code = self.first
        self.monitor = DsvMarketMonitor((code,), test_online_dsv.split_day_calendar())
        for moment, expected in (('03:29:40', False), ('06:59:39', False),
                                 ('06:59:40', True), ('07:00:00', True)):
            with self.subTest(moment=moment):
                self.port.clock.value = datetime.fromisoformat(f'2026-11-10T{moment}Z')
                self.wall = self.port.clock.value
                if moment != '07:00:00':
                    self.port.quotes[code].datetime = self.wall.isoformat()
                self.poll()
                self.assertEqual(self.monitor.closing_protection(code, 20), expected)

    def test_diagnostics_require_same_tick_and_source_epoch(self):
        self.poll()
        event = {'contract_id':self.first, 'source_epoch':1, 'source_time_utc':'2026-11-10T01:06:02.400Z',
                 'native_tick_id':'44', 'trading_day':'2026-11-10', 'session_id':'day'}
        self.monitor.processed(event | {'source_epoch':2}, 5, 8, self.port.clock)
        self.monitor.processed(event | {'source_time_utc':'2026-11-10T01:06:02.401Z'}, 5, 8, self.port.clock)
        self.monitor.processed(event, 5, 8, self.port.clock)
        ticks = self.monitor.diagnostics(10)['ticks']
        self.assertEqual([(tick['native_tick_id'],tick['strategy_end_monotonic_ns']) for tick in ticks],[('44','8')])

    def test_latency_summary_matches_first_display_and_reports_missing_quotes(self):
        at = '2026-11-10T01:00:01Z'
        tick = {'contract_id':self.first, 'trading_day':'2026-11-10','session_id':'day','source_epoch':1,
                'native_tick_id':'41','native_time_ns':'1794272401000000000','source_time_utc':at,
                'sdk_returned_at_utc':'2026-11-10T01:00:01.1Z','sdk_returned_monotonic_ns':'100000000',
                'strategy_start_monotonic_ns':'110000000','strategy_end_monotonic_ns':'120000000',
                'clock':{'reference_minus_system_ms':'50'},'phase':'OPEN'}
        owned = {'account_key':'a','kernel_boot_id':'boot','source_epoch':1,'trading_day':'2026-11-10',
                 'snapshot_monotonic_ns':'130000000','ticks':[tick, tick | {'native_tick_id':'42'}],
                 'symbols':[{'contract_id':self.second,'active':True,'quote_age_seconds':None}]}
        displayed = owned | {'snapshot_monotonic_ns':'145000000','received_monotonic_ns':'150000000',
                             'qt_rendered_monotonic_ns':'155000000','ticks':[tick]}
        actual = latency_summary([owned], [displayed | {'kernel_boot_id':'old'},
            displayed | {'qt_rendered_monotonic_ns':'185000000'}, displayed])
        opening, quiet = actual
        self.assertEqual((opening['executor_samples'],opening['desktop_matches'],opening['match_ratio']), (2,1,0.5))
        self.assertEqual(opening['latency_ms']['source_to_sdk_reference_wall']['p95'], 150.0)
        self.assertEqual(opening['latency_ms']['strategy']['max'], 10.0)
        self.assertEqual(opening['latency_ms']['desktop_to_qt']['max'], 5.0)
        self.assertEqual((quiet['phase'],quiet['snapshots_without_quote']), ('QUIET',1))

    def test_monitor_view_prioritizes_forced_exit_over_stopped_and_does_not_use_hold_margin(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.entered(trade={'half_close_enabled':False, 'stale_close_day_stop':True, 'tick_diag':True})
        code = case.code
        case.api.quotes[code].margin = 1055
        case.quote()
        snapshot = workbench_monitor_view(case.kernel)
        self.assertEqual(snapshot['symbols'][0]['real_margin_rate'], '0.1')
        self.assertEqual(snapshot['symbols'][0]['state'], 'ACTIVE')
        self.assertEqual(snapshot['symbols'][0]['position_quantity'], 5)
        cycle = copy.deepcopy(case.cycle())
        cycle['protection'].update(exit_reasons=['DATA_GAP_EXIT'], block_new_for_trading_day=True)
        case.kernel._stage_cycle(cycle, 'TX_PROTECT', 'UNIT_FORCED_EXIT')
        case.kernel.wait_idle()
        case.session.step()
        snapshot = workbench_monitor_view(case.kernel)
        self.assertEqual([row['state'] for row in snapshot['symbols']], ['PENDING'])
        self.assertEqual(snapshot['symbols'][0]['opening_orders'], 1)
        validate_message(StatusClient._message('WorkbenchMonitorSnapshot',
            {'payload':snapshot,'content_hash':content_hash(snapshot)}))

    def test_latency_files_rotate_with_bounded_size(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'monitor.jsonl'
            for index in range(30):
                append_latency(path, {'index':index,'data':'x'*25}, capacity=150)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)
            self.assertTrue(all(path.stat().st_size <= 150 for path in Path(directory).iterdir()))


class RuntimeLogUnitTests(unittest.TestCase):
    def test_start_and_old_owner_attach_require_matching_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run = root/'desktop-a'/'run'
            comparison = run/'comparison-a'
            view = {'account_key':'account-a','kernel_boot_id':'boot-a','configuration':{}}
            manifest = {'schema_version':'DSV_COMPARISON/1','events_file':'events.jsonl',
                        'account_key':'account-a','kernel_boot_id':'boot-a'}
            self.assertEqual(resolve_run(None, [run]), run)
            comparison.mkdir(parents=True)
            (comparison/'manifest.json').write_bytes(canonical_bytes(manifest))
            self.assertEqual(resolve_run(None, [run]), run)
            self.assertEqual(resolve_run(None, [], finished=('account-a','boot-a',run)), run)
            self.assertEqual(resolve_run(view, [], account_root=root), run)
            self.assertIsNone(resolve_run(view | {'kernel_boot_id':'boot-b'}, [run]))
            self.assertIsNone(resolve_run(view | {'account_key':'account-b'}, [run]))
            supplied = view | {'configuration':{'run_directory':str(run)}}
            self.assertEqual(resolve_run(supplied, []), run)
            self.assertIsNone(resolve_run(supplied | {'account_key':'account-b'}, []))
            self.assertIsNone(resolve_run(supplied | {'kernel_boot_id':'boot-b'}, []))
            (run/'comparison-older').mkdir()
            self.assertIsNone(resolve_run(supplied, [run]))
            self.assertIsNone(resolve_run(None, [], finished=('account-a','boot-a',run)))
            (run/'run-identity.json').write_bytes(canonical_bytes({
                'schema_version':'DSV_RUN_IDENTITY/1', 'account_key':'account-a',
                'kernel_boot_id':'boot-a', 'run_directory':str(run)}))
            self.assertEqual(resolve_run(supplied, []), run)
            self.assertIsNone(resolve_run(supplied | {'account_key':'account-b'}, [run]))
            self.assertEqual(executor_log_path(run), run.with_name('run.executor.log'))
            (run/'executor.log').touch()
            self.assertEqual(executor_log_path(run), run/'executor.log')

    def test_incremental_read_half_line_clear_and_atomic_replace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'run.executor.log'
            path.write_bytes(b'{"i":1}\n{"i"')
            tail = JsonLogTail(1_024)
            self.assertEqual(list(tail.read(path)), [{'i':1}])
            with path.open('ab') as stream:
                stream.write(b':2}\n{corrupt}\n{"i":3')
            self.assertEqual(list(tail.read(path)), [{'i':1},{'i':2}])
            tail.clear(path)
            with path.open('ab') as stream:
                stream.write(b'}\n{"i":4}\n')
            self.assertEqual(list(tail.read(path)), [{'i':4}])
            replacement = path.with_suffix('.tmp')
            replacement.write_bytes(b'{"i":5}\n')
            replacement.replace(path)
            self.assertEqual(list(tail.read(path)), [{'i':5}])
