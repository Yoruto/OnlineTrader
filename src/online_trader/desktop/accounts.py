"""Account editing UI. Saving/selecting never starts an execution connection."""
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QFormLayout,QTableWidget,QTableWidgetItem,QMessageBox)
from online_trader.errors import TraderError


class AccountDialog(QDialog):
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self.state=None
        self.setWindowTitle('OnlineTrader · 账户管理');self.resize(850,530)
        layout=QVBoxLayout(self)
        self.notice=QLabel('快期模拟账户 · 选择账户不登录。口令仅保存在当前Windows用户的凭据管理器。');self.notice.setWordWrap(True);layout.addWidget(self.notice)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['名称','登录身份','环境','公司 / 线路','账户标识'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows);self.table.setEditTriggers(QTableWidget.NoEditTriggers);layout.addWidget(self.table)
        form=QFormLayout();self.alias=QLineEdit();self.username=QLineEdit();self.username.setEchoMode(QLineEdit.Password)
        self.password=QLineEdit();self.password.setEchoMode(QLineEdit.Password)
        self.broker=QLineEdit('快期模拟');self.line=QLineEdit('TqKq')
        for label,edit in [('名称',self.alias),('登录账号（创建/保存凭据时输入）',self.username),('口令',self.password),('公司（显示信息）',self.broker),('线路（显示信息）',self.line)]:form.addRow(label,edit)
        layout.addLayout(form);actions=QHBoxLayout()
        for text,callback in [('新增',self.add),('编辑所选',self.edit),('选择所选',self.select),('保存凭据',self.save),('忘记凭据',self.forget),('删除所选',self.delete),('刷新',self.reload)]:
            button=QPushButton(text);button.clicked.connect(callback);actions.addWidget(button)
        layout.addLayout(actions);self.table.itemSelectionChanged.connect(self.fill);self.reload()

    def reload(self):
        self.state=self.store.read();self.table.setRowCount(len(self.state['accounts']))
        for i,row in enumerate(self.state['accounts']):
            values=[row['alias']+('（已选择）' if row['account_id']==self.state['selected_id'] else ''),row['masked_login'],row['environment'],row['broker']+' / '+row['line'],row['account_key']]
            for j,value in enumerate(values):self.table.setItem(i,j,QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def selected(self):
        i=self.table.currentRow()
        if i<0:raise TraderError('E_ACCOUNT','请先选择账户')
        return self.state['accounts'][i]

    def fill(self):
        if self.table.currentRow()<0:return
        row=self.selected();self.alias.setText(row['alias']);self.broker.setText(row['broker']);self.line.setText(row['line'])
        self.username.clear();self.password.clear()

    def _apply(self,callback):
        try:callback();self.reload();self.notice.setText('操作已保存。账户连接和新增交易权限没有改变。')
        except TraderError as error:self.notice.setText('账户操作未完成：'+error.code)
        finally:self.password.clear();self.username.clear()

    def add(self):self._apply(lambda:self.store.change('add',expected_revision=self.state['revision'],alias=self.alias.text(),username=self.username.text(),broker=self.broker.text(),line=self.line.text()))
    def edit(self):self._apply(lambda:self.store.change('edit',expected_revision=self.state['revision'],account_id=self.selected()['account_id'],alias=self.alias.text(),broker=self.broker.text(),line=self.line.text()))
    def select(self):self._apply(lambda:self.store.change('select',expected_revision=self.state['revision'],account_id=self.selected()['account_id']))
    def save(self):self._apply(lambda:self.store.save_credentials(self.selected()['account_id'],self.username.text(),self.password.text()))
    def forget(self):self._apply(lambda:self.store.forget_credentials(self.selected()['account_id']))
    def delete(self):
        if QMessageBox.question(self,'删除账户档案','删除所选档案及保存的凭据？原账本和历史证据保留。')==QMessageBox.Yes:
            self._apply(lambda:self.store.change('delete',expected_revision=self.state['revision'],account_id=self.selected()['account_id']))

    def done(self,result):
        self.password.clear();self.username.clear();super().done(result)
