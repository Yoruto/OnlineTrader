"""Manual IPC, position allocation and source-epoch recovery using native-shaped facts."""
import unittest
import test_control
import test_online_dsv


class ManualWorkbenchTests(unittest.TestCase):
    setUp=test_online_dsv.OnlineDsvTests.setUp
    tearDown=test_online_dsv.OnlineDsvTests.tearDown
    start=test_online_dsv.OnlineDsvTests.start
    quote=test_online_dsv.OnlineDsvTests.quote
    cycle=test_online_dsv.OnlineDsvTests.cycle
    finish_last=test_online_dsv.OnlineDsvTests.finish_last
    restart=test_online_dsv.OnlineDsvTests.restart

    def start_manual(self,code='DCE.p2701'):
        self.start(code=code,grant_changes={'schema_version':'KQ_WORKBENCH_SIM/1',
            'trading_days':['2026-11-10'],'resume_entries_on_day_change':False})
        self.account=self.kernel.account_key

    message=test_control.ControlTests.message
    run_command=test_control.ControlTests.run_command

    def open_manual(self,direction):
        policy='sim-execution-'+self.grant.content_hash[:24]
        return self.run_command('MANUAL_OPEN',{'contract_id':self.code,'direction':direction,'quantity':2,
            'limit_price':'1048','order_policy_id':policy,'protection_plan':None},
            {'scope_type':'CONTRACT','scope_ids':[self.code]})

    def test_manual_short_uses_limit_and_no_automatic_opening(self):
        self.start_manual()
        self.quote()
        self.assertEqual(self.api.inserted,[])
        receipt=self.open_manual('SHORT')
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.assertEqual([(o['direction'],o['volume'],o['limit_price']) for o in self.api.inserted],[('SELL',2,1048.)])
        self.quote(1056)
        self.assertEqual(self.api.cancelled,[])
        self.api.fill(self.api.inserted[0]['order_id'],2,1048)
        self.session.step()
        self.assertEqual(self.cycle()['protection']['owned_qty'],2)
        policy='sim-execution-'+self.grant.content_hash[:24];cid=self.cycle()['position_cycle_id']
        self.run_command('MANUAL_CLOSE',{'position_cycle_id':cid,'quantity':2,'execution_policy_id':policy,
            'external_takeover_id':None},{'scope_type':'CYCLE','scope_ids':[cid]})
        self.quote(1050)
        self.assertEqual((self.api.inserted[-1]['direction'],self.api.inserted[-1]['offset']),('BUY','CLOSE'))

    def test_direct_manual_order_is_not_filtered_as_an_automatic_entry_near_the_limit(self):
        self.start_manual()
        self.api.quotes[self.code].lower_limit = 1040.
        self.quote(1048)
        receipt = self.open_manual('LONG')
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.assertEqual(len(self.api.inserted), 1)
        self.assertEqual(self.api.inserted[0]['limit_price'], 1048.)

    def test_manual_order_has_no_strategy_entry_timeout(self):
        self.start_manual()
        receipt = self.open_manual('LONG')
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.clock.advance(61_000)
        self.quote(1049)
        self.assertEqual(self.api.cancelled, [])
        self.assertEqual(self.api.orders[self.api.inserted[0]['order_id']].status, 'ALIVE')

    def owned_manual_position(self,direction='LONG'):
        self.start_manual()
        self.quote(1048)
        self.open_manual(direction)
        self.api.fill(self.api.inserted[0]['order_id'],2,1048)
        self.session.step()
        self.quote(1048)
        return self.cycle()['position_cycle_id']

    def close_at_mode(self,cid,mode,quantity=2):
        return self.run_command('MANUAL_CLOSE',{'position_cycle_id':cid,'quantity':quantity,
            'execution_policy_id':'sim-execution-'+self.grant.content_hash[:24],
            'external_takeover_id':None,'price_mode':mode},{'scope_type':'CYCLE','scope_ids':[cid]})

    def test_manual_sell_queue_price_is_frozen_until_terminal(self):
        cid=self.owned_manual_position()
        receipt=self.close_at_mode(cid,'排队价')
        order=self.api.inserted[-1]
        self.assertEqual((order['offset'],order['direction'],order['limit_price']),('CLOSE','SELL',1049.))
        self.quote(1040)
        self.assertEqual(self.api.cancelled,[])
        self.api.fill(order['order_id'],1,1049,finish=False)
        self.api.terminal(order['order_id'])
        self.session.step()
        final=self.kernel.control.get_status(receipt['command_id'],receipt['payload_hash']).result()
        self.assertEqual(final['status'],'FAILED',final)
        self.quote(1041)
        self.assertEqual(len(self.api.inserted),2)
        self.close_at_mode(cid,'最新价',1)
        self.assertEqual((self.api.inserted[-1]['volume'],self.api.inserted[-1]['limit_price']),(1,1041.))

    def test_manual_close_waits_four_seconds_then_uses_near_exchange_limit(self):
        cid = self.owned_manual_position()
        self.api.quotes[self.code].lower_limit = 1040.
        self.quote(1048,bid=0)
        receipt = self.close_at_mode(cid,'对手价')
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.assertEqual(len(self.api.inserted),1)
        self.quote(1048,bid=0,milliseconds=3999)
        self.assertEqual(len(self.api.inserted),1)
        self.quote(1048,bid=0,milliseconds=1)
        self.assertEqual(len(self.api.inserted),2)
        self.assertEqual(self.api.inserted[-1]['limit_price'],1040.)
        self.assertEqual(self.api.inserted[-1]['direction'],'SELL')
        self.quote(1049)
        self.assertEqual(self.api.cancelled,[])

    def test_manual_close_uses_requested_side_when_it_arrives_during_wait(self):
        cid = self.owned_manual_position()
        self.quote(1048,bid=0)
        receipt = self.close_at_mode(cid,'对手价')
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.quote(1048,bid=1046,milliseconds=2000)
        self.assertEqual(len(self.api.inserted),2)
        self.assertEqual(self.api.inserted[-1]['limit_price'],1046.)

    def test_manual_close_refuses_far_limit_and_does_not_send_later(self):
        cid = self.owned_manual_position()
        self.quote(1048,bid=0)
        receipt = self.close_at_mode(cid,'对手价')
        self.quote(1048,bid=0,milliseconds=4000)
        final = self.kernel.control.get_status(receipt['command_id'],receipt['payload_hash']).result()
        self.assertEqual(final['status'],'FAILED',final)
        self.assertEqual(self.cycle()['manual_reduction']['price_failure'],'MANUAL_LIMIT_FALLBACK_TOO_FAR')
        self.quote(1048)
        self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(self.cycle()['protection']['owned_qty'],2)

    def test_manual_close_price_wait_cannot_resume_on_new_boot(self):
        cid = self.owned_manual_position()
        self.quote(1048,bid=0)
        receipt = self.close_at_mode(cid,'对手价')
        self.restart()
        self.quote(1048)
        self.assertEqual(self.api.inserted,[])
        final = self.kernel.control.get_status(receipt['command_id'],receipt['payload_hash']).result()
        self.assertEqual(final['status'],'FAILED',final)
        self.assertEqual(self.cycle()['manual_reduction']['price_failure'],'MANUAL_PRICE_CONTEXT_CHANGED')

    def test_manual_buy_opponent_price_yields_to_new_protection_trigger(self):
        cid=self.owned_manual_position('SHORT')
        self.close_at_mode(cid,'对手价',1)
        manual=self.api.inserted[-1]
        self.assertEqual((manual['offset'],manual['direction'],manual['limit_price']),('CLOSE','BUY',1049.))
        self.quote(1066)
        self.assertEqual(self.api.cancelled,[manual['order_id']])
        self.api.terminal(manual['order_id'])
        self.session.step()
        protection=self.api.inserted[2:]
        self.assertEqual({(o['offset'],o['limit_price']) for o in protection},{('CLOSE',1067.)})
        self.assertEqual(sum(o['volume'] for o in protection),2)

    def test_close_all_binds_owned_quantities_without_disarming_the_account(self):
        from types import SimpleNamespace
        cid=self.owned_manual_position()
        self.api.positions['SHFE.rb2612']=SimpleNamespace(pos_long=1,pos_short=0)
        policy='sim-execution-'+self.grant.content_hash[:24]
        params={'price_mode':'最新价','execution_policy_id':policy,'positions':{cid:1}}
        receipt=self.run_command('MANUAL_CLOSE_ALL',params)
        self.assertEqual(receipt['status'],'REJECTED',receipt)
        self.assertTrue(self.kernel.snapshot()['armed'])
        self.assertEqual(len(self.api.inserted),1)
        params['positions'][cid]=2
        receipt=self.run_command('MANUAL_CLOSE_ALL',params)
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.assertTrue(self.kernel.snapshot()['armed'])
        self.assertEqual([(o['symbol'],o['volume'],o['limit_price']) for o in self.api.inserted[1:]],
                         [(self.code,2,1048.)])
        self.finish_last(1048)
        final=self.kernel.control.get_status(receipt['command_id'],receipt['payload_hash']).result()
        self.assertEqual(final['status'],'SUCCEEDED',final)
        self.assertEqual(self.api.positions['SHFE.rb2612'].pos_long,1)

    def test_adopted_buckets_do_not_create_opening_trade_or_consume_open_attempt(self):
        from online_trader.kernel.workbench import workbench_view
        self.start_manual('SHFE.bu2701')
        native=self.api.positions[self.code]
        native.pos_long=3;native.pos_long_today=1;native.pos_long_his=2;native.open_price_long=1040.
        row=workbench_view(self.kernel)['external_positions'][0]
        self.assertTrue(row['eligible'],row)
        params={key:row[key] for key in ('contract_id','direction','evidence_hash')}
        scope={'scope_type':'CONTRACT','scope_ids':[self.code]}
        native.open_price_long=1041.
        self.assertEqual(self.run_command('ADOPT_EXTERNAL_POSITION',params,scope)['status'],'REJECTED')
        native.open_price_long=1040.
        receipt=self.run_command('ADOPT_EXTERNAL_POSITION',params,scope)
        self.assertEqual(receipt['status'],'SUCCEEDED',receipt)
        self.assertFalse(self.kernel._armed)
        self.assertEqual(self.api.inserted,[])
        self.assertEqual(self.kernel.simulation.opening_attempt_count(),0)
        self.assertEqual(self.kernel.ledger.query('SELECT confirmed_open_qty,allocation_adjustment_qty FROM position_cycles'),
            [{'confirmed_open_qty':0,'allocation_adjustment_qty':3}])
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.assertEqual(self.kernel.money.report()['cycles'][0]['remaining_quantity'],3)
        cid=self.cycle()['position_cycle_id']
        original_plan=self.cycle()['plan']
        self.restart()
        self.assertEqual(self.cycle()['plan'],original_plan)
        self.assertEqual(self.kernel.money.report()['cycles'][0]['remaining_quantity'],3)
        self.assertEqual(self.api.inserted,[])
        self.kernel.manual_close(cid,3);self.kernel.wait_idle();self.quote(1040)
        self.assertEqual([(o['offset'],o['volume']) for o in self.api.inserted],[('CLOSETODAY',1)])
        self.finish_last(1040)
        self.assertEqual([(o['offset'],o['volume']) for o in self.api.inserted],[('CLOSETODAY',1),('CLOSE',2)])
        self.finish_last(1040)
        report=self.kernel.money.report()
        self.assertIsNone(report['summary']['gross_realized_pnl'])
        self.assertEqual([o['effect'] for o in report['trades']],['REDUCE','REDUCE'])

    def test_reconnect_on_same_day_disarms_and_checks_new_epoch(self):
        self.start_manual()
        self.assertTrue(self.kernel._armed)
        self.api._data['notify']['lost']={'code':2019112910}
        self.session.step()
        self.assertFalse(self.kernel._armed)
        self.assertTrue(self.kernel.recovery_required)
        self.api._data['notify']['restored']={'code':2019112902}
        self.quote(1041)
        self.assertFalse(self.kernel._armed)
        self.assertFalse(self.kernel.recovery_required)
        self.assertTrue(self.kernel.simulation.days.ready('2026-11-10'))
        self.assertEqual(self.api.inserted,[])

    def test_dual_sided_adoption_manages_two_independent_close_buckets(self):
        from online_trader.kernel.workbench import workbench_view
        self.start_manual('SHFE.bu2701')
        native=self.api.positions[self.code]
        native.pos_long=1;native.pos_long_today=1;native.open_price_long=1040.
        native.pos_short=2;native.pos_short_his=2;native.open_price_short=1040.
        rows=workbench_view(self.kernel)['external_positions']
        self.assertEqual({r['direction'] for r in rows if r['eligible']},{'LONG','SHORT'})
        row=rows[0];params={key:row[key] for key in ('contract_id','direction','evidence_hash')}
        scope={'scope_type':'CONTRACT','scope_ids':[self.code]}
        receipt=self.run_command('ADOPT_EXTERNAL_POSITION',params,scope)
        self.assertEqual(receipt['status'],'SUCCEEDED',receipt)
        self.assertEqual(len(receipt['evidence_ids']),2)
        policy='sim-execution-'+self.grant.content_hash[:24]
        flatten=self.run_command('FLATTEN_THEN_STOP',{'execution_policy_id':policy})
        self.quote(1040)
        self.assertEqual({(o['direction'],o['offset'],o['volume']) for o in self.api.inserted},
            {('SELL','CLOSETODAY',1),('BUY','CLOSE',2)})
        for order in self.api.inserted.copy():
            self.api.fill(order['order_id'],order['volume'],1040)
        self.session.step()
        self.assertTrue(all(c['lifecycle']=='CLOSED' for c in self.kernel._staged.values()))
        status=self.kernel.control.get_status(flatten['command_id'],flatten['payload_hash']).result()
        self.assertEqual(status['status'],'SUCCEEDED',status)

    def test_account_cancel_confirms_exact_foreign_terminal_without_replay(self):
        from types import SimpleNamespace
        from online_trader.kernel.workbench import workbench_view
        self.start_manual()
        native=SimpleNamespace(exchange_id='DCE',instrument_id='p2701',direction='BUY',offset='OPEN',
            status='ALIVE',volume_orign=2,volume_left=2,limit_price=1040.,trade_records={})
        self.api.orders['other-1']=native
        review=workbench_view(self.kernel)['configuration']['native_cancel_review']
        self.assertTrue(review['eligible'])
        params={'evidence_hash':review['evidence_hash']}
        self.api.orders['other-2']=native
        self.assertEqual(self.run_command('CANCEL_NATIVE_ACCOUNT',params)['status'],'REJECTED')
        del self.api.orders['other-2']
        receipt=self.run_command('CANCEL_NATIVE_ACCOUNT',params)
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.assertEqual(self.api.cancelled,['other-1'])
        self.assertEqual(self.run_command('CANCEL_NATIVE_ACCOUNT',params)['error_code'],'E_UNKNOWN_SEND')
        native.status='FINISHED'
        self.kernel.control.poll();self.kernel.wait_idle()
        stored=self.kernel.control.get_status(receipt['command_id'],receipt['payload_hash']).result()
        self.assertEqual(stored['status'],'SUCCEEDED',stored)
        self.assertEqual(self.api.cancelled,['other-1'])
        self.assertFalse(self.kernel._armed)

    def test_manual_long_and_account_query_preserve_command_receipts(self):
        self.start_manual()
        receipt=self.run_command('REQUEST_RECONCILIATION',{'requested_scope':'FULL_ACCOUNT','position_cycle_id':None})
        self.assertEqual(receipt['status'],'SUCCEEDED',receipt)
        parts=self.kernel.ledger.query('SELECT part_kind FROM reconciliation_parts WHERE reconciliation_id=?',receipt['evidence_ids'])
        self.assertEqual({r['part_kind'] for r in parts},{'FUNDS','POSITIONS','ORDERS','TRADES'})
        receipt=self.open_manual('LONG')
        self.assertEqual(receipt['status'],'EXECUTING',receipt)
        self.assertEqual(self.api.inserted[-1]['direction'],'BUY')
