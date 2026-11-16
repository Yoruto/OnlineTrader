from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from qa.dsv_parity.compare_live_logs import read_rounds


class LiveLogReaderTest(unittest.TestCase):
    def test_writer_can_commit_while_observer_processes_a_record(self):
        directory = self.enterContext(TemporaryDirectory())
        database = Path(directory) / 'facts.db'
        writer = self.enterContext(closing(sqlite3.connect(database, timeout=0.2)))
        writer.execute('PRAGMA journal_mode=DELETE')
        writer.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT, payload TEXT)')
        payloads = tuple((f'{{"round": {index}}}',) for index in range(96))
        writer.executemany("INSERT INTO events (kind, payload) VALUES ('ROUND', ?)", payloads)
        writer.commit()

        # The live SDK's COMMIT must complete even if the observer pauses to process its first record.
        reader = self.enterContext(closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)))
        records = read_rounds(reader)
        self.addCleanup(records.close)
        first = next(records)
        writer.execute("INSERT INTO events (kind, payload) VALUES ('ROUND', '{}')")
        writer.commit()

        # A later append belongs to the next observation, while all previously committed rows remain visible.
        self.assertEqual([first, *records], [(i, value) for i, (value,) in enumerate(payloads, 1)])
        self.assertEqual(writer.execute('SELECT COUNT(*) FROM events').fetchone(), (97,))

    def test_cli_matches_activation_only_in_its_native_contract_date_and_minute(self):
        run = Path(self.enterContext(TemporaryDirectory()))
        logs = run / 'original-dsv' / 'logs'
        logs.mkdir(parents=True)
        day = '2020-07-13'
        contract = 'DCE.p2701'
        # The afternoon DSV capture logged the same activation several seconds after OnlineTrader consumed the minute.
        observations = (
            ('30', '04', 'DCE.p2701:20200713T0530', 'LONG', '9039.4', 'MATCH'),
            ('31', '02', 'DCE.p2701:20200712T0531', 'LONG', '9039.4', 'MISSING_REASON'),
            ('32', '02', None, 'LONG', '9039.4', 'MISSING_REASON'),
            ('33', '02', 'DCE.l2701:20200713T0533', 'LONG', '9039.4', 'MISSING_REASON'),
            ('34', '02', 'DCE.p2701:20200713T0534', 'SHORT', '9039.3', 'DIFFERENT_DETAIL'),
            ('35', '02', 'DCE.p2701:20200713T0536', 'LONG', '9039.4', 'MISSING_REASON'),
        )
        writer = self.enterContext(closing(sqlite3.connect(run / 'candidate.db')))
        writer.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT, payload TEXT)')
        writer.execute('CREATE TABLE native_orders (id INTEGER PRIMARY KEY)')
        writer.execute('CREATE TABLE native_trades (id INTEGER PRIMARY KEY)')
        messages = []
        for minute, second, consume, direction, activation, _ in observations:
            messages.append(f'[13:{minute}:10] [跳过] 棕榈油({contract}) 当日行情已跑过多头激活位'
                            '（触发 8981.0 ∓ 止盈×0.80 = 9039.4），本次不开仓')
            decision = {'contract_id': contract, 'reason': 'NO_CHASE_AFTER_ACT', 'direction': direction,
                        'trigger_price': '8981.0', 'activation_price': activation}
            if consume is not None:
                decision['consume'] = consume
            event = {'input': {'wall_time': f'{day}T13:{minute}:{second}+08:00', 'market': [{'contract_id': contract, 'ticks': [
                        {'candle_id': f'{contract}:20200713T05{minute}'}]}]},
                     'receipt': {'decisions': [decision], 'effects': []}}
            writer.execute("INSERT INTO events (kind, payload) VALUES ('ROUND', ?)", (json.dumps(event),))
        writer.commit()
        (logs / f'trade_{day}.log').write_text('\n'.join(messages), encoding='utf-8')
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'qa.dsv_parity.compare_live_logs',
                                 f'{day}T13:30:05+08:00', f'{day}T13:36:05+08:00', '--run-dir', str(run)],
                                check=False, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['verdict'], 'MISMATCH')
        self.assertEqual(report['candidate_rounds'], len(observations) - 1)
        self.assertEqual([row['status'] for row in report['activation_matches']], [row[-1] for row in observations])
