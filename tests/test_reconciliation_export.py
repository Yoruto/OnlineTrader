"""Comparison exports preserve review evidence without modifying or applying it."""

import copy
import csv
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import zipfile

from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError
from online_trader.reconciliation_export import export_comparison


STATUSES=('INCOMPLETE','INVALID_ROW','SCOPE_MISMATCH','DUPLICATE_EXACT','DUPLICATE_CONFLICT',
    'EXTERNAL_ONLY','AMBIGUOUS','CONFLICT','MATCHED_FEE_UNKNOWN','ALREADY_CONFIRMED','FEE_CONFLICT',
    'MATCHED_FEE_NEW','MATCHED_ESTIMATED_REPLACEMENT')
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r':'http://schemas.openxmlformats.org/package/2006/relationships',
    'c':'http://schemas.openxmlformats.org/package/2006/content-types'}


def signed(preview):
    preview['preview_hash']=content_hash({key:value for key,value in preview.items() if key!='preview_hash'})
    return preview


def preview(statuses=STATUSES):
    rows=[]
    for index,status in enumerate(statuses,2):
        rows.append({'row_number':index,'status':status,'issues':['ROW_REVIEW'] if index==2 else [],
            'account_key':'DEV:comparison-export','environment':'ONLINE_SIM','trading_day':'2026-11-10',
            'currency':'CNY','trade_key':'external-'+str(index),'contract_id':'DCE.p2701','side':'BUY',
            'effect':'OPEN','quantity':2,'price':'1055.125','fee':None if index==2 else '0',
            'time':'09:00:00','local_trade_key':'owned-'+str(index),
            'candidate_trade_keys':['owned-'+str(index)],'local_price':'1055.1','local_quantity':2,
            'local_fee':'1.25','local_fee_quality':'ESTIMATED','fee_delta':'-1.25'})
    return signed({'schema_version':'FILL_RECONCILIATION/1','account_key':'DEV:comparison-export',
        'environment':'ONLINE_SIM','trading_day':'2026-11-10','currency':'CNY','source_account':'DEV:comparison-export',
        'source_sha256':'a'*64,'statement_hash':'b'*64,'checkpoint_event_seq':'18','rows':rows,
        'local_only':[{'trade_key':'only-owned','contract_id':'DCE.p2701','direction':'LONG','side':'SELL',
            'effect':'REDUCE','quantity':1,'price':'1044.25','fee':None,'fee_quality':'UNKNOWN',
            'trading_day':'2026-11-10','observed_at_utc':'2026-11-10T01:00:02Z',
            'position_cycle_id':'owned-cycle','status':'LOCAL_ONLY'}],
        'actions':[{'trade_key':'owned-2','amount':'0','quality':'CONFIRMED','revision':2,
            'evidence_id':'statement-fee-proof','fill_binding_hash':'c'*64,'row_number':2,
            'before_fee':{'amount':'1.25','quality':'ESTIMATED','revision':1,'evidence_id':'old-fee-proof'}}],
        'blocking':True,'summary':{'external_rows':len(rows),'local_rows':len(rows)+1,'matched_rows':3,
            'local_only_rows':1,'external_only_rows':1,'duplicate_rows':2,'blocking_rows':8,
            'fee_action_count':1,'total_fee_amount':'0','by_status':{status:1 for status in statuses}}})


class ReconciliationExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def export(self,value=None,kind='csv',name=None):
        target=self.folder/(name or 'comparison.'+kind)
        return export_comparison(preview() if value is None else value,target,format=kind)

    def csv_rows(self,path):
        return list(csv.DictReader(StringIO(path.read_text(encoding='utf-8-sig'))))

    @staticmethod
    def cells(archive,sheet):
        root=ET.fromstring(archive.read('xl/worksheets/sheet'+str(sheet)+'.xml'))
        return {node.attrib['r']:''.join(node.itertext()) for node in root.findall('.//s:c',NS)}

    def test_csv_bom_and_every_status_are_exported_without_hiding_blocking_rows(self):
        path=self.export();self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        rows=self.csv_rows(path)
        self.assertEqual([row['status'] for row in rows[:-1]],list(STATUSES))
        self.assertEqual(len(rows),len(STATUSES)+1)
        self.assertTrue(all(row['preview_hash']==preview()['preview_hash'] for row in rows))
        self.assertTrue(all(row['currency']=='CNY' for row in rows))

    def test_csv_keeps_both_external_only_and_local_only_evidence(self):
        rows=self.csv_rows(self.export())
        external=next(row for row in rows if row['status']=='EXTERNAL_ONLY')
        local=next(row for row in rows if row['row_type']=='LOCAL_ONLY')
        self.assertEqual(external['row_type'],'EXTERNAL');self.assertEqual(local['local_trade_key'],'only-owned')
        self.assertEqual(local['statement_price'],'');self.assertEqual(local['local_price'],'1044.25')
        self.assertEqual(local['local_fee'],'');self.assertIn('LOCAL_NOT_IN_STATEMENT',local['issues'])

    def test_missing_fee_is_empty_and_explicit_zero_is_not_made_unknown(self):
        rows=self.csv_rows(self.export())
        self.assertEqual(rows[0]['statement_fee'],'')
        self.assertEqual(rows[1]['statement_fee'],'0')
        self.assertEqual(rows[0]['local_fee'],'1.25');self.assertEqual(rows[0]['fee_delta'],'-1.25')

    def test_csv_records_original_scope_differences_and_candidate_identities(self):
        value=preview(('SCOPE_MISMATCH',));value['rows'][0]['account_key']='OTHER:source';signed(value)
        row=self.csv_rows(self.export(value))[0]
        self.assertEqual(row['account_key'],'DEV:comparison-export')
        self.assertEqual(row['statement_account_key'],'OTHER:source')
        self.assertEqual(json.loads(row['candidate_trade_keys']),['owned-2'])
        self.assertEqual(row['checkpoint_event_seq'],'18')

    def test_csv_neutralizes_formula_text_without_changing_real_negative_amounts(self):
        value=preview(('CONFLICT',));row=value['rows'][0]
        row['trade_key']='=HYPERLINK("https://example.invalid")';row['contract_id']=' @malicious'
        row['local_trade_key']='\t=1+1';value['source_account']='+formula';signed(value)
        out=self.csv_rows(self.export(value))[0]
        self.assertTrue(out['statement_trade_key'].startswith("'="))
        self.assertTrue(out['statement_contract_id'].startswith("' @"))
        self.assertTrue(out['local_trade_key'].startswith("'\t="))
        self.assertEqual(out['source_account'],"'+formula");self.assertEqual(out['fee_delta'],'-1.25')

    def test_json_is_a_display_schema_and_preserves_original_preview_identity(self):
        value=preview();before=copy.deepcopy(value)
        result=json.loads(self.export(value,'json').read_text(encoding='utf-8'))
        self.assertEqual(result['schema_version'],'FILL_RECONCILIATION_EXPORT/1')
        self.assertEqual(result['source_schema_version'],'FILL_RECONCILIATION/1')
        self.assertEqual(result['source_preview_hash'],value['preview_hash'])
        self.assertEqual(result['preview_hash'],value['preview_hash'])
        self.assertEqual(result['actions'][0]['before_fee']['quality'],'ESTIMATED')
        self.assertEqual(result['summary']['by_status']['AMBIGUOUS'],1)
        self.assertEqual(value,before)

    def test_json_recursively_whitelists_metadata_rows_actions_fees_and_issues(self):
        value=preview(('INVALID_ROW',));value['password']='TOP-SECRET-EXTRA'
        value['rows'][0]['api_token']='ROW-SECRET-EXTRA'
        value['rows'][0]['issues']=[{'code':'BAD_FIELD','field':'fee','password':'ISSUE-SECRET-EXTRA'}]
        value['actions'][0]['secret']='ACTION-SECRET-EXTRA'
        value['actions'][0]['before_fee']['password']='BEFORE-SECRET-EXTRA'
        value['local_only'][0]['password']='LOCAL-SECRET-EXTRA'
        value['summary']['token']='SUMMARY-SECRET-EXTRA';signed(value)
        raw=self.export(value,'json').read_text(encoding='utf-8')
        for forbidden in ('TOP-SECRET','ROW-SECRET','ISSUE-SECRET','ACTION-SECRET','BEFORE-SECRET','LOCAL-SECRET','SUMMARY-SECRET','password','api_token'):
            self.assertNotIn(forbidden,raw)
        result=json.loads(raw);self.assertEqual(result['rows'][0]['issues'],[{'code':'BAD_FIELD','field':'fee'}])

    def test_unknown_csv_columns_are_never_promoted_into_headers_or_values(self):
        value=preview(('CONFLICT',));value['rows'][0]['unknown_account_password']='hidden-private-value';signed(value)
        path=self.export(value);raw=path.read_text(encoding='utf-8-sig')
        self.assertNotIn('unknown_account_password',raw);self.assertNotIn('hidden-private-value',raw)

    def test_flat_fields_win_while_nested_normalized_statement_is_supported(self):
        value=preview(('MATCHED_FEE_NEW',));row=value['rows'][0]
        row['statement']={'price':'999','fee':'12','trade_key':'nested-wrong','unknown_secret':'keep-out'}
        del row['time'];row['statement']['time']='09:00:03';signed(value)
        out=json.loads(self.export(value,'json').read_text(encoding='utf-8'))['rows'][0]
        self.assertEqual(out['price'],'1055.125');self.assertIsNone(out['fee'])
        self.assertEqual(out['trade_key'],'external-2');self.assertEqual(out['time'],'09:00:03')
        self.assertNotIn('statement',out);self.assertNotIn('unknown_secret',out)

    def test_changed_preview_content_is_refused_before_any_destination_is_created(self):
        value=preview();value['rows'][0]['price']='1030'
        for kind in ('csv','json','xlsx'):
            target=self.folder/('tampered.'+kind)
            with self.subTest(kind=kind),self.assertRaises(TraderError) as error:export_comparison(value,target)
            self.assertEqual(error.exception.code,'E_COMPARISON_EXPORT_HASH');self.assertFalse(target.exists())

    def test_malformed_hash_schema_and_noncanonical_float_are_refused(self):
        for changed in ({'preview_hash':'x'*64},{'schema_version':'UNKNOWN/1'},{'blocking':1},{'checkpoint_event_seq':1.5}):
            value=preview();value.update(changed)
            if 'preview_hash' not in changed:signed(value) if 'checkpoint_event_seq' not in changed else None
            target=self.folder/'invalid.json'
            with self.subTest(changed=changed),self.assertRaises(TraderError):export_comparison(value,target)
            self.assertFalse(target.exists())

    def test_existing_file_is_never_overwritten_for_any_format(self):
        for kind in ('csv','json','xlsx'):
            target=self.folder/('keep.'+kind);target.write_bytes(b'original-file')
            with self.subTest(kind=kind),self.assertRaises(TraderError) as error:export_comparison(preview(),target)
            self.assertEqual(error.exception.code,'E_COMPARISON_EXPORT_EXISTS')
            self.assertEqual(target.read_bytes(),b'original-file')

    def test_unsupported_format_or_missing_directory_does_not_create_files(self):
        for target in (self.folder/'invalid.exe',self.folder/'missing'/'output.csv'):
            with self.subTest(target=target),self.assertRaises(TraderError):export_comparison(preview(),target)
            self.assertFalse(target.exists())
        with self.assertRaises(TraderError):export_comparison(preview(),self.folder/'bad.csv',format=3)

    def test_row_and_serialized_workbook_capacities_fail_without_partial_files(self):
        target=self.folder/'limited.xlsx'
        with patch('online_trader.reconciliation_export.MAX_ROWS',1),self.assertRaises(TraderError):
            export_comparison(preview(('CONFLICT','EXTERNAL_ONLY')),target)
        self.assertFalse(target.exists())
        value=preview(('CONFLICT',))
        with patch('online_trader.reconciliation_export.MAX_EXPORT_BYTES',6000),self.assertRaises(TraderError) as error:
            export_comparison(value,target)
        self.assertEqual(error.exception.code,'E_COMPARISON_EXPORT_LIMIT');self.assertFalse(target.exists())

    def test_xlsx_contains_a_valid_minimal_package_with_three_named_sheets(self):
        path=self.export(kind='xlsx')
        with zipfile.ZipFile(path) as archive:
            expected={'[Content_Types].xml','_rels/.rels','xl/workbook.xml','xl/_rels/workbook.xml.rels',
                'xl/styles.xml','xl/worksheets/sheet1.xml','xl/worksheets/sheet2.xml','xl/worksheets/sheet3.xml'}
            self.assertEqual(set(archive.namelist()),expected)
            for name in expected:ET.fromstring(archive.read(name))
            workbook=ET.fromstring(archive.read('xl/workbook.xml'))
            self.assertEqual([node.attrib['name'] for node in workbook.findall('s:sheets/s:sheet',NS)],['成交对照','本地缺失','概览'])
            types=ET.fromstring(archive.read('[Content_Types].xml'))
            self.assertTrue(any(node.attrib.get('ContentType')=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml' for node in types))

    def test_xlsx_relationships_stay_internal_and_have_no_formulas_macros_or_external_links(self):
        value=preview(('CONFLICT',));value['rows'][0]['trade_key']='=HYPERLINK("https://example.invalid")';signed(value)
        with zipfile.ZipFile(self.export(value,'xlsx')) as archive:
            root=ET.fromstring(archive.read('_rels/.rels'));self.assertEqual(root[0].attrib['Target'],'xl/workbook.xml')
            links=ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
            for node in links:
                self.assertNotIn('TargetMode',node.attrib)
                self.assertIn('xl/'+node.attrib['Target'],archive.namelist())
            self.assertFalse(any('external' in name.lower() or 'vba' in name.lower() for name in archive.namelist()))
            for index in range(1,4):
                sheet=ET.fromstring(archive.read('xl/worksheets/sheet'+str(index)+'.xml'))
                self.assertEqual(sheet.findall('.//s:f',NS),[])
            self.assertEqual(self.cells(archive,1)['D2'],'=HYPERLINK("https://example.invalid")')

    def test_xlsx_uses_inline_text_for_precise_money_and_leading_zero_identities(self):
        value=preview(('MATCHED_FEE_NEW',));row=value['rows'][0]
        row.update(trade_key='000000001234567890',price='999999999999999999.123456789012',fee='0.000000000001');signed(value)
        with zipfile.ZipFile(self.export(value,'xlsx')) as archive:
            cells=self.cells(archive,1)
            self.assertEqual(cells['D2'],'000000001234567890')
            self.assertEqual(cells['I2'],'999999999999999999.123456789012')
            self.assertEqual(cells['J2'],'0.000000000001')
            root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertTrue(all(node.attrib['t']=='inlineStr' for node in root.findall('.//s:c',NS)))

    def test_xlsx_headers_filters_freeze_panes_unknowns_and_fee_actions_are_visible(self):
        with zipfile.ZipFile(self.export(kind='xlsx')) as archive:
            for index in range(1,4):
                root=ET.fromstring(archive.read('xl/worksheets/sheet'+str(index)+'.xml'))
                self.assertEqual(root.find('s:sheetViews/s:sheetView/s:pane',NS).attrib['state'],'frozen')
                self.assertIsNotNone(root.find('s:autoFilter',NS));self.assertIsNotNone(root.find('s:cols',NS))
            main=self.cells(archive,1);self.assertEqual(main['B1'],'对照状态')
            self.assertNotIn('J2',main);self.assertEqual(main['J3'],'0')
            local=self.cells(archive,2);self.assertNotIn('H2',local);self.assertEqual(local['M2'],'LOCAL_ONLY')
            overview=self.cells(archive,3)
            self.assertIn('仅预览；未写入',overview.values());self.assertIn('statement-fee-proof',overview.values())
            self.assertIn(preview()['preview_hash'],overview.values());self.assertIn('1.25',overview.values())

    def test_invalid_xml_text_fails_before_creating_a_workbook(self):
        for bad in ('09:00:00\x00','\ufffe'):
            value=preview(('CONFLICT',));value['rows'][0]['time']=bad;signed(value)
            target=self.folder/'invalid-control.xlsx'
            with self.subTest(bad=bad),self.assertRaises(TraderError):export_comparison(value,target)
            self.assertFalse(target.exists())

    def test_write_failure_removes_only_the_new_partial_destination(self):
        target=self.folder/'partial.csv';original_open=Path.open
        class Broken:
            def __init__(self,path):self.file=original_open(path,'xb')
            def __enter__(self):return self
            def write(self,data):self.file.write(data[:5]);raise OSError('injected write failure')
            def __exit__(self,*_args):self.file.close()
        def opening(path,*args,**kwargs):
            return Broken(path) if path==target and args==('xb',) else original_open(path,*args,**kwargs)
        with patch('pathlib.Path.open',new=opening),self.assertRaises(TraderError):export_comparison(preview(),target)
        self.assertFalse(target.exists())

    def test_ledger_boot_and_facts_binding_survive_exports_without_unknown_secrets(self):
        value=preview(('MATCHED_FEE_NEW',));value.update(ledger_boot_id='boot-reviewed-comparison',
            ledger_facts_hash='d'*64,unrecognized_secret='private-extra-must-not-leak');signed(value)
        exported=json.loads(self.export(value,'json').read_text(encoding='utf-8'))
        self.assertEqual(exported['ledger_boot_id'],'boot-reviewed-comparison')
        self.assertEqual(exported['ledger_facts_hash'],'d'*64)
        self.assertNotIn('unrecognized_secret',exported)
        self.assertNotIn('private-extra-must-not-leak',json.dumps(exported))
        row=self.csv_rows(self.export(value,'csv'))[0]
        self.assertEqual(row['ledger_boot_id'],'boot-reviewed-comparison')
        self.assertEqual(row['ledger_facts_hash'],'d'*64)
        with zipfile.ZipFile(self.export(value,'xlsx')) as archive:
            cells=self.cells(archive,3)
            self.assertIn('boot-reviewed-comparison',cells.values());self.assertIn('d'*64,cells.values())


if __name__=='__main__':unittest.main()
