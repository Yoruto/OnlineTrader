"""Source-bound DSV 1.2 execution policy compilation and legacy isolation."""

import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.validation import validate_plan
from online_trader.errors import TraderError
from online_trader.protection import initial_state
from online_trader.strategy.plan import build_development_plan, capability_notes


def profile(**trade):
    settings=load_confirmed_baseline()[0].to_settings()
    settings['trade'].update(trade)
    return DsvProfile.from_settings(settings)


def make_plan(selected=None, *, execution_version='1.2', **scope):
    selected=selected or load_confirmed_baseline()[0]
    row=next(r for r in selected.to_record()['settings']['symbols'] if r['code']=='DCE.p2701')
    candidate={'account_key':'DEV:execution-plan','trading_day':'2026-11-09','contract_id':row['code'],
        'profile_hash':selected.content_hash,'direction':'LONG','theoretical_price':'1055','limit_price':'1056',
        'open_offset':row['open_offset'],'stop_distance':row['stop_offset'],'take_profit_distance':row['profit_offset']}
    params={'account_key':candidate['account_key'],'trading_day':candidate['trading_day'],
        'config_revision':'1','config_hash':'a'*64,'rule_set_id':load_confirmed_baseline()[1]['rule_set_id'],
        'quantity':5,'plan_id':'plan-execution-1','position_cycle_id':'cycle-execution-1'}
    params.update(scope)
    return build_development_plan(selected,candidate,execution_version=execution_version,**params)


class DsvExecutionPlanTests(unittest.TestCase):
    def test_explicit_new_policy_binds_source_constants_and_no_invented_attempt_limit(self):
        plan=make_plan();policy=plan['execution_policy']
        self.assertEqual(plan['schema_version'],'1.2')
        self.assertEqual(plan['execution_scope'],'DEVELOPMENT')
        self.assertEqual(policy['algorithm'],'DSV_TARGET_POSITION')
        self.assertIsNone(policy['max_child_order_attempts'])
        self.assertEqual(policy['price_steps'],[])
        self.assertEqual({k:policy[k] for k in ('entry_confirm_timeout_ms','passive_wait_ms','close_retry_ms',
            'cancel_check_interval_ms','extra_tick_remaining_ms','exchange_limit_remaining_ms','force_retry_ms')},
            {'entry_confirm_timeout_ms':60000,'passive_wait_ms':3000,'close_retry_ms':3000,
             'cancel_check_interval_ms':3000,'extra_tick_remaining_ms':10000,
             'exchange_limit_remaining_ms':5000,'force_retry_ms':5000})
        self.assertEqual({k:policy[k] for k in ('passive_ttl_ms','aggr_ttl_ms','market_ttl_ms','cap_ttl_ms')},
                         {'passive_ttl_ms':90000,'aggr_ttl_ms':60000,'market_ttl_ms':120000,'cap_ttl_ms':90000})
        self.assertEqual((policy['entry_slip_ratio'],policy['chase_slip_ratio'],policy['chase_window_ms']),
                         ('0.03','0.05',60000))
        self.assertEqual(policy['open_offset'],'55')
        validate_plan(plan);initial_state(plan)

    def test_default_compiler_call_retains_one_shot_legacy_execution_policy(self):
        selected=load_confirmed_baseline()[0]
        # Deliberately omit the new keyword at the actual call boundary.
        new=make_plan(selected)
        row=selected.to_record()['settings']['symbols'][0]
        old=build_development_plan(selected,{'account_key':new['account_key'],'trading_day':new['trading_day'],
            'contract_id':row['code'],'profile_hash':selected.content_hash,'direction':'LONG',
            'theoretical_price':'1055','limit_price':'1056','open_offset':row['open_offset'],
            'stop_distance':row['stop_offset'],'take_profit_distance':row['profit_offset']},
            account_key=new['account_key'],trading_day=new['trading_day'],config_revision='1',
            config_hash='a'*64,rule_set_id=new['rule_set_id'],quantity=5,
            plan_id=new['plan_id'],position_cycle_id=new['position_cycle_id'])
        self.assertEqual(old['schema_version'],'1.1')
        self.assertEqual(old['execution_policy']['max_child_order_attempts'],1)
        self.assertEqual(old['execution_policy']['cancel_check_interval_ms'],5000)
        self.assertNotIn('algorithm',old['execution_policy'])
        self.assertNotEqual(old['execution_policy']['policy_id'],new['execution_policy']['policy_id'])
        validate_plan(old)

    def test_new_passive_flag_compiles_and_does_not_change_old_supported_scope(self):
        selected=profile(passive_entry_first=True)
        plan=make_plan(selected)
        self.assertTrue(plan['execution_policy']['passive_entry_first'])
        self.assertEqual(plan['execution_policy']['first_price_mode'],'PASSIVE_LIMIT')
        self.assertIsNone(plan['approval_record_id'])
        with self.assertRaises(TraderError) as raised:make_plan(selected,execution_version='1.1')
        self.assertEqual(raised.exception.code,'E_UNSUPPORTED')

    def test_manual_close_mode_does_not_override_new_strategy_policy(self):
        for mode in ('对手价','排队价','最新价'):
            with self.subTest(mode=mode):
                selected=profile(close_price_mode=mode)
                plan=make_plan(selected)
                self.assertEqual(plan['execution_policy']['first_price_mode'],'OPPONENT_LIMIT')
                if mode!='对手价':
                    with self.assertRaises(TraderError):make_plan(selected,execution_version='1.1')

    def test_nondefault_half_protection_primitives_inherit_unchanged_one_one_contract(self):
        selected=profile(half_close_enabled=True,half_stage2=True,half_breakeven=True,
                         protect_80_enabled=False,trailing_activate_ratio=Decimal('0.6'))
        old=make_plan(selected,execution_version='1.1');new=make_plan(selected)
        for key in ('fixed_stop_loss','fixed_take_profit','partial_reduction','profit_protection',
                    'trailing_stop','slippage_exit'):
            self.assertEqual(new[key],old[key],key)
        self.assertEqual(new['partial_reduction']['quantity_rule'],'DSV_ROUND_HALF_CURRENT')
        validate_plan(new);initial_state(new)

    def test_zero_durations_and_ratios_preserve_explicit_source_values(self):
        plan=make_plan(profile(entry_confirm_timeout=0,chase_window_minutes=0,
                               entry_slip_ratio=0,chase_slip_ratio=0))
        policy=plan['execution_policy']
        self.assertEqual((policy['entry_confirm_timeout_ms'],policy['chase_window_ms']), (0,0))
        self.assertEqual((policy['entry_slip_ratio'],policy['chase_slip_ratio']),('0','0'))
        validate_plan(plan)

    def test_fractional_window_compiles_exact_integer_millisecond_closed_boundary(self):
        plan=make_plan(profile(chase_window_minutes=Decimal('0.016675000001')))
        self.assertEqual(plan['execution_policy']['chase_window_ms'],1000)

    def test_unbounded_source_durations_fail_explicitly_without_clipping(self):
        for changes in ({'entry_confirm_timeout':2147484},{'chase_window_minutes':35792}):
            with self.subTest(changes=changes),self.assertRaises(TraderError) as raised:
                make_plan(profile(**changes))
            self.assertEqual(raised.exception.code,'E_UNSUPPORTED')

    def test_policy_cannot_silently_change_source_algorithm_constants_or_attempt_semantics(self):
        for field,value in {'algorithm':'OTHER','cancel_check_interval_ms':5000,'max_child_order_attempts':1,
            'passive_wait_ms':1,'close_retry_ms':0,'extra_tick_remaining_ms':10001,
            'exchange_limit_remaining_ms':4999,'force_retry_ms':5001,'passive_ttl_ms':90001,
            'aggr_ttl_ms':60001,'market_ttl_ms':119999,'cap_ttl_ms':90001}.items():
            with self.subTest(field=field):
                plan=make_plan();plan['execution_policy'][field]=value
                with self.assertRaises(TraderError):validate_plan(plan)

    def test_policy_requires_all_facts_and_forbids_unknown_fields(self):
        original=make_plan()
        for field in original['execution_policy']:
            with self.subTest(missing=field):
                plan=copy.deepcopy(original);del plan['execution_policy'][field]
                with self.assertRaises(TraderError):validate_plan(plan)
        original['execution_policy']['send_now']=True
        with self.assertRaises(TraderError):validate_plan(original)

    def test_ratios_and_timer_types_cannot_bypass_canonical_or_bounded_facts(self):
        for field,values in {'entry_slip_ratio':['-0.01','1.01','0.030','3e-2',True],
                             'chase_slip_ratio':['-1','2',0.05],
                             'entry_confirm_timeout_ms':[True,-1,2147483648],
                             'chase_window_ms':[True,-1,2147483648],
                             'open_offset':['0','-1','55.0','5.5e1',True,55]}.items():
            for value in values:
                with self.subTest(field=field,value=value):
                    plan=make_plan();plan['execution_policy'][field]=value
                    with self.assertRaises(TraderError):validate_plan(plan)

    def test_passive_flag_and_first_mode_must_describe_same_policy(self):
        for selected in (profile(passive_entry_first=False),profile(passive_entry_first=True)):
            plan=make_plan(selected)
            plan['execution_policy']['first_price_mode']='OPPONENT_LIMIT' if selected.to_settings()['trade']['passive_entry_first'] else 'PASSIVE_LIMIT'
            with self.assertRaises(TraderError):validate_plan(plan)

    def test_old_versions_and_resource_evidence_are_not_rewritten(self):
        root=Path(__file__).resolve().parents[1]
        original=json.loads((root/'docs/spec-source.json').read_text(encoding='utf-8'))
        extension=json.loads((root/'docs/spec-extension-workbench.json').read_text(encoding='utf-8'))
        self.assertEqual(set(extension['extends_copied_resources']),{'schemas/ipc.schema.json'})
        for name,digest in original['copied_specification_resources'].items():
            if name in extension['extends_copied_resources']:
                self.assertEqual(extension['extends_copied_resources'][name],digest)
                digest=extension['resources'][Path(name).name]
            self.assertEqual(hashlib.sha256((root/'src/online_trader/resources'/Path(name).name).read_bytes()).hexdigest(),digest)
        old=json.loads((root/'docs/spec-extension-dsv-protection.json').read_text(encoding='utf-8'))
        for name,digest in old['resources'].items():
            self.assertEqual(hashlib.sha256((root/'src/online_trader/resources'/name).read_bytes()).hexdigest(),digest)
        old_plan=make_plan(execution_version='1.1')
        old_plan['execution_policy']=make_plan()['execution_policy']
        with self.assertRaises(TraderError):validate_plan(old_plan)

    def test_new_version_is_explicit_and_capability_notes_distinguish_execution_scopes(self):
        selected=load_confirmed_baseline()[0]
        old=capability_notes(selected);new=capability_notes(selected,execution_version='1.2')
        self.assertIn('OPPONENT_FIRST_REQUEST_ONLY_NO_DSV_CANCEL_REPRICE_ALGORITHM',old)
        self.assertNotIn('OPPONENT_FIRST_REQUEST_ONLY_NO_DSV_CANCEL_REPRICE_ALGORITHM',new)
        self.assertIn('CANCEL_CHECK_3000MS_IS_CLOSE_TARGET_REFRESH_NOT_SDK_CANCEL_INTERVAL',new)
        for version in ('1.0','1.3',None,True):
            with self.subTest(version=version):
                with self.assertRaises(TraderError):make_plan(execution_version=version)
                with self.assertRaises(TraderError):capability_notes(selected,execution_version=version)


if __name__=='__main__':unittest.main()
