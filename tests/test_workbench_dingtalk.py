"""DSV robot contract and snapshot notifications with isolated test transport."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from online_trader.desktop.dingtalk import DingTalkSettings, WorkbenchDingTalk, send_dingtalk
from online_trader.desktop.notifications import WorkbenchSounds
from online_trader.errors import TraderError


WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=fake-unit-test-token'
SECRET = 'unit-test-signing-secret'


class Vault:
    def __init__(self):
        self.items = {}

    def put(self, target, username, password):
        self.items[target] = {'username':username,'password':password}

    def get(self, target):
        return self.items[target]

    def delete(self, target):
        self.items.pop(target, None)


class WorkbenchDingTalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'settings.ini'
        self.settings = QSettings(str(self.path), QSettings.IniFormat)
        self.vault = Vault()
        self.sent = []
        self.log = []
        self.moment = 100
        self.delivered = Event()
        self.ding = WorkbenchDingTalk(self.settings, vault=self.vault, sender=self.sender,
                                     monotonic=lambda:self.moment, log=self.log.append)

    def tearDown(self):
        self.app.processEvents()
        self.folder.cleanup()

    def sender(self, config, message):
        self.sent.append((config, message))
        self.delivered.set()
        return True, '发送成功'

    def settle(self, count):
        deadline = time.monotonic() + 2
        while len(self.sent) < count and time.monotonic() < deadline:
            self.delivered.wait(0.01)
            self.delivered.clear()
        self.assertEqual(len(self.sent), count)

    def test_signed_post_matches_dsv_signature_mentions_and_timeout(self):
        config = DingTalkSettings.parse(True, WEBHOOK, SECRET, '13800138000, 13900139000', 10)
        calls = []
        result = send_dingtalk(config, '通知测试', clock=lambda:1_700_000_000.123,
                               post=lambda *args: calls.append(args) or {'errcode':0})
        self.assertEqual(result, (True,'发送成功'))
        url, payload, timeout = calls[0]
        self.assertEqual(parse_qs(urlsplit(url).query), {
            'access_token':['fake-unit-test-token'], 'timestamp':['1700000000123'],
            'sign':['m5OWKjuv4Pnw/xnoHouEagr/sFeW2+PIUF/VA62VWvk='],
        })
        self.assertEqual(payload, {'msgtype':'text', 'text':{'content':'通知测试'},
                                   'at':{'atMobiles':['13800138000','13900139000'],'isAtAll':False}})
        self.assertEqual(timeout, 8)

    def test_unsigned_post_preserves_url_and_redacts_server_errors(self):
        config = DingTalkSettings.parse(False, WEBHOOK, '', '', 10)
        urls = []
        result = send_dingtalk(config, '测试', post=lambda url,*_: urls.append(url) or {
            'errcode':310000,'errmsg':WEBHOOK,
        })
        self.assertEqual(urls, [WEBHOOK])
        self.assertEqual(result, (False,'钉钉返回错误码 310000'))
        def fail(*_):
            raise HTTPError(WEBHOOK, 302, SECRET, {}, None)
        self.assertEqual(send_dingtalk(config,'测试',post=fail), (False,'HTTP 302'))

    def test_invalid_non_dingtalk_endpoint_and_preexisting_signature_are_rejected(self):
        for endpoint in (WEBHOOK.replace('oapi.dingtalk.com', 'example.invalid'),
                         WEBHOOK.replace('https:', 'http:'), WEBHOOK+'&sign=bogus'):
            with self.subTest(endpoint=endpoint), self.assertRaises(TraderError):
                DingTalkSettings.parse(True, endpoint, SECRET, '', 10)

    def test_vault_preserves_settings_without_storing_sensitive_values_in_qsettings(self):
        config = DingTalkSettings.parse(True, WEBHOOK, SECRET, '13800138000', 20)
        self.assertTrue(self.ding.save(config))
        self.assertEqual(self.sent, [])
        value = self.path.read_text('utf-8')
        for secret in (WEBHOOK, SECRET, '13800138000'):
            self.assertNotIn(secret, value)
        new = WorkbenchDingTalk(self.settings, vault=self.vault, sender=self.sender)
        self.assertEqual(new.current(), config)
        self.assertTrue(new.save(DingTalkSettings()))
        self.assertEqual(self.vault.items, {})

    def test_same_contract_and_type_are_throttled_independently(self):
        config = DingTalkSettings.parse(True, WEBHOOK, SECRET, '', 10)
        self.ding.save(config)
        self.assertTrue(self.ding.notify('open','DCE.p2701','open'))
        self.moment += 9
        self.assertFalse(self.ding.notify('open','DCE.p2701','suppressed'))
        self.assertTrue(self.ding.notify('close','DCE.p2701','close'))
        self.assertTrue(self.ding.notify('open','DCE.l2701','other symbol'))
        self.moment += 1
        self.assertTrue(self.ding.notify('open','DCE.p2701','equal boundary'))
        self.settle(4)

    def test_test_button_uses_unsaved_masked_fields_without_enabling_auto_delivery(self):
        panel = self.ding.settings_panel()
        webhook = panel.findChild(QLineEdit, 'dingtalkWebhook')
        mobiles = panel.findChild(QLineEdit, 'dingtalkMobiles')
        secret = panel.findChild(QLineEdit, 'dingtalkSecret')
        self.assertEqual(webhook.echoMode(), QLineEdit.Password)
        self.assertEqual(secret.echoMode(), QLineEdit.Password)
        webhook.setText(WEBHOOK)
        mobiles.setText('13800138000')
        secret.setText(SECRET)
        test = next(button for button in panel.findChildren(QPushButton) if button.text() == '测试推送')
        test.click()
        self.settle(1)
        self.assertIn('[测试] 交易策略通知测试', self.sent[0][1])
        self.assertEqual(self.vault.items, {})
        self.assertFalse(self.ding.notify('open','DCE.p2701','disabled'))
        panel.deleteLater()

    @staticmethod
    def view(seq, *, trades=()):
        return {'account_key':'SIM:fixture','kernel_boot_id':'test-boot','checkpoint_event_seq':str(seq),
                'trades':list(trades),'orders':[],'cycles':[],'connection':{},'commands':[],
                'markets':[],'truncated':False,'configuration':{}}

    def test_accepted_new_fill_pushes_while_sound_muted_and_initial_or_repeat_do_not(self):
        self.settings.setValue('sound/enabled', False)
        self.ding.save(DingTalkSettings.parse(True, WEBHOOK, '', '', 0))
        tones = []
        sounds = WorkbenchSounds(self.settings, tone=lambda *value: tones.append(value), dingtalk=self.ding)
        old = {'contract_id':'DCE.p2701','effect':'OPEN','event_seq':'1','position_cycle_id':'old',
               'child_order_id':'old-child','quantity':1,'price':'9000'}
        sounds.observe(self.view(1,trades=[old]))
        self.assertEqual(self.sent, [])
        first = old | {'position_cycle_id':'new','child_order_id':'child','event_seq':'2','price':'9100','quantity':2}
        second = first | {'event_seq':'3','price':'9103'}
        view = self.view(3,trades=[old,first,second])
        sounds.observe(view)
        sounds.observe(deepcopy(view))
        self.settle(1)
        self.assertIn('开仓已成交 4 手 @9101.50', self.sent[0][1])
        self.assertEqual(tones, [])
        self.settings.setValue('sound/close', False)
        closed = first | {'event_seq':'4','effect':'REDUCE','child_order_id':'close-child'}
        sounds.observe(self.view(4,trades=[old,first,second,closed]))
        self.assertEqual(len(self.sent), 1)

    def test_proved_first_open_aggregate_ignores_later_partial_fills_and_slippage_abort(self):
        self.ding.save(DingTalkSettings.parse(True, WEBHOOK, '', '', 0))
        sounds = WorkbenchSounds(self.settings, tone=lambda *_:None, dingtalk=self.ding)
        sounds.observe(self.view(1))
        cycle = {'position_cycle_id':'strategy-cycle','contract_id':'DCE.p2701','remaining_quantity':1,
                 'direction':'LONG','entry_tag':'CONTINUATION','initial_open_confirmation':{'status':'PENDING'}}
        partial = {'contract_id':'DCE.p2701','effect':'OPEN','event_seq':'2','position_cycle_id':'strategy-cycle',
                   'child_order_id':'open-child','quantity':1,'price':'9050'}
        sounds.observe(self.view(2,trades=[partial]) | {'cycles':[cycle]})
        self.assertEqual(self.sent, [])
        ready = cycle | {'remaining_quantity':3,'initial_open_confirmation':{'status':'CONFIRMED',
                         'quantity':2,'value':'18103'}}
        later = partial | {'event_seq':'4','price':'9060','quantity':2}
        sounds.observe(self.view(4,trades=[partial,later]) | {'cycles':[ready]})
        sounds.observe(self.view(5,trades=[partial,later]) | {'cycles':[ready]})
        self.settle(1)
        self.assertIn('延续开多已成交 2 手 @9051.5',self.sent[0][1])
        slipped = ready | {'position_cycle_id':'slipped','initial_open_confirmation':
                          {'status':'SLIPPAGE_EXIT','quantity':1,'value':'9300'},
                          'target_remaining_quantity':0,'exit_reason':'ADVERSE_SLIPPAGE'}
        sounds.observe(self.view(6) | {'cycles':[ready,slipped]})
        self.settle(2)
        self.assertIn('滑点超限平仓已触发',self.sent[1][1])

    def test_half_and_protection_notify_at_committed_targets_without_duplicate_fill_push(self):
        self.ding.save(DingTalkSettings.parse(True, WEBHOOK, '', '', 0))
        sounds = WorkbenchSounds(self.settings, tone=lambda *_:None, dingtalk=self.ding)
        cycle = {'position_cycle_id':'original','contract_id':'DCE.p2701','remaining_quantity':4,
                 'direction':'LONG','target_remaining_quantity':None}
        sounds.observe(self.view(1) | {'cycles':[cycle]})
        first = cycle | {'phase1_target':2,'target_remaining_quantity':2}
        sounds.observe(self.view(2) | {'cycles':[first]})
        self.settle(1)
        self.assertIn('第1档半仓平仓已触发，目标剩余 2 手', self.sent[0][1])
        self.assertNotIn('已成交', self.sent[0][1])
        partial = {'contract_id':'DCE.p2701','effect':'REDUCE','event_seq':'3','position_cycle_id':'original',
                   'child_order_id':'first-close','quantity':2,'price':'9105'}
        second = first | {'remaining_quantity':2,'phase2_target':1,'target_remaining_quantity':1}
        sounds.observe(self.view(3,trades=[partial]) | {'cycles':[second]})
        self.settle(2)
        self.assertIn('第2档半仓平仓已触发', self.sent[1][1])
        activation = second | {'remaining_quantity':1,'mode':'PROTECTED','exit_reasons':['PROTECTION_ACTIVATED']}
        sounds.observe(self.view(4,trades=[partial]) | {'cycles':[activation]})
        self.assertEqual(len(self.sent), 2)
        protected = activation | {'target_remaining_quantity':0,'exit_reason':'PROTECTED_FLOOR',
                                  'exit_reference_price':'9100.5'}
        sounds.observe(self.view(5,trades=[partial]) | {'cycles':[protected]})
        self.settle(3)
        self.assertIn('80%保护平仓已触发，参考价 9100.5', self.sent[2][1])
        final = partial | {'event_seq':'6','child_order_id':'protected-close','quantity':1}
        sounds.observe(self.view(6,trades=[partial,final]) | {'cycles':[protected]})
        self.assertEqual(len(self.sent), 3)

    def test_first_complete_view_after_truncated_attach_does_not_replay_old_targets(self):
        self.ding.save(DingTalkSettings.parse(True, WEBHOOK, '', '', 0))
        sounds = WorkbenchSounds(self.settings, tone=lambda *_:None, dingtalk=self.ding)
        sounds.observe(self.view(1) | {'truncated':True})
        old = {'position_cycle_id':'old','contract_id':'DCE.p2701','remaining_quantity':0,
               'target_remaining_quantity':0,'exit_reason':'PROTECTED_FLOOR'}
        sounds.observe(self.view(2) | {'cycles':[old]})
        self.assertEqual(self.sent, [])
        new = old | {'position_cycle_id':'new','contract_id':'DCE.l2701'}
        sounds.observe(self.view(3) | {'cycles':[old,new]})
        self.settle(1)
        self.assertIn('DCE.l2701',self.sent[0][1])

    def test_owner_projects_first_confirmed_native_average_before_later_partial_trade(self):
        from online_trader.kernel.workbench import workbench_view
        from test_online_dsv import OnlineDsvTests

        owner = OnlineDsvTests()
        owner.setUp()
        try:
            owner.start(trade={'entry_slip_abort':False})
            owner.quote()
            order_id = owner.api.inserted[0]['order_id']
            owner.api.fill(order_id,1,1054,finish=False)
            owner.api.fill(order_id,2,1055,finish=False)
            owner.session.step()
            proof = {'status':'CONFIRMED','quantity':3,'value':'3164'}
            self.assertEqual(workbench_view(owner.kernel)['cycles'][0]['initial_open_confirmation'], proof)
            owner.api.fill(order_id,2,1056)
            owner.session.step()
            self.assertEqual(workbench_view(owner.kernel)['cycles'][0]['initial_open_confirmation'], proof)
        finally:
            owner.tearDown()
