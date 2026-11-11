"""The public reports workflow only reads committed data and exports new files."""

from contextlib import redirect_stdout,redirect_stderr
import csv
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from online_trader.cli import main
from online_trader.ledger import Ledger
import test_reporting as fixtures


class ReportCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.key='DEV:report-cli-'+uuid4().hex
        self.ledger=Ledger(self.folder/'ledger.db',self.key);self.addCleanup(self.ledger.close)
        self.ledger.bootstrap('report-cli-boot-'+uuid4().hex,'a'*64)
        source=fixtures.ReportingTests();source.key=self.key
        for effect in ('OPEN','REDUCE'):
            self.ledger.submit('TX_FILL','CLI_ANONYMOUS_CONFIRMED_FACT',source.data(effect,fee='1')).result(timeout=3)

    def args(self,action):return ['reports',action,'--data-dir',str(self.folder),'--account-key',self.key]

    def test_show_reads_real_ledger_without_creating_an_execution_boot(self):
        before=self.ledger.query('SELECT COUNT(*) AS n FROM kernel_boots')[0]['n'];out=StringIO()
        with redirect_stdout(out):self.assertEqual(main(self.args('show')),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['summary']['gross_realized_pnl'],'200')
        self.assertEqual(result['summary']['net_realized_pnl'],'198')
        self.assertEqual(result['boundary'],'DATABASE_COMMITTED')
        self.assertEqual(self.ledger.query('SELECT COUNT(*) AS n FROM kernel_boots')[0]['n'],before)

    def test_export_csv_and_json_through_the_public_command(self):
        for kind in ('csv','json'):
            target=self.folder/('report.'+kind);out=StringIO()
            with redirect_stdout(out):self.assertEqual(main(self.args('export')+['--output',str(target)]),0)
            result=json.loads(out.getvalue());self.assertFalse(result['broker_connected'])
            if kind=='csv':
                with target.open(encoding='utf-8-sig',newline='') as stream:rows=list(csv.DictReader(stream))
                self.assertEqual(len(rows),2);self.assertEqual(rows[0]['fee_quality'],'CONFIRMED')
            else:self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['summary']['net_realized_pnl'],'198')

    def test_export_refuses_to_overwrite_an_existing_file(self):
        target=self.folder/'existing.csv';target.write_text('keep',encoding='utf-8');err=StringIO()
        with redirect_stderr(err):self.assertEqual(main(self.args('export')+['--output',str(target)]),2)
        self.assertIn('E_REPORT_EXISTS',err.getvalue());self.assertEqual(target.read_text(encoding='utf-8'),'keep')

    def test_wrong_account_cannot_read_or_export_another_scope(self):
        target=self.folder/'wrong-scope.json';err=StringIO();args=self.args('export');args[-1]='DEV:other'
        with redirect_stderr(err):self.assertEqual(main(args+['--output',str(target)]),2)
        self.assertIn('E_SCOPE',err.getvalue());self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
