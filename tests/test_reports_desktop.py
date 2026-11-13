"""Read-only report UI preserves source scope, unknown values and export intent."""

import copy
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from online_trader.desktop.app import StatusWindow
from online_trader.desktop.reports import EquityChart, ReportsWindow
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from tests.qt_lifecycle import dispose_widget


def report():
    totals = {"gross_realized_pnl": "120", "confirmed_fees": "8", "estimated_fees": "2",
        "fees": None, "net_realized_pnl": None, "estimated_net_realized_pnl": "110",
        "unrealized_pnl": None, "entry_quantity": 3, "exit_quantity": 2, "fill_count": 2}
    return {"schema_version": "MONEY_REPORT/1", "account_key": "DEV:report-view", "currency": "CNY",
        "checkpoint_event_seq": "17", "scope": "LOCAL_OWNED_CYCLES",
        "accounting_basis": "CONFIRMED_FILL_FIFO", "summary": totals,
        "by_contract": [dict(totals, contract_id="DCE.p2701")],
        "by_day": [dict(totals, trading_day="2026-11-09", gross_realized_pnl="0", confirmed_fees="8",
            estimated_fees="0", fees="8", net_realized_pnl="-8", estimated_net_realized_pnl="-8",
            entry_quantity=3, exit_quantity=0, fill_count=1),
            dict(totals, trading_day="2026-11-10", confirmed_fees="0", estimated_net_realized_pnl="118",
                 entry_quantity=0, exit_quantity=2, fill_count=1)],
        "cycles": [dict(totals, position_cycle_id="cycle-view", contract_id="DCE.p2701", direction="LONG",
            trading_day="2026-11-09", remaining_quantity=1, closed=False)],
        "trades": [{"trading_day": day, "observed_at_utc": day+"T01:06:00Z", "contract_id": "DCE.p2701",
            "direction": "LONG", "effect": "OPEN" if index == 0 else "REDUCE", "quantity": 3 if index == 0 else 2,
            "price": "1055" if index == 0 else "1061", "realized_pnl": "0" if index == 0 else "120",
            "fee": "8" if index == 0 else "2", "fee_quality": "CONFIRMED" if index == 0 else "ESTIMATED",
            "directional_slippage_points": "1" if index == 0 else None, "trade_key": "trade-view-"+str(index)}
            for index, day in enumerate(("2026-11-09", "2026-11-10"))],
        "equity": [{"trading_day": "2026-11-09", "observed_at_utc": "2026-11-09T01:05:00Z",
            "balance": "100000", "available": "80000", "margin": "10000", "pre_balance": "99000",
            "commission": "8", "today_pnl": "1000", "net_cash_flow": None, "trading_pnl": None}],
        "nominal": [{"contract_id": "DCE.p2701", "trading_day": "2026-11-09", "daily_pnl_points": "16",
            "net_loss_points": "-16", "trade_count": 2, "stop_count": 0, "profit_count": 1, "stop_streak": 0}],
        "issues": ["MISSING_CONFIRMED_FEES"]}


class ReportDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):self.windows = []

    def tearDown(self):
        for window in self.windows:
            if getattr(window, "_worker", None) is not None:self.wait_idle(window)
            dispose_widget(window)
        self.app.processEvents()

    def window(self, *, reader=None, exporter=None, pipe_name=None, account_key="DEV:report-view", path="ledger.db"):
        window = ReportsWindow(path, account_key, reader=reader or (lambda *_args, **_kwargs: report()),
                               exporter=exporter, pipe_name=pipe_name)
        self.windows.append(window)
        return window

    def wait_idle(self, window):
        deadline = time.monotonic()+3
        while window._worker is not None and time.monotonic()<deadline:
            self.app.processEvents();QTest.qWait(5)
        self.assertIsNone(window._worker)

    def loaded(self, **kwargs):
        window = self.window(**kwargs);window.refresh();self.wait_idle(window);return window

    def test_empty_window_has_unknown_funds_and_no_generated_data(self):
        window = self.window(path=None, account_key=None)
        window.refresh()
        self.assertIsNone(window._worker)
        self.assertIsNone(window._report)
        self.assertIn("未知", window.account_label.text())
        self.assertEqual(window.tables["trades"].rowCount(), 0)
        self.assertFalse(window.csv_button.isEnabled())

    def test_money_unknown_and_estimated_values_are_distinct_from_confirmed(self):
        window = self.loaded()
        self.assertIn("确认费用小计 8", window.local_label.text())
        self.assertIn("费用合计 未知", window.local_label.text())
        self.assertIn("已实现净收益 未知", window.local_label.text())
        self.assertIn("估算净收益 ≈110", window.local_label.text())
        self.assertIn("权益 100000", window.account_label.text())
        self.assertEqual(window.tables["trades"].item(0, 8).text(), "8")
        self.assertEqual(window.tables["trades"].item(1, 8).text(), "≈2")
        self.assertEqual(window.tables["nominal"].item(0, 2).text(), "16")
        self.assertFalse(window._stale)

    def test_read_failure_keeps_last_complete_rows_and_marks_them_stale(self):
        calls = []
        def read(*_args, **_kwargs):
            calls.append(1)
            if len(calls)>1:raise TraderError("E_STORAGE", "Unavailable")
            return report()
        window = self.loaded(reader=read)
        before = window.tables["trades"].item(0, 11).text()
        window.refresh();self.wait_idle(window)
        self.assertTrue(window._stale)
        self.assertEqual(window.tables["trades"].item(0, 11).text(), before)
        self.assertIn("E_STORAGE", window.status_label.text())
        self.assertIn("已过期", window.status_label.text())

    def test_date_filter_changes_detail_rows_without_rewriting_account_or_scope_totals(self):
        window = self.loaded();before = window.local_label.text()
        index = window.from_day.findData("2026-11-10")
        window.from_day.setCurrentIndex(index)
        self.assertEqual(window.tables["trades"].rowCount(), 1)
        self.assertEqual(window.tables["trades"].item(0, 0).text(), "2026-11-10")
        self.assertEqual(window.tables["by_contract"].rowCount(), 1)
        self.assertEqual(window.local_label.text(), before)
        self.assertIn("完整账本范围", window.rows_label.text())

    def test_reverse_date_filter_does_not_fabricate_matching_rows(self):
        window = self.loaded()
        window.from_day.setCurrentIndex(window.from_day.findData("2026-11-10"))
        window.to_day.setCurrentIndex(window.to_day.findData("2026-11-09"))
        self.assertEqual(window.tables["trades"].rowCount(), 0)
        self.assertEqual(window.tables["by_day"].rowCount(), 0)
        self.assertEqual(window.tables["by_contract"].rowCount(), 1)

    def test_csv_exports_the_selected_filtered_table_and_json_keeps_the_complete_report(self):
        calls = []
        def export(value, destination, **kwargs):
            calls.append((copy.deepcopy(value), destination, kwargs));return destination
        window = self.loaded(exporter=export)
        window.tabs.setCurrentIndex(3)
        window.from_day.setCurrentIndex(window.from_day.findData("2026-11-10"))
        with patch("online_trader.desktop.reports.QFileDialog.getSaveFileName", return_value=("daily.csv", "CSV")):
            window.export("csv");self.wait_idle(window)
        self.assertEqual(len(calls[0][0]["trades"]), 1)
        self.assertEqual(calls[0][2], {"table": "trades", "format": "csv"})
        with patch("online_trader.desktop.reports.QFileDialog.getSaveFileName", return_value=("complete.json", "JSON")):
            window.export("json");self.wait_idle(window)
        self.assertEqual(len(calls[1][0]["trades"]), 2)
        self.assertEqual(calls[1][2]["format"], "json")
        self.assertEqual(len(window._report["trades"]), 2)

    def test_export_failure_keeps_the_report_and_never_retries_with_an_overwrite_flag(self):
        calls = []
        def export(*args, **kwargs):
            calls.append(kwargs);raise TraderError("E_EXISTS", "Output exists")
        window = self.loaded(exporter=export)
        with patch("online_trader.desktop.reports.QFileDialog.getSaveFileName", return_value=("existing.csv", "CSV")):
            window.export("csv");self.wait_idle(window)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("overwrite", calls[0])
        self.assertIn("E_EXISTS", window.status_label.text())
        self.assertIsNotNone(window._report)
        self.assertFalse(window._stale)

    def test_cancel_export_has_no_file_or_kernel_operation(self):
        calls = []
        window = self.loaded(exporter=lambda *_args, **_kwargs: calls.append(1))
        with patch("online_trader.desktop.reports.QFileDialog.getSaveFileName", return_value=("", "")):
            window.export("csv")
        self.assertEqual(calls, [])
        self.assertIsNone(window._worker)

    def test_pipe_reader_uses_the_matching_accepted_owner_boundary_without_commands(self):
        calls = []
        class Client:
            last_metadata = {"account_key": "DEV:report-view", "durable_seq": "17", "received_at_utc": "2026-11-10T01:00:00Z", "kernel_boot_id": "boot-report"}
            def __init__(self, pipe):calls.append(("connect", pipe))
            def snapshot(self):
                calls.append("snapshot")
                return {"records": [{"record_type": "ACCOUNT", "record_id": "DEV:report-view"}]}
            def pause(self):raise AssertionError("Report cannot send control commands")
        def read(path, account, **kwargs):
            calls.append((str(path), account, kwargs));return report()
        with patch("online_trader.desktop.reports.StatusClient", Client):
            window = self.loaded(reader=read, pipe_name="report-pipe")
        self.assertEqual(calls[2], ("ledger.db", "DEV:report-view", {"checkpoint_event_seq": "17", "as_of_utc": "2026-11-10T01:00:00Z", "expected_kernel_boot_id": "boot-report"}))
        self.assertIn("内核已接受", window.status_label.text())
        self.assertNotIn("pause", calls)

    def test_pipe_account_mismatch_refuses_to_read_the_ledger(self):
        calls = []
        class Client:
            def __init__(self, _pipe):pass
            def snapshot(self):return {"records": [{"record_type": "ACCOUNT", "record_id": "DEV:other"}]}
        with patch("online_trader.desktop.reports.StatusClient", Client):
            window = self.loaded(reader=lambda *_args, **_kwargs: calls.append(1), pipe_name="report-pipe")
        self.assertEqual(calls, [])
        self.assertIsNone(window._report)
        self.assertIn("E_SCOPE", window.status_label.text())

    def test_report_cannot_publish_beyond_the_owner_snapshot_watermark(self):
        class Client:
            last_metadata = {"account_key": "DEV:report-view", "durable_seq": "16", "received_at_utc": "2026-11-10T01:00:00Z", "kernel_boot_id": "boot-report"}
            def __init__(self, _pipe):pass
            def snapshot(self):return {"records": [{"record_type": "ACCOUNT", "record_id": "DEV:report-view"}]}
        with patch("online_trader.desktop.reports.StatusClient", Client):window = self.loaded(pipe_name="report-pipe")
        self.assertIsNone(window._report)
        self.assertIn("E_FACT_BARRIER", window.status_label.text())

    def test_pipe_missing_boot_identity_refuses_to_read_any_report(self):
        calls = []
        class Client:
            last_metadata = {"account_key": "DEV:report-view", "durable_seq": "17", "received_at_utc": "2026-11-10T01:00:00Z"}
            def __init__(self, _pipe):pass
            def snapshot(self):return {"records": [{"record_type": "ACCOUNT", "record_id": "DEV:report-view"}]}
        with patch("online_trader.desktop.reports.StatusClient", Client):
            window = self.loaded(reader=lambda *_args, **_kwargs: calls.append(1), pipe_name="report-pipe")
        self.assertEqual(calls, [])
        self.assertIsNone(window._report)
        self.assertIn("E_FACT_BARRIER", window.status_label.text())

    def test_bound_daily_report_falls_back_only_when_the_account_owner_is_absent(self):
        class UnavailableClient:
            def __init__(self, _pipe):pass
            def snapshot(self):raise TraderError("E_TIMEOUT", "No IPC response")

        calls = []
        def read(path, account, **options):
            calls.append((path, account, options))
            return report()

        window = self.window(reader=read, account_key=None, pipe_name="missing-pipe")
        window.bind_source("selected-ledger.db", "DEV:report-view")
        with patch("online_trader.desktop.reports.StatusClient", UnavailableClient):
            with AccountLock("DEV:report-view"):
                window.request_daily_export(allow_offline=True)
                self.wait_idle(window)
                self.assertIn("E_INSTANCE_EXISTS", window.status_label.text())
                self.assertEqual(calls, [])
            with patch("online_trader.desktop.reports.QInputDialog.getItem", return_value=("", False)):
                window.request_daily_export(allow_offline=True)
                self.wait_idle(window)
                self.app.processEvents()
        self.assertIn("离线账本已提交", window.status_label.text())
        self.assertEqual(calls, [(Path("selected-ledger.db"), "DEV:report-view", {})])

    def test_reader_boot_mismatch_preserves_old_rows_and_marks_the_report_stale(self):
        calls = []
        class Client:
            last_metadata = {"account_key": "DEV:report-view", "durable_seq": "17", "received_at_utc": "2026-11-10T01:00:00Z", "kernel_boot_id": "boot-report"}
            def __init__(self, _pipe):pass
            def snapshot(self):return {"records": [{"record_type": "ACCOUNT", "record_id": "DEV:report-view"}]}
        def read(*_args, **kwargs):
            calls.append(kwargs)
            if len(calls)>1:raise TraderError("E_BOOT_MISMATCH", "Ledger belongs to another kernel boot")
            return report()
        with patch("online_trader.desktop.reports.StatusClient", Client):
            window = self.loaded(reader=read, pipe_name="report-pipe")
            before = window.tables['trades'].item(0, 11).text()
            window.refresh();self.wait_idle(window)
        self.assertEqual(calls[1]['expected_kernel_boot_id'], 'boot-report')
        self.assertEqual(window.tables['trades'].item(0, 11).text(), before)
        self.assertTrue(window._stale)
        self.assertIn('E_BOOT_MISMATCH', window.status_label.text())

    def test_incomplete_money_source_keeps_known_fields_but_clearly_labels_incompleteness(self):
        value = report()
        value['equity'][0].update(complete=False,today_pnl=None)
        value['account'] = copy.deepcopy(value['equity'][0])
        value['issues'] += [{'code':'FEE_INPUT_INVALID'}, {'code':'RISK_MONEY_FIELD_INVALID'}]
        window = self.loaded(reader=lambda *_args, **_kwargs: value)
        self.assertIn('资金来源不完整', window.account_label.text())
        self.assertIn('权益 100000', window.account_label.text())
        self.assertIn('DSV当日盈亏 未知', window.account_label.text())
        self.assertIn('资金来源不完整', window.quality_label.text())
        self.assertIn('费用输入无效', window.quality_label.text())
        self.assertIn('资金来源字段无效', window.quality_label.text())
        self.assertEqual(window.tables['equity'].item(0,2).text(),'否')

    def test_close_while_reader_runs_is_local_and_does_not_block_or_publish_late_data(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def read(*_args, **_kwargs):
            calls.append("read");entered.set()
            if not release.wait(2):raise TraderError("E_TIMEOUT", "Timed out")
            return report()
        window = self.window(reader=read)
        try:
            start = time.monotonic();window.refresh()
            self.assertLess(time.monotonic()-start, 0.25)
            self.assertTrue(entered.wait(1))
            window.refresh()
            before = time.monotonic();window.close()
            self.assertLess(time.monotonic()-before, 0.25)
            self.assertTrue(window._closing)
            release.set();self.wait_idle(window);self.app.processEvents()
            self.assertIsNone(window._report)
            self.assertEqual(calls, ["read"])
            window.show();window.refresh();self.wait_idle(window)
            self.assertIsNotNone(window._report)
            self.assertEqual(calls, ["read", "read"])
        finally:release.set()

    def test_changed_source_marks_existing_report_stale_without_reading_or_clearing_it(self):
        window = self.loaded()
        window.path_edit.setText("another-ledger.db");window._source_changed()
        self.assertTrue(window._stale)
        self.assertEqual(window.tables["trades"].rowCount(), 2)
        self.assertIsNone(window._worker)

    def test_status_console_opens_a_readonly_report_window_without_fetching_or_pausing(self):
        calls = []
        class Client:
            def __init__(self, _pipe):calls.append("client")
            def snapshot(self):calls.append("snapshot")
            def pause(self):calls.append("pause")
        status = StatusWindow("readonly-pipe", client_factory=Client)
        self.windows.append(status)
        status._account_key = "DEV:report-view"
        status.reports_button.click()
        window = status._reports_window
        self.windows.append(window)
        self.assertIsInstance(window, ReportsWindow)
        self.assertEqual(window.account_edit.text(), "DEV:report-view")
        self.assertEqual(window.pipe_name, "readonly-pipe")
        self.assertEqual(calls, [])

    def test_equity_chart_preserves_missing_points_and_does_not_convert_them_to_zero(self):
        chart = EquityChart()
        self.windows.append(chart)
        chart.set_rows([{"balance": "100000"}, {"balance": None}, {"balance": "100010"}])
        self.assertIsNone(chart.values()[1])
        chart.resize(800, 130);chart.show();self.app.processEvents()
        self.assertFalse(chart.grab().isNull())


if __name__ == "__main__":unittest.main()
