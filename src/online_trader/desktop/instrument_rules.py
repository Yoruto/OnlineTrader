"""Desktop DSV-compatible PDF rule preview and display-only markup."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSettings, Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHeaderView,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from online_trader.configuration.instrument_rules import (
    EXCHANGE_LABELS, MAX_RULE_PAGES, MAX_RULE_TEXT, ProductRule, parse_rule_pages, product_code, rule_display,
)
from online_trader.errors import TraderError


MAX_PDF_BYTES = 16 * 1024 * 1024
_SETTINGS_KEY = 'instrumentRules/reference'
_MARKUP_KEY = 'instrumentRules/markup'


def read_rule_pdf(path):
    from PySide6.QtPdf import QPdfDocument

    path = Path(path)
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_PDF_BYTES + 1)
    except OSError as error:
        raise TraderError('E_INSTRUMENT_PDF', '无法读取所选 PDF') from error
    if len(raw) > MAX_PDF_BYTES:
        raise TraderError('E_INSTRUMENT_PDF', 'PDF 文件超出支持范围')
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.ReadOnly)
    document = QPdfDocument()
    try:
        document.load(buffer)
        if document.status() != QPdfDocument.Status.Ready:
            raise TraderError('E_INSTRUMENT_PDF', '无法打开所选 PDF，请检查文件格式或密码')
        pages = []
        total = 0
        for index in range(min(document.pageCount(), MAX_RULE_PAGES)):
            page = document.getAllText(index).text()
            total += len(page)
            if total > MAX_RULE_TEXT:
                raise TraderError('E_INSTRUMENT_RULES', 'PDF 规则表文本超出支持范围')
            pages.append(page)
        rows = parse_rule_pages(pages)
    finally:
        document.close()
    return {
        'source': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
        'date': datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
    }, rows


def _table(headers):
    grid = QTableWidget(0, len(headers))
    grid.setHorizontalHeaderLabels(headers)
    grid.setEditTriggers(QTableWidget.NoEditTriggers)
    grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    return grid


def _fill(grid, rows):
    grid.setRowCount(len(rows))
    for index, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignCenter)
            grid.setItem(index, column, item)


def _percent(rate, from_table=False):
    return '—' if rate is None else f'{rate * 100:.2f}%' + (' 表' if from_table else '')


class InstrumentRulesPanel(QGroupBox):
    def __init__(self, settings, log, parent=None):
        super().__init__('保证金与手续费（行情及期货公司加价）', parent)
        self.settings = settings
        self.log = log
        self.reference, self.rules = self._load_reference()
        self.margin_add, self.fee_add = self._load_markup()
        self.logged_differences = set()
        self.symbols, self.markets = {}, {}
        self.last_rows = None
        layout = QVBoxLayout(self)
        markup = QHBoxLayout()
        self.margin_input = QDoubleSpinBox()
        self.margin_input.setRange(0, 100)
        self.margin_input.setValue(float(self.margin_add))
        self.fee_input = QDoubleSpinBox()
        self.fee_input.setRange(0, 1_000_000)
        self.fee_input.setValue(float(self.fee_add))
        apply = QPushButton('应用')
        apply.clicked.connect(self.apply_markup)
        for widget in (QLabel('保证金 +'), self.margin_input, QLabel('个百分点'),
                       QLabel('手续费 +'), self.fee_input, QLabel('元／手'), apply):
            markup.addWidget(widget)
        markup.addStretch()
        layout.addLayout(markup)
        source = QHBoxLayout()
        import_button = QPushButton('导入规则表(PDF)')
        import_button.clicked.connect(self.import_pdf)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        source.addWidget(import_button)
        source.addWidget(self.source_label, 1)
        layout.addLayout(source)
        self.grid = _table(('品种','交易所','保证金（交易所）','保证金（公司）','手续费（交易所）','手续费（公司）'))
        layout.addWidget(self.grid, 1)
        self.render()

    def _load_reference(self):
        try:
            raw = self.settings.value(_SETTINGS_KEY, '{}', type=str)
            if len(raw) > MAX_RULE_TEXT:
                return {}, {}
            record = json.loads(raw)
            rows = [ProductRule.from_record(row) for row in record.get('rows', [])]
            metadata = {key: record[key] for key in ('source','sha256','date')}
            return metadata, {row.code:row for row in rows}
        except (TraderError, ValueError, TypeError, AttributeError, KeyError):
            return {}, {}

    def _load_markup(self):
        try:
            raw = json.loads(self.settings.value(_MARKUP_KEY, '[0,0]', type=str))
            margin, fee = (Decimal(str(value)) for value in raw)
            if 0 <= margin <= 100 and 0 <= fee <= 1_000_000:
                return margin, fee
        except (TypeError, ValueError, InvalidOperation):
            pass
        return Decimal(0), Decimal(0)

    def _save(self, key, value):
        self.settings.setValue(key, json.dumps(value, ensure_ascii=False))
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise TraderError('E_INSTRUMENT_SETTINGS', '无法保存品种信息设置')

    def apply_markup(self):
        margin = Decimal(str(self.margin_input.value()))
        fee = Decimal(str(self.fee_input.value()))
        try:
            self._save(_MARKUP_KEY, [str(margin), str(fee)])
        except TraderError as error:
            QMessageBox.warning(self, '期货公司加价', str(error))
            return
        self.margin_add, self.fee_add = margin, fee
        self.log(f'[品种信息] 公司加价：保证金 +{margin:.2f} 个百分点，手续费 +{fee:.2f} 元／手')
        self.render()

    def import_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择规则表 PDF', '', 'PDF 文件 (*.pdf)')
        if not path:
            return
        try:
            metadata, rows = read_rule_pdf(path)
            if self.preview_import(metadata, rows):
                self.apply_reference(metadata, rows)
        except TraderError as error:
            QMessageBox.warning(self, '导入规则表', str(error))

    def preview_import(self, metadata, rows):
        dialog = QDialog(self)
        dialog.setWindowTitle('规则表预览')
        dialog.resize(860, 480)
        layout = QVBoxLayout(dialog)
        summary = QLabel(f"从 {metadata['source']} 解析出 {len(rows)} 个品种。行情优先，缺失时显示规则表数据。")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        grid = _table(('品种','代码','交易所','合约单位','最小变动','保证金（交易所）','保证金（公司）'))
        _fill(grid, [(r.name, r.code, EXCHANGE_LABELS[r.exchange], r.multiplier, r.tick,
                      _percent(r.exchange_margin), _percent(r.company_margin)) for r in rows])
        layout.addWidget(grid, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).setText('应用导入')
        buttons.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def apply_reference(self, metadata, rows):
        combined = self.rules | {rule.code:rule for rule in rows}
        self._save(_SETTINGS_KEY, metadata | {'rows':[rule.record() for rule in combined.values()]})
        self.reference, self.rules = metadata, combined
        self.log(f"[规则表] 已导入 {len(rows)} 个品种（{metadata['source']}）")
        self.render()

    def set_markets(self, symbols, markets):
        self.symbols, self.markets = symbols, markets
        self.render()

    def render(self):
        displayed = []
        differences = 0
        for contract, name in self.symbols.items():
            rule = self.rules.get(product_code(contract))
            values = rule_display(self.markets.get(contract, {}), rule, self.margin_add, self.fee_add)
            difference = values['difference']
            if difference is not None:
                differences += 1
                key = (self.reference.get('sha256'), contract)
                if key not in self.logged_differences:
                    self.logged_differences.add(key)
                    self.log(f'[规则核对] {contract} 行情保证金与表格差 {difference * 100:+.2f} 个百分点')
            exchange = contract.partition('.')[0]
            table = values['margin_source'] == '规则表'
            fee, company_fee = values['fee'], values['company_fee']
            displayed.append((name + (' ⚠' if difference is not None else ''), EXCHANGE_LABELS.get(exchange, exchange),
                              _percent(values['exchange_margin'], table), _percent(values['company_margin'], table),
                              f'{fee:.2f}' if fee is not None else '—', f'{company_fee:.2f}' if company_fee is not None else '—'))
        if displayed != self.last_rows:
            _fill(self.grid, displayed)
            self.last_rows = displayed
        if self.reference:
            self.source_label.setText(f"规则表 {self.reference['date']}（{self.reference['source']} · {len(self.rules)} 品种）"
                                      + (f' · 差异 {differences} 项' if differences else ''))
        else:
            self.source_label.setText('（未导入规则表）')
