"""Account editing UI. Saving/selecting never starts an execution connection."""
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QFormLayout,QTableWidget,QTableWidgetItem,QMessageBox,QComboBox)
from online_trader.errors import TraderError


class AccountDialog(QDialog):
    def __init__(self,store,parent=None,*,use_account=None):
        super().__init__(parent);self.store=store;self.state=None
        self._use_account=use_account
        self.setWindowTitle('OnlineTrader · 账户管理');self.resize(900,650)
        layout=QVBoxLayout(self)
        self.notice=QLabel('SimNow / 快期模拟账户 · 选择账户不登录。口令仅保存在当前 Windows 用户的凭据管理器。');self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['名称','登录身份','环境','公司 / 线路','账户标识'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows);self.table.setEditTriggers(QTableWidget.NoEditTriggers);layout.addWidget(self.table)
        form=QFormLayout();self.alias=QLineEdit();self.username=QLineEdit();self.username.setEchoMode(QLineEdit.Password)
        self.password=QLineEdit();self.password.setEchoMode(QLineEdit.Password)
        self.provider=QComboBox();self.provider.addItem('SimNow 模拟盘','TQ_SIMNOW');self.provider.addItem('快期模拟盘','TQKQ')
        self.broker=QLineEdit('SimNow');self.line=QLineEdit('TqAccount / SimNow')
        self.market_username=QLineEdit();self.market_username.setEchoMode(QLineEdit.Password)
        self.market_password=QLineEdit();self.market_password.setEchoMode(QLineEdit.Password)
        self.market_username_label=QLabel('快期行情账号（SimNow 行情）');self.market_password_label=QLabel('快期行情口令')
        for label,edit in [('账户类型',self.provider),('名称',self.alias),('登录账号（创建/保存凭据时输入）',self.username),('口令',self.password),
                (self.market_username_label,self.market_username),(self.market_password_label,self.market_password),
                ('公司（显示信息）',self.broker),('线路（显示信息）',self.line)]:form.addRow(label,edit)
        layout.addLayout(form);actions=QHBoxLayout()
        for text,callback in [('新增',self.add),('编辑所选',self.edit),('选择所选',self.select),('保存凭据',self.save),('忘记凭据',self.forget),('删除所选',self.delete),('刷新',self.reload)]:
            button=QPushButton(text);button.clicked.connect(callback);actions.addWidget(button)
        layout.addLayout(actions);self.table.itemSelectionChanged.connect(self.fill)
        self.table.cellDoubleClicked.connect(self._double_click)
        self.provider.currentIndexChanged.connect(self.provider_changed);self.provider_changed();self.reload()

    def provider_changed(self):
        simnow=self.provider.currentData()=='TQ_SIMNOW'
        for widget in (self.market_username_label,self.market_username,self.market_password_label,self.market_password):widget.setVisible(simnow)
        self.broker.setText('SimNow' if simnow else '快期模拟');self.line.setText('TqAccount / SimNow' if simnow else 'TqKq')
        self.clear_credentials()

    def clear_credentials(self):
        for widget in (self.username,self.password,self.market_username,self.market_password):widget.clear()

    def reload(self):
        self.state=self.store.read();self.table.setRowCount(len(self.state['accounts']))
        for i,row in enumerate(self.state['accounts']):
            values=[row['alias']+('（已选择）' if row['account_id']==self.state['selected_id'] else ''),row['masked_login'],
                    'SimNow 模拟盘' if row['provider']=='TQ_SIMNOW' else '快期模拟盘',row['broker']+' / '+row['line'],row['account_key']]
            for j,value in enumerate(values):self.table.setItem(i,j,QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        selected=next((i for i,row in enumerate(self.state['accounts']) if row['account_id']==self.state['selected_id']),None)
        if selected is not None:self.table.selectRow(selected)

    def selected(self):
        i=self.table.currentRow()
        if i<0:raise TraderError('E_ACCOUNT','请先选择账户')
        return self.state['accounts'][i]

    def fill(self):
        if self.table.currentRow()<0:return
        row=self.selected();self.provider.setCurrentIndex(self.provider.findData(row['provider']))
        self.alias.setText(row['alias']);self.broker.setText(row['broker']);self.line.setText(row['line'])
        self.clear_credentials()

    def _apply(self,callback):
        try:callback();self.reload();self.notice.setText('操作已保存。账户连接和新增交易权限没有改变。')
        except TraderError as error:
            if error.code=='E_REVISION_CONFLICT':
                self.reload()
            message = '正在刷新或启动账号，请稍后重试。' if error.code=='E_ACCOUNT_BUSY' else error.code
            self.notice.setText('账户操作未完成 · '+message)
        finally:self.clear_credentials()

    def add(self):self._apply(lambda:self.store.change('add',expected_revision=self.state['revision'],alias=self.alias.text(),username=self.username.text(),broker=self.broker.text(),line=self.line.text(),provider=self.provider.currentData()))
    def edit(self):
        def action():
            row=self.selected()
            if row['provider']!=self.provider.currentData():raise TraderError('E_ACCOUNT','账户类型不可修改，请新增独立账户')
            self.store.change('edit',expected_revision=self.state['revision'],account_id=row['account_id'],alias=self.alias.text(),broker=self.broker.text(),line=self.line.text())
        self._apply(action)
    def _double_click(self, row, _column):
        self.table.selectRow(row)
        self.select()

    def select(self):
        def action():
            account_id = self.selected()['account_id']
            if self._use_account is None:
                self.store.change('select', expected_revision=self.state['revision'], account_id=account_id)
            else:
                self._use_account(account_id, self.state['revision'])
        self._apply(action)
    def save(self):
        def action():
            row=self.selected()
            if row['provider']!=self.provider.currentData():raise TraderError('E_ACCOUNT','账户类型不可修改，请新增独立账户')
            fields={'market_username':self.market_username.text(),'market_password':self.market_password.text()} if row['provider']=='TQ_SIMNOW' else {}
            self.store.save_credentials(row['account_id'],self.username.text(),self.password.text(),**fields)
        self._apply(action)
    def forget(self):self._apply(lambda:self.store.forget_credentials(self.selected()['account_id']))
    def delete(self):
        if QMessageBox.question(self,'删除账户档案','删除所选档案及保存的凭据？原账本和历史证据保留。')==QMessageBox.Yes:
            self._apply(lambda:self.store.change('delete',expected_revision=self.state['revision'],account_id=self.selected()['account_id']))

    def done(self,result):
        self.clear_credentials();super().done(result)
