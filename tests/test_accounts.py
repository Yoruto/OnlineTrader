from pathlib import Path
import tempfile
import unittest
import uuid

from online_trader.accounts import AccountStore,WindowsVault,account_identity
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.runtime.instance import AccountLock
from online_trader.gateway.tqkq import credentials
from qa.crash_worker import seed_position
from qa.recording_port import RecordingPort


class MemoryVault:
    def __init__(self):self.values={}
    def put(self,target,username,password):self.values[target]={'username':username,'password':password}
    def get(self,target):
        if target not in self.values:raise TraderError('E_CREDENTIALS','Missing')
        return self.values[target].copy()
    def delete(self,target):self.values.pop(target,None)


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.vault=MemoryVault();self.store=AccountStore(self.root,vault=self.vault)
        state=self.store.change('add',expected_revision=0,alias='离线账户A',username='offline-user-A')
        self.a=state['accounts'][0]
    def test_catalogue_contains_masked_identity_only_and_never_credentials(self):
        self.store.save_credentials(self.a['account_id'],'offline-user-A','never-export-password')
        text=''.join(p.read_text() for p in (self.root/'catalogue').glob('*.json'))
        self.assertNotIn('offline-user-A',text);self.assertNotIn('never-export-password',text)
        self.assertEqual(credentials(self.store.credential_reference(self.a['account_id']))['username'],'offline-user-A')
        self.assertNotIn('never-export-password',repr(self.store.credential_reference(self.a['account_id'])))
    def test_forget_and_delete_remove_vault_but_retain_ledger_directory(self):
        self.store.save_credentials(self.a['account_id'],'offline-user-A','offline-password')
        self.store.forget_credentials(self.a['account_id']);self.assertEqual(self.vault.values,{})
        history=self.store.ledger_path(self.a).parent/'historical-report.json';history.parent.mkdir(parents=True);history.write_text('preserve')
        self.store.change('delete',expected_revision=1,account_id=self.a['account_id'])
        self.assertTrue(history.exists());self.assertEqual(self.store.read()['accounts'],[])
    def test_stale_revision_cannot_overwrite_another_change(self):
        self.store.change('edit',expected_revision=1,account_id=self.a['account_id'],alias='新名称')
        with self.assertRaises(TraderError):self.store.change('delete',expected_revision=1,account_id=self.a['account_id'])
        self.assertEqual(self.store.get(self.a['account_id'])['alias'],'新名称')
    def test_duplicate_account_identity_is_rejected(self):
        with self.assertRaises(TraderError):self.store.change('add',expected_revision=1,alias='重复',username='offline-user-A')
        self.assertEqual(self.store.read()['revision'],1)
    def test_selected_id_persists_without_creating_a_ledger(self):
        self.store.change('select',expected_revision=1,account_id=self.a['account_id'])
        other=AccountStore(self.root,vault=self.vault)
        self.assertEqual(other.read()['selected_id'],self.a['account_id']);self.assertFalse(other.ledger_path(self.a).exists())
    def test_active_owner_blocks_edit_forget_delete_and_switch(self):
        with AccountLock(self.a['account_key']):
            for operation in ('edit','delete','select'):
                with self.subTest(operation=operation),self.assertRaises(TraderError):
                    self.store.change(operation,expected_revision=1,account_id=self.a['account_id'],alias='不得编辑')
            with self.assertRaises(TraderError):self.store.forget_credentials(self.a['account_id'])
    def test_stopped_but_held_ledger_blocks_switch_and_delete(self):
        self.store.change('select',expected_revision=1,account_id=self.a['account_id'])
        state=self.store.change('add',expected_revision=2,alias='账户B',username='offline-user-B')
        b=state['accounts'][1];path=self.store.ledger_path(self.a);path.parent.mkdir(parents=True)
        with Kernel(path,self.a['account_key'],development=True,port=RecordingPort(),risk_budget='1000') as kernel:
            seed_position(kernel)
        with self.assertRaises(TraderError):self.store.change('select',expected_revision=3,account_id=b['account_id'])
        with self.assertRaises(TraderError):self.store.change('delete',expected_revision=3,account_id=self.a['account_id'])
        self.assertTrue(path.exists());self.assertEqual(self.store.read()['selected_id'],self.a['account_id'])
    def test_safe_switch_preserves_both_account_identities(self):
        self.store.change('select',expected_revision=1,account_id=self.a['account_id'])
        state=self.store.change('add',expected_revision=2,alias='B',username='offline-user-B');b=state['accounts'][1]
        state=self.store.change('select',expected_revision=3,account_id=b['account_id'])
        self.assertNotEqual(self.store.ledger_path(self.a),self.store.ledger_path(b));self.assertEqual(state['selected_id'],b['account_id'])
    def test_saved_credential_cannot_be_used_for_another_identity(self):
        with self.assertRaises(TraderError):self.store.save_credentials(self.a['account_id'],'other-user','offline-password')
        self.assertEqual(self.vault.values,{})
    def test_corrupt_history_fails_closed(self):
        (self.root/'catalogue/00000001.json').write_text('{}')
        with self.assertRaises(TraderError):self.store.read()
    def test_real_windows_vault_roundtrip_and_forget_synthetic_values_only(self):
        vault=WindowsVault();target='OnlineTrader/OfflineUnitTest/'+uuid.uuid4().hex
        try:
            vault.put(target,'offline-synthetic-user','offline-synthetic-password')
            self.assertEqual(vault.get(target),{'username':'offline-synthetic-user','password':'offline-synthetic-password'})
            vault.delete(target)
            with self.assertRaises(TraderError):vault.get(target)
        finally:vault.delete(target)
