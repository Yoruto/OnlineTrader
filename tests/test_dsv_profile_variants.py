"""Development parameter variants retain source approval and durable barriers."""

import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.kernel.engine import Kernel
from qa.recording_port import RecordingPort
from test_dsv_runtime import facts


def variant():
    settings=load_confirmed_baseline()[0].to_settings()
    settings['trade'].update(half_close_enabled=True,half_stage2=True,half_breakeven=True)
    return DsvProfile.from_settings(settings)


class DevelopmentProfileTests(unittest.TestCase):
    def test_variant_cannot_acquire_default_or_missing_port_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            for development,port in ((False,RecordingPort()),(True,None)):
                with Kernel(Path(folder)/str(development),'DEV:variant-'+uuid.uuid4().hex,
                            development=development,port=port) as kernel:
                    with self.assertRaises(TraderError) as raised:
                        DsvStrategyRuntime(kernel,profile=variant())
                    self.assertEqual(raised.exception.code,'E_RULE_UNAPPROVED')
                    self.assertFalse(kernel.snapshot()['armed'])

    def test_variant_descriptor_preserves_reference_and_binds_execution_implementation(self):
        baseline,approval=load_confirmed_baseline()
        profile=variant()
        with tempfile.TemporaryDirectory() as folder:
            with Kernel(Path(folder)/'ledger.db','DEV:variant-'+uuid.uuid4().hex,
                        development=True,port=RecordingPort(),risk_budget='1000000') as kernel:
                runtime=DsvStrategyRuntime(kernel,profile=profile)
                status=runtime.activate()
                self.assertFalse(status['armed'])
                self.assertEqual(status['profile_source'],'ISOLATED_DEVELOPMENT_VARIANT')
                row=kernel.ledger.query('SELECT payload_json FROM config_revisions WHERE config_revision=?',
                                        (kernel._config_revision,))[0]
                descriptor=json.loads(row['payload_json'])
                self.assertEqual(descriptor['profile'],profile.to_record())
                self.assertEqual(descriptor['baseline_profile_hash'],baseline.content_hash)
                self.assertEqual(descriptor['reference_approval_id'],approval['record_id'])
                self.assertFalse(descriptor['broker_authority'])
                self.assertTrue({'protection','validation','protection-plan.schema.json',
                                 'protection-plan-v1.1.schema.json'}<=descriptor['code_hashes'].keys())
                self.assertEqual(len(kernel.ledger.query("SELECT * FROM rule_decisions WHERE status='APPROVED'")),20)

    def test_profile_change_cannot_reset_rejected_bar_or_claim_path_completeness(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ledger.db';key='DEV:variant-'+uuid.uuid4().hex
            market,account,instrument=facts(key)
            account['available']='0'
            with Kernel(path,key,development=True,port=RecordingPort(),risk_budget='1000000') as kernel:
                runtime=DsvStrategyRuntime(kernel);runtime.activate()
                kernel.arm_development();kernel.wait_idle()
                future=runtime.on_market(market,account,instrument);kernel.wait_idle()
                self.assertEqual(future.result()['status'],'OBSERVED')
                self.assertEqual(runtime.business_state()['entry_states'][0]['last_signal_bar'],market['candle_id'])
            port=RecordingPort()
            with Kernel(path,key,development=True,port=port,risk_budget='1000000') as kernel:
                runtime=DsvStrategyRuntime(kernel,profile=variant())
                state=runtime.business_state()['entry_states'][0]
                self.assertEqual(state['last_signal_bar'],market['candle_id'])
                self.assertTrue(state['coverage_lost'])
                self.assertEqual(state['profile_hash'],runtime.profile.content_hash)
                runtime.activate();kernel.arm_development();kernel.wait_idle()
                account.update(available='1000000',revision=account['revision']+1)
                future=runtime.on_market(market,account,instrument);kernel.wait_idle()
                self.assertEqual(future.result()['status'],'OBSERVED')
                self.assertIn('DUPLICATE_MARKET_EVENT',future.result()['reasons'])
                market=market|{'sequence':market['sequence']+1,
                    'source_time_utc':(datetime.fromisoformat(market['source_time_utc'])+timedelta(seconds=1)).isoformat().replace('+00:00','Z')}
                future=runtime.on_market(market,account,instrument);kernel.wait_idle()
                self.assertIn('MARKET_PATH_INCOMPLETE',future.result()['reasons'])
                self.assertEqual(port.orders,[])

    def test_rebuild_receipt_from_old_profile_cannot_claim_new_profile_path_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ledger.db';key='DEV:variant-'+uuid.uuid4().hex
            market,_,_=facts(key)
            with Kernel(path,key,development=True,port=RecordingPort(),risk_budget='1000000') as kernel:
                runtime=DsvStrategyRuntime(kernel);runtime.activate()
                future=runtime.rebuild_observation([market],evidence_id='original-profile-path')
                kernel.wait_idle()
                self.assertEqual(future.result()['profile_hash'],runtime.profile.content_hash)
            with Kernel(path,key,development=True,port=RecordingPort(),risk_budget='1000000') as kernel:
                runtime=DsvStrategyRuntime(kernel,profile=variant());runtime.activate()
                self.assertTrue(runtime.business_state()['entry_states'][0]['coverage_lost'])
                with self.assertRaises(TraderError) as raised:
                    runtime.rebuild_observation([market],evidence_id='original-profile-path')
                self.assertEqual(raised.exception.code,'E_DUPLICATE_CONFLICT')
                future=runtime.rebuild_observation([market],evidence_id='new-profile-path')
                kernel.wait_idle()
                self.assertFalse(runtime.business_state()['entry_states'][0]['coverage_lost'])
                self.assertEqual(future.result()['profile_hash'],runtime.profile.content_hash)
                self.assertFalse(kernel.snapshot()['armed'])


if __name__=='__main__':unittest.main()
