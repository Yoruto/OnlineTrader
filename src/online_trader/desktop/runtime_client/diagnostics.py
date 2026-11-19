"""Stable diagnostic catalog and Chinese presentation for the Runtime client."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PresentedDiagnostic:
    title: str
    explanation: str
    action: str


_MESSAGES = {
    'OT-CON-1001': PresentedDiagnostic('交易连接已中断', '当前不能确认新的交易请求。', '等待重连并重新同步。'),
    'OT-CON-1003': PresentedDiagnostic('账户正在同步', '资金、委托或持仓截面尚不完整。', '等待 Runtime 完成核对。'),
    'OT-TRD-2101': PresentedDiagnostic('订单结果待确认', 'Runtime 或柜台可能已经受理。', '查询原命令，勿重复提交。'),
    'OT-TRD-2104': PresentedDiagnostic('账户事实待核对', '新增交易暂时受限。', '完成账户核对后再操作。'),
    'OT-CFG-4002': PresentedDiagnostic('配置已变化', '草稿基于旧版本，未覆盖当前配置。', '重新核对后提交。'),
    'OT-DB-5001': PresentedDiagnostic('交易记录存储异常', '交易责任可能需要人工核对。', '保持停开并检查 Runtime 诊断。'),
    'OT-UI-7001': PresentedDiagnostic('状态更新不完整', '界面正在重新取得完整基线。', '等待同步完成。'),
}


def present(diagnostic):
    code = diagnostic.get('code', 'OT-SYS-9999')
    return _MESSAGES.get(code, PresentedDiagnostic(
        '运行状态需要核对', '界面收到未注册的诊断原因。', '查看诊断详情并按 Runtime 状态处理。'))


class NotificationCenter:
    def __init__(self):
        self._items = {}

    def update(self, diagnostics, *, account_key, boot_id):
        active = set()
        for row in diagnostics:
            key = (row.get('code'), account_key, row.get('impact_scope'), boot_id,
                   row.get('command_id'), row.get('trace_id'))
            active.add(key)
            prior = self._items.get(key)
            # A Runtime snapshot repeats active facts.  Refreshing the same
            # stable identity is not a new notification occurrence.
            self._items[key] = {'diagnostic': dict(row), 'count': 1 if prior is None else prior['count'],
                                'active': row.get('recovery_state') not in {'RECOVERED', 'RESOLVED'}}
        for key, value in self._items.items():
            # The publication is the complete active diagnostic set.  Items
            # absent from the next snapshot have recovered, regardless of
            # whether the original source was Runtime or this client.
            if key not in active:
                value['active'] = False
        return tuple(value for value in self._items.values() if value['active'])
