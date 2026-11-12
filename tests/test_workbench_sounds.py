"""DSV sound preferences and event detection without physical sound or a broker."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

from online_trader.desktop.notifications import WorkbenchSounds


class WorkbenchSoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / 'settings.ini')
        self.settings = QSettings(self.path, QSettings.IniFormat)
        self.tones = []
        self.sound = WorkbenchSounds(self.settings, tone=lambda *event: self.tones.append(event))

    def tearDown(self):
        self.settings.sync()
        self.folder.cleanup()

    @staticmethod
    def view(seq, *, trades=(), orders=(), connection=None, markets=(), grace=False):
        return {'account_key':'account-sound-test','kernel_boot_id':'boot-sound-test',
                'checkpoint_event_seq':str(seq),'trades':list(trades),'orders':list(orders),'cycles':[],
                'connection':connection or {},'commands':[],'markets':list(markets),'truncated':False,
                'configuration':{'margin_alert_grace':grace,'margin_rate_alert_threshold':'0.005'}}

    def test_switches_persist_and_test_button_plays_dsv_tone_while_muted(self):
        panel = self.sound.settings_panel()
        for check in panel.findChildren(QCheckBox):
            if check.text() in {'启用声音提醒','平仓提醒'}:
                check.setChecked(False)
        panel.findChild(QPushButton).click()
        self.assertEqual(self.tones, [(880,200)])
        self.settings.sync()
        saved = QSettings(self.path, QSettings.IniFormat)
        self.assertFalse(saved.value('sound/enabled', True, bool))
        self.assertFalse(saved.value('sound/close', True, bool))
        panel.deleteLater()

    def test_open_close_and_error_events_only_sound_once(self):
        def trade(seq, effect, cycle, child):
            return {'event_seq':str(seq),'effect':effect,'position_cycle_id':cycle,'child_order_id':child}

        self.sound.observe(self.view(2, trades=[trade(1,'OPEN','old','old-child')]))
        self.assertEqual(self.tones, [])
        opens = [trade(3,'OPEN','new','open-child'),trade(4,'OPEN','new','open-child')]
        self.sound.observe(self.view(4,trades=opens))
        self.sound.observe(self.view(4,trades=opens))
        self.assertEqual(self.tones, [(880,200)])
        closes = [trade(5,'REDUCE','new','reduce-child-a'),trade(6,'REDUCE','new','reduce-child-b')]
        orders = [{'child_order_id':child,'intent_id':'same-intent'} for child in ('reduce-child-a','reduce-child-b')]
        self.sound.observe(self.view(6,trades=closes,orders=orders,connection={'fault':True}))
        self.sound.observe(self.view(6,trades=closes,orders=orders,connection={'fault':True}))
        self.sound.observe_request_error()
        self.assertEqual(self.tones, [(880,200),(660,200),(440,200)])

    def test_margin_reference_uses_source_threshold_grace_and_reset(self):
        base = {'name':'棕榈油','contract_id':'DCE.p2701','fresh':True,'last':'1000','volume_multiple':'10'}
        def market(margin):
            return [base | {'margin':margin}]

        self.sound.observe(self.view(1,markets=market('1200'),grace=True))
        self.sound.observe(self.view(2,markets=market('1250'),grace=True))
        self.sound.observe(self.view(3,markets=market('1250')))
        self.assertEqual(self.tones, [])
        self.sound.observe(self.view(4,markets=market('1249')))
        self.sound.observe(self.view(5,markets=market('1250')))
        self.sound.observe(self.view(6,markets=market('1260')))
        self.assertEqual(self.tones, [(990,200)])

    def test_unfilled_terminal_reduction_warns_once_per_active_cycle(self):
        self.sound.dingtalk=Mock()
        self.sound.observe(self.view(1))
        block={'active':True,'reason_code':'EXIT_REDUCTION_TERMINAL_UNFILLED',
               'position_cycle_id':'failed-cycle','contract_id':'DCE.p2701'}
        self.sound.observe(self.view(2) | {'blocks':[block]})
        self.sound.observe(self.view(3) | {'blocks':[block]})
        self.assertEqual(self.tones,[(440,200)])
        self.sound.dingtalk.notify.assert_called_once_with(
            'error',None,'DCE.p2701 平仓委托已终态，剩余目标未完成，请人工核对')
