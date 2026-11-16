"""Adversarial live-log fixtures: matching a later notice cannot hide bad state."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from qa.dsv_parity.compare_live_logs import compare

CODE = 'DCE.p2701'
DAY = '2020-07-13'
BAR = CODE + ':20200713T0530'


def event(second='05', minute='30', consume=BAR, **changes):
    decision = {'contract_id': CODE, 'reason': 'NO_CHASE_AFTER_ACT', 'direction': 'LONG',
                'trigger_price': '8981.0', 'activation_price': '9039.4', 'consume': consume}
    decision.update(changes)
    return {'input': {'wall_time': f'{DAY}T13:{minute}:{second}+08:00', 'session_id': 'one',
                     'market': [{'contract_id': CODE, 'ticks': [{'candle_id': CODE + ':20200713T05' + minute}]}]},
            'receipt': {'decisions': [decision], 'effects': []}}


class StrictLiveLogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        logs = self.root / 'original-dsv/logs'
        logs.mkdir(parents=True)
        self.log = logs / f'trade_{DAY}.log'
        self.log.write_text(f'[13:30:10] [跳过] 棕榈油({CODE}) 当日行情已跑过多头激活位'
                            '（触发 8981.0 ∓ 止盈×0.80 = 9039.4），本次不开仓', encoding='utf-8')
        with closing(sqlite3.connect(self.root / 'candidate.db')) as db:
            db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT, payload TEXT)')
            db.execute('CREATE TABLE native_orders (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE native_trades (id INTEGER PRIMARY KEY)')
        self.start, self.end = map(datetime.fromisoformat, (DAY + 'T13:30:00+08:00', DAY + 'T13:32:00+08:00'))

    def report(self, *events):
        with closing(sqlite3.connect(self.root / 'candidate.db')) as db:
            db.executemany("INSERT INTO events(kind,payload) VALUES('ROUND',?)", [(json.dumps(e),) for e in events])
            db.commit()
        return compare(self.root, self.start, self.end)

    def cli(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', '-m', 'qa.dsv_parity.compare_live_logs',
            self.start.isoformat(), self.end.isoformat(), '--run-dir', str(self.root), *args],
            capture_output=True, encoding='utf-8', timeout=10)

    def test_single_consumption_matches_at_explicitly_limited_precision(self):
        report = self.report(event())
        self.assertEqual(report['verdict'], 'MATCH_AT_LOG_PRECISION')
        self.assertEqual(report['consumption_issues'], [])
        self.assertEqual(self.cli().returncode, 0)

    def test_repeated_same_minute_is_not_a_set_membership_match(self):
        report = self.report(event(), event(second='09'))
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(report['activation_matches'][0]['status'], 'DUPLICATE_CONSUMPTION')
        self.assertEqual(report['consumption_issues'][0]['event_seq'], 2)

    def test_wrong_direction_then_right_direction_cannot_hide_first_decision(self):
        report = self.report(event(direction='SHORT'), event(second='09'))
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(report['activation_matches'][0]['status'], 'DUPLICATE_CONSUMPTION')

    def test_right_direction_then_wrong_direction_is_also_a_conflict(self):
        report = self.report(event(), event(second='09', direction='SHORT'))
        self.assertEqual(report['verdict'], 'MISMATCH')

    def test_consumed_bar_must_match_actual_market_candle(self):
        report = self.report(event(minute='31'))
        self.assertEqual(report['activation_matches'][0]['status'], 'INVALID_CANDIDATE')
        self.assertEqual(report['consumption_issues'][0]['code'], 'CANDLE_ID_MISMATCH')

    def test_missing_market_evidence_is_not_a_match(self):
        value = event(); value['input']['market'] = []
        report = self.report(value)
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(report['consumption_issues'][0]['code'], 'CANDLE_EVIDENCE_MISSING')

    def test_native_candle_can_carry_forward_within_same_feed_session(self):
        first = event(); first['receipt']['decisions'] = []
        second = event(second='09'); second['input']['market'][0]['ticks'] = []
        self.assertEqual(self.report(first, second)['verdict'], 'MATCH_AT_LOG_PRECISION')

    def test_candle_evidence_cannot_carry_across_feed_session(self):
        first = event(); first['receipt']['decisions'] = []
        second = event(second='09'); second['input']['session_id'] = 'new'
        second['input']['market'][0]['ticks'] = []
        self.assertEqual(self.report(first, second)['verdict'], 'MISMATCH')

    def test_precise_candidate_price_is_not_rounded_into_a_match(self):
        report = self.report(event(activation_price='9039.44'))
        self.assertEqual(report['activation_matches'][0]['status'], 'DIFFERENT_DETAIL')

    def test_decimal_formatting_without_value_change_matches(self):
        self.assertEqual(self.report(event(trigger_price='8981', activation_price='9039.40'))['verdict'],
                         'MATCH_AT_LOG_PRECISION')

    def test_invalid_price_is_reported_not_silently_discarded(self):
        report = self.report(event(activation_price='NaN'))
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(report['consumption_issues'][0]['code'], 'INVALID_ACTIVATION_DETAIL')

    def test_boolean_price_is_invalid_even_when_decimal_would_accept_it(self):
        report = self.report(event(activation_price=True))
        self.assertEqual(report['consumption_issues'][0]['code'], 'INVALID_ACTIVATION_DETAIL')

    def test_no_reference_notices_are_insufficient_not_pass(self):
        self.log.write_text('', encoding='utf-8')
        self.assertEqual(self.report(event())['verdict'], 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(self.cli().returncode, 2)

    def test_missing_candidate_notice_returns_nonzero_unless_report_only(self):
        report = self.report()
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(self.cli().returncode, 1)
        result = self.cli('--report-only')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['verdict'], 'MISMATCH')

    def test_window_can_match_a_single_earlier_observation_in_the_same_minute(self):
        self.start = datetime.fromisoformat(DAY + 'T13:30:07+08:00')
        report = self.report(event(second='04'))
        self.assertEqual(report['verdict'], 'MATCH_AT_LOG_PRECISION')
        self.assertEqual(report['candidate_rounds'], 0)

    def test_duplicate_candidate_without_reference_still_reported(self):
        self.log.write_text('', encoding='utf-8')
        report = self.report(event(), event(second='09'))
        self.assertEqual(report['verdict'], 'MISMATCH')


if __name__ == '__main__':
    unittest.main()
