"""One instrument at a time, with a fixed footer and atomic preview updates."""
from decimal import Decimal
import re

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import (QCompleter, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QComboBox)

from online_trader.configuration.editor import EditError, form_values, integer_text, number_text, profile_from_form
from online_trader.configuration.symbol_csv import PRODUCT_NAMES, match_contract_catalog, resolve_contract
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError


def fit_dialog(dialog, width, height):
    """Reserve room for the native frame and taskbar, including scaled displays."""
    available = dialog.screen().availableGeometry()
    width, height = min(width, available.width() - 40), min(height, available.height() - 64)
    dialog.resize(width, height)
    parent = dialog.parentWidget()
    center = parent.frameGeometry().center() if parent else available.center()
    dialog.move(max(available.left() + 10, min(center.x() - width // 2, available.right() - width - 10)),
                max(available.top() + 30, min(center.y() - height // 2, available.bottom() - height - 30)))


class SymbolEditorDialog(QDialog):
    def __init__(self, profile, parent=None, *, symbol_row=None, contract_catalog=(), catalog_loader=None):
        super().__init__(parent)
        self.base_profile, self.base_hash = profile, profile.content_hash
        self.symbol_row = symbol_row
        self.result_profile = self.result_symbol = None
        self._catalog_loader = catalog_loader
        self._catalog_loading = False
        self._contract_catalog = tuple(dict.fromkeys(contract_catalog or ()))
        rows = form_values(profile)['symbols']
        if symbol_row is not None and not 0 <= symbol_row < len(rows):
            raise TraderError('E_CONFIG_EDIT', '请选择一个有效品种。')
        self.adding = symbol_row is None
        source = ({'name': '', 'code': '', 'open_offset': '0', 'stop_offset': '0', 'profit_offset': '0',
                   'order_volume': '自动', 'half_close_ratio': '0.5', 'hysteresis_points': '0', 'code_new': ''}
                  if self.adding else rows[symbol_row])
        title = '添加品种' if self.adding else '编辑品种'
        self.setWindowTitle('OnlineTrader · ' + title)
        self.setMinimumSize(420, 300)
        self.setSizeGripEnabled(True)
        self.setStyleSheet('QLineEdit { min-height: 27px; padding: 2px 7px; }'
                           'QLineEdit[invalid="true"] { border: 1px solid #c83a36; }'
                           'QPushButton { min-height: 30px; padding: 2px 14px; }')
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 14, 18, 14); layout.setSpacing(10)
        heading = QLabel(title); heading.setStyleSheet('font-size: 18px; font-weight: bold;')
        layout.addWidget(heading)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body = QWidget(); content = QVBoxLayout(body); content.setContentsMargins(0, 0, 10, 0); content.setSpacing(12)
        self.fields = {}

        def field(key, label, value, form, placeholder=''):
            edit = QLineEdit(str(value)); edit.setObjectName('symbol_' + key); edit.setAccessibleName(label)
            edit.setPlaceholderText(placeholder); self.fields[key] = edit; form.addRow(label, edit)
            return edit

        identity = QFormLayout(); identity.setVerticalSpacing(9)
        field('code', '合约代码 *', source['code'], identity, '例如 cu2701、铜2701 或 SHFE.cu2701')
        self.contract_hint = QLabel('填写具体月份的合约，名称会自动补全。'); self.contract_hint.setWordWrap(True)
        self.contract_hint.setStyleSheet('color: #63738a;'); identity.addRow('', self.contract_hint)
        field('name', '品种名称 *', source['name'], identity, '自动补全后也可以自行修改')
        content.addLayout(identity)
        parameters = QGroupBox('交易参数'); form = QFormLayout(parameters); form.setVerticalSpacing(9)
        for key, label in (('open_offset', '开仓（点）'), ('stop_offset', '止损（点）'), ('profit_offset', '止盈（点）')):
            field(key, label, source[key], form)
        defaults = QLabel('偏移填 0 时沿用策略启动值：开仓 100、止损 15、止盈 60。')
        defaults.setWordWrap(True); defaults.setStyleSheet('color: #63738a;'); form.addRow(defaults)
        volume = QWidget(); volume_row = QHBoxLayout(volume); volume_row.setContentsMargins(0, 0, 0, 0)
        self.volume_mode = QComboBox(); self.volume_mode.setAccessibleName('下单方式')
        self.volume_mode.addItems(['自动计算手数', '固定手数'])
        self.volume_input = QLineEdit('1' if source['order_volume'] == '自动' else source['order_volume'])
        self.volume_input.setAccessibleName('固定手数'); self.volume_input.setObjectName('symbol_order_volume')
        self.fields['order_volume'] = self.volume_input
        volume_row.addWidget(self.volume_mode); volume_row.addWidget(self.volume_input); volume_row.addWidget(QLabel('手'))
        form.addRow('下单手数', volume); content.addWidget(parameters)
        self.more_button = QPushButton('更多设置 ▸'); self.more_button.setCheckable(True); self.more_button.setAutoDefault(False)
        content.addWidget(self.more_button, alignment=Qt.AlignLeft)
        self.more_body = QWidget(); advanced = QFormLayout(self.more_body); advanced.setContentsMargins(0, 0, 0, 0)
        field('half_close_ratio', '半仓比例（%）', decimal_text(Decimal(source['half_close_ratio']) * 100), advanced)
        field('hysteresis_points', '滞回点', source['hysteresis_points'], advanced)
        field('code_new', '候选换月合约', source.get('code_new', ''), advanced, '选填，例如 cu2702')
        content.addWidget(self.more_body); content.addStretch()
        self.more_body.hide(); self.more_button.toggled.connect(self._show_more)
        self.volume_mode.currentIndexChanged.connect(lambda index: self.volume_input.setEnabled(index == 1))
        self.volume_mode.setCurrentIndex(0 if source['order_volume'] == '自动' else 1)
        self.volume_input.setEnabled(self.volume_mode.currentIndex() == 1)
        self.scroll.setWidget(body); layout.addWidget(self.scroll, 1)
        self.error_label = QLabel(); self.error_label.setTextFormat(Qt.PlainText); self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet('color: #b42318;'); self.error_label.hide(); layout.addWidget(self.error_label)
        footer = QWidget(); footer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        actions = QHBoxLayout(footer); actions.setContentsMargins(0, 5, 0, 0)
        note = QLabel('确认后更新列表；取消不保留修改。'); note.setWordWrap(True)
        actions.addWidget(note, 1)
        self.cancel_button = QPushButton('取消'); self.cancel_button.setAutoDefault(False)
        self.confirm_button = QPushButton('确认添加' if self.adding else '保存修改'); self.confirm_button.setDefault(True)
        self.confirm_button.setStyleSheet('background: #245bcb; color: white;')
        actions.addWidget(self.cancel_button); actions.addWidget(self.confirm_button); layout.addWidget(footer)
        self.cancel_button.clicked.connect(self.reject); self.confirm_button.clicked.connect(self.validate_symbol)
        self._auto_name = ''
        self._catalog_model = QStringListModel(self)
        self._catalog_completer = QCompleter(self._catalog_model, self)
        self._catalog_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._catalog_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._catalog_completer.setMaxVisibleItems(12)
        self.fields['code'].setCompleter(self._catalog_completer)
        self.fields['code'].textChanged.connect(self._sync_contract)
        self.fields['code'].textEdited.connect(self._refresh_contract_suggestions)
        self.fields['code'].editingFinished.connect(self._normalize_contract)
        self._sync_contract()
        fit_dialog(self, 620, 610)
        self.fields['code'].setFocus()

    def _show_more(self, checked):
        self.more_body.setVisible(checked)
        self.more_button.setText('更多设置 ▾' if checked else '更多设置 ▸')

    def start_catalog_load(self):
        if not self.adding or self._contract_catalog or self._catalog_loader is None:
            return
        self._catalog_loading = True
        self.contract_hint.setText('正在加载可选期货合约；也可以直接输入完整合约代码。')
        try:
            started = self._catalog_loader()
        except Exception:
            started = False
        if started is False and self._catalog_loading:
            self.set_contract_catalog_error('E_CONTRACT_CATALOG')

    def set_contract_catalog(self, contracts):
        self._contract_catalog = tuple(dict.fromkeys(code for code in (contracts or ()) if isinstance(code, str)))
        self._catalog_loading = False
        self._refresh_contract_suggestions(self.fields['code'].text())

    def set_contract_catalog_error(self, code):
        self._catalog_loading = False
        text = self.fields['code'].text().strip()
        try:
            resolve_contract(text)
        except TraderError:
            self.contract_hint.setText(f'合约目录加载失败（{code}），仍可手动输入完整合约代码。')

    def _refresh_contract_suggestions(self, text):
        matches = match_contract_catalog(text, self._contract_catalog, limit=50)
        self._catalog_model.setStringList(matches)
        if matches:
            self.contract_hint.setText(f'找到 {len(matches)} 个匹配合约，可用上下键选择并按 Enter 确认。')
            if self.fields['code'].hasFocus():
                self._catalog_completer.setCompletionPrefix('')
                self._catalog_completer.complete()
        elif text.strip() and self._contract_catalog:
            try:
                resolve_contract(text.strip())
            except TraderError:
                self.contract_hint.setText('当前目录没有匹配合约；可继续输入或手动填写完整代码。')
        elif text.strip() and self._catalog_loading:
            self.contract_hint.setText('正在加载可选期货合约；也可以直接输入完整合约代码。')

    def _sync_contract(self):
        try: code = resolve_contract(self.fields['code'].text().strip())
        except TraderError:
            if not self._catalog_loading and not self._contract_catalog:
                self.contract_hint.setText('支持品种简称或完整合约代码，必须填写月份。')
            return
        name = PRODUCT_NAMES.get(re.sub(r'\d+$', '', code), '')
        self.contract_hint.setText('已识别：' + code)
        current = self.fields['name'].text()
        if name and current in ('', self._auto_name): self.fields['name'].setText(name)
        self._auto_name = name

    def _normalize_contract(self):
        try: self.fields['code'].setText(resolve_contract(self.fields['code'].text().strip()))
        except TraderError: pass

    def _failure(self, key, message):
        self.error_label.setText(message); self.error_label.show()
        if key in {'half_close_ratio', 'hysteresis_points', 'code_new'}: self.more_button.setChecked(True)
        widget = self.fields.get(key, self.fields['code'])
        widget.setProperty('invalid', True); widget.style().unpolish(widget); widget.style().polish(widget)
        widget.setFocus(); widget.selectAll(); self.scroll.ensureWidgetVisible(widget)
        return False

    def validate_symbol(self):
        for widget in self.fields.values():
            widget.setProperty('invalid', False); widget.style().unpolish(widget); widget.style().polish(widget)
        self.error_label.hide()
        if self.adding and len(self.base_profile.to_settings()['symbols']) >= 64:
            return self._failure('code', '最多添加 64 个品种，请先移除不需要的品种。')
        self._sync_contract()
        row = {key: widget.text().strip() for key, widget in self.fields.items()}
        try: row['code'] = resolve_contract(row['code'])
        except TraderError: return self._failure('code', '请填写带月份的合约代码，例如 cu2701 或 SHFE.cu2701。')
        if not row['name']: return self._failure('name', '请填写品种名称。')
        existing = self.base_profile.to_settings()['symbols']
        if any(item['code'] == row['code'] for index, item in enumerate(existing) if index != self.symbol_row):
            return self._failure('code', '这个合约已在列表中，请选中原品种后编辑。')
        row['order_volume'] = '自动' if self.volume_mode.currentIndex() == 0 else row['order_volume']
        if self.volume_mode.currentIndex() == 1:
            try:
                if integer_text(row['order_volume'], 'order_volume') < 1: raise ValueError()
            except (TraderError, ValueError):
                return self._failure('order_volume', '固定手数请填写大于等于 1 的整数；自动下单请选“自动计算手数”。')
        try:
            percent = Decimal(number_text(row['half_close_ratio'], 'half_close_ratio'))
            if percent > 100: return self._failure('half_close_ratio', '半仓比例请填写 0～100 之间的百分数。')
            row['half_close_ratio'] = decimal_text(percent / 100)
        except TraderError: return self._failure('half_close_ratio', '半仓比例请填写 0～100 之间的百分数。')
        values = form_values(self.base_profile)
        index = len(existing) if self.adding else self.symbol_row
        if self.adding: values['symbols'].append(row)
        else: values['symbols'][index] = row
        try: candidate = profile_from_form(self.base_profile, values)
        except EditError as error:
            key = error.path.rsplit('.', 1)[-1]
            messages = {'open_offset': '开仓请填写大于等于 0 的数字。', 'stop_offset': '止损请填写大于等于 0 的数字。',
                        'profit_offset': '止盈请填写大于等于 0 的数字。', 'order_volume': '固定手数请填写正整数。',
                        'hysteresis_points': '滞回点请填写大于等于 0 的数字。', 'code_new': '候选换月合约需要填写具体月份，或留空。'}
            return self._failure(key, messages.get(key, '名称或参数格式不正确，请检查填写内容。'))
        self.result_profile = candidate; self.result_symbol = candidate.to_settings()['symbols'][index]
        self.accept(); return True
