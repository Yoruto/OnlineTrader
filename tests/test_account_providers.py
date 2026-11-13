import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QLineEdit

from online_trader.accounts import AccountStore,StoredCredential,StoredSimNowCredential,account_identity
from online_trader.desktop.accounts import AccountDialog
from online_trader.errors import TraderError
from online_trader.gateway.simnow import account_fingerprint,credentials as simnow_credentials
from online_trader.gateway.tqkq import credentials as kq_credentials
from online_trader.runtime.instance import AccountLock
from tests.test_accounts import MemoryVault


class SimulationAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.vault=MemoryVault();self.store=AccountStore(self.root,vault=self.vault)
        self.values={'simnow_username':'offline-simnow','simnow_password':'simnow-secret',
                     'kq_username':'offline-kq','kq_password':'market-secret'}

    def seed(self):
        state=self.store.initialize_simulation_accounts(self.values)
        return state,{row['provider']:row for row in state['accounts']}

    def test_import_saves_two_masked_profiles_and_defaults_to_simnow_without_connection(self):
        with (patch('online_trader.gateway.simnow.SimNowDsvPort.connect',side_effect=AssertionError('no connection')),
                patch('online_trader.gateway.tqkq.TqKqPort.connect',side_effect=AssertionError('no connection'))):
            state,rows=self.seed()
        self.assertEqual(state['revision'],1)
        self.assertEqual(set(rows),{'TQ_SIMNOW','TQKQ'})
        self.assertEqual(state['selected_id'],rows['TQ_SIMNOW']['account_id'])
        self.assertEqual(rows['TQ_SIMNOW']['account_key'],'SIM:SIMNOW:'+account_fingerprint(self.values['simnow_username']))
        self.assertNotEqual(self.store.ledger_path(rows['TQKQ']),self.store.ledger_path(rows['TQ_SIMNOW']))
        text=''.join(path.read_text('utf-8') for path in self.root.rglob('*.json'))
        for value in self.values.values():self.assertNotIn(value,text)
        self.assertFalse((self.root/'data').exists())

    def test_repeat_import_reuses_profiles_and_preserves_explicit_kq_selection(self):
        state,rows=self.seed()
        selected=self.store.change('select',expected_revision=state['revision'],account_id=rows['TQKQ']['account_id'])
        with patch.object(self.vault,'put',wraps=self.vault.put) as put:
            again=self.store.initialize_simulation_accounts(self.values)
        self.assertEqual(again,selected);put.assert_not_called()

    def test_import_reuses_existing_kq_identity_alias_and_selection(self):
        current=self.store.change('add',expected_revision=0,alias='我的快期',username=self.values['kq_username'])
        row=current['accounts'][0]
        self.store.change('select',expected_revision=1,account_id=row['account_id'])
        state,rows=self.seed()
        self.assertEqual(state['selected_id'],row['account_id'])
        self.assertEqual(rows['TQKQ'],row)

    def test_invalid_input_writes_neither_catalogue_nor_credentials(self):
        for fields in (self.values|{'extra':'no'},self.values|{'kq_password':''},self.values|{'simnow_username':' bad '}):
            with self.subTest(fields=list(fields)),self.assertRaises(TraderError):
                self.store.initialize_simulation_accounts(fields)
        self.assertEqual(self.store.read()['revision'],0);self.assertEqual(self.vault.values,{})

    def test_provider_references_resolve_only_their_own_vault_fields(self):
        _,rows=self.seed()
        simnow=self.store.credential_reference(rows['TQ_SIMNOW']['account_id'])
        kq=self.store.credential_reference(rows['TQKQ']['account_id'])
        self.assertEqual(simnow_credentials(simnow),self.values)
        self.assertEqual(kq_credentials(kq),{'username':self.values['kq_username'],'password':self.values['kq_password']})
        for read,reference in ((simnow_credentials,kq),(kq_credentials,simnow),
                               (lambda value:value.resolve(),StoredCredential(self.store,rows['TQ_SIMNOW']['account_id'])),
                               (lambda value:value.resolve(),StoredSimNowCredential(self.store,rows['TQKQ']['account_id']))):
            with self.subTest(reference=repr(reference)),self.assertRaises(TraderError):read(reference)
        for value in self.values.values():self.assertNotIn(value,repr(simnow))

    def test_partial_vault_failure_restores_previous_credentials_and_keeps_catalogue(self):
        state,rows=self.seed();before={key:value.copy() for key,value in self.vault.values.items()}
        original=self.vault.put
        market_target=self.store.target(rows['TQ_SIMNOW'])+':market'
        def fail_market(target,username,password):
            if target==market_target and password=='changed-market':raise TraderError('E_CREDENTIALS','Synthetic failure')
            original(target,username,password)
        with patch.object(self.vault,'put',side_effect=fail_market),self.assertRaises(TraderError):
            self.store.initialize_simulation_accounts(self.values|{'simnow_password':'changed-trade','kq_password':'changed-market'})
        self.assertEqual(self.vault.values,before);self.assertEqual(self.store.read(),state)

    def test_active_provider_owner_blocks_import_before_any_vault_write(self):
        state,rows=self.seed();before={key:value.copy() for key,value in self.vault.values.items()}
        with AccountLock(rows['TQ_SIMNOW']['account_key']),self.assertRaises(TraderError):
            self.store.initialize_simulation_accounts(self.values|{'simnow_password':'blocked-update'})
        self.assertEqual(self.store.read(),state);self.assertEqual(self.vault.values,before)

    def test_simnow_forget_removes_both_slots_and_preserves_kq(self):
        _,rows=self.seed();self.store.forget_credentials(rows['TQ_SIMNOW']['account_id'])
        with self.assertRaises(TraderError):self.store.credential_reference(rows['TQ_SIMNOW']['account_id']).resolve()
        self.assertEqual(self.store.credential_reference(rows['TQKQ']['account_id']).resolve()['username'],self.values['kq_username'])

    def test_provider_identity_keeps_legacy_kq_hash_and_separates_same_login(self):
        import hashlib
        login='offline-identical'
        self.assertEqual(account_identity(login)[0],'SIM:KQ:'+hashlib.sha256(login.encode()).hexdigest()[:16])
        self.assertNotEqual(account_identity(login)[0],account_identity(login,'TQ_SIMNOW')[0])

    def test_account_dialog_saves_simnow_and_clears_all_secrets(self):
        _,rows=self.seed();dialog=AccountDialog(self.store);self.addCleanup(dialog.close)
        self.assertEqual(dialog.provider.currentData(),'TQ_SIMNOW')
        for field,key in ((dialog.username,'simnow_username'),(dialog.password,'simnow_password'),
                          (dialog.market_username,'kq_username'),(dialog.market_password,'kq_password')):
            self.assertEqual(field.echoMode(),QLineEdit.Password);field.setText(self.values[key])
        dialog.save()
        self.assertEqual(self.store.credential_reference(rows['TQ_SIMNOW']['account_id']).resolve(),self.values)
        for field in (dialog.username,dialog.password,dialog.market_username,dialog.market_password):self.assertEqual(field.text(),'')
        dialog.done(0)
