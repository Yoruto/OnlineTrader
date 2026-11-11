"""Explicit local history/calendar tools, independent of execution authority."""
from PySide6.QtWidgets import (QApplication,QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QFileDialog,QPlainTextEdit)
import json
from online_trader.errors import TraderError


class DataDialog(QDialog):
    def __init__(self,store,parent=None):
        super().__init__(parent);self.store=store;self.setWindowTitle('OnlineTrader · 历史数据与交易日历');self.resize(930,600)
        layout=QVBoxLayout(self);notice=QLabel('导入本地参考日线或有明确来源的日历包。数据不会注入实时行情，当前策略和旧保护的日历哈希不变。')
        notice.setWordWrap(True);layout.addWidget(notice);controls=QHBoxLayout()
        self.at=QLineEdit();self.at.setPlaceholderText('核对时间 YYYY-MM-DDTHH:MM:SSZ（明确输入）')
        self.daily=QPushButton('导入日线CSV');self.calendar=QPushButton('核对并保存日历包');self.export=QPushButton('导出缓存日线CSV')
        for widget in (self.at,self.daily,self.calendar,self.export):controls.addWidget(widget)
        layout.addLayout(controls);self.result=QPlainTextEdit();self.result.setReadOnly(True);layout.addWidget(self.result)
        self.digest=QLineEdit();self.digest.setPlaceholderText('缓存记录content_hash');layout.addWidget(self.digest)
        self.daily.clicked.connect(self.import_daily);self.calendar.clicked.connect(self.import_calendar);self.export.clicked.connect(self.export_daily)
    def _show(self,callback):
        try:self.result.setPlainText(json.dumps(callback(),ensure_ascii=False,indent=2))
        except (TraderError,OSError) as error:self.result.setPlainText('操作未完成：'+getattr(error,'code','E_REFERENCE_DATA'))
    def import_daily(self):
        path,_=QFileDialog.getOpenFileName(self,'选择参考日线CSV','','CSV (*.csv)')
        if path:self._show(lambda:self.store.import_daily(path))
    def import_calendar(self):
        path,_=QFileDialog.getOpenFileName(self,'选择显式交易日历来源包','','JSON (*.json)')
        if path:self._show(lambda:self.store.import_calendar(path,as_of_utc=self.at.text()))
    def export_daily(self):
        path,_=QFileDialog.getSaveFileName(self,'导出缓存日线为新文件','','CSV (*.csv)')
        if path:self._show(lambda:{'exported':str(self.store.daily_csv(self.digest.text(),path))})


def run(store):
    app=QApplication.instance() or QApplication([]);window=DataDialog(store);window.show();return app.exec()
