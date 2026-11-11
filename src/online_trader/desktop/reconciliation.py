"""Explicit statement review and offline fee registration; no trading controls."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import re

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel,
                              QLineEdit, QMainWindow, QMessageBox, QPushButton,
                              QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from online_trader.desktop.client import StatusClient
from online_trader.errors import TraderError


_STATUS = {
    'INCOMPLETE': '字段不完整，不能登记', 'INVALID_ROW': '行数据无效',
    'SCOPE_MISMATCH': '账户/环境/交易日不符', 'DUPLICATE_EXACT': '完全重复，已去重',
    'DUPLICATE_CONFLICT': '重复身份内容冲突', 'EXTERNAL_ONLY': '仅外部存在',
    'LOCAL_ONLY': '仅本地存在', 'CONFLICT': '成交事实冲突', 'AMBIGUOUS': '身份匹配不唯一',
    'MATCHED_FEE_UNKNOWN': '成交一致，外部费用未知', 'ALREADY_CONFIRMED': '费用已确认，无需登记',
    'FEE_CONFLICT': '已确认费用不符', 'MATCHED_FEE_NEW': '精确匹配，可登记费用',
    'MATCHED_ESTIMATED_REPLACEMENT': '精确匹配，可替换估算费用',
}
_VALUES = {'BUY': '买', 'SELL': '卖', 'OPEN': '开', 'REDUCE': '平',
           'LONG': '多', 'SHORT': '空', 'CONFIRMED': '确认', 'ESTIMATED': '估算',
           'UNKNOWN': '未知', 'ONLINE_SIM': '模拟', 'LIVE': '实盘'}
_COLUMNS = (('row_number', '原文件行'), ('status', '对照结果'), ('source_account', '来源账户'),
    ('environment', '环境'), ('trading_day', '交易日'), ('contract_id', '合约'),
    ('side', '买卖'), ('effect', '开平'), ('quantity', '外部量'), ('local_quantity', '本地量'),
    ('price', '外部价'), ('local_price', '本地价'), ('fee', '外部费用'), ('local_fee', '本地费用'),
    ('local_fee_quality', '本地费用质量'), ('local_trade_key', '本地成交身份'), ('issues', '问题原因'))
_LOCAL_COLUMNS = (('trade_key', '本地成交身份'), ('trading_day', '交易日'), ('contract_id', '合约'),
    ('direction', '方向'), ('effect', '开平'), ('quantity', '成交量'), ('price', '成交价'),
    ('fee', '已记录费用'), ('fee_quality', '费用质量'))
_LIMIT = 5000


def _display(value):
    if value is None:return '未知'
    if isinstance(value, list):return '；'.join(str(item) for item in value) if value else '无'
    if type(value) is bool:return '是' if value else '否'
    return _STATUS.get(str(value), _VALUES.get(str(value), str(value)))


def _fee_total(actions):
    with localcontext() as context:
        context.prec = 80
        total = Decimal(0)
        for action in actions:
            if type(action) is not dict or action.get('quality') != 'CONFIRMED' or type(action.get('amount')) is not str:
                raise ValueError('Expected a confirmed fee action')
            value = Decimal(action['amount'])
            if not value.is_finite() or value < 0:raise ValueError('Invalid fee amount')
            total += value
        return format(total, 'f')


class _ReconciliationTask(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, *, action, inputs=None, generation=0, pipe_name=None, preview=None,
                 previewer=None, registrar=None, exporter=None, destination=None, format=None):
        super().__init__()
        self.action, self.inputs, self.generation = action, copy.deepcopy(inputs or {}), generation
        self.pipe_name, self.preview = pipe_name, copy.deepcopy(preview)
        self.previewer, self.registrar, self.exporter = previewer, registrar, exporter
        self.destination, self.format = destination, format

    def run(self):
        try:
            inputs = copy.deepcopy(self.inputs)
            if self.action == 'preview':
                metadata = None
                if self.pipe_name:
                    client = StatusClient(self.pipe_name)
                    projection = client.snapshot()
                    account = next(row for row in projection['records'] if row['record_type'] == 'ACCOUNT')
                    if inputs.get('account_key') and inputs['account_key'] != account['record_id']:
                        raise TraderError('E_SCOPE', 'Statement account differs from the current kernel')
                    inputs['account_key'] = account['record_id']
                    metadata = client.last_metadata
                    if (type(metadata) is not dict or metadata.get('account_key') != inputs['account_key'] or
                            not metadata.get('durable_seq') or not metadata.get('kernel_boot_id')):
                        raise TraderError('E_FACT_BARRIER', 'Statement preview requires an accepted owner boundary')
                    inputs.update(checkpoint_event_seq=metadata['durable_seq'],
                                  expected_kernel_boot_id=metadata['kernel_boot_id'])
                if self.previewer is None:
                    from online_trader.statement_workflow import preview_file
                    previewer = preview_file
                else:previewer = self.previewer
                preview = previewer(**inputs)
                if (type(preview) is not dict or preview.get('schema_version') != 'FILL_RECONCILIATION/1' or
                        preview.get('account_key') != inputs['account_key'] or
                        preview.get('environment') != inputs['environment'] or
                        preview.get('trading_day') != inputs['trading_day'] or
                        preview.get('source_account') != (inputs.get('source_account') or inputs['account_key']) or
                        type(preview.get('blocking')) is not bool or
                        any(type(preview.get(key)) is not list for key in ('rows', 'local_only', 'actions')) or
                        any(any(type(row) is not dict for row in preview[key]) for key in ('rows','local_only','actions')) or
                        type(preview.get('checkpoint_event_seq')) is not str or
                        not re.fullmatch('0|[1-9][0-9]*', preview['checkpoint_event_seq']) or
                        any(type(preview.get(key)) is not str or not re.fullmatch('[0-9a-f]{64}', preview[key])
                            for key in ('source_sha256', 'statement_hash', 'preview_hash'))):
                    raise TraderError('E_RECONCILIATION', 'Invalid comparison preview')
                if metadata and str(preview.get('checkpoint_event_seq')) != str(metadata['durable_seq']):
                    raise TraderError('E_FACT_BARRIER', 'Statement preview crossed the accepted owner boundary')
                result = {'action': self.action, 'preview': preview, 'metadata': metadata,
                          'generation': self.generation, 'inputs': self.inputs}
            elif self.action == 'register':
                if self.pipe_name:raise TraderError('E_NOT_READY', 'Stop the kernel and reopen an offline preview before registration')
                if self.registrar is None:
                    from online_trader.statement_workflow import register_fees
                    registrar = register_fees
                else:registrar = self.registrar
                receipt = registrar(**inputs, expected_preview_hash=self.preview['preview_hash'])
                result = {'action': self.action, 'receipt': receipt, 'generation': self.generation}
            else:
                if self.exporter is None:
                    from online_trader.reconciliation_export import export_comparison
                    exporter = export_comparison
                else:exporter = self.exporter
                destination = exporter(self.preview, self.destination, format=self.format)
                result = {'action': self.action, 'destination': str(destination), 'generation': self.generation}
            if not self.isInterruptionRequested():self.result.emit(result)
        except TraderError as error:
            if not self.isInterruptionRequested():self.failed.emit(error.code)
        except Exception:
            if not self.isInterruptionRequested():self.failed.emit('E_INTERNAL')


class ReconciliationWindow(QMainWindow):
    def __init__(self, ledger_path=None, account_key=None, *, pipe_name=None,
                 previewer=None, registrar=None, exporter=None):
        super().__init__()
        self.pipe_name = pipe_name
        self._previewer, self._registrar, self._exporter = previewer, registrar, exporter
        self._worker = None;self._task_action = None;self._closing = False;self._stale = True
        self._preview = None;self._preview_inputs = None;self._generation = 0
        self.setWindowTitle('OnlineTrader · 离线成交对照与费用导入')
        self.resize(1380, 740)
        central = QWidget(self);layout = QVBoxLayout(central);self.setCentralWidget(central)
        ledger = QHBoxLayout()
        self.path_edit = QLineEdit('' if ledger_path is None else str(ledger_path))
        self.path_edit.setPlaceholderText('本产品 ledger.db')
        self.ledger_button = QPushButton('选择账本')
        self.account_edit = QLineEdit(account_key or '');self.account_edit.setPlaceholderText('本产品账户档案标识')
        ledger.addWidget(self.path_edit, 1);ledger.addWidget(self.ledger_button);ledger.addWidget(self.account_edit)
        layout.addLayout(ledger)
        source = QHBoxLayout()
        self.statement_edit = QLineEdit();self.statement_edit.setPlaceholderText('外部成交 CSV')
        self.statement_button = QPushButton('选择成交 CSV')
        self.source_account_edit = QLineEdit();self.source_account_edit.setPlaceholderText('来源账户，留空表示与本产品档案相同')
        source.addWidget(self.statement_edit, 1);source.addWidget(self.statement_button);source.addWidget(self.source_account_edit)
        layout.addLayout(source)
        scope = QHBoxLayout()
        self.environment = QComboBox();self.environment.addItem('请选择环境', None)
        self.environment.addItem('模拟 · ONLINE_SIM', 'ONLINE_SIM');self.environment.addItem('实盘 · LIVE', 'LIVE')
        self.day_edit = QLineEdit();self.day_edit.setPlaceholderText('明确交易日 YYYY-MM-DD');self.day_edit.setMaximumWidth(220)
        self.preview_button = QPushButton('预览成交对照')
        self.register_button = QPushButton('登记精确匹配费用')
        scope.addWidget(QLabel('环境：'));scope.addWidget(self.environment);scope.addWidget(QLabel('交易日：'));scope.addWidget(self.day_edit)
        scope.addStretch();scope.addWidget(self.preview_button);scope.addWidget(self.register_button);layout.addLayout(scope)
        self.notice_label = QLabel('仅登记精确匹配成交的手续费；不生成成交，不改原成交价量、持仓或保护计划。登记不获得交易权，新增保持禁开。')
        self.notice_label.setWordWrap(True)
        self.mode_label = QLabel('内核连接模式只读预览；登记须在内核退出后，离线重开并重新预览。' if pipe_name else
                                 '离线登记要求账本无运行中的writer、无持仓或活动单；本窗口不停止内核。')
        self.mode_label.setWordWrap(True)
        self.status_label = QLabel('请选择账本、成交文件、账户、环境和交易日 · 尚无预览')
        self.scope_label = QLabel('尚无已核对作用域。');self.scope_label.setWordWrap(True)
        self.hash_label = QLabel('来源文件SHA-256：未知');self.hash_label.setWordWrap(True)
        self.summary_label = QLabel('外部行 未知 · 本地缺失 未知 · 可登记费用 未知');self.summary_label.setWordWrap(True)
        for label in (self.notice_label, self.mode_label, self.status_label, self.scope_label, self.hash_label, self.summary_label):layout.addWidget(label)
        self.tabs = QTabWidget()
        self.external_table = self._table(_COLUMNS);self.local_table = self._table(_LOCAL_COLUMNS)
        self.tabs.addTab(self.external_table, '外部成交逐行对照');self.tabs.addTab(self.local_table, '仅本地存在的成交')
        layout.addWidget(self.tabs, 1)
        controls = QHBoxLayout();self.rows_label = QLabel('尚无对照；重复、冲突及双侧缺失将分别列出。')
        self.rows_label.setWordWrap(True);controls.addWidget(self.rows_label, 1)
        self.csv_button = QPushButton('导出 CSV');self.json_button = QPushButton('导出 JSON');self.xlsx_button = QPushButton('导出 XLSX')
        for button in (self.csv_button, self.json_button, self.xlsx_button):controls.addWidget(button)
        layout.addLayout(controls)
        for field in (self.path_edit, self.statement_edit, self.account_edit, self.source_account_edit, self.day_edit):
            field.textChanged.connect(self._source_changed)
        self.environment.currentIndexChanged.connect(self._source_changed)
        self.ledger_button.clicked.connect(self.choose_ledger);self.statement_button.clicked.connect(self.choose_statement)
        self.preview_button.clicked.connect(self.preview);self.register_button.clicked.connect(self.register_fees)
        self.csv_button.clicked.connect(lambda: self.export('csv'));self.json_button.clicked.connect(lambda: self.export('json'))
        self.xlsx_button.clicked.connect(lambda: self.export('xlsx'));self._enabled(True)

    @staticmethod
    def _table(columns):
        table = QTableWidget(0, len(columns));table.setHorizontalHeaderLabels([label for _, label in columns])
        table.setEditTriggers(QTableWidget.NoEditTriggers);table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        for index, (field, _) in enumerate(columns):
            table.setColumnWidth(index, 190 if field in {'status', 'local_trade_key', 'trade_key', 'source_account'} else
                                 240 if field == 'issues' else 110 if field in {'trading_day','contract_id'} else 85)
        return table

    def _enabled(self, enabled):
        for widget in (self.path_edit,self.statement_edit,self.account_edit,self.source_account_edit,self.day_edit,
                       self.environment,self.ledger_button,self.statement_button,self.preview_button):widget.setEnabled(enabled)
        for button in (self.csv_button,self.json_button,self.xlsx_button):button.setEnabled(enabled and self._preview is not None)
        self.register_button.setEnabled(enabled and self._can_register())

    def _can_register(self):
        if self.pipe_name or self._stale or not self._preview or self._preview.get('blocking') is not False:return False
        if self._preview.get('environment')!='ONLINE_SIM':return False
        actions = self._preview.get('actions')
        if type(actions) is not list or not actions:return False
        rows = self._preview.get('rows', [])
        if any(type(row) is not dict or row.get('status') not in _STATUS for row in rows):return False
        eligible = {row.get('local_trade_key') for row in rows if row.get('status') in
                    {'MATCHED_FEE_NEW','MATCHED_ESTIMATED_REPLACEMENT'}}
        if any(type(action) is not dict or action.get('trade_key') not in eligible for action in actions):return False
        try:_fee_total(actions)
        except (InvalidOperation, ValueError, TypeError):return False
        return True

    def _source_changed(self, *_args):
        self._generation += 1;self._stale = True;self.register_button.setEnabled(False)
        if self._preview is not None:self.status_label.setText('来源或作用域已更改 · 旧预览已过期，须重新预览后登记')

    def choose_ledger(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 OnlineTrader 账本', '', 'SQLite账本 (*.db);;所有文件 (*)')
        if path:self.path_edit.setText(path)

    def choose_statement(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择外部成交 CSV', '', '成交CSV (*.csv *.tsv *.txt);;所有文件 (*)')
        if path:self.statement_edit.setText(path)

    def _inputs(self):
        ledger, statement = self.path_edit.text().strip(), self.statement_edit.text().strip()
        account, day = self.account_edit.text().strip(), self.day_edit.text().strip()
        environment = self.environment.currentData()
        if not ledger or not statement or not account or not environment or not day:
            raise TraderError('E_RECONCILIATION_INPUT', 'Statement and account scope must be explicit')
        try:
            if date.fromisoformat(day).isoformat() != day:raise ValueError()
        except ValueError as error:raise TraderError('E_RECONCILIATION_INPUT', 'Expected an explicit YYYY-MM-DD trading day') from error
        return {'statement_path':Path(statement),'ledger_path':Path(ledger),'account_key':account,
                'environment':environment,'trading_day':day,'source_account':self.source_account_edit.text().strip() or None}

    def _start(self, task):
        if self._worker is not None or self._closing:return
        self._worker = task;self._task_action = task.action;self._enabled(False)
        task.result.connect(self._result);task.failed.connect(self._failed);task.finished.connect(self._finished);task.start()

    def preview(self):
        if self._worker is not None or self._closing:return
        try:inputs = self._inputs()
        except TraderError as error:
            self._stale = True;self.register_button.setEnabled(False)
            self.status_label.setText('请填写账本、成交文件、账户、环境及明确交易日 · '+error.code);return
        self.status_label.setText('正在核对文件与本地已确认成交…')
        self._start(_ReconciliationTask(action='preview',inputs=inputs,generation=self._generation,
                                      pipe_name=self.pipe_name,previewer=self._previewer))

    def register_fees(self):
        if self._worker is not None or self._closing or not self._can_register():return
        preview = copy.deepcopy(self._preview);inputs = copy.deepcopy(self._preview_inputs);generation = self._generation
        count, total = len(preview['actions']), _fee_total(preview['actions'])
        question = (f"仅登记 {count} 条精确匹配成交的确认费用，合计 {total} CNY。\n"
            f"账户：{preview['account_key']}\n环境：{preview['environment']} · 交易日：{preview['trading_day']}\n"
            f"来源文件 SHA-256：{preview['source_sha256']}\n预览：{preview['preview_hash']}\n"
            '登记追加费用证据，不修改原始成交，维护后新增保持禁开；存在writer、持仓、活动单或文件变更时将拒绝。')
        answer = QMessageBox.question(self,'确认登记匹配费用',question,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer != QMessageBox.Yes:return
        if self._stale or generation != self._generation or preview['preview_hash'] != self._preview['preview_hash']:
            self.status_label.setText('确认期间来源已更改 · 请重新预览');return
        self.status_label.setText('正在登记本次已确认的匹配费用…')
        self._start(_ReconciliationTask(action='register',inputs=inputs,generation=generation,preview=preview,
                                      registrar=self._registrar,pipe_name=self.pipe_name))

    def export(self, format):
        if self._worker is not None or self._closing or self._preview is None:return
        path, _ = QFileDialog.getSaveFileName(self,'导出完整成交对照 '+format.upper(),'',format.upper()+' (*.'+format+')')
        if not path:return
        self._start(_ReconciliationTask(action='export',generation=self._generation,preview=self._preview,
                                      exporter=self._exporter,destination=Path(path),format=format))

    def _result(self, result):
        if self._closing:return
        if result['action'] == 'export':
            self.status_label.setText('已导出：'+result['destination']+(' · 预览已过期' if self._stale else ''));return
        if result['action'] == 'register':
            self._stale = True
            receipt = result['receipt'];stage = receipt.get('status','UNKNOWN') if isinstance(receipt,dict) else 'UNKNOWN'
            self.status_label.setText('费用登记回执：'+str(stage)+' · 原预览已失效，请重新预览核对结果');return
        if result['generation'] != self._generation:
            self._stale = True;self.status_label.setText('读取期间来源已改变 · 未发布旧预览，请重新预览');return
        preview = copy.deepcopy(result['preview']);self._preview = preview;self._preview_inputs = copy.deepcopy(result['inputs']);self._stale = False
        self.mode_label.setText('实盘账本仅用于只读对照；此入口不登记实盘费用。' if preview['environment']=='LIVE' else
            '内核连接模式只读预览；登记须在内核退出后，离线重开并重新预览。' if self.pipe_name else
            '离线登记要求账本无运行中的writer、无持仓或活动单；本窗口不停止内核。')
        self.scope_label.setText(f"账户 {preview['account_key']} · 来源账户 {preview['source_account']} · 环境 {_display(preview['environment'])} · 交易日 {preview['trading_day']} · 本地事件 {preview['checkpoint_event_seq']}")
        self.hash_label.setText('来源文件 SHA-256：'+preview['source_sha256']+'\n规范内容 SHA-256：'+preview['statement_hash'])
        try:total = _fee_total(preview['actions'])
        except (InvalidOperation,ValueError,TypeError):total = '未知'
        self.summary_label.setText(f"外部 {len(preview['rows'])} 行 · 仅本地 {len(preview['local_only'])} 行 · 可登记 {len(preview['actions'])} 条费用 / {total} CNY · "+
                                  ('存在阻断项，整批不得登记' if preview['blocking'] else '按精确匹配项目登记，不会生成外部成交'))
        self.status_label.setText(('内核已接受边界，只读预览' if result.get('metadata') else '离线账本预览已完成')+
                                  (' · 有阻断项' if preview['blocking'] else ' · 请逐项核对'))
        self._render(self.external_table,preview['rows'],_COLUMNS)
        self._render(self.local_table,preview['local_only'],_LOCAL_COLUMNS)
        self.rows_label.setText(f"外部 {len(preview['rows'])} 行 / 本地缺失 {len(preview['local_only'])} 行；每表最多显示 {_LIMIT} 行，导出包含完整对照。")

    @staticmethod
    def _render(table,rows,columns):
        table.setRowCount(min(len(rows),_LIMIT))
        for index,row in enumerate(rows[:_LIMIT]):
            for column,(field,_) in enumerate(columns):
                value = row.get('source_account',row.get('account_key')) if field == 'source_account' else row.get(field)
                item = QTableWidgetItem(_display(value));item.setToolTip(item.text())
                item.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter);table.setItem(index,column,item)

    def _failed(self, code):
        if self._closing:return
        if self._task_action in {'preview','register'}:
            self._stale = True
            self.status_label.setText(('预览失败' if self._task_action == 'preview' else '登记失败')+' · '+code+' · 保留原预览并标过期，请重新预览')
        else:self.status_label.setText('导出失败 · '+code+'；已有文件不会被覆盖')

    def _finished(self):
        task,self._worker = self._worker,None
        if task is not None:task.deleteLater()
        self._task_action = None
        if self._closing:QTimer.singleShot(0,self.close)
        else:self._enabled(True)

    def closeEvent(self,event):
        if self._worker is not None:
            self._closing = True;self._worker.requestInterruption();self.hide();event.ignore();return
        event.accept()

    def showEvent(self,event):
        if self._worker is None:self._closing = False
        super().showEvent(event)


def run(ledger_path=None,account_key=None,*,pipe_name=None):
    app = QApplication.instance() or QApplication([])
    window = ReconciliationWindow(ledger_path,account_key,pipe_name=pipe_name);window.show()
    return app.exec()
