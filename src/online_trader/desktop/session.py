"""Explicit saved-account scope review and launch; no launch in constructor."""
import json
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QFormLayout,QLineEdit,QPushButton,QPlainTextEdit,QCheckBox,QMessageBox)
from online_trader.errors import TraderError
from online_trader.runtime.launch import SessionLauncher
from online_trader.runtime.executable import bundled_cli


class SessionDialog(QDialog):
    def __init__(self,store,account_id,parent=None,*,paired_launcher=None):
        super().__init__(parent);self.store=store;self.account_id=account_id;self.review_result=None;self.launcher=None
        self.paired_launcher=paired_launcher
        packaged=bundled_cli()
        python=packaged or Path(sys.executable)
        if python.name.lower()=='pythonw.exe':python=python.with_name('python.exe')
        self.setWindowTitle('OnlineTrader · 模拟执行范围');self.resize(800,600);layout=QVBoxLayout(self);form=QFormLayout();self.fields={}
        for key,label in [('scope','新的有限scope文件'),('profile','对应profile文件'),('calendar','对应calendar文件'),
                          ('python','执行器程序' if packaged else '已安装当前版本及SDK的python.exe'),('output','本次独立的新输出目录')]:
            edit=QLineEdit(str(python) if key=='python' else '');self.fields[key]=edit;form.addRow(label,edit)
            edit.textChanged.connect(self.invalidate)
        layout.addLayout(form);self.arm=QCheckBox('显式请求首次策略入场权（重启仍只管理原退出）');layout.addWidget(self.arm)
        self.prepare_button=QPushButton('离线核对范围');self.start_button=QPushButton('启动该模拟执行器');self.start_button.setEnabled(False)
        self.use_schedule=QCheckBox('按已保存的 DSV 定时和软件启动设置启动')
        self.cancel_timer_button=QPushButton('取消定时启动');self.cancel_timer_button.setEnabled(False)
        if paired_launcher is not None:
            layout.addWidget(self.use_schedule)
            layout.addWidget(self.cancel_timer_button)
        else:
            self.use_schedule.hide()
            self.cancel_timer_button.hide()
        layout.addWidget(self.prepare_button);layout.addWidget(self.start_button);self.result=QPlainTextEdit();self.result.setReadOnly(True);layout.addWidget(self.result)
        self.prepare_button.clicked.connect(self.prepare);self.start_button.clicked.connect(self.start)
        self.run_directory=None
        self.status_timer=QTimer(self);self.status_timer.setInterval(1500)
        self.status_timer.timeout.connect(self.update_launch_status)
        self.scheduled_request=None
        self.schedule_timer=QTimer(self);self.schedule_timer.setInterval(1_000)
        self.schedule_timer.timeout.connect(self.check_scheduled_start)
        self.finished.connect(lambda _result:self.clear_scheduled_start())
        self.cancel_timer_button.clicked.connect(self.cancel_scheduled_start)
        self.use_schedule.toggled.connect(lambda checked:self.start_button.setText(
            '安排本次模拟启动' if checked else '启动该模拟执行器'))
        if paired_launcher is not None:
            self.setWindowTitle('OnlineTrader · SimNow 模拟盘')
            for key,path in zip(('scope','profile','calendar'),paired_launcher.paths):
                self.fields[key].setText(str(path));self.fields[key].setReadOnly(True)
            run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
            self.fields['output'].setText(str(paired_launcher.packet/'runs'/run_id))
            self.fields['python'].setReadOnly(True)
            self.prepare()
    def invalidate(self):
        self.review_result=None
        self.clear_scheduled_start()
        self.arm.setEnabled(True)
        if hasattr(self,'start_button'):self.start_button.setEnabled(False)
    def prepare(self):
        self.clear_scheduled_start()
        try:
            self.launcher=self.paired_launcher or SessionLauncher(self.store,self.account_id,*(self.fields[k].text() for k in ('scope','profile','calendar')))
            self.review_result=self.launcher.review();self.result.setPlainText(json.dumps(self.review_result,ensure_ascii=False,indent=2));self.start_button.setEnabled(True)
            if self.review_result['manual_workbench']:self.arm.setChecked(False)
            self.arm.setEnabled(not self.review_result['manual_workbench'])
            self.use_schedule.setEnabled(self._schedule_configured())
            if not self.use_schedule.isEnabled():self.use_schedule.setChecked(False)
        except (TraderError,OSError) as error:self.invalidate();self.result.setPlainText('未完成：'+getattr(error,'code','E_REFERENCE_DATA'))
    def start(self):
        if self.review_result is None:return
        review=self.review_result
        schedule=self.paired_launcher is not None and self.use_schedule.isChecked()
        armed=self.arm.isChecked()
        request=(self.launcher,self.fields['python'].text(),self.fields['output'].text(),review['review_hash'],armed)
        beijing=timezone(timedelta(hours=8))
        def local(key):return datetime.fromisoformat(review[key]).astimezone(beijing).strftime('%m-%d %H:%M')
        if 'daily_targets_per_contract' in review:
            quota = f"每品种每柜台日最多 {review['daily_targets_per_contract']} 个策略开仓目标"
        else:
            quota = f"最多 {review['opening_attempts']} 个开仓子单"
        text=f"账户 {review['account_key']}\n{'、'.join(review['contracts'])}\n北京时间 {local('login_from_utc')} 起可连接\n策略入场范围 {local('not_before_utc')} 至 {local('not_after_utc')}\n柜台交易日 {', '.join(review['trading_days'])}\n{quota}、每单最多 {review['max_order_quantity']} 手\n请求首次策略入场权：{'是' if armed else '否'}"
        if schedule:
            schedule_settings=review['schedule']
            if schedule_settings['enabled']:
                text+=f"\n北京时间日盘 {schedule_settings['day_start_time']}、夜盘 {schedule_settings['night_start_time']}，补启动 {schedule_settings['catch_up_minutes']} 分钟。"
            text+='\n启用本次定时启动。到时按已保存设置自动连接，关闭此窗口可取消等待。'
        if QMessageBox.question(self,'启动已审查的模拟范围',text)!=QMessageBox.Yes:return
        if self.review_result is not review:
            return
        if schedule:
            self.scheduled_request=request
            self.start_button.setEnabled(False)
            self.use_schedule.setEnabled(False)
            self.arm.setEnabled(False)
            self.cancel_timer_button.setEnabled(True)
            self.result.setPlainText('已登记本次定时启动，等待已保存的时点或开盘窗口。')
            from online_trader.scheduling.reference import CalibratedClock
            self._scheduled_clock = CalibratedClock(lazy=True)
            self.schedule_timer.start()
            self.check_scheduled_start()
        else:
            self._launch(request)

    def _schedule_configured(self):
        review=self.review_result or {}
        return self.paired_launcher is not None and bool(
            review.get('auto_start_on_launch') or review.get('schedule',{}).get('enabled'))

    def clear_scheduled_start(self):
        self.scheduled_request=None
        self.schedule_timer.stop()
        self.cancel_timer_button.setEnabled(False)
        self.use_schedule.setEnabled(self._schedule_configured())
        self.arm.setEnabled(not (self.review_result or {}).get('manual_workbench',False))

    def cancel_scheduled_start(self):
        self.clear_scheduled_start()
        self.start_button.setEnabled(self.review_result is not None)
        self.result.setPlainText('本次定时启动已取消。')

    def check_scheduled_start(self, now=None):
        if self.scheduled_request is None:
            return
        request=self.scheduled_request
        if now is None:
            self._scheduled_clock.poll()
            if not self._scheduled_clock.entry_ready():
                self.result.setPlainText('已登记定时启动，等待应用授时。')
                return
            now = self._scheduled_clock.now()
        instant = now
        launcher,_,_,review_hash,_=request
        try:
            trigger=launcher.scheduled_trigger(expected_review_hash=review_hash,now=instant)
        except (TraderError,OSError) as error:
            self.invalidate()
            self.result.setPlainText('定时启动已结束：'+getattr(error,'code','E_LAUNCH'))
            return
        if trigger is None:
            return
        self.clear_scheduled_start()
        parent=self.parent()
        view=getattr(parent,'view',None)
        if (view is not None and not getattr(parent,'_stale',True)
                and view.get('account_key')==(self.review_result or {}).get('account_key')):
            self.result.setPlainText('该账户的执行器已存在，定时启动结束。')
            return
        self._launch(request,now=instant)

    def _launch(self, request, *, now=None):
        launcher,python,output,review_hash,arm=request
        try:
            value=launcher.launch(python,output,expected_review_hash=review_hash,arm=arm,now=now)
            self.result.setPlainText(json.dumps(value,ensure_ascii=False,indent=2));self.invalidate()
            self.run_directory=Path(output);self.status_timer.start()
        except (TraderError,OSError) as error:
            self.start_button.setEnabled(self.review_result is not None)
            self.result.setPlainText('未完成：'+getattr(error,'code','E_LAUNCH'))

    def closeEvent(self,event):
        if self.scheduled_request is not None:
            self.cancel_scheduled_start()
        super().closeEvent(event)

    def update_launch_status(self):
        if self.run_directory is None:return
        failure=self.run_directory/'startup-failure.json'
        phases=sorted(self.run_directory.glob('phase-*.json'))
        path=failure if failure.is_file() else phases[-1] if phases else None
        if path is None:return
        try:record=json.loads(path.read_bytes())
        except (OSError,ValueError):return
        phase=record.get('phase') or record.get('status')
        code=record.get('error_code')
        status=phase if code is None else f'{phase} · {code}'
        self.result.setPlainText(f'执行器状态  {status}\n运行记录  {self.run_directory}')
        if failure.is_file() or (self.run_directory/'result.json').is_file():self.status_timer.stop()
