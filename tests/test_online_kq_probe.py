from datetime import datetime,timedelta
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sqlite3,tempfile,unittest
from unittest.mock import patch
from qa.online_kq_one_minute import Journal,RoundTrip,UTC8,fresh_quote,limit_price
from qa.online_clock_reference import ClockReference


class MemoryJournal:
    def __init__(self):self.events=[]
    def append(self,kind,payload):self.events.append((kind,payload))


class OnlineProbePolicyTests(unittest.TestCase):
    def setUp(self):self.journal=MemoryJournal();self.state=RoundTrip(self.journal)
    def opened(self):
        self.state.prepare_open("DCE.m2701",Decimal("3078"),position_long=0,position_short=0,active_orders=0)
        self.state.observe_open(traded=1,reported=1,terminal=True,now_ns=1_000_000_000)
    def close(self,now=61_000_000_000,position_long=1,position_short=0):
        return self.state.prepare_close(Decimal("3077"),now_ns=now,position_long=position_long,position_short=position_short)

    def test_no_existing_position_or_active_order_allowed_on_selected_contract(self):
        for long,short,orders in ((1,0,0),(0,1,0),(0,0,1)):
            with self.subTest(values=(long,short,orders)),self.assertRaises(ValueError):self.state.prepare_open("DCE.m2701",3078,position_long=long,position_short=short,active_orders=orders)
        self.assertEqual(self.journal.events,[])

    def test_open_intent_reserved_before_send_and_cannot_be_repeated(self):
        identity=self.state.prepare_open("DCE.m2701",3078,position_long=0,position_short=0,active_orders=0)
        self.assertEqual(self.journal.events[0][0],"OPEN_INTENT")
        self.assertEqual(self.journal.events[0][1]["order_id"],identity)
        with self.assertRaises(ValueError):self.state.prepare_open("DCE.c2701",2211,position_long=0,position_short=0,active_orders=0)

    def test_failed_durable_commit_cannot_authorize_send(self):
        def fail(*_):raise OSError("disk failure")
        self.journal.append=fail
        with self.assertRaises(OSError):self.state.prepare_open("DCE.m2701",3078,position_long=0,position_short=0,active_orders=0)
        self.assertIsNone(self.state.open_id)

    def test_fill_not_send_starts_exact_sixty_second_deadline(self):
        self.state.prepare_open("DCE.m2701",3078,position_long=0,position_short=0,active_orders=0)
        self.state.observe_open(traded=0,reported=0,terminal=False,now_ns=1)
        self.assertIsNone(self.state.deadline_ns)
        self.state.observe_open(traded=1,reported=1,terminal=True,now_ns=50_000_000_000)
        self.assertEqual(self.state.deadline_ns,110_000_000_000)
        with self.assertRaises(ValueError):self.close(now=109_999_999_999)
        self.close(now=110_000_000_000)

    def test_repeated_entry_fill_observation_does_not_extend_deadline(self):
        self.opened();self.state.observe_open(traded=1,reported=1,terminal=True,now_ns=20_000_000_000)
        self.assertEqual(self.state.deadline_ns,61_000_000_000)
        self.assertEqual(sum(kind=="OPEN_FILL_CONFIRMED" for kind,_ in self.journal.events),1)

    def test_missing_trade_consistency_prevents_exit_send(self):
        self.state.prepare_open("DCE.m2701",3078,position_long=0,position_short=0,active_orders=0)
        self.state.observe_open(traded=1,reported=0,terminal=True,now_ns=1_000_000_000)
        with self.assertRaises(ValueError):self.close()

    def test_early_or_unowned_or_conflicting_position_never_closes(self):
        self.opened()
        for now,long,short in ((60_999_999_999,1,0),(61_000_000_000,0,0),(61_000_000_000,2,0),(61_000_000_000,1,1)):
            with self.subTest(values=(now,long,short)),self.assertRaises(ValueError):self.close(now=now,position_long=long,position_short=short)

    def test_close_intent_is_persisted_before_send_and_unknown_send_occupies(self):
        self.opened();identity=self.close()
        self.assertEqual(self.journal.events[-1][0],"CLOSE_INTENT")
        self.assertEqual(self.journal.events[-1][1]["order_id"],identity)
        with self.assertRaises(ValueError):self.close()

    def test_cancel_request_or_inconsistent_terminal_does_not_release_close(self):
        self.opened();self.close()
        self.state.observe_close(traded=0,reported=0,terminal=False)
        with self.assertRaises(ValueError):self.close()
        self.state.observe_close(traded=0,reported=1,terminal=True)
        with self.assertRaises(ValueError):self.close()

    def test_verified_zero_fill_terminal_allows_single_remaining_requote(self):
        self.opened();first=self.close();self.state.observe_close(traded=0,reported=0,terminal=True)
        second=self.close();self.assertNotEqual(first,second)
        self.assertEqual(self.state.open_qty-self.state.close_qty,1)

    def test_exit_fill_only_counted_once_and_no_reopen_or_more_exit(self):
        self.opened();self.close()
        self.state.observe_close(traded=1,reported=1,terminal=True)
        self.state.observe_close(traded=1,reported=1,terminal=True)
        self.assertTrue(self.state.closed);self.assertEqual(self.state.close_qty,1)
        with self.assertRaises(ValueError):self.close()
        with self.assertRaises(ValueError):self.state.prepare_open("DCE.m2701",3078,position_long=0,position_short=0,active_orders=0)

    def test_invalid_or_regressing_fill_volume_rejected(self):
        self.opened()
        for amount in (-1,2):
            with self.subTest(amount=amount),self.assertRaises(ValueError):self.state.observe_open(traded=amount,reported=amount,terminal=True,now_ns=10)
        with self.assertRaises(ValueError):self.state.observe_open(traded=0,reported=0,terminal=True,now_ns=10)

    def test_stale_future_expired_empty_crossed_and_no_volume_quotes_rejected(self):
        now=datetime(2026,11,10,10,30,tzinfo=UTC8)
        base={"datetime":"2026-11-10 10:30:00","expired":False,"bid_price1":3077,"ask_price1":3078,"price_tick":1,"lower_limit":2800,"upper_limit":3400,"bid_volume1":10,"ask_volume1":10}
        self.assertTrue(fresh_quote(SimpleNamespace(**base),now))
        for changes in ({"datetime":"2026-11-10 10:14:59"},{"datetime":"2026-11-10 10:30:02"},{"expired":True},
                        {"bid_price1":float("nan")},{"ask_price1":3076},{"bid_volume1":0},{"datetime":""}):
            with self.subTest(changes=changes):self.assertFalse(fresh_quote(SimpleNamespace(**(base|changes)),now))

    def test_aggressive_limits_stay_within_daily_limits_and_align_ticks(self):
        quote=SimpleNamespace(price_tick=2,ask_price1=100,bid_price1=98,upper_limit=102,lower_limit=96)
        self.assertEqual(limit_price(quote,"BUY"),Decimal(102))
        self.assertEqual(limit_price(quote,"SELL"),Decimal(96))

    def test_sqlite_journal_commit_and_hash_chain_are_recoverable(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/"probe.db";journal=Journal(path)
            first=journal.append("INTENT",{"quantity":1});second=journal.append("OBSERVED",{"quantity":1});journal.close()
            with closing(sqlite3.connect(path)) as conn:rows=conn.execute("SELECT kind,previous_hash,event_hash FROM events ORDER BY seq").fetchall()
            self.assertEqual(rows[0][2],first["hash"]);self.assertEqual(rows[1][1],first["hash"]);self.assertEqual(rows[1][2],second["hash"])
            reopened=Journal(path);self.assertEqual(reopened.previous,second["hash"]);reopened.close()

    def test_monotonic_clock_reference_aligns_quote_without_changing_freshness_gate(self):
        host=datetime(2026,11,10,10,30,tzinfo=UTC8)
        reference=ClockReference(host.timestamp()+2.6,100,2.6,())
        quote=SimpleNamespace(datetime="2026-11-10 10:30:02.600000",expired=False,bid_price1=3077,ask_price1=3078,price_tick=1,lower_limit=2800,upper_limit=3400,bid_volume1=10,ask_volume1=10)
        self.assertFalse(fresh_quote(quote,host))
        with patch("qa.online_clock_reference.time.monotonic",return_value=100):self.assertTrue(fresh_quote(quote,reference.now()))
        stale=SimpleNamespace(**(vars(quote)|{"datetime":"2026-11-10 10:14:59"}))
        with patch("qa.online_clock_reference.time.monotonic",return_value=100):self.assertFalse(fresh_quote(stale,reference.now()))

    def test_expired_or_regressing_clock_reference_blocks(self):
        reference=ClockReference(1000,100,0,())
        for now in (99,400):
            with patch("qa.online_clock_reference.time.monotonic",return_value=now),self.assertRaises(ValueError):reference.now()


if __name__=="__main__":unittest.main()
