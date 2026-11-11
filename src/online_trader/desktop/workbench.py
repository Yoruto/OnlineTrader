"""Trading workbench over the original owner IPC. Closing is local only."""
from pathlib import Path
from PySide6.QtCore import QThread,QTimer,Signal
from PySide6.QtWidgets import (QApplication,QHBoxLayout,QLabel,QLineEdit,QPushButton,QTabWidget,QTableWidget,
    QTableWidgetItem,QInputDialog,QFileDialog,QMessageBox)

from online_trader.contracts.codec import strict_loads
from online_trader.desktop.app import StatusWindow
from online_trader.errors import TraderError
from online_trader.transport.status_client import StatusClient


class _WorkbenchRequest(QThread):
    result=Signal(object);failed=Signal(object)
    def __init__(self,pipe,factory,action,args,account_key,boot):
        super().__init__();self.pipe=pipe;self.factory=factory;self.action=action;self.args=args
        self.account_key=account_key;self.boot=boot
    def run(self):
        client=None
        try:
            client=self.factory(self.pipe,expected_account_key=self.account_key,expected_kernel_boot_id=self.boot)
            if self.action=='refresh':
                base=client.snapshot();metadata=client.last_metadata
                client.expected_kernel_boot_id=metadata['kernel_boot_id'];view=client.workbench()
                value={'base':base,'metadata':metadata,'view':view}
            elif self.action=='status':value=client.command_status(self.args)
            else:value=client.command(*self.args)
            if not self.isInterruptionRequested():self.result.emit({'action':self.action,'value':value})
        except Exception as error:
            if not self.isInterruptionRequested():self.failed.emit({'code':getattr(error,'code','E_INTERNAL'),
                'identity':None if client is None else client.last_command})


TABLES={
 'markets':(['合约','名称','允许该品种新增','最新','买一','卖一','日高','日低','交易日','来源时间','新鲜','路径完整','阻挡原因'],
            ['contract_id','name','enabled','last','bid1','ask1','day_high','day_low','trading_day','source_time_utc','fresh','path_complete','entry_reasons']),
 'orders':(['合约','子单','开平','手数','已成','在途占量','价格','原生开平','发送','柜台','撤单'],
            ['contract_id','child_order_id','effect','original_qty','filled_qty','potential_remaining','limit_price','native_offset','send_state','broker_state','cancel_status']),
 'trades':(['合约','成交身份','方向','开平','手数','价格','交易日'],['contract_id','trade_key','direction','effect','quantity','price','trading_day']),
 'protection':(['合约','周期','模式','质量','第一档','第二档','保本','最高有利变动','剩量','目标','管理权','今昨仓路由核对'],
               ['contract_id','position_cycle_id','mode','quality','phase1_status','phase2_status','break_even_status','max_favorable_move','remaining_quantity','target_remaining_quantity','management_state','close_route_review']),
 'blocks':(['原因','生效'],['reason_code','active']),
 'commands':(['命令','状态','错误','说明','回执版本'],['command_id','status','error_code','reason','receipt_revision'])}

DISPLAY={'LONG':'多','SHORT':'空','OPEN':'开仓','REDUCE':'减仓','NORMAL':'普通','PROTECTED':'利润保护',
    'CONTIGUOUS':'连续','DATA_GAP':'断流','UNKNOWN':'未知','NOT_TRIGGERED':'未触发','DONE':'完成','ACTIVE':'生效',
    'OFF':'关闭','WAITING_FILL':'等待成交','WAITING_ENTRY_TERMINAL':'等待开仓终态','SUPPRESSED_PROTECTED':'利润保护优先',
    'MANAGED':'原内核管理','TAKEN_OVER':'已交接','TAKEOVER_PENDING':'交接待确认','NONE':'无'}


def display_cell(key,value):
    if key=='close_route_review' and isinstance(value,dict):
        if value.get('status')=='BLOCKED':return '待核对 · '+value['error_code']
        if value.get('status')=='NO_OWNED_QUANTITY':return '无自有持仓'
        routes=' / '.join(('平今' if r['native_offset']=='CLOSETODAY' else '平仓')+' '+str(r['quantity'])+'手' for r in value.get('routes',[]))
        return f"今{value['owned_today']} / 昨{value['owned_historical']} · {routes or '已预占'}（核对预览）"
    if key=='entry_reasons' and isinstance(value,list):return ' / '.join(value) if value else '无新增原因'
    if value is None:return '原周期' if key=='management_state' else '未知'
    if type(value) is bool:return '是' if value else '否'
    return DISPLAY.get(str(value),str(value))


class WorkbenchWindow(StatusWindow):
    def __init__(self,pipe_name,*,store=None,account_id=None,client_factory=StatusClient):
        super().__init__(pipe_name,client_factory=client_factory)
        self.store=store;self.selected_account=store.get(account_id) if store is not None and account_id is not None else None
        self.view=None;self.receipt_identity=None;self._boot=None;self._account_dialog=None
        self.setWindowTitle('OnlineTrader · 交易工作台');self.resize(1360,820)
        layout=self.centralWidget().layout();layout.removeWidget(self.cycles)
        self.tabs=QTabWidget();self.tabs.addTab(self.cycles,'自有持仓');self.tables={}
        for name,title in [('markets','行情'),('orders','委托'),('trades','成交'),('protection','保护明细'),('blocks','阻挡'),('commands','命令回执')]:
            table=QTableWidget(0,len(TABLES[name][0]));table.setHorizontalHeaderLabels(TABLES[name][0])
            table.setEditTriggers(QTableWidget.NoEditTriggers);table.setSelectionBehavior(QTableWidget.SelectRows)
            self.tables[name]=table;self.tabs.addTab(table,title)
        layout.insertWidget(5,self.tabs)
        self.connection_detail=QLabel('登录/交易同步/行情/时钟/自动权限尚未核对');layout.insertWidget(3,self.connection_detail)
        tools=QHBoxLayout();self.filter_input=QLineEdit();self.filter_input.setPlaceholderText('搜索合约、名称或月份')
        self.accounts_button=QPushButton('账户管理');self.attach_button=QPushButton('查看所选账户')
        self.accounts_button.clicked.connect(self.open_accounts);self.attach_button.clicked.connect(self.attach_selected)
        self.data_button=QPushButton('历史与交易日历');self.data_button.clicked.connect(self.open_data);self._data_dialog=None
        self.session_button=QPushButton('模拟连接设置');self.session_button.clicked.connect(self.open_session);self._session_dialog=None
        for widget in (self.accounts_button,self.attach_button,self.session_button,self.data_button,self.filter_input):tools.addWidget(widget)
        layout.insertLayout(5,tools);self.filter_input.textChanged.connect(self.filter_markets)
        commands=QHBoxLayout();self.command_buttons={}
        for name,text,callback in [('arm','启动策略',self.arm),('close','平选定自有仓',self.close_selected),('flatten','平仓后停开',self.flatten),
                                  ('cancel','撤所选自有单',self.cancel_selected),('cancel_entries','撤自有开仓单',self.cancel_entries),
                                  ('buy','手动开多',lambda:self.manual_open('LONG')),('sell','手动开空',lambda:self.manual_open('SHORT')),
                                  ('reconcile','请求完整核对',self.reconcile),('receipt','查询上次回执',self.receipt)]:
            button=QPushButton(text);button.clicked.connect(callback);commands.addWidget(button);self.command_buttons[name]=button
        layout.addLayout(commands);self.notice_label.setText('选择账户、保存参数或关闭窗口不授予交易权；控制请求按原内核回执显示。')
        symbols=QHBoxLayout();symbols.addWidget(QLabel('行情页所选品种（保留已有保护）：'))
        for name,text,enabled in [('symbol_enable','启用品种新增',True),('symbol_pause','停用品种新增',False)]:
            button=QPushButton(text);button.clicked.connect(lambda checked=False,value=enabled:self.set_symbol_entries(value))
            symbols.addWidget(button);self.command_buttons[name]=button
        symbols.addStretch();layout.addLayout(symbols)
        self.timer=QTimer(self);self.timer.setInterval(2500);self.timer.timeout.connect(self.refresh);self.timer.start()
        self._enable_commands(False)

    def _enable_commands(self,enabled):
        for button in self.command_buttons.values():button.setEnabled(enabled)
        self.command_buttons['receipt'].setEnabled(enabled and self.receipt_identity is not None)

    def _start_job(self,action,args=None):
        if self._worker is not None or self._closing:return
        if self.pipe_name is None:
            self.notice_label.setText('请在账户管理中新增并选择账户，再查看所选账户；账户选择不启动连接。')
            return
        self.refresh_button.setEnabled(False);self.pause_button.setEnabled(False);self._enable_commands(False)
        account=self.selected_account['account_key'] if self.selected_account is not None else self.view['account_key'] if self.view is not None else None
        worker=_WorkbenchRequest(self.pipe_name,self._factory,action,args,account,None if action=='refresh' else self._boot)
        self._worker=worker;worker.result.connect(self._job_result);worker.failed.connect(self._job_failed);worker.finished.connect(self._finished);worker.start()

    def refresh(self):self._start_job('refresh')
    def pause_entries(self):self.send('PAUSE_NEW_ENTRIES',{'scope_type':'ACCOUNT','scope_ids':[]},{'cancel_owned_entry_remainders':False})

    def _job_result(self,result):
        if self._closing:return
        if result['action']=='refresh':
            value=result['value'];super()._result({'action':'snapshot','value':value['base'],'metadata':value['metadata']})
            self._base_cycle_ids=[r['record_id'] for r in value['base']['records'] if r['record_type']=='CYCLE']
            self.view=value['view'];self._boot=self.view['kernel_boot_id'];self.render_view()
        else:
            receipt=result['value'];self.receipt_identity={k:receipt[k] for k in ('command_id','payload_hash')}
            self._stale=True;self.notice_label.setText(f"命令回执：{receipt['status']} · {receipt['error_code']} · {receipt['reason']}。持久回执不冒充成交。")

    def _job_failed(self,error):
        if error['identity'] is not None:self.receipt_identity=error['identity']
        self._failed(error['code'])
        if error['identity'] is not None:self.notice_label.setText('请求结果未知，保留原命令身份。请查询上次回执；不会自动重新发送。')

    def _finished(self):
        super()._finished()
        self._enable_commands(not self._closing and not self._stale and self.view is not None and not self.view['truncated'])
        self.command_buttons['receipt'].setEnabled(not self._closing and self._worker is None and self.receipt_identity is not None)

    def render_view(self):
        v=self.view;c=v['connection']
        self.connection_detail.setText(f"账户连接：{'已连接' if c['broker_connected'] else '未连接'} · 事实同步：{'完整' if c['synchronized'] else '不完整'} · 通道：{'故障' if c['fault'] else '无已知故障'} · 持仓核对：{'待处理' if c['recovery_required'] else '无未决屏障'} · 时钟：{'故障' if c['clock_fault'] else '无已知故障'} · 自动权限：{v['entry_authority']}")
        for name,table in self.tables.items():
            rows=v['cycles'] if name=='protection' else v[name];table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                for j,key in enumerate(TABLES[name][1]):
                    value=row.get(key);item=QTableWidgetItem(display_cell(key,value));item.setToolTip(str(value));table.setItem(i,j,item)
            table.resizeColumnsToContents()
            for column in range(table.columnCount()):table.setColumnWidth(column,min(260,table.columnWidth(column)))
        self.filter_markets()
        self.notice_label.setText('视图超过容量，已截断；批量操作关闭。' if v['truncated'] else '状态来自原内核；行情来源时间与新鲜度单独显示。关闭工作台保留原持仓保护。')

    def filter_markets(self):
        table=self.tables['markets'];term=self.filter_input.text().casefold()
        for row in range(table.rowCount()):table.setRowHidden(row,not any(term in table.item(row,col).text().casefold() for col in (0,1)))

    def policy(self,kind):
        if self.view is None:raise TraderError('E_NOT_READY','请先刷新状态')
        items=[row['policy_id'] for row in self.view['policies'] if row['policy_kind']==kind]
        if len(items)!=1:raise TraderError('E_RULE_UNAPPROVED','缺少唯一适用的执行政策')
        return items[0]

    def send(self,operation,scope,parameters):
        if self._stale or self.view is None:
            self.notice_label.setText('状态未知或已过期，请先刷新。');return
        self._start_job('command',(operation,scope,parameters))

    def _action(self,callback):
        try:callback()
        except TraderError as error:self.notice_label.setText(str(error)+' · '+error.code)
        except (OSError,KeyError,ValueError,TypeError):self.notice_label.setText('输入文件或当前选中记录不完整，请重新核对。')

    def arm(self):
        def action():
            config=self.view['configuration'];policy=self.policy('AUTHORITY')
            if QMessageBox.question(self,'启动策略','向当前已显示账户请求启动策略？原范围、时窗和核对门禁仍由内核复核。')==QMessageBox.Yes:
                self.send('ARM_ENTRIES',{'scope_type':'ACCOUNT','scope_ids':[]},{'authority_policy_id':policy,'rule_set_id':config['rule_set_id'],'config_revision':config['config_revision']})
        self._action(action)

    def close_selected(self):
        def action():
            i=self.cycles.currentRow()
            if i<0:raise TraderError('E_SCOPE','请选择一个自有持仓周期')
            # Base STATUS row identities, not a potentially different detail ordering.
            cid=self._base_cycle_ids[i]
            row=next(r for r in self.view['cycles'] if r['position_cycle_id']==cid)
            if row['remaining_quantity']<=0:raise TraderError('E_SCOPE','所选周期没有可减持仓')
            qty,ok=QInputDialog.getInt(self,'平选定自有仓',row['contract_id']+' · 平仓手数',1,1,row['remaining_quantity'])
            if ok:self.send('MANUAL_CLOSE',{'scope_type':'CYCLE','scope_ids':[cid]},{'position_cycle_id':cid,'quantity':qty,'execution_policy_id':self.policy('EXECUTION'),'external_takeover_id':None})
        self._action(action)

    def set_symbol_entries(self,enabled):
        def action():
            selected=sorted({index.row() for index in self.tables['markets'].selectedIndexes()})
            if not selected:raise TraderError('E_SCOPE','请在行情页选择要启停的品种')
            codes=sorted({self.view['markets'][index]['contract_id'] for index in selected})
            scope={'scope_type':'CONTRACT','scope_ids':codes}
            if enabled:
                config=self.view['configuration']
                self.send('ARM_ENTRIES',scope,{'authority_policy_id':self.policy('AUTHORITY'),
                    'rule_set_id':config['rule_set_id'],'config_revision':config['config_revision']})
            else:self.send('PAUSE_NEW_ENTRIES',scope,{'cancel_owned_entry_remainders':True})
        self._action(action)

    def flatten(self):
        def action():
            policy=self.policy('EXECUTION')
            if QMessageBox.question(self,'平仓后停开','将当前账户所有自有周期的统一剩余目标设为0，并停止新增？外部持仓不会认领。')==QMessageBox.Yes:
                self.send('FLATTEN_THEN_STOP',{'scope_type':'ACCOUNT','scope_ids':[]},{'execution_policy_id':policy})
        self._action(action)

    def cancel_selected(self):
        rows=sorted({index.row() for index in self.tables['orders'].selectedIndexes()})
        if not rows:self.notice_label.setText('请选择自有委托。');return
        ids=[self.view['orders'][i]['child_order_id'] for i in rows]
        self.send('CANCEL_SELECTED',{'scope_type':'SELECTED_ORDERS','scope_ids':ids},{'child_order_ids':ids})

    def cancel_entries(self):self.send('CANCEL_OWNED',{'scope_type':'OWNED_ORDERS','scope_ids':[]},{'include_exits':False})
    def reconcile(self):self.send('REQUEST_RECONCILIATION',{'scope_type':'ACCOUNT','scope_ids':[]},{'requested_scope':'FULL_ACCOUNT','position_cycle_id':None})
    def receipt(self):
        if self.receipt_identity is not None:self._start_job('status',self.receipt_identity)

    def manual_open(self,direction):
        def action():
            file,_=QFileDialog.getOpenFileName(self,'选择已审批的人工计划（不从预览参数生成权限）','','JSON (*.json)')
            if not file:return
            path=Path(file)
            if path.stat().st_size>65536:raise TraderError('E_SCHEMA','计划文件过大')
            plan=strict_loads(path.read_bytes())
            if plan.get('direction')!=direction or plan.get('account_key')!=self.view['account_key']:raise TraderError('E_SCOPE','计划账户或方向不符')
            price,ok=QInputDialog.getText(self,'人工限价',f"{plan['contract_id']} · {plan['initial_open_quantity']}手 · 限价")
            if ok:self.send('MANUAL_OPEN',{'scope_type':'CONTRACT','scope_ids':[plan['contract_id']]},{'contract_id':plan['contract_id'],
                'direction':direction,'quantity':plan['initial_open_quantity'],'limit_price':price,'order_policy_id':self.policy('EXECUTION'),'protection_plan':plan})
        self._action(action)

    def open_accounts(self):
        if self.store is None:self.notice_label.setText('请通过 --account-store 指定本产品账户目录。');return
        from online_trader.desktop.accounts import AccountDialog
        self._account_dialog=AccountDialog(self.store,self);self._account_dialog.exec()

    def attach_selected(self):
        if self.store is None:return
        if self._worker is not None:return
        state=self.store.read()
        if state['selected_id'] is None:self.notice_label.setText('请先在账户管理中选择账户。');return
        self.selected_account=self.store.get(state['selected_id']);self.pipe_name=self.store.pipe_name(self.selected_account)
        self._boot=None;self.view=None;self.receipt_identity=None;self._stale=True;self._account_key=None
        # Child report windows are account-bound and must not silently follow.
        if self._reports_window is not None:self._reports_window.close();self._reports_window=None
        self.refresh()

    def open_data(self):
        if self.store is None:
            self.notice_label.setText('请通过 --account-store 指定本产品账户目录，或使用 data desktop。');return
        from online_trader.marketdata import ReferenceStore
        from online_trader.desktop.data import DataDialog
        self._data_dialog=DataDialog(ReferenceStore(self.store.directory/'reference'),self);self._data_dialog.show()

    def open_session(self):
        if self.store is None or self.selected_account is None:
            self.notice_label.setText('请先通过本产品账户管理选择模拟账户。');return
        from online_trader.desktop.session import SessionDialog
        self._session_dialog=SessionDialog(self.store,self.selected_account['account_id'],self);self._session_dialog.show()

    def _result(self,result):super()._result(result)

    def closeEvent(self,event):
        self.timer.stop();super().closeEvent(event)


def run(pipe_name,*,store=None,account_id=None):
    app=QApplication.instance() or QApplication([])
    window=WorkbenchWindow(pipe_name,store=store,account_id=account_id);window.show();QTimer.singleShot(0,window.refresh)
    return app.exec()
