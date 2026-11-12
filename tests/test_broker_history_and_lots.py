"""Read-only vendor query and batch-proof regressions with offline facts."""
import io
import json
import unittest
from types import SimpleNamespace

import test_external_position_recovery as fixtures
from online_trader.errors import TraderError
from online_trader.gateway.qpro_history import query_trades
from online_trader.kernel.workbench import workbench_view


class QProHistoryTests(unittest.TestCase):
    def test_paginated_read_is_comparison_only_and_stays_on_loopback(self):
        requests=[]
        def open_page(request, *, timeout):
            requests.append(request)
            offset=json.loads(request.data)['offset']
            row={'futures_company_name':'Test broker','client_id':'001','trading_day':'2026-11-10',
                 'exchange':'SHFE','trade_id':f'000{offset}','direction':'BUY','price':1048.5}
            return io.BytesIO(json.dumps({'code':0,'data':{'total':2,'trades':[row]}}).encode())
        result=query_trades('Test broker','001','2026-11-10','2026-11-10',opener=SimpleNamespace(open=open_page))
        self.assertEqual(result['usage'],'COMPARISON_ONLY')
        self.assertEqual([t['trade_id'] for t in result['trades']],['0000','0001'])
        self.assertTrue(all(r.full_url=='http://127.0.0.1:8815/trades/query' for r in requests))

    def test_truncated_response_and_wrong_scope_refuse_comparison(self):
        response={'code':0,'data':{'total':2,'trades':[]}}
        opener=SimpleNamespace(open=lambda *a,**k:io.BytesIO(json.dumps(response).encode()))
        with self.assertRaises(TraderError):
            query_trades('Test broker','001','2026-11-10','2026-11-10',opener=opener)
        response['data']={'total':1,'trades':[{'futures_company_name':'Test broker','client_id':'another'}]}
        with self.assertRaises(TraderError):
            query_trades('Test broker','001','2026-11-10','2026-11-10',opener=opener)


class EqualLotTests(unittest.TestCase):
    setUp=fixtures.ExternalPositionRecoveryTests.setUp
    tearDown=fixtures.ExternalPositionRecoveryTests.tearDown
    start=fixtures.ExternalPositionRecoveryTests.start
    start_manual=fixtures.ExternalPositionRecoveryTests.start_manual
    open_manual=fixtures.ExternalPositionRecoveryTests.open_manual
    cycle=fixtures.ExternalPositionRecoveryTests.cycle
    quote=fixtures.ExternalPositionRecoveryTests.quote
    restart=fixtures.ExternalPositionRecoveryTests.restart
    message=fixtures.ExternalPositionRecoveryTests.message
    run_command=fixtures.ExternalPositionRecoveryTests.run_command

    def test_equal_review_keeps_the_specified_original_lot_through_restart(self):
        self.start_manual('SHFE.bu2701')
        self.assertEqual(self.open_manual('LONG')['status'],'EXECUTING')
        oid=self.api.inserted[0]['order_id']
        self.api.fill(oid,1,1048,trade_id='original-first')
        self.api.fill(oid,1,1048,trade_id='original-second')
        self.session.step()
        for foreign,direction,offset,price in (('foreign-close','SELL','CLOSETODAY',1049),('foreign-open','BUY','OPEN',1050)):
            self.api.insert_order(symbol=self.code,direction=direction,offset=offset,volume=1,limit_price=price,
                order_id=foreign,account=self.port.account)
            self.api.fill(foreign,1,price,trade_id=foreign+'-fill')
        self.api.positions[self.code].open_price_long=1049.
        row=workbench_view(self.kernel)['external_positions'][0]
        self.assertTrue(row['lot_review_available'])
        self.assertFalse(row['eligible'])
        self.assertEqual(set(row['original_lots']),{'kq-original-first','kq-original-second'})
        packet={'schema_version':'POSITION_LOT_REVIEW/1','account_key':self.account,
            'gateway_identity':self.port.gateway_identity,'calendar_hash':self.calendar.content_hash,
            'contract_id':self.code,'trading_day':row['trading_day'],'statement_reference':'offline-surviving-lots',
            'original_lots':[{'lot_key':'kq-original-first','remaining_quantity':1},
                {'lot_key':'kq-original-second','remaining_quantity':0}],
            'replacement_lots':[{'trade_id':'foreign-open-fill','remaining_quantity':1}]}
        scope={'scope_type':'CONTRACT','scope_ids':[self.code]}
        params={key:row[key] for key in ('contract_id','direction','evidence_hash')}
        wrong=packet|{'replacement_lots':[{'trade_id':'foreign-close-fill','remaining_quantity':1}]}
        self.assertEqual(self.run_command('ADOPT_EXTERNAL_POSITION',params|{'lot_review':wrong},scope)['status'],'REJECTED')
        missing=self.api.trades.pop('foreign-close-fill')
        incomplete=workbench_view(self.kernel)['external_positions'][0]['evidence_hash']
        self.assertEqual(self.run_command('ADOPT_EXTERNAL_POSITION',
            params|{'evidence_hash':incomplete,'lot_review':packet},scope)['status'],'REJECTED')
        self.api.trades['foreign-close-fill']=missing
        receipt=self.run_command('ADOPT_EXTERNAL_POSITION',params|{'lot_review':packet},scope)
        self.assertEqual(receipt['status'],'SUCCEEDED',receipt)
        self.quote(1048)
        self.restart()
        from online_trader.kernel.equal_lot_review import original_lots
        lots,_=original_lots(self.kernel.simulation,self.code,row['trading_day'])
        self.assertEqual(set(lots),{'kq-original-first','kq-foreign-open-fill'})
        self.assertEqual([c['protection']['owned_qty'] for c in self.kernel._staged.values()],[1,1])
        self.assertEqual(self.kernel.ledger.query('SELECT COUNT(*) AS n FROM fill_facts')[0]['n'],2)
        self.assertFalse(self.kernel._armed)
        self.assertEqual(self.api.inserted,[])


class LocalArchiveTests(unittest.TestCase):
    setUp=fixtures.HistoricalOrderRecoveryTests.setUp
    tearDown=fixtures.HistoricalOrderRecoveryTests.tearDown
    start=fixtures.HistoricalOrderRecoveryTests.start
    next_day=fixtures.HistoricalOrderRecoveryTests.next_day
    missing=fixtures.HistoricalOrderRecoveryTests.missing
    message=fixtures.HistoricalOrderRecoveryTests.message
    run_command=fixtures.HistoricalOrderRecoveryTests.run_command

    def query_archive(self):
        return self.run_command('REQUEST_RECONCILIATION',{'requested_scope':'FULL_ACCOUNT',
            'position_cycle_id':None,'query_order_archive':True})

    def test_query_uses_a_durable_terminal_observation_saved_before_interruption(self):
        self.missing(2)
        self.assertEqual(self.query_archive()['evidence_ids'],[])
        self.assertTrue(self.kernel.recovery_required)
        native=self.original_native_order;native.status='FINISHED'
        self.x.api.orders[self.oid]=native
        owner=self.kernel.simulation
        owner.capture('ONLINE_ORDER_OBSERVATIONS',{'gateway_identity':self.x.port.gateway_identity,
            'source_epoch':self.x.port.source_epoch,'orders':self.x.port.observations()})
        del self.x.api.orders[self.oid]
        receipt=self.query_archive()
        self.assertEqual(receipt['status'],'SUCCEEDED',receipt)
        self.assertEqual(len(receipt['evidence_ids']),1)
        self.x.session.step()
        order=self.x.cycle()['orders'][0]
        self.assertTrue(order['terminal_confirmed'])
        self.assertEqual(order['filled_qty'],2)
        self.assertEqual(self.x.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(owner.historical_orders.records[self.oid]['source_kind'],'DURABLE_NATIVE_ORDER_OBSERVATION')
