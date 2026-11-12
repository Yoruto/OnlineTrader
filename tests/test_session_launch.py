from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from online_trader.accounts import AccountStore
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.desktop.session import SessionDialog
from online_trader.desktop.accounts import AccountDialog
from online_trader.errors import TraderError
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.launch import SessionLauncher
from online_trader.runtime.dsv_simulation import run
from tests.test_accounts import MemoryVault
from tests import test_online_dsv as fixtures


class SessionLaunchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.vault=MemoryVault();self.store=AccountStore(self.root/'accounts',vault=self.vault)
        state=self.store.change('add',expected_revision=0,alias='离线档案',username='offline-synthetic-launch');self.row=state['accounts'][0]
        self.store.change('select',expected_revision=1,account_id=self.row['account_id'])
        self.profile=load_confirmed_baseline()[0];self.calendar=fixtures.day_calendar()
        self.grant=fixtures.scope_for(self.store.ledger_path(self.row),self.profile,self.calendar,account_fingerprint=self.row['account_key'].split(':')[-1])
        self.paths=[self.root/(name+'.json') for name in ('scope','profile','calendar')]
        for path,value in zip(self.paths,(self.grant.value,self.profile.to_record(),self.calendar.to_record())):path.write_bytes(canonical_bytes(value))
        self.launcher=SessionLauncher(self.store,self.row['account_id'],*self.paths)
    def test_review_and_dialog_prepare_do_not_read_credentials_or_start_process(self):
        with patch.object(self.vault,'get') as get,patch.object(TqKqDsvPort,'connect') as connect,patch('subprocess.Popen') as spawn:
            result=self.launcher.review();dialog=SessionDialog(self.store,self.row['account_id'])
            try:
                for key,path in zip(('scope','profile','calendar'),self.paths):dialog.fields[key].setText(str(path))
                dialog.prepare();self.assertIsNotNone(dialog.review_result);self.assertFalse(dialog.arm.isChecked())
            finally:dialog.close()
            get.assert_not_called();connect.assert_not_called();spawn.assert_not_called()
        self.assertFalse(result['broker_connected']);self.assertFalse(self.store.ledger_path(self.row).exists())
    def test_changed_scope_and_expired_window_refuse_before_any_process(self):
        review=self.launcher.review()
        with patch('subprocess.run') as probe,patch('subprocess.Popen') as spawn:
            with self.assertRaises(TraderError):self.launcher.launch(sys.executable,self.root/'run',expected_review_hash=review['review_hash'],now=datetime.fromisoformat('2026-11-11T01:00:00Z'))
            with self.assertRaises(TraderError):self.launcher.launch(sys.executable,self.root/'run',expected_review_hash='0'*64,now=datetime.fromisoformat('2026-11-10T02:00:00Z'))
            probe.assert_not_called();spawn.assert_not_called()
    def test_explicit_launch_builds_saved_account_arguments_with_no_plaintext_credentials(self):
        review=self.launcher.review()
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as probe,patch('subprocess.Popen',return_value=SimpleNamespace(pid=12345)) as spawn:
            result=self.launcher.launch(sys.executable,self.root/'offline-mocked-run',expected_review_hash=review['review_hash'],now=datetime.fromisoformat('2026-11-10T02:00:00Z'))
        argv=spawn.call_args.args[0];self.assertIn('--account-store',argv);self.assertNotIn('--arm',argv)
        self.assertNotIn('offline-synthetic-launch',' '.join(argv));self.assertNotIn('--credentials',argv)
        self.assertEqual(result['status'],'PROCESS_LAUNCHED_NOT_CONNECTED');self.assertFalse(result['broker_connected'])
        scope=Path(argv[argv.index('--scope')+1]);self.assertEqual(scope.parent,self.root/'offline-mocked-run')
        self.assertEqual(scope.read_bytes(),canonical_bytes(self.grant.value))

    def test_portable_launch_probes_sibling_cli_and_keeps_original_scope_arguments(self):
        executable=self.root/'OnlineTraderCLI.exe'
        executable.touch()
        review=self.launcher.review()
        with (patch('online_trader.runtime.launch.bundled_cli',return_value=executable),
              patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as probe,
              patch('subprocess.Popen',return_value=SimpleNamespace(pid=12345)) as spawn):
            self.launcher.launch(executable,self.root/'portable-run',expected_review_hash=review['review_hash'],
                                 now=datetime.fromisoformat('2026-11-10T02:00:00Z'))
        self.assertEqual(probe.call_args.args[0][0:2],[str(executable),'--verify-runtime'])
        argv=spawn.call_args.args[0]
        self.assertEqual(argv[:3],[str(executable),'dsv','run'])
        self.assertNotIn('--arm',argv)
        self.assertEqual(Path(argv[argv.index('--scope')+1]).read_bytes(),canonical_bytes(self.grant.value))

    def test_comparison_entrypoint_prefills_selected_account_and_statement_offline(self):
        from online_trader.desktop.entrypoint import main
        statement=self.root/'成交 明细.csv'
        with (patch('online_trader.desktop.reconciliation.run',return_value=0) as run_comparison,
              patch('online_trader.desktop.entrypoint.run') as workbench,
              patch.object(TqKqDsvPort,'connect') as connect):
            result=main(['--account-store',str(self.store.directory),'--compare-fills',str(statement)])
        self.assertEqual(result,0)
        run_comparison.assert_called_once_with(self.store.ledger_path(self.row),self.row['account_key'],
                                               statement_path=statement)
        workbench.assert_not_called()
        connect.assert_not_called()
    def test_grant_changed_during_interpreter_probe_cannot_launch_unreviewed_scope(self):
        def probe(*args,**kwargs):
            self.paths[0].write_bytes(canonical_bytes(self.grant.value|{'opening_attempts':21}))
            return SimpleNamespace(returncode=0)
        review=self.launcher.review()
        with patch('subprocess.run',side_effect=probe),patch('subprocess.Popen') as spawn:
            with self.assertRaises(TraderError):self.launcher.launch(sys.executable,self.root/'no-run',expected_review_hash=review['review_hash'],now=datetime.fromisoformat('2026-11-10T02:00:00Z'))
            spawn.assert_not_called();self.assertFalse((self.root/'no-run').exists())
    def test_wrong_product_or_sdk_interpreter_prevents_spawn(self):
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=1)),patch('subprocess.Popen') as spawn:
            with self.assertRaises(TraderError):self.launcher.launch(sys.executable,self.root/'no-run',expected_review_hash=self.launcher.review()['review_hash'],now=datetime.fromisoformat('2026-11-10T02:00:00Z'))
            spawn.assert_not_called();self.assertFalse((self.root/'no-run').exists())

    def paired_launcher(self,profile=None):
        profile=profile or self.profile
        packet=self.root/'paired';packet.mkdir()
        config=self.root/'DSV settings.json';config.write_text(profile.to_json(),encoding='utf-8')
        ref={'schema_version':'SIMNOW_CREDENTIAL_REFERENCE/1','account_fingerprint':'1234567890123456'}
        grant=fixtures.scope_for(packet/'ledger.db',profile,self.calendar,schema_version='SIMNOW_DSV_SIM/1',
            trading_days=['2026-11-10'],resume_entries_on_day_change=False,account_fingerprint=ref['account_fingerprint'])
        for name,value in {'scope':grant.value,'profile':profile.to_record(),'calendar':self.calendar.to_record(),
                           'simnow-credentials':ref,'dsv-comparison':{'config':str(config),'log_dir':str(self.root)}}.items():
            (packet/(name+'.json')).write_bytes(canonical_bytes(value))
        return SessionLauncher.for_simnow(packet),grant,ref,config

    def test_paired_desktop_keeps_preopen_login_and_entry_windows_distinct(self):
        launcher,grant,ref,config=self.paired_launcher()
        review=launcher.review()
        dialog=SessionDialog(None,None,paired_launcher=launcher)
        try:
            self.assertEqual(dialog.review_result,review)
            self.assertFalse(dialog.arm.isChecked())
        finally:dialog.close()
        with patch('subprocess.run',return_value=SimpleNamespace(returncode=0)) as probe,patch('subprocess.Popen',return_value=SimpleNamespace(pid=42)) as spawn:
            with self.assertRaises(TraderError):
                launcher.launch(sys.executable,self.root/'too-early',expected_review_hash=review['review_hash'],
                    now=datetime.fromisoformat('2026-11-10T00:44:59Z'))
            probe.assert_not_called();spawn.assert_not_called()
            out=self.root/'paired-run'
            launcher.launch(sys.executable,out,expected_review_hash=review['review_hash'],arm=True,
                now=datetime.fromisoformat('2026-11-10T00:55:00Z'))
        argv=spawn.call_args.args[0]
        self.assertIn('simnow',argv);self.assertIn('--arm',argv);self.assertNotIn('--account-store',argv)
        self.assertEqual(argv[argv.index('--dsv-config')+1],str(config))
        self.assertEqual((out/'reviewed-credential-reference.json').read_bytes(),canonical_bytes(ref))
        self.assertEqual((out/'reviewed-scope.json').read_bytes(),canonical_bytes(grant.value))

    def test_timed_launch_matches_dsv_minutes_and_respects_scope_expiry(self):
        settings=self.profile.to_settings()
        settings['trade']['auto_start_on_launch']=False
        launcher,_,_,_=self.paired_launcher(DsvProfile.from_settings(settings))
        review=launcher.review()
        def trigger(value):
            return launcher.scheduled_trigger(expected_review_hash=review['review_hash'],now=datetime.fromisoformat(value))
        self.assertIsNone(trigger('2026-11-10T00:54:59Z'))
        self.assertEqual(trigger('2026-11-10T00:55:00Z'),'2026-11-10_day')
        self.assertEqual(trigger('2026-11-10T01:55:59Z'),'2026-11-10_day')
        self.assertIsNone(trigger('2026-11-10T01:56:00Z'))
        with self.assertRaises(TraderError):trigger('2026-11-10T07:00:00Z')

    def test_paired_timed_launch_requires_confirmation_and_uses_the_frozen_arm_choice(self):
        launcher,_,_,_=self.paired_launcher()
        review=launcher.review()
        at=datetime.fromisoformat('2026-11-10T00:45:00Z')
        self.assertEqual(launcher.scheduled_trigger(expected_review_hash=review['review_hash'],now=at),'AUTO_START_ON_LAUNCH')
        dialog=SessionDialog(None,None,paired_launcher=launcher)
        try:
            dialog.use_schedule.setChecked(True)
            dialog.arm.setChecked(True)
            with patch('online_trader.desktop.session.QMessageBox.question',return_value=0),patch.object(launcher,'launch') as launch:
                dialog.start()
                self.assertIsNone(dialog.scheduled_request)
                launch.assert_not_called()
            from PySide6.QtWidgets import QMessageBox
            with patch('online_trader.desktop.session.QMessageBox.question',return_value=QMessageBox.Yes), \
                    patch.object(launcher,'scheduled_trigger',return_value=None):
                dialog.start()
            self.assertIsNotNone(dialog.scheduled_request)
            dialog.arm.setChecked(False)
            with patch.object(launcher,'launch',return_value={'status':'OFFLINE_FAKE'}) as launch:
                dialog.check_scheduled_start(at)
                dialog.check_scheduled_start(at)
            launch.assert_called_once()
            self.assertTrue(launch.call_args.kwargs['arm'])
            self.assertEqual(launch.call_args.kwargs['expected_review_hash'],review['review_hash'])
            self.assertIsNone(dialog.scheduled_request)
        finally:dialog.close()
    def test_existing_owner_refuses_second_login_before_reference_and_credential_reads(self):
        with AccountLock(self.row['account_key']),patch('online_trader.runtime.dsv_simulation.measure_reference') as reference,patch.object(TqKqDsvPort,'connect') as connect:
            with self.assertRaises(TraderError):run(self.paths[0],'never-read',self.paths[1],self.paths[2],self.root/'no-run')
            reference.assert_not_called();connect.assert_not_called()
    def test_account_dialog_saves_without_retaining_secret_in_widgets(self):
        dialog=AccountDialog(self.store)
        try:
            dialog.table.selectRow(0);dialog.username.setText('offline-synthetic-launch');dialog.password.setText('offline-ephemeral-secret')
            dialog.save();self.assertEqual(dialog.password.text(),'');self.assertEqual(dialog.username.text(),'')
            self.assertTrue(self.vault.values);self.assertNotIn('offline-ephemeral-secret',dialog.notice.text())
            dialog.forget();self.assertEqual(self.vault.values,{})
        finally:dialog.close()

    def test_reference_data_dialog_import_export_never_creates_execution_ledger(self):
        from online_trader.desktop.data import DataDialog
        from online_trader.marketdata import ReferenceStore
        dialog=DataDialog(ReferenceStore(self.root/'reference'))
        try:
            source=Path(__file__).parents[1]/'docs/examples/offset-daily-sample.csv'
            with patch('online_trader.desktop.data.QFileDialog.getOpenFileName',return_value=(str(source),'csv')):dialog.import_daily()
            import json
            value=json.loads(dialog.result.toPlainText());self.assertFalse(value['runtime_activation']);dialog.digest.setText(value['content_hash'])
            with patch('online_trader.desktop.data.QFileDialog.getSaveFileName',return_value=(str(self.root/'reference-export.csv'),'csv')):dialog.export_daily()
            self.assertTrue((self.root/'reference-export.csv').exists());self.assertFalse(self.store.ledger_path(self.row).exists())
        finally:dialog.close()
