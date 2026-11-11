"""Public comparison commands do not import or rewrite trade facts."""

from contextlib import redirect_stdout,redirect_stderr
import csv
from io import StringIO
import json
import unittest
import zipfile

from online_trader.cli import main
from online_trader.reporting import read_report
import test_statement_workflow as fixtures


class ReconcileCommandTests(unittest.TestCase):
    def setUp(self):
        self.env=fixtures.StatementWorkflowTests();self.env.setUp();self.env.seed()

    def tearDown(self):self.env.tearDown()

    def args(self,action):
        return ['reconcile',action,'--data-dir',str(self.env.folder),'--account-key',self.env.env.key,
                '--input',str(self.env.source),'--trading-day','2026-11-09']

    def counts(self):
        report=read_report(self.env.env.path,self.env.env.key)
        return report['checkpoint_event_seq'],len(report['fee_imports'])

    def preview(self):
        stream=StringIO()
        with redirect_stdout(stream):self.assertEqual(main(self.args('preview')),0)
        return json.loads(stream.getvalue())

    def test_preview_is_read_only_and_shows_exact_source_fee_proposals(self):
        before=self.counts();preview=self.preview()
        self.assertFalse(preview['blocking']);self.assertEqual(len(preview['actions']),2)
        self.assertEqual(preview['summary']['total_fee_amount'],'9')
        self.assertEqual(self.counts(),before)

    def test_csv_json_and_xlsx_exports_use_the_same_real_ledger_comparison(self):
        before=self.counts()
        for kind in ('csv','json','xlsx'):
            path=self.env.folder/('actual-comparison.'+kind);stream=StringIO()
            with redirect_stdout(stream):self.assertEqual(main(self.args('export')+['--output',str(path)]),0)
            self.assertFalse(json.loads(stream.getvalue())['trading_authority'])
            if kind=='csv':
                with path.open(encoding='utf-8-sig',newline='') as source:rows=list(csv.DictReader(source))
                self.assertEqual(len(rows),2);self.assertEqual(rows[0]['status'],'MATCHED_FEE_NEW')
            elif kind=='json':
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['schema_version'],'FILL_RECONCILIATION_EXPORT/1')
            else:
                with zipfile.ZipFile(path) as workbook:self.assertIn('xl/worksheets/sheet3.xml',workbook.namelist())
        self.assertEqual(self.counts(),before)

    def test_register_uses_the_review_hash_then_replays_without_new_events(self):
        preview=self.preview();args=self.args('register')+['--preview-hash',preview['preview_hash']]
        stream=StringIO()
        with redirect_stdout(stream):self.assertEqual(main(args),0)
        receipt=json.loads(stream.getvalue());self.assertEqual(receipt['status'],'REGISTERED')
        self.assertFalse(receipt['trading_authority'])
        report=read_report(self.env.env.path,self.env.env.key)
        self.assertEqual(report['summary']['net_realized_pnl'],'-639')
        before=self.counts();stream=StringIO()
        with redirect_stdout(stream):self.assertEqual(main(args),0)
        self.assertEqual(json.loads(stream.getvalue())['status'],'ALREADY_REGISTERED')
        self.assertEqual(self.counts(),before)

    def test_changed_source_cannot_use_the_previously_reviewed_hash(self):
        preview=self.preview();before=self.counts()
        self.env.rows[0]['fee']='4';self.env.write(self.env.rows)
        errors=StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(self.args('register')+['--preview-hash',preview['preview_hash']]),2)
        self.assertIn('E_PREVIEW_STALE',errors.getvalue());self.assertEqual(self.counts(),before)

    def test_wrong_account_and_existing_export_path_fail_without_changes(self):
        before=self.counts();args=self.args('preview');args[args.index('--account-key')+1]='DEV:foreign'
        errors=StringIO()
        with redirect_stderr(errors):self.assertEqual(main(args),2)
        self.assertIn('E_SCOPE',errors.getvalue())
        path=self.env.folder/'keep.xlsx';path.write_bytes(b'keep');errors=StringIO()
        with redirect_stderr(errors):self.assertEqual(main(self.args('export')+['--output',str(path)]),2)
        self.assertIn('EXISTS',errors.getvalue());self.assertEqual(path.read_bytes(),b'keep')
        self.assertEqual(self.counts(),before)

    def test_unknown_source_fees_are_no_op_without_a_maintenance_boot(self):
        for row in self.env.rows:row['fee']=''
        self.env.write(self.env.rows);preview=self.preview();before=self.counts();stream=StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(self.args('register')+['--preview-hash',preview['preview_hash']]),0)
        self.assertEqual(json.loads(stream.getvalue())['status'],'NO_FEE_CHANGES')
        self.assertEqual(self.counts(),before)


if __name__=='__main__':unittest.main()
