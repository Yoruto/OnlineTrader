"""Focused broker-shaped tests for explicit allocation and reviewed history."""
import copy
import unittest

import test_control
import test_dsv_day_execution
import test_manual_workbench
from online_trader.domain.orders import OrderState
from online_trader.kernel.workbench import workbench_view


class ExternalPositionRecoveryTests(unittest.TestCase):
    setUp=test_manual_workbench.ManualWorkbenchTests.setUp
    tearDown=test_manual_workbench.ManualWorkbenchTests.tearDown
    start=test_manual_workbench.ManualWorkbenchTests.start
    start_manual=test_manual_workbench.ManualWorkbenchTests.start_manual
    quote=test_manual_workbench.ManualWorkbenchTests.quote
    open_manual=test_manual_workbench.ManualWorkbenchTests.open_manual
    cycle=test_manual_workbench.ManualWorkbenchTests.cycle
    restart=test_manual_workbench.ManualWorkbenchTests.restart
    message=test_control.ControlTests.message
    run_command=test_control.ControlTests.run_command

    def adjust(self,row=None):
        row=row or workbench_view(self.kernel)['external_positions'][0]
        self.assertTrue(row['eligible'],row)
        return self.run_command('ADOPT_EXTERNAL_POSITION',{k:row[k] for k in ('contract_id','direction','evidence_hash')},
            {'scope_type':'CONTRACT','scope_ids':[self.code]})

    def test_mix_then_reduce_old_lots_and_reverse_with_exact_cycle_attribution(self):
        self.start_manual('SHFE.bu2701')
        self.assertEqual(self.open_manual('LONG')['status'],'EXECUTING')
        oid=self.api.inserted[0]['order_id'];self.api.fill(oid,2,1048);self.session.step()
        original=copy.deepcopy(self.cycle());cid=original['position_cycle_id']
        native=self.api.positions[self.code]
        native.pos_long=4;native.pos_long_today=4;native.open_price_long=1047.
        stale=workbench_view(self.kernel)['external_positions'][0]
        native.pos_long=3;native.pos_long_today=3
        self.assertEqual(self.adjust(stale)['status'],'REJECTED')
        self.assertEqual(self.adjust()['status'],'SUCCEEDED')
        self.quote(1048)
        self.assertEqual([c['protection']['owned_qty'] for c in self.kernel._staged.values()],[2,1])
        self.assertEqual(self.kernel._staged[cid]['plan'],original['plan'])

        native.pos_long=2;native.pos_long_today=2
        row=workbench_view(self.kernel)['external_positions'][0]
        self.assertEqual(row['reductions'],[{'position_cycle_id':cid,'direction':'LONG','bucket':'today','quantity':1}])
        self.assertEqual(self.adjust(row)['status'],'SUCCEEDED')
        self.quote(1048)
        self.assertEqual([c['protection']['owned_qty'] for c in self.kernel._staged.values()],[1,1])
        self.assertEqual(self.kernel._staged[cid]['protection']['phase1_confirmed_reduction_qty'],0)
        self.restart()
        native=self.api.positions[self.code]
        self.assertEqual([c['protection']['owned_qty'] for c in self.kernel._staged.values()],[1,1])
        self.assertEqual(len(self.api.inserted),0)

        native.pos_long=0;native.pos_long_today=0
        native.pos_short=2;native.pos_short_today=2;native.open_price_short=1048.
        self.assertEqual(self.adjust()['status'],'SUCCEEDED')
        self.quote(1048)
        cycles=list(self.kernel._staged.values())
        self.assertEqual([(c['plan']['direction'],c['protection']['owned_qty']) for c in cycles],
                         [('LONG',0),('LONG',0),('SHORT',2)])
        self.assertEqual([c['lifecycle'] for c in cycles[:2]],['CLOSED','CLOSED'])
        self.assertEqual(self.kernel.ledger.query('SELECT COUNT(*) AS count FROM fill_facts')[0]['count'],1)
        self.assertEqual(len(self.api.inserted),0)
        self.assertIsNone(self.kernel.money.report()['summary']['gross_realized_pnl'])

    def test_mix_with_working_original_order_refuses_allocation(self):
        self.start_manual('SHFE.bu2701')
        self.assertEqual(self.open_manual('LONG')['status'],'EXECUTING')
        native=self.api.positions[self.code]
        native.pos_long=1;native.pos_long_today=1;native.open_price_long=1048.
        self.assertFalse(workbench_view(self.kernel)['external_positions'][0]['eligible'])

    def test_strategy_scope_explicitly_permits_review_without_arming_or_opening(self):
        self.start(code='SHFE.bu2701',grant_changes={'schema_version':'KQ_DSV_SIM/2',
            'trading_days':['2026-11-10'],'resume_entries_on_day_change':False,'allow_external_allocation':True})
        self.account=self.kernel.account_key
        native=self.api.positions[self.code]
        native.pos_long=1;native.pos_long_today=1;native.open_price_long=1048.
        self.assertEqual(self.adjust()['status'],'SUCCEEDED')
        self.assertFalse(self.kernel._armed)
        self.assertEqual(self.api.inserted,[])

    def test_equal_bucket_totals_do_not_hide_observed_external_replacement(self):
        self.start_manual('SHFE.bu2701')
        self.open_manual('LONG');self.api.fill(self.api.inserted[0]['order_id'],2,1048);self.session.step()
        for oid,direction,offset in (('foreign-close','SELL','CLOSETODAY'),('foreign-open','BUY','OPEN')):
            self.api.insert_order(symbol=self.code,direction=direction,offset=offset,volume=2,limit_price=1048.,
                order_id=oid,account=self.port.account)
            self.api.fill(oid,2,1048)
        self.api.positions[self.code].open_price_long=1048.
        row=workbench_view(self.kernel)['external_positions'][0]
        self.assertEqual(row['difference'],'0')
        self.assertEqual(row['review_error'],'E_OWNERSHIP')
        self.assertFalse(row['eligible'])
        count=len(self.api.inserted)
        self.kernel.manual_close(self.cycle()['position_cycle_id'],1);self.kernel.wait_idle()
        self.assertEqual(len(self.api.inserted),count)


class HistoricalOrderRecoveryTests(unittest.TestCase):
    setUp=test_dsv_day_execution.DsvDayExecutionTests.setUp
    tearDown=test_dsv_day_execution.DsvDayExecutionTests.tearDown
    start=test_dsv_day_execution.DsvDayExecutionTests.start
    next_day=test_dsv_day_execution.DsvDayExecutionTests.next_day
    message=test_control.ControlTests.message
    run_command=test_control.ControlTests.run_command

    def import_history(self,packet):
        return self.run_command('REQUEST_RECONCILIATION',{'requested_scope':'FULL_ACCOUNT',
            'position_cycle_id':None,'historical_orders':packet})

    def missing(self,filled,*,trade_id=None):
        x=self.start();self.kernel=x.kernel;self.account=x.grant.account_key
        sent=x.api.orders[self.oid];self.original_native_order=sent
        if filled:x.api.fill(self.oid,filled,1079,trade_id=trade_id)
        native=x.port.snapshot()
        order=next(r for r in native['orders'] if r['order_id']==self.oid)
        trades=[{key:t[key] for key in ('trade_id','quantity','price','source_time_ns')} for t in native['trades']]
        order.update(status='FINISHED',source_time_ns=str(sent.insert_date_time),trades=trades)
        cycle=copy.deepcopy(x.cycle());cycle['orders'][0]['send_state']='UNKNOWN';cycle['revision']+=1
        self.kernel._stage_cycle(cycle,'TX_ORDER','OFFLINE_UNKNOWN_FACT');self.kernel.wait_idle()
        self.next_day();x.api.orders.clear();x.api.trades.clear();x.session.step()
        self.assertTrue(self.kernel.recovery_required)
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,5)
        return {'schema_version':'NATIVE_TERMINAL_HISTORY/1','account_key':self.account,
            'gateway_identity':x.port.gateway_identity,'calendar_hash':x.calendar.content_hash,
            'statement_reference':'offline-native-statement','orders':[order]}

    def test_absent_partial_fills_recover_original_order_before_new_day_exit(self):
        packet=self.missing(2)
        malformed=copy.deepcopy(packet);malformed['orders'][0]['trades'].clear()
        self.assertEqual(self.import_history(malformed)['status'],'REJECTED')
        self.assertEqual(self.import_history(packet)['status'],'SUCCEEDED')
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.x.session.step()
        cycle=self.x.cycle()
        self.assertEqual(cycle['protection']['owned_qty'],2)
        self.assertEqual(cycle['orders'][0]['send_state'],'CONFIRMED')
        self.assertEqual(cycle['orders'][0]['filled_qty'],2)
        self.assertEqual(self.x.api.inserted[-1]['offset'],'CLOSE',self.x.session.phase)
        self.assertEqual(self.x.api.inserted[-1]['volume'],2)
        self.assertFalse(self.kernel._armed)
        self.assertEqual(self.import_history(packet)['status'],'SUCCEEDED')
        self.assertEqual(len(self.kernel.ledger.query('SELECT * FROM fill_facts')),1)

    def test_zero_terminal_evidence_survives_restart_before_consumption(self):
        packet=self.missing(0)
        self.assertEqual(self.import_history(packet)['status'],'SUCCEEDED')
        self.x.restart()
        self.kernel=self.x.kernel
        self.assertEqual(self.x.cycle()['lifecycle'],'CLOSED')
        self.assertFalse(self.kernel._armed)
        self.assertEqual(self.x.api.inserted,[])
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'),[])

    def test_reappearing_live_order_conflicts_with_reviewed_terminal_before_release(self):
        packet=self.missing(0)
        self.assertEqual(self.import_history(packet)['status'],'SUCCEEDED')
        self.x.api.orders[self.oid]=self.original_native_order
        self.x.session.step()
        self.assertTrue(self.kernel.recovery_required)
        self.assertFalse(self.kernel._armed)
        self.assertEqual(OrderState.from_dict(self.x.cycle()['orders'][0]).potential_remaining,5)
        self.assertEqual(len(self.x.api.inserted),1)

    def test_missing_partial_close_continues_original_intent_after_terminal_history(self):
        x=self.start();self.kernel=x.kernel;self.account=x.grant.account_key
        x.api.fill(self.oid,5,1079);x.session.step()
        self.kernel.manual_close(x.cycle()['position_cycle_id'],2);self.kernel.wait_idle()
        old=x.cycle()['orders'][-1];oid=x.api.inserted[-1]['order_id']
        x.api.fill(oid,1,1079)
        snapshot=x.port.snapshot()
        native=next(row for row in snapshot['orders'] if row['order_id']==oid)
        details=[{key:t[key] for key in ('trade_id','quantity','price','source_time_ns')}
                 for t in snapshot['trades'] if t['order_id']==oid]
        native.update(status='FINISHED',source_time_ns=str(x.api.orders[oid].insert_date_time),trades=details)
        self.next_day();del x.api.orders[oid]
        for trade in details:del x.api.trades[trade['trade_id']]
        x.session.step()
        self.assertEqual(len(x.api.inserted),2)
        packet={'schema_version':'NATIVE_TERMINAL_HISTORY/1','account_key':self.account,
            'gateway_identity':x.port.gateway_identity,'calendar_hash':x.calendar.content_hash,
            'statement_reference':'offline-close-history','orders':[native]}
        self.assertEqual(self.import_history(packet)['status'],'SUCCEEDED')
        x.session.step()
        self.assertEqual(x.cycle()['protection']['owned_qty'],4)
        latest=x.cycle()['orders'][-1]
        self.assertEqual(latest['native_offset'],'CLOSE',x.session.phase)
        self.assertEqual(latest['intent_id'],old['intent_id'])
        self.assertEqual(latest['parent_child_order_id'],old['child_order_id'])
        self.assertEqual(latest['original_qty'],4)
