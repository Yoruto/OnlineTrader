import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.rollover import expiry_date, expiry_warning, preview_rollover, rollover_hint
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.errors import TraderError
from online_trader.kernel.workbench import workbench_view
from tests import test_online_dsv, test_workbench


class DsvRolloverTests(unittest.TestCase):
    def test_dsv_display_thresholds_and_native_epoch_units(self):
        today = date(2026, 11, 12)
        stamp = int(datetime(2026, 11, 14, 16, tzinfo=timezone.utc).timestamp())
        for factor in (1, 1000, 1000000, 1000000000):
            self.assertEqual(expiry_date(stamp * factor), date(2026, 11, 15))
        self.assertEqual(expiry_date("2026/11/15 09:00:00"), date(2026, 11, 15))
        self.assertEqual(expiry_date(20261115), date(2026, 11, 15))
        self.assertEqual(expiry_warning("2026-11-15", today),
                         ("⚠ 还有 3 天到期（2026-11-15），请准备换月", True))
        self.assertEqual(expiry_warning("2026-12-12", today), ("2026-12-12 到期（还有 30 天）", False))
        self.assertEqual(expiry_warning("2026-12-13", today), ("2026-12-13 到期", False))
        self.assertTrue(expiry_warning("2026-11-12", today)[1])
        self.assertTrue(expiry_warning("2026-11-11", today)[1])
        self.assertEqual(expiry_warning(float("nan"), today), ("到期日未取到（可不换月）", False))

    def test_main_and_single_factor_are_advisory_dual_is_strict(self):
        old, main, high_oi = "DCE.p2701", "DCE.p2705", "DCE.p2709"
        self.assertEqual(rollover_hint(old,main,{})["candidate_basis"],"MAIN")
        activity = {old:(100,200),main:(110,1200),high_oi:(120,200),"DCE.l2701":(1000,10000)}
        self.assertEqual(rollover_hint(old,main,activity),
                         {"main_contract":main,"candidate":main,"candidate_basis":"VOLUME"})
        activity[high_oi] = (120,201)
        self.assertEqual(rollover_hint(old,main,activity),
                         {"main_contract":main,"candidate":high_oi,"candidate_basis":"DUAL"})
        activity[main] = (110,200)
        activity[high_oi] = (120,200)
        self.assertEqual(rollover_hint(old,main,activity)["candidate_basis"],"OPEN_INTEREST")

    def test_preview_changes_selected_contract_and_consumes_only_its_registration(self):
        reference = load_confirmed_baseline()[0]
        settings = reference.to_record()
        settings["settings"]["symbols"][0]["code_new"] = "DCE.p2705"
        profile = DsvProfile.from_record(settings)
        changed = preview_rollover(profile, "DCE.p2701", "DCE.p2705")
        rows = changed.to_record()["settings"]["symbols"]
        self.assertEqual(rows[0],{key:("DCE.p2705" if key=="code" else value)
                                 for key,value in settings["settings"]["symbols"][0].items() if key!="code_new"})
        self.assertEqual(rows[1:],settings["settings"]["symbols"][1:])
        self.assertEqual(profile.to_record(),settings)
        with self.assertRaises(TraderError):
            preview_rollover(profile, "DCE.p2701", "DCE.l2701")
        settings["settings"]["symbols"].append(rows[0])
        with self.assertRaises(TraderError):
            preview_rollover(DsvProfile.from_record(settings), "DCE.p2701", "DCE.p2705")

    def test_owner_projection_reads_async_product_quotes_without_changing_scope(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.start()
        old, main = case.code, "DCE.p2705"
        current = case.api.quotes[old]
        current.volume, current.open_interest = 800, 1000
        current.expire_datetime = "2027-01-20"
        case.api.quotes["KQ.m@DCE.p"] = SimpleNamespace(datetime=current.datetime,underlying_symbol=main)
        case.api.quotes[main] = SimpleNamespace(datetime=current.datetime,volume=900,open_interest=1100,
                                               expire_datetime="2027-05-20",expired=False)
        case.api.query_quotes = AsyncMock(return_value=[old,main,"DCE.l2705"])
        case.api.get_quote_list = Mock(side_effect=lambda codes:[case.api.quotes[code] for code in codes])
        asyncio.run(case.port._subscribe_rollover_quotes(case.port.source_epoch))
        view = workbench_view(case.kernel)
        row = next(item for item in view["markets"] if item["contract_id"]==old)
        self.assertEqual(row["rollover"],{"main_contract":main,"candidate":main,"candidate_basis":"DUAL",
                                        "expiry_date":"2027-01-20","main_expiry_date":"2027-05-20",
                                        "as_of_date":"2026-11-10"})
        self.assertEqual(view["configuration"]["scoped_contracts"],[old])
        self.assertEqual(list(case.port.quotes),[old])
        self.assertEqual(list(case.port.tick_serials),[old])
        self.assertEqual(case.api.inserted,[])
        event = case.session.driver.strategy._latest[(old,"2026-11-10")]["event"]
        self.assertEqual(case.session.feed.facts(event,case.kernel)[1]["days_to_expiry"],70)
        case.api.quotes[main].datetime = "2026-11-09 09:06:00"
        self.assertEqual(case.port.rollover_market_facts(old,"2026-11-10")["candidate_basis"],"MAIN")

    def test_workbench_renders_main_and_changes_only_the_parameter_preview(self):
        app = QApplication.instance() or QApplication([])
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(window.close)
        view = test_workbench.view()
        view["markets"] = [{"contract_id":"DCE.p2701","rollover":{
            "main_contract":"DCE.p2705","candidate":"DCE.p2705","candidate_basis":"DUAL",
            "expiry_date":"2026-11-13","as_of_date":"2026-11-12"}}]
        dashboard = window.dashboard
        window._stale = False
        dashboard.render(view)
        self.assertEqual(dashboard.symbol_grid.item(0,2).text(),"DCE.p2705")
        self.assertIn("还有 1 天到期",dashboard.symbol_grid.item(0,3).text())
        self.assertIn("双超",dashboard.symbol_grid.item(0,3).text())
        dashboard.symbol_grid.selectRow(0)
        with patch("online_trader.desktop.workbench_layout.QMessageBox.question",return_value=QMessageBox.Yes):
            dashboard.stage_selected_rollover()
        self.assertEqual(dashboard.parameter_dialog().profile.to_record()["settings"]["symbols"][0]["code"],"DCE.p2705")
        self.assertEqual(dashboard.profile.to_record()["settings"]["symbols"][0]["code"],"DCE.p2701")
        self.assertIsNotNone(app)
