import unittest
from online_trader.errors import TraderError
from online_trader.execution.close_routes import close_routes


def fill(key,day,qty,*,effect='OPEN',offset='OPEN',seq=1):
    return {'trade_key':key,'trading_day':day,'quantity':qty,'contract_id':'SHFE.rb2701','direction':'LONG','effect':effect,'offset_kind':offset,'event_seq':str(seq)}


class CrossDayCloseTests(unittest.TestCase):
    def review(self,*,fills=None,native=None,orders=None,quantity=5,**kwargs):
        return close_routes(contract_id='SHFE.rb2701',direction='LONG',current_day='2026-11-10',
            fills=fills if fills is not None else [fill('old','2026-11-09',3),fill('today','2026-11-10',2,seq=2)],
            native=native if native is not None else {'today':2,'historical':3,'total':5},orders=orders or [],target_quantity=quantity,**kwargs)
    def order(self,**changes):
        return {'contract_id':'SHFE.rb2701','direction':'LONG','original_qty':2,'filled_qty':0,
                'native_offset':'CLOSE','send_state':'UNKNOWN','terminal_confirmed':False,'reported_fill_qty':None,'trading_day':'2026-11-10'}|changes
    def test_mixed_yesterday_today_lots_use_distinct_native_offsets(self):
        result=self.review();self.assertEqual(result['routes'],[{'native_offset':'CLOSETODAY','quantity':2},{'native_offset':'CLOSE','quantity':3}])
        self.assertFalse(result['execution_eligible'])
    def test_unknown_keeps_its_exact_offset_reservation(self):
        result=self.review(orders=[self.order()]);self.assertEqual(result['reserved']['historical'],2)
        self.assertEqual(sum(r['quantity'] for r in result['routes']),3)
    def test_cancel_request_and_terminal_detail_mismatch_never_release(self):
        for changes in ({'send_state':'CONFIRMED','terminal_confirmed':False},{'send_state':'CONFIRMED','terminal_confirmed':True,'reported_fill_qty':1}):
            self.assertEqual(self.review(orders=[self.order(**changes)])['reserved']['historical'],2)
    def test_confirmed_zero_fill_cancel_releases_exact_remainder(self):
        result=self.review(orders=[self.order(send_state='CONFIRMED',terminal_confirmed=True,reported_fill_qty=0)])
        self.assertEqual(result['reserved']['historical'],0)
    def test_partial_close_consumes_proved_historical_bucket_only(self):
        fills=[fill('old','2026-11-09',3),fill('today','2026-11-10',2,seq=2),fill('close','2026-11-10',1,effect='REDUCE',offset='CLOSE',seq=3)]
        result=self.review(fills=fills,native={'today':2,'historical':2,'total':4},orders=[self.order(filled_qty=1)],quantity=4)
        self.assertEqual(result['routes'],[{'native_offset':'CLOSETODAY','quantity':2},{'native_offset':'CLOSE','quantity':1}])
    def test_foreign_native_lots_and_bucket_mismatch_are_not_adopted(self):
        for native in ({'today':2,'historical':4,'total':6},{'today':3,'historical':2,'total':5},{'today':None,'historical':3,'total':5}):
            with self.subTest(native=native),self.assertRaises(TraderError):self.review(native=native)
    def test_invalid_original_offset_cannot_consume_any_other_bucket(self):
        fills=[fill('old','2026-11-09',3),fill('close','2026-11-10',1,effect='REDUCE',offset='CLOSETODAY',seq=2)]
        with self.assertRaises(TraderError):self.review(fills=fills)
    def test_duplicate_future_and_foreign_fills_fail_closed(self):
        for fills in ([fill('same','2026-11-09',3),fill('same','2026-11-10',2,seq=2)],
                      [fill('future','2026-11-11',5)],[fill('short','2026-11-10',5)|{'direction':'SHORT'}]):
            with self.subTest(fills=fills),self.assertRaises(TraderError):self.review(fills=fills)
    def test_reservation_cannot_exceed_its_native_bucket(self):
        with self.assertRaises(TraderError):self.review(orders=[self.order(original_qty=4)])
    def test_requested_reduction_is_not_silently_clamped_to_owned_quantity(self):
        with self.assertRaises(TraderError):self.review(quantity=6)
    def test_night_label_not_civil_open_date_determines_today(self):
        result=self.review(fills=[fill('night','2026-11-10',5)],native={'today':5,'historical':0,'total':5})
        self.assertEqual(result['routes'],[{'native_offset':'CLOSETODAY','quantity':5}])
    def test_previous_day_unknown_close_today_cannot_reserve_a_new_days_today_bucket(self):
        for day in ('2026-11-09',None):
            with self.subTest(day=day),self.assertRaises(TraderError):
                self.review(orders=[self.order(native_offset='CLOSETODAY',trading_day=day)])
