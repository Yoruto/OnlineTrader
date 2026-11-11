"""External files remain review data until full owned-fill identity agrees."""

import copy
import csv
from hashlib import sha256
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes,content_hash
from online_trader.errors import TraderError
from online_trader.reconciliation import load_statement,compare_statement


DAY='2026-11-10'
ACCOUNT='DEV:statement-owner'
HEADERS=['account_key','environment','trading_day','trade_key','contract_id','side','effect','quantity','price','fee','time']


def local_trade(**changes):
    row={'trade_key':'owned-entry','position_cycle_id':'owned-cycle','contract_id':'DCE.p2701',
         'direction':'LONG','effect':'OPEN','quantity':2,'price':'1055','trading_day':DAY,
         'event_seq':'2','observed_at_utc':DAY+'T01:06:00Z','fee':None,
         'fee_quality':'UNKNOWN','fee_revision':None,'fee_evidence_id':None}
    row.update(changes);return row


def external(**changes):
    row={'account_key':ACCOUNT,'environment':'ONLINE_SIM','trading_day':DAY,'trade_key':'owned-entry',
         'contract_id':'DCE.p2701','side':'BUY','effect':'OPEN','quantity':'2','price':'1055',
         'fee':'3','time':DAY+'T01:06:00Z'}
    row.update(changes);return row


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'statement.csv'

    def write(self,rows,*,headers=None,encoding='utf-8-sig',delimiter=','):
        headers=headers or HEADERS
        stream=io.StringIO(newline='');writer=csv.writer(stream,delimiter=delimiter)
        writer.writerow(headers)
        for row in rows:writer.writerow([row.get(field,'') for field in headers])
        self.path.write_bytes(stream.getvalue().encode(encoding));return load_statement(self.path)

    def report(self,rows=None,**changes):
        result={'schema_version':'MONEY_REPORT/1','account_key':ACCOUNT,'environment':'ONLINE_SIM',
                'currency':'CNY','checkpoint_event_seq':'10','trades':rows if rows is not None else [local_trade()]}
        result.update(changes);return result

    def compare(self,rows=None,*,statement=None,report=None,**scope):
        statement=statement or self.write(rows if rows is not None else [external()])
        return compare_statement(statement,report or self.report(),environment=scope.get('environment','ONLINE_SIM'),
            trading_day=scope.get('trading_day',DAY),source_account=scope.get('source_account'))

    def test_strict_known_identity_forms_one_confirmed_fee_proposal(self):
        preview=self.compare()
        self.assertFalse(preview['blocking']);self.assertEqual(preview['rows'][0]['status'],'MATCHED_FEE_NEW')
        action=preview['actions'][0]
        self.assertEqual((action['trade_key'],action['amount'],action['quality'],action['revision']),('owned-entry','3','CONFIRMED',1))
        self.assertIsNone(action['before_fee']);self.assertEqual(preview['summary']['total_fee_amount'],'3')

    def test_chinese_aliases_and_gb18030_preserve_explicit_scope_and_zero_fee(self):
        headers=['资金账号','环境','交易日期','成交编号','合约代码','买卖方向','开平标志','手数','成交价','手续费','成交时间']
        row=dict(zip(headers,[ACCOUNT,'模拟','20261110','owned-entry','DCE.p2701','买','开仓','2','1055.00','0','09:06:00']))
        statement=self.write([row],headers=headers,encoding='gb18030')
        self.assertEqual(statement['encoding'],'gb18030')
        preview=self.compare(statement=statement)
        self.assertFalse(preview['blocking']);self.assertEqual(preview['actions'][0]['amount'],'0')

    def test_utf8_bom_and_uppercase_english_headers_are_supported_exactly(self):
        row={key.upper():value for key,value in external().items()}
        preview=self.compare(statement=self.write([row],headers=[field.upper() for field in HEADERS]))
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),1)

    def test_tab_separated_input_keeps_the_same_economic_statement_identity(self):
        comma=self.write([external()]);tab=self.write([external()],delimiter='\t')
        self.assertEqual(tab['delimiter'],'\t');self.assertEqual(comma['statement_hash'],tab['statement_hash'])
        self.assertNotEqual(comma['source_sha256'],tab['source_sha256'])

    def test_legacy_four_column_file_is_visible_but_cannot_supply_fee_scope(self):
        row={'时间':'09:06:00','合约':'DCE.p2701','手数':'2','价格':'1055'}
        preview=self.compare(statement=self.write([row],headers=list(row)))
        self.assertEqual(preview['rows'][0]['status'],'INCOMPLETE');self.assertTrue(preview['blocking'])
        self.assertEqual(preview['actions'],[]);self.assertEqual(preview['rows'][0]['quantity'],2)

    def test_missing_volume_never_defaults_to_one(self):
        preview=self.compare([external(quantity='')])
        self.assertEqual(preview['rows'][0]['status'],'INVALID_ROW')
        self.assertIsNone(preview['rows'][0]['quantity']);self.assertEqual(preview['actions'],[])

    def test_invalid_or_fractional_volume_is_an_explained_row_failure(self):
        for quantity in ('0','-1','2.0','1e2','true','2147483648'):
            with self.subTest(quantity=quantity):
                preview=self.compare([external(quantity=quantity)])
                self.assertTrue(preview['blocking']);self.assertIn('INVALID_QUANTITY',preview['rows'][0]['issues'])

    def test_numeric_text_is_canonicalized_without_binary_float(self):
        preview=self.compare([external(price='001055.000',fee='03.2500')])
        self.assertEqual(preview['rows'][0]['price'],'1055');self.assertEqual(preview['actions'][0]['amount'],'3.25')

    def test_invalid_price_and_non_finite_or_scientific_fee_values_are_not_zero(self):
        for changed in ({'price':'0'},{'price':'NaN'},{'price':'1055e0'},
                        {'fee':'-1'},{'fee':'Infinity'},{'fee':'1e-2'}):
            with self.subTest(changed=changed):
                preview=self.compare([external(**changed)])
                self.assertTrue(preview['blocking']);self.assertEqual(preview['actions'],[])

    def test_unknown_fee_remains_unknown_even_for_an_exact_owned_match(self):
        preview=self.compare([external(fee='')])
        self.assertFalse(preview['blocking']);self.assertEqual(preview['rows'][0]['status'],'MATCHED_FEE_UNKNOWN')
        self.assertEqual(preview['actions'],[]);self.assertIsNone(preview['rows'][0]['fee'])

    def test_explicit_zero_fee_is_a_confirmable_source_fact(self):
        preview=self.compare([external(fee='0')])
        self.assertEqual(preview['actions'][0]['amount'],'0');self.assertFalse(preview['blocking'])

    def test_invalid_date_is_not_inferred_from_filename_or_requested_day(self):
        self.path=self.path.with_name('20261110-trades.csv')
        preview=self.compare([external(trading_day='2026-02-30')])
        self.assertEqual(preview['rows'][0]['status'],'INVALID_ROW');self.assertTrue(preview['blocking'])

    def test_trade_time_is_checked_but_does_not_select_an_owned_fill(self):
        for value in ('25:01:00','09:61:00','09:00:60','2026-02-30 09:00:00'):
            with self.subTest(time=value):
                preview=self.compare([external(time=value)])
                self.assertIn('INVALID_TIME',preview['rows'][0]['issues'])
        preview=self.compare([external(trade_key='not-owned',time=DAY+'T01:06:00Z')])
        self.assertEqual(preview['rows'][0]['status'],'EXTERNAL_ONLY');self.assertEqual(preview['actions'],[])

    def test_timestamp_format_aliases_share_a_normalized_identity(self):
        plain=self.write([external(time=DAY+'T01:06:00Z')])
        precise=self.write([external(time=DAY+'T01:06:00.000000Z')])
        self.assertEqual(plain['statement_hash'],precise['statement_hash'])

    def test_wrong_account_blocks_the_whole_batch_despite_an_exact_id(self):
        preview=self.compare([external(account_key='broker-account-2')])
        self.assertEqual(preview['rows'][0]['status'],'SCOPE_MISMATCH');self.assertTrue(preview['blocking'])
        self.assertEqual(preview['actions'],[])

    def test_explicit_source_account_binding_still_requires_every_source_account_cell(self):
        preview=self.compare([external(account_key='broker-account-2')],source_account='broker-account-2')
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),1)
        missing=self.compare([external(account_key='')],source_account='broker-account-2')
        self.assertTrue(missing['blocking']);self.assertEqual(missing['actions'],[])

    def test_missing_environment_is_not_filled_from_the_requested_scope(self):
        preview=self.compare([external(environment='')])
        self.assertEqual(preview['rows'][0]['status'],'INCOMPLETE');self.assertTrue(preview['blocking'])

    def test_live_and_simulation_source_scopes_never_mix(self):
        preview=self.compare([external(environment='实盘')])
        self.assertEqual(preview['rows'][0]['status'],'SCOPE_MISMATCH');self.assertTrue(preview['blocking'])
        with self.assertRaises(TraderError):self.compare(environment='LIVE')

    def test_requested_trading_day_is_checked_for_each_row(self):
        preview=self.compare([external(trading_day='2026-11-09')])
        self.assertEqual(preview['rows'][0]['status'],'SCOPE_MISMATCH')
        self.assertIn('TRADING_DAY_MISMATCH',preview['rows'][0]['issues'])

    def test_foreign_explicit_currency_is_not_silently_ignored_as_an_extra_column(self):
        headers=HEADERS+['currency'];preview=self.compare(statement=self.write([external(currency='USD')],headers=headers))
        self.assertEqual(preview['rows'][0]['status'],'INVALID_ROW');self.assertIn('UNSUPPORTED_CURRENCY',preview['rows'][0]['issues'])
        cny=self.compare(statement=self.write([external(currency='人民币')],headers=headers))
        self.assertFalse(cny['blocking']);self.assertEqual(cny['currency'],'CNY')

    def test_exact_duplicates_are_visible_and_create_only_one_fee_action(self):
        single=self.write([external()]);duplicate=self.write([external(),external()])
        self.assertEqual(single['statement_hash'],duplicate['statement_hash'])
        preview=self.compare(statement=duplicate)
        self.assertEqual([row['status'] for row in preview['rows']],['MATCHED_FEE_NEW','DUPLICATE_EXACT'])
        self.assertEqual(len(preview['actions']),1);self.assertFalse(preview['blocking'])

    def test_same_source_identity_with_different_economics_blocks_both_rows(self):
        preview=self.compare([external(),external(fee='4')])
        self.assertEqual([row['status'] for row in preview['rows']],['DUPLICATE_CONFLICT','DUPLICATE_CONFLICT'])
        self.assertTrue(preview['blocking']);self.assertEqual(preview['actions'],[])

    def test_source_row_reordering_does_not_change_fee_evidence_identity(self):
        second=external(trade_key='not-owned',contract_id='DCE.m2701')
        one=self.compare([external(),second]);two=self.compare([second,external()])
        self.assertEqual(one['statement_hash'],two['statement_hash'])
        self.assertEqual(one['actions'][0]['evidence_id'],two['actions'][0]['evidence_id'])

    def test_local_and_external_missing_rows_are_both_visible_without_creating_fills(self):
        report=self.report([local_trade(),local_trade(trade_key='local-missing',event_seq='3')])
        before=copy.deepcopy(report)
        preview=self.compare([external(),external(trade_key='external-extra')],report=report)
        self.assertEqual(preview['summary']['external_only_rows'],1)
        self.assertEqual(preview['local_only'][0]['trade_key'],'local-missing')
        self.assertEqual(preview['local_only'][0]['status'],'LOCAL_ONLY')
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),1)
        self.assertEqual(report,before)

    def test_an_ambiguous_local_identity_never_chooses_the_first_candidate(self):
        report=self.report([local_trade(),local_trade(position_cycle_id='second-cycle',price='1054',event_seq='3')])
        preview=self.compare(report=report)
        self.assertEqual(preview['rows'][0]['status'],'AMBIGUOUS');self.assertTrue(preview['blocking'])
        self.assertEqual(preview['actions'],[])

    def test_contract_price_volume_side_and_effect_differences_are_individually_reviewable(self):
        for changed,code in (({'contract_id':'DCE.m2701'},'CONTRACT_ID_MISMATCH'),({'price':'1056'},'PRICE_MISMATCH'),
                             ({'quantity':'3'},'QUANTITY_MISMATCH'),({'side':'SELL'},'SIDE_MISMATCH'),({'effect':'REDUCE'},'EFFECT_MISMATCH')):
            with self.subTest(changed=changed):
                preview=self.compare([external(**changed)]);row=preview['rows'][0]
                self.assertEqual(row['status'],'CONFLICT');self.assertIn(code,row['issues']);self.assertTrue(preview['blocking'])
                self.assertEqual(row['local_price'],'1055');self.assertEqual(row['local_quantity'],2)

    def test_short_open_and_close_buy_sell_are_derived_from_position_direction(self):
        for effect,side in (('OPEN','SELL'),('REDUCE','BUY')):
            with self.subTest(effect=effect):
                preview=self.compare([external(effect=effect,side=side)],report=self.report([local_trade(direction='SHORT',effect=effect)]))
                self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),1)

    def test_already_confirmed_fee_same_amount_is_a_no_op_and_difference_is_blocked(self):
        report=self.report([local_trade(fee='3',fee_quality='CONFIRMED',fee_revision=2,fee_evidence_id='fee-proof')])
        same=self.compare(report=report)
        self.assertEqual(same['rows'][0]['status'],'ALREADY_CONFIRMED');self.assertEqual(same['actions'],[])
        changed=self.compare([external(fee='2')],report=report)
        self.assertEqual(changed['rows'][0]['status'],'FEE_CONFLICT');self.assertTrue(changed['blocking'])
        self.assertEqual(changed['rows'][0]['fee_delta'],'-1')

    def test_estimate_replacement_keeps_original_fee_revision_and_does_not_multiply_fee_by_quantity(self):
        report=self.report([local_trade(fee='5',fee_quality='ESTIMATED',fee_revision=4,fee_evidence_id='estimate-proof')])
        preview=self.compare(report=report);action=preview['actions'][0]
        self.assertEqual(preview['rows'][0]['status'],'MATCHED_ESTIMATED_REPLACEMENT')
        self.assertEqual(action['amount'],'3');self.assertEqual(action['revision'],5)
        self.assertEqual(action['before_fee'],{'amount':'5','quality':'ESTIMATED','revision':4,'evidence_id':'estimate-proof'})
        self.assertEqual(preview['rows'][0]['fee_delta'],'-2')

    def test_unproven_or_exhausted_estimate_revision_cannot_be_replaced(self):
        for revision,issue in ((None,'FEE_REVISION_UNPROVEN'),(2147483647,'FEE_REVISION_EXHAUSTED')):
            with self.subTest(revision=revision):
                preview=self.compare(report=self.report([local_trade(fee='5',fee_quality='ESTIMATED',fee_revision=revision)]))
                self.assertTrue(preview['blocking']);self.assertIn(issue,preview['rows'][0]['issues'])
                self.assertEqual(preview['actions'],[])

    def test_source_and_preview_hashes_are_exact_and_fill_proposals_bind_frozen_local_facts(self):
        statement=self.write([external()]);preview=self.compare(statement=statement)
        self.assertEqual(statement['source_sha256'],sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(preview['preview_hash'],content_hash({key:value for key,value in preview.items() if key!='preview_hash'}))
        local=local_trade();fields=('trade_key','contract_id','direction','effect','quantity','price','trading_day','event_seq')
        self.assertEqual(preview['actions'][0]['fill_binding_hash'],content_hash({key:local[key] for key in fields}))

    def test_unknown_sensitive_columns_never_enter_normalized_rows_or_semantic_hash(self):
        headers=HEADERS+['password','token'];one=self.write([external(password='secret-password',token='secret-api-token')],headers=headers)
        two=self.write([external(password='changed-secret',token='changed-token')],headers=headers)
        self.assertEqual(one['statement_hash'],two['statement_hash'])
        self.assertNotEqual(one['source_sha256'],two['source_sha256'])
        raw=canonical_bytes(self.compare(statement=one))
        self.assertNotIn(b'secret-password',raw);self.assertNotIn(b'secret-api-token',raw)
        self.assertNotIn('password',one['columns'])

    def test_duplicate_alias_columns_and_substring_headers_are_not_guessed(self):
        headers=HEADERS+['成交编号']
        with self.assertRaises(TraderError) as error:self.write([external(**{'成交编号':'different'})],headers=headers)
        self.assertEqual(error.exception.code,'E_STATEMENT_COLUMNS')
        with self.assertRaises(TraderError):self.write([{'my_symbol_secret':'DCE.p2701'}],headers=['my_symbol_secret'])

    def test_malformed_csv_encoding_and_nul_are_rejected_without_lossy_recovery(self):
        for raw,code in ((b'symbol,quantity,price\n"broken,1,1055\n','E_STATEMENT_FORMAT'),
                         (b'\x81','E_STATEMENT_ENCODING'),(b'symbol,quantity,price\nDCE.p2701,1,10\x00\n','E_STATEMENT_FORMAT')):
            with self.subTest(code=code):
                self.path.write_bytes(raw)
                with self.assertRaises(TraderError) as error:load_statement(self.path)
                self.assertEqual(error.exception.code,code)

    def test_byte_row_column_and_cell_capacity_limits_fail_explicitly(self):
        self.path.write_bytes(b'symbol,quantity,price\nDCE.p2701,2,1055\n')
        with patch('online_trader.reconciliation.MAX_STATEMENT_BYTES',8),self.assertRaises(TraderError):load_statement(self.path)
        with patch('online_trader.reconciliation.MAX_STATEMENT_ROWS',1),self.assertRaises(TraderError):self.write([external(),external()])
        with patch('online_trader.reconciliation.MAX_STATEMENT_COLUMNS',3),self.assertRaises(TraderError):self.write([external()])
        with patch('online_trader.reconciliation.MAX_CELL_CHARACTERS',5),self.assertRaises(TraderError):self.write([external()])

    def test_bad_row_width_is_visible_and_blocks_registration(self):
        self.path.write_text('symbol,quantity,price\nDCE.p2701,2,1055,unexpected\n',encoding='utf-8')
        preview=self.compare(statement=load_statement(self.path))
        self.assertTrue(preview['blocking']);self.assertIn('COLUMN_COUNT_MISMATCH',preview['rows'][0]['issues'])

    def test_changed_normalized_source_content_without_matching_hash_is_refused(self):
        statement=self.write([external()]);statement['rows'][0]['fee']='8'
        with self.assertRaises(TraderError) as error:self.compare(statement=statement)
        self.assertEqual(error.exception.code,'E_STATEMENT_INTEGRITY')

    def test_python_float_or_boolean_cannot_become_a_normalized_money_or_volume(self):
        for field,value in (('fee',0.1),('quantity',True)):
            statement=self.write([external()]);statement['rows'][0][field]=value
            with self.subTest(field=field),self.assertRaises(TraderError):self.compare(statement=statement)

    def test_comparison_and_actions_are_deep_copies_and_never_change_inputs(self):
        statement=self.write([external()]);report=self.report();before=(copy.deepcopy(statement),copy.deepcopy(report))
        preview=self.compare(statement=statement,report=report)
        self.assertEqual((statement,report),before)
        preview['rows'][0]['price']='1';preview['actions'][0]['amount']='99'
        self.assertEqual((statement,report),before)

    def test_local_other_days_are_not_falsely_reported_missing_for_the_selected_day(self):
        preview=self.compare(report=self.report([local_trade(),local_trade(trade_key='prior-day',trading_day='2026-11-09',event_seq='1')]))
        self.assertEqual(preview['local_only'],[]);self.assertEqual(preview['summary']['local_rows'],1)


if __name__=='__main__':unittest.main()
