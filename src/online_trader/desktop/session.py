"""Explicit saved-account scope review and launch; no launch in constructor."""
import json
import sys
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QLineEdit,QPushButton,QPlainTextEdit,QCheckBox,QMessageBox)
from online_trader.errors import TraderError
from online_trader.runtime.launch import SessionLauncher


class SessionDialog(QDialog):
    def __init__(self,store,account_id,parent=None):
        super().__init__(parent);self.store=store;self.account_id=account_id;self.review_result=None;self.launcher=None
        self.setWindowTitle('OnlineTrader · 模拟执行范围');self.resize(800,600);layout=QVBoxLayout(self);form=QFormLayout();self.fields={}
        for key,label in [('scope','新的有限scope文件'),('profile','对应profile文件'),('calendar','对应calendar文件'),
                          ('python','已安装当前版本及SDK的python.exe'),('output','本次独立的新输出目录')]:
            edit=QLineEdit(sys.executable if key=='python' else '');self.fields[key]=edit;form.addRow(label,edit)
            edit.textChanged.connect(self.invalidate)
        layout.addLayout(form);self.arm=QCheckBox('显式请求首次策略入场权（重启仍只管理原退出）');layout.addWidget(self.arm)
        self.prepare_button=QPushButton('离线核对范围');self.start_button=QPushButton('启动该模拟执行器');self.start_button.setEnabled(False)
        layout.addWidget(self.prepare_button);layout.addWidget(self.start_button);self.result=QPlainTextEdit();self.result.setReadOnly(True);layout.addWidget(self.result)
        self.prepare_button.clicked.connect(self.prepare);self.start_button.clicked.connect(self.start)
    def invalidate(self):
        self.review_result=None
        if hasattr(self,'start_button'):self.start_button.setEnabled(False)
    def prepare(self):
        try:
            self.launcher=SessionLauncher(self.store,self.account_id,*(self.fields[k].text() for k in ('scope','profile','calendar')))
            self.review_result=self.launcher.review();self.result.setPlainText(json.dumps(self.review_result,ensure_ascii=False,indent=2));self.start_button.setEnabled(True)
        except (TraderError,OSError) as error:self.invalidate();self.result.setPlainText('未完成：'+getattr(error,'code','E_REFERENCE_DATA'))
    def start(self):
        if self.review_result is None:return
        review=self.review_result
        text=f"账户 {review['account_key']}\n{review['contracts']}\n{review['not_before_utc']} 至 {review['not_after_utc']}\n柜台日：{review['trading_days']}\n完整日切核对后延续新增：{review['resume_entries_on_day_change']}\n最多 {review['opening_attempts']} 个开仓子单、每单最多 {review['max_order_quantity']} 手\n请求初始武装：{self.arm.isChecked()}\n启动将进行网络校时和模拟账户连接，必须具有本范围的明确授权。"
        if QMessageBox.question(self,'启动已审查的模拟范围',text)!=QMessageBox.Yes:return
        try:
            value=self.launcher.launch(self.fields['python'].text(),self.fields['output'].text(),expected_review_hash=review['review_hash'],arm=self.arm.isChecked())
            self.result.setPlainText(json.dumps(value,ensure_ascii=False,indent=2));self.invalidate()
        except (TraderError,OSError) as error:self.result.setPlainText('未完成：'+getattr(error,'code','E_LAUNCH'))
