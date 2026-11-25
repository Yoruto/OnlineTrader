"""Shared diagnostic error catalog and presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re


_OT_CODE_RE = re.compile(r"OT-[A-Z]{2,8}-\d{1,6}\Z", re.ASCII)
_INTERNAL_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z", re.ASCII)


@dataclass(frozen=True)
class DiagnosticCatalogEntry:
    user_code: str
    title: str
    explanation: str
    action: str
    severity: str = "WARNING"
    internal_codes: tuple[str, ...] = ()


_CATALOG: tuple[DiagnosticCatalogEntry, ...] = (
    DiagnosticCatalogEntry(
        user_code="OT-CON-1001",
        title="交易连接已中断",
        explanation="当前不能确认新的交易请求。",
        action="等待重连并重新同步。",
        severity="WARNING",
        internal_codes=("E_GATEWAY_CONNECT", "E_GATEWAY_SYNC", "E_DSV_SDK_SYNC"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-CON-1003",
        title="账户正在同步",
        explanation="资金、委托或持仓截面尚不完整。",
        action="等待 Runtime 完成核对。",
        severity="INFO",
        internal_codes=("E_DSV_SYNC", "E_ACCOUNT_SYNC", "AWAITING_NATIVE_SYNC"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-TRD-2101",
        title="订单结果待确认",
        explanation="Runtime 或柜台可能已经受理。",
        action="查询原命令，勿重复提交。",
        severity="WARNING",
        internal_codes=("E_UNKNOWN_SEND", "INSERT_ORDER_EXCEPTION_UNKNOWN", "SEND_RESULT_UNKNOWN"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-TRD-2104",
        title="账户事实待核对",
        explanation="新增交易暂时受限。",
        action="完成账户核对后再操作。",
        severity="WARNING",
        internal_codes=(
            "E_DSV_NATIVE_FACT",
            "E_DSV_NATIVE_ID",
            "E_DSV_SDK_FACT",
            "E_DSV_OWNERSHIP",
            "E_DSV_SCOPE",
            "E_DSV_SDK_SCOPE",
        ),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-CFG-4002",
        title="配置已变化",
        explanation="草稿基于旧版本，未覆盖当前配置。",
        action="重新核对后提交。",
        severity="WARNING",
        internal_codes=("E_REVISION_CONFLICT", "E_CONFIG", "E_RUNTIME_CONFIG"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-DB-5001",
        title="交易记录存储异常",
        explanation="交易责任可能需要人工核对。",
        action="保持停开并检查 Runtime 诊断。",
        severity="ERROR",
        internal_codes=("E_STORAGE", "E_PENDING_STORAGE", "STORAGE_COMMIT_FAILED", "E_DSV_STORE"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-SYS-9001",
        title="运行状态文件写入受阻",
        explanation="辅助状态或诊断文件暂未能更新，不影响既有交易权威与风控。",
        action="检查目录占用或杀软拦截；核心交易状态以 Runtime 实时投影为准。",
        severity="WARNING",
        internal_codes=("E_RUNTIME_STATUS_WRITE", "E_RUNTIME_DIAGNOSTICS_WRITE"),
    ),
    DiagnosticCatalogEntry(
        user_code="OT-UI-7001",
        title="状态更新不完整",
        explanation="界面正在重新取得完整基线。",
        action="等待同步完成。",
        severity="WARNING",
        internal_codes=("E_RUNTIME_VIEW", "E_RUNTIME_CONNECTION", "SNAPSHOT_EXPIRED"),
    ),
)

_BY_USER_CODE = {entry.user_code: entry for entry in _CATALOG}
_BY_INTERNAL_CODE = {
    code: entry for entry in _CATALOG for code in entry.internal_codes
}
_DEFAULT_ENTRY = DiagnosticCatalogEntry(
    user_code="OT-SYS-9999",
    title="运行状态需要核对",
    explanation="界面收到未注册的诊断原因。",
    action="查看诊断详情并按 Runtime 状态处理。",
    severity="WARNING",
)


def is_valid_error_code(value) -> bool:
    return isinstance(value, str) and (
        _INTERNAL_CODE_RE.fullmatch(value) is not None
        or _OT_CODE_RE.fullmatch(value) is not None
    )


def lookup_diagnostic(code: str | None) -> DiagnosticCatalogEntry:
    if not isinstance(code, str) or not code:
        return _DEFAULT_ENTRY
    return _BY_USER_CODE.get(code) or _BY_INTERNAL_CODE.get(code) or _DEFAULT_ENTRY


def user_code_for_error(code: str | None) -> str | None:
    if not isinstance(code, str) or not code:
        return None
    if _OT_CODE_RE.fullmatch(code) is not None:
        return code
    entry = _BY_INTERNAL_CODE.get(code)
    return None if entry is None else entry.user_code
