"""Local and JiHuLab Generic Package diagnostic backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from online_trader.contracts.codec import strict_loads


UPLOAD_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_UPLOAD/1"
UPLOAD_BACKEND = "JIHULAB_GENERIC_PACKAGE"
_CONFIG_KEYS = {
    "schema_version", "enabled", "backend", "host", "project_id", "project_path",
    "package_name", "private_token", "connect_timeout_seconds", "upload_timeout_seconds",
}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class UploadConfig:
    enabled: bool
    host: str
    project_id: int
    project_path: str
    package_name: str
    private_token: str
    connect_timeout_seconds: int
    upload_timeout_seconds: int


@dataclass(frozen=True)
class UploadResult:
    status: str
    backend: str
    host: str | None
    project_id: int | None
    report_id: str
    bundle_file: str
    bundle_bytes: int
    bundle_sha256: str
    http_status: int | None = None
    error_code: str | None = None
    error_type: str | None = None

    def to_record(self):
        return {"schema_version": "ONLINE_TRADER_DIAGNOSTIC_UPLOAD_RESULT/1", **asdict(self)}


def _token(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(not char.isascii() or char.isspace() or ord(char) < 33 for char in value)):
        raise ValueError("invalid diagnostic upload token")
    return value


def load_upload_config(runtime_root) -> UploadConfig | None:
    path = Path(runtime_root).resolve() / "private" / "diagnostics-upload.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("diagnostic upload configuration is too large")
    value = strict_loads(raw)
    if type(value) is not dict or set(value) != _CONFIG_KEYS:
        raise ValueError("invalid diagnostic upload configuration fields")
    if value["schema_version"] != UPLOAD_SCHEMA or type(value["enabled"]) is not bool:
        raise ValueError("invalid diagnostic upload configuration schema")
    if not value["enabled"]:
        return None
    if value["backend"] != UPLOAD_BACKEND:
        raise ValueError("unsupported diagnostic upload backend")
    if value["host"] != "jihulab.com" or value["project_id"] != 390001:
        raise ValueError("diagnostic upload target differs from the authorized project")
    if value["project_path"] != "Yoruto/online-trader-diagnostics":
        raise ValueError("diagnostic upload project path differs from the authorized project")
    package_name = value["package_name"]
    if not isinstance(package_name, str) or _SAFE_NAME.fullmatch(package_name) is None:
        raise ValueError("invalid diagnostic package name")
    connect_timeout = value["connect_timeout_seconds"]
    upload_timeout = value["upload_timeout_seconds"]
    if (type(connect_timeout) is not int or not 1 <= connect_timeout <= 30
            or type(upload_timeout) is not int or not 1 <= upload_timeout <= 30):
        raise ValueError("invalid diagnostic upload timeout")
    return UploadConfig(
        enabled=True,
        host=value["host"],
        project_id=value["project_id"],
        project_path=value["project_path"],
        package_name=package_name,
        private_token=_token(value["private_token"]),
        connect_timeout_seconds=connect_timeout,
        upload_timeout_seconds=upload_timeout,
    )


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "diagnostic upload redirect rejected", headers, fp)


class JiHuLabBackend:
    def __init__(self, config: UploadConfig, *, opener=None):
        self.config = config
        self._open = opener or build_opener(_RejectRedirects()).open

    def upload(self, bundle, *, report_id: str, bundle_sha256: str) -> UploadResult:
        path = Path(bundle).resolve()
        size = path.stat().st_size
        if size <= 0 or size > _MAX_BUNDLE_BYTES:
            raise ValueError("diagnostic bundle size is outside the upload bound")
        if _SAFE_NAME.fullmatch(report_id) is None or _SAFE_NAME.fullmatch(path.name) is None:
            raise ValueError("invalid diagnostic upload identity")
        url = (
            f"https://{self.config.host}/api/v4/projects/{self.config.project_id}/packages/generic/"
            f"{quote(self.config.package_name, safe='')}/{quote(report_id, safe='')}/{quote(path.name, safe='')}"
        )
        request = Request(
            url,
            data=path.read_bytes(),
            method="PUT",
            headers={
                "PRIVATE-TOKEN": self.config.private_token,
                "Content-Type": "application/zip",
                "User-Agent": "OnlineTrader-Diagnostics/1",
            },
        )
        with self._open(request, timeout=self.config.upload_timeout_seconds) as response:
            status = int(response.status)
            response.read(4097)
        if status != 201:
            raise RuntimeError(f"unexpected diagnostic upload status {status}")
        return UploadResult(
            status="UPLOADED",
            backend=UPLOAD_BACKEND,
            host=self.config.host,
            project_id=self.config.project_id,
            report_id=report_id,
            bundle_file=path.name,
            bundle_bytes=size,
            bundle_sha256=bundle_sha256,
            http_status=status,
        )


def failed_upload_result(*, bundle, report_id, bundle_sha256, error) -> UploadResult:
    path = Path(bundle)
    status = error.code if isinstance(error, HTTPError) else None
    if isinstance(error, HTTPError):
        code = "E_DIAGNOSTIC_UPLOAD_HTTP"
    elif isinstance(error, (URLError, TimeoutError, OSError)):
        code = "E_DIAGNOSTIC_UPLOAD_NETWORK"
    elif isinstance(error, ValueError):
        code = "E_DIAGNOSTIC_UPLOAD_CONFIG"
    else:
        code = "E_DIAGNOSTIC_UPLOAD"
    return UploadResult(
        status="FAILED",
        backend=UPLOAD_BACKEND,
        host="jihulab.com",
        project_id=390001,
        report_id=report_id,
        bundle_file=path.name,
        bundle_bytes=path.stat().st_size if path.is_file() else 0,
        bundle_sha256=bundle_sha256,
        http_status=status,
        error_code=code,
        error_type=type(error).__name__,
    )
