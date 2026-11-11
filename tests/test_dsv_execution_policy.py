"""Source pricing boundaries and pure order-action quantity/fact barriers."""

import copy
import unittest

from online_trader.errors import TraderError
from online_trader.execution.policy import (
    close_price_context, decide_order_action, select_manual_price, select_price,
)


def market(**changes):
    return {"last":"100","bid1":"99","ask1":"101","tick_size":"1",
            "lower_limit":"80","upper_limit":"120","complete":True,
            "fresh":True,"is_trading":True,"quality":"CONTIGUOUS"} | changes


def order(**changes):
    return {"side":"BUY","effect":"OPEN","original_qty":5,"filled_qty":0,
            "limit_price":"101","terminal_confirmed":False,"reported_fill_qty":None,
            "send_state":"CALL_RETURNED","broker_state":"ALIVE","cancel_status":None} | changes


def action(value=None, price="102", **changes):
    args={"can_increase":True,"remaining_quantity":5,"other_pending_quantity":0}
    args.update(changes)
    return decide_order_action(value or order(),price,**args)


class DsvExecutionPricingTests(unittest.TestCase):
    def test_active_side_and_missing_opponent_fallback(self):
        self.assertEqual(select_price("BUY",market())["price"],"101")
        self.assertEqual(select_price("SELL",market())["price"],"99")
        for side,key in (("BUY","ask1"),("SELL","bid1")):
            self.assertEqual(select_price(side,market(**{key:None}))["price"],"100")
            self.assertEqual(select_price(side,market(**{key:"0"}))["price"],"100")

    def test_exact_context_priority_is_passive_aggressive_market_cap(self):
        contexts={"PASSIVE":{"side":"BUY","age_ms":0,"level":"98"},
                  "AGGR1":{"side":"BUY","age_ms":0},"MARKET":{"side":"BUY","age_ms":0},
                  "CAP":{"side":"BUY","age_ms":0,"cap":"100"}}
        for expected in ("PASSIVE","AGGR1","MARKET","CAP"):
            self.assertEqual(select_price("BUY",market(),contexts=contexts)["mode"],expected)
            contexts.pop(expected)

    def test_passive_and_cap_have_inclusive_ninety_second_ttl(self):
        for mode,field,value,expected in (("PASSIVE","level","98","98"),("CAP","cap","100","100")):
            for age in (89_999,90_000,90_001):
                result=select_price("BUY",market(),contexts={mode:{"side":"BUY","age_ms":age,field:value}})
                self.assertEqual(result["price"],expected if age<=90_000 else "101")

    def test_aggressive_and_market_have_distinct_inclusive_ttls(self):
        for mode,ttl,price in (("AGGR1",60_000,"102"),("MARKET",120_000,"120")):
            self.assertEqual(select_price("BUY",market(),contexts={mode:{"side":"BUY","age_ms":ttl}})["price"],price)
            self.assertEqual(select_price("BUY",market(),contexts={mode:{"side":"BUY","age_ms":ttl+1}})["price"],"101")

    def test_context_for_opposite_order_side_does_not_apply(self):
        contexts={"PASSIVE":{"side":"SELL","age_ms":0,"level":"98"},"MARKET":{"side":"SELL","age_ms":0}}
        self.assertEqual(select_price("BUY",market(),contexts=contexts)["price"],"101")

    def test_aggressive_one_tick_mirrors_buy_and_sell(self):
        for side,price in (("BUY","102"),("SELL","98")):
            result=select_price(side,market(),contexts={"AGGR1":{"side":side,"age_ms":0}})
            self.assertEqual(result["price"],price)

    def test_market_uses_actual_exchange_limit_with_equality_legal(self):
        for side,price in (("BUY","120"),("SELL","80")):
            self.assertEqual(select_price(side,market(),contexts={"MARKET":{"side":side,"age_ms":0}})["price"],price)

    def test_missing_market_limit_retains_raw_ten_tick_fallback_but_refuses_send(self):
        for side,field,raw in (("BUY","upper_limit","111"),("SELL","lower_limit","89")):
            result=select_price(side,market(**{field:None}),contexts={"MARKET":{"side":side,"age_ms":0}})
            self.assertFalse(result["allowed"])
            self.assertIsNone(result["price"])
            self.assertEqual(result["raw_price"],raw)
            self.assertIn("PRICE_LIMITS_UNKNOWN",result["reasons"])

    def test_cap_minimum_buy_and_maximum_sell_keep_theory_bound(self):
        for side,cap,expected in (("BUY","100","100"),("BUY","103","101"),("SELL","100","100"),("SELL","97","99")):
            result=select_price(side,market(),contexts={"CAP":{"side":side,"age_ms":0,"cap":cap}})
            self.assertEqual(result["price"],expected)

    def test_source_price_is_not_silently_clamped_or_rounded(self):
        for quote,reason in ((market(ask1="121"),"PRICE_OUTSIDE_EXCHANGE_LIMITS"),
                             (market(ask1="100.5"),"PRICE_NOT_TICK_ALIGNED")):
            result=select_price("BUY",quote)
            self.assertFalse(result["allowed"])
            self.assertIn(reason,result["reasons"])
        result=select_price("BUY",market(tick_size="0"),contexts={"AGGR1":{"side":"BUY","age_ms":0}})
        self.assertEqual(result["raw_price"],"102")
        self.assertIn("TICK_SIZE_UNKNOWN",result["reasons"])

    def test_stale_incomplete_or_nontrading_facts_cannot_price_an_order(self):
        for change in ({"fresh":False},{"complete":False},{"is_trading":False},{"quality":"DATA_GAP"}):
            self.assertFalse(select_price("BUY",market(**change))["allowed"])
        result=select_price("BUY",market(last=None,ask1=None))
        self.assertIn("PRICE_UNAVAILABLE",result["reasons"])

    def test_invalid_scalar_or_context_identity_cannot_be_coerced(self):
        for context in ({"BOGUS":{}},{"PASSIVE":{"side":"BUY","age_ms":-1,"level":"100"}},
                        {"PASSIVE":{"side":"BUY","age_ms":True,"level":"100"}}):
            with self.assertRaises(TraderError):select_price("BUY",market(),contexts=context)
        for side,change in (("LONG",{}),("BUY",{"ask1":"-1"}),("BUY",{"last":100.0})):
            with self.assertRaises(TraderError):select_price(side,market(**change))

    def test_manual_direct_modes_have_distinct_prices_without_automatic_chasing(self):
        for side,expected in (("BUY",("101","99","100")),("SELL",("99","101","100"))):
            for mode,price in zip(("对手价","排队价","最新价"),expected):
                result=select_manual_price(mode,side,market())
                self.assertEqual(result["price"],price)
                self.assertFalse(result["automatic_reprice"])
        self.assertFalse(select_manual_price("最新价","BUY",market(last=None))["allowed"])
        with self.assertRaises(TraderError):select_manual_price("市价","BUY",market())

    def test_manual_missing_side_price_does_not_immediately_use_last(self):
        for side,mode,field in (("BUY","对手价","ask1"),("SELL","对手价","bid1"),
                                ("BUY","排队价","bid1"),("SELL","排队价","ask1")):
            result=select_manual_price(mode,side,market(**{field:None}),elapsed_ms=3999)
            self.assertFalse(result["allowed"])
            self.assertIsNone(result["raw_price"])

    def test_manual_four_second_limit_fallback_uses_relative_distance_with_equality(self):
        quote=market(ask1=None,upper_limit="101")
        self.assertFalse(select_manual_price("对手价","BUY",quote,elapsed_ms=3999)["allowed"])
        result=select_manual_price("对手价","BUY",quote,elapsed_ms=4000)
        self.assertEqual(result["price"],"101")
        self.assertFalse(result["automatic_reprice"])
        far=select_manual_price("对手价","BUY",market(ask1=None,upper_limit="102"),elapsed_ms=4000)
        self.assertEqual(far["reasons"],["MANUAL_LIMIT_FALLBACK_TOO_FAR"])
        short=select_manual_price("对手价","SELL",market(bid1=None,lower_limit="99"),elapsed_ms=4000)
        self.assertEqual(short["price"],"99")

    def test_close_steps_are_seconds_remaining_not_attempt_counts(self):
        for elapsed,expected in ((0,"ACTIVE"),(9999,"ACTIVE"),(10_000,"AGGR1"),(14_999,"AGGR1"),(15_000,"MARKET"),(20_000,"MARKET")):
            self.assertEqual(close_price_context("SELL",elapsed_close_ms=elapsed,advance_seconds=20)["mode"],expected)

    def test_close_refresh_gate_is_three_seconds_inclusive_and_first_is_due(self):
        for age,expected in ((None,True),(2999,False),(3000,True),(3001,True)):
            result=close_price_context("BUY",elapsed_close_ms=10_000,advance_seconds=20,last_context_age_ms=age)
            self.assertEqual(result["refresh"],expected)
            self.assertEqual(result["contexts"],{"AGGR1":{"side":"BUY","age_ms":0}})

    def test_pure_price_calls_do_not_mutate_facts(self):
        quote=market();contexts={"PASSIVE":{"side":"BUY","age_ms":0,"level":"100"}}
        before=copy.deepcopy((quote,contexts))
        select_price("BUY",quote,contexts=contexts)
        self.assertEqual((quote,contexts),before)


class DsvOrderActionTests(unittest.TestCase):
    def test_price_worsening_cancels_only_and_never_replaces_same_action(self):
        for side,old,new in (("BUY","101","102"),("SELL","99","98")):
            result=action(order(side=side,limit_price=old),new)
            self.assertEqual(result["action"],"CANCEL")
            self.assertEqual(result["quantity"],0)
            self.assertIsNone(result["price"])

    def test_equal_or_improving_quotes_do_not_cancel(self):
        for side,old,new in (("BUY","101","101"),("BUY","101","100"),
                             ("SELL","99","99"),("SELL","99","100")):
            self.assertEqual(action(order(side=side,limit_price=old),new)["action"],"WAIT")

    def test_cancel_return_and_unknown_cancel_keep_quantity_reserved(self):
        for status in ("SEND_POSSIBLE","CALL_RETURNED","UNKNOWN"):
            self.assertEqual(action(order(cancel_status=status))["reasons"],["CANCELLATION_PENDING"])

    def test_unknown_or_not_yet_proven_send_never_replays(self):
        for state in ("UNKNOWN","SEND_POSSIBLE"):
            result=action(order(send_state=state),timeout_due=True)
            self.assertEqual(result["action"],"WAIT")
            self.assertEqual(result["quantity"],0)

    def test_proven_reprice_terminal_uses_fills_and_other_pending_once(self):
        value=order(terminal_confirmed=True,broker_state="CANCELLED",send_state="CONFIRMED",
                    filled_qty=2,reported_fill_qty=2,cancel_status="CONFIRMED")
        result=action(value,remaining_quantity=3,other_pending_quantity=1,reprice_cancelled=True)
        self.assertEqual((result["action"],result["quantity"],result["price"]),("REPLACE",2,"102"))
        self.assertEqual(value["filled_qty"],2)

    def test_cancelled_without_reprice_evidence_or_rejected_stops(self):
        for state,reprice in (("CANCELLED",False),("REJECTED",False),("REJECTED",True)):
            value=order(terminal_confirmed=True,broker_state=state,send_state="CONFIRMED",reported_fill_qty=0)
            self.assertEqual(action(value,reprice_cancelled=reprice)["action"],"STOP")

    def test_inconsistent_terminal_or_unknown_send_is_not_reusable(self):
        for state,reported in (("CONFIRMED",None),("CONFIRMED",1),("UNKNOWN",0)):
            value=order(terminal_confirmed=True,broker_state="CANCELLED",send_state=state,reported_fill_qty=reported)
            self.assertEqual(action(value,reprice_cancelled=True)["action"],"WAIT")

    def test_timeout_cancels_open_then_stops_instead_of_resurrecting_target(self):
        self.assertEqual(action(timeout_due=True)["action"],"CANCEL")
        value=order(terminal_confirmed=True,broker_state="CANCELLED",send_state="CONFIRMED",reported_fill_qty=0)
        self.assertEqual(action(value,timeout_due=True,reprice_cancelled=True)["action"],"STOP")

    def test_revoking_increase_does_not_disable_needed_protection(self):
        self.assertEqual(action(can_increase=False)["action"],"CANCEL")
        self.assertEqual(action(order(effect="REDUCE"),can_increase=False)["action"],"CANCEL")
        value=order(effect="REDUCE",terminal_confirmed=True,broker_state="CANCELLED",send_state="CONFIRMED",reported_fill_qty=0)
        self.assertEqual(action(value,can_increase=False,reprice_cancelled=True)["action"],"REPLACE")

    def test_no_send_proof_may_be_replaced_but_full_fill_or_covered_target_cannot(self):
        value=order(terminal_confirmed=True,send_state="DEFINITELY_NOT_SENT",reported_fill_qty=0)
        self.assertEqual(action(value)["action"],"REPLACE")
        self.assertEqual(action(value,remaining_quantity=1,other_pending_quantity=1)["action"],"DONE")
        full=order(terminal_confirmed=True,broker_state="FILLED",send_state="CONFIRMED",filled_qty=5,reported_fill_qty=5)
        self.assertEqual(action(full)["action"],"DONE")

    def test_target_reduction_requests_cancel_without_releasing_old_order(self):
        result=action(order(effect="REDUCE"),"101",remaining_quantity=2)
        self.assertEqual(result["action"],"CANCEL")
        self.assertEqual(result["reasons"],["TARGET_QUANTITY_REDUCED"])
        self.assertEqual(result["quantity"],0)

    def test_missing_price_waits_for_a_real_new_legal_price(self):
        self.assertEqual(action(price=None)["action"],"WAIT")
        value=order(terminal_confirmed=True,broker_state="CANCELLED",send_state="CONFIRMED",reported_fill_qty=0)
        self.assertEqual(action(value,price=None,reprice_cancelled=True)["action"],"WAIT")

    def test_action_rejects_coerced_or_impossible_quantities(self):
        for value,changes in ((order(filled_qty=6),{}),(order(),{"remaining_quantity":True}),
                              (order(),{"can_increase":1}),(order(side="LONG"),{})):
            with self.assertRaises(TraderError):action(value,**changes)


if __name__ == "__main__":unittest.main()
