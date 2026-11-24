"""GitLab and JiHuLab release discovery and immutable Windows update staging."""

from dataclasses import asdict, dataclass
import hashlib
import json
import ntpath
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from online_trader import __version__


PACKAGE = 'online-trader-windows-x64'
MAX_BUNDLE = 512 * 1024 * 1024
VERSION = re.compile(r'(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})(?:\.(dev|rc)(0|[1-9]\d{0,8}))?', re.ASCII)
SHA256 = re.compile(r'[0-9a-f]{64}')
PAYLOAD_MARKER = 'update-payload.json'
PAYLOAD_SCHEMA = 'ONLINE_TRADER_UPDATE_PAYLOAD/1'
ALLOWED_HOSTS = frozenset({'gitlab.com', 'jihulab.com'})
ALLOWED_REDIRECT_HOSTS = {
    'gitlab.com': frozenset({'gitlab.com', 'storage.googleapis.com', 'gitlab-com-packages.storage.googleapis.com'}),
    'jihulab.com': frozenset({'jihulab.com'}),
}
_FORBIDDEN_PAYLOAD_MEMBERS = frozenset({
    'onlinetrader-defaults.json',
    'onlinetrader-parameters.json',
    'onlinetrader-overrides.json',
    'update-source.json',
    'active.json',
    'onlinetrader.ini',
    'runtime.lock',
    'dsv-baseline-20261109.json',
})
_FORBIDDEN_PAYLOAD_TOP_DIRS = frozenset({'runtime', 'updates', 'versions'})


class UpdateError(RuntimeError):
    pass


def normalize_host(value):
    if value is None:
        return 'gitlab.com'
    if not isinstance(value, str):
        raise UpdateError('更新托管域名无效')
    normalized = value.strip().lower()
    if normalized not in ALLOWED_HOSTS:
        raise UpdateError('更新托管域名不受支持')
    return normalized


def _validate_token(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(ch.isspace() or not ch.isascii() or ord(ch) < 32 for ch in value):
        raise UpdateError('更新只读令牌无效')
    return value


def load_update_source(root):
    try:
        with (Path(root) / 'update-source.json').open('rb') as stream:
            raw = stream.read(65_537)
    except OSError as error:
        raise UpdateError('无法读取更新来源配置') from error
    if len(raw) > 65_536:
        raise UpdateError('更新来源配置过大')
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as error:
        raise UpdateError('更新来源配置无效') from error
    if not isinstance(value, dict):
        raise UpdateError('更新来源配置无效')
    host = normalize_host(value.get('host'))
    project_id = value.get('project_id')
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise UpdateError('GitLab 项目 ID 无效')
    channel = value.get('channel')
    if channel not in {'preview', 'stable'}:
        raise UpdateError('更新渠道无效')
    read_token = _validate_token(value.get('read_token'))
    return {
        'host': host,
        'project_id': project_id,
        'channel': channel,
        'read_token': read_token,
        'portable_version': value.get('portable_version'),
    }


def version_key(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError('更新版本号无效')
    major, minor, patch, stage, number = match.groups()
    return int(major), int(minor), int(patch), {'dev': 0, 'rc': 1, None: 2}[stage], int(number or 0)


def archive_name(version):
    version_key(version)
    return f'OnlineTrader-{version}-windows-x64.zip'


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode='w', encoding='utf-8') as output:
        temporary = Path(output.name)
        json.dump(value, output)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Candidate:
    version: str
    size: int
    sha256: str


class GitLabRedirects(HTTPRedirectHandler):
    def __init__(self, *, api_host='gitlab.com', allowed_hosts=None):
        self.api_host = normalize_host(api_host)
        self.allowed_hosts = frozenset(allowed_hosts or ALLOWED_REDIRECT_HOSTS[self.api_host])

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        external = self.allowed_hosts - {self.api_host}
        if (target.scheme != 'https' or target.netloc not in self.allowed_hosts
                or ('/packages/generic/' not in req.full_url
                    and urlsplit(req.full_url).netloc not in external)):
            raise UpdateError('GitLab 更新下载跳转被拒绝')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and target.netloc != self.api_host:
            for key in list(redirected.headers):
                if key.lower() in {'private-token', 'authorization'}:
                    redirected.remove_header(key)
            for key in list(redirected.unredirected_hdrs):
                if key.lower() in {'private-token', 'authorization'}:
                    redirected.unredirected_hdrs.pop(key, None)
        return redirected


class GitLab:
    def __init__(self, project_id, *, host='gitlab.com', token=None, opener=None):
        if type(project_id) is not int or not 0 < project_id < 2**63:
            raise UpdateError('GitLab 项目 ID 无效')
        self.host = normalize_host(host)
        self._token = _validate_token(token)
        self.api = f'https://{self.host}/api/v4/projects/{project_id}'
        self.open = opener or build_opener(
            GitLabRedirects(api_host=self.host, allowed_hosts=ALLOWED_REDIRECT_HOSTS[self.host])
        ).open

    def _auth_headers(self, extra=None):
        headers = {} if extra is None else dict(extra)
        if self._token is not None:
            headers['PRIVATE-TOKEN'] = self._token
        return headers

    def pages(self, path, **filters):
        for page in range(1, 21):
            query = urlencode(dict(filters, page=page, per_page=50))
            request = Request(f'{self.api}/{path}?{query}', headers=self._auth_headers())
            with self.open(request, timeout=10) as response:
                data = response.read(2_000_001)
                next_page = response.headers.get('X-Next-Page')
            if len(data) > 2_000_000:
                raise UpdateError('GitLab 更新列表过大')
            rows = json.loads(data)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise UpdateError('GitLab 更新列表无效')
            yield from rows
            if next_page == '' or (next_page is None and len(rows) < 50):
                return
        raise UpdateError('GitLab 更新列表分页过多')

    def metadata(self, version):
        packages = [row for row in self.pages('packages', package_type='generic', package_name=PACKAGE)
                    if row.get('name') == PACKAGE and row.get('version') == version
                    and row.get('package_type') == 'generic']
        if len(packages) != 1 or type(packages[0].get('id')) is not int or packages[0]['id'] <= 0:
            raise UpdateError('GitLab 更新包缺失或重复')
        files = [row for row in self.pages(f"packages/{packages[0]['id']}/package_files")
                 if row.get('file_name') == archive_name(version)]
        if len(files) != 1:
            raise UpdateError('GitLab 更新文件缺失或重复')
        size, digest = files[0].get('size'), files[0].get('file_sha256')
        if (type(size) is not int or not 0 < size <= MAX_BUNDLE
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            raise UpdateError('GitLab 更新文件摘要或大小无效')
        return Candidate(version, size, digest)

    def latest(self, current, channel):
        if channel not in {'preview', 'stable'}:
            raise UpdateError('更新渠道无效')
        candidates = set()
        for row in self.pages('releases'):
            tag = row.get('tag_name')
            if not isinstance(tag, str) or not tag.startswith('onlinetrader-'):
                continue
            version = tag.removeprefix('onlinetrader-')
            if not VERSION.fullmatch(version) or (channel == 'stable' and version_key(version)[3] != 2):
                continue
            if version_key(version) > version_key(current):
                candidates.add(version)
        return self.metadata(max(candidates, key=version_key)) if candidates else None

    def download(self, candidate, path, *, on_progress=None):
        url = f'{self.api}/packages/generic/{PACKAGE}/{candidate.version}/{archive_name(candidate.version)}'
        digest = hashlib.sha256()
        count = path.stat().st_size if path.exists() else 0
        if count == candidate.size:
            with path.open('rb') as cached:
                if hashlib.file_digest(cached, 'sha256').hexdigest() == candidate.sha256:
                    if on_progress is not None:
                        on_progress('DOWNLOADING', candidate.version, candidate.size, candidate.size)
                    return
        if count >= candidate.size:
            count = 0
        if on_progress is not None:
            on_progress('DOWNLOADING', candidate.version, count, candidate.size)
        extra = {'Range': f'bytes={count}-'} if count else None
        try:
            response = self.open(Request(url, headers=self._auth_headers(extra)), timeout=20)
        except HTTPError as error:
            if error.code != 416 or not count:
                raise
            error.close()
            count = 0
            response = self.open(Request(url, headers=self._auth_headers()), timeout=20)
        with response:
            if response.status == 206:
                if not count or response.headers.get('Content-Range') != (
                        f'bytes {count}-{candidate.size - 1}/{candidate.size}'):
                    path.unlink(missing_ok=True)
                    raise UpdateError('GitLab 更新续传范围无效')
                with path.open('rb') as previous:
                    for block in iter(lambda: previous.read(1024 * 1024), b''):
                        digest.update(block)
            elif response.status == 200:
                count = 0
            else:
                raise UpdateError('GitLab 更新下载响应无效')
            with path.open('ab' if count else 'wb') as target:
                while block := response.read(1024 * 1024):
                    count += len(block)
                    if count > candidate.size:
                        break
                    target.write(block)
                    digest.update(block)
                    if on_progress is not None:
                        on_progress('DOWNLOADING', candidate.version, min(count, candidate.size), candidate.size)
        if count < candidate.size:
            raise UpdateError('GitLab 更新下载中断，可重试')
        if count != candidate.size or digest.hexdigest() != candidate.sha256:
            path.unlink(missing_ok=True)
            raise UpdateError('GitLab 更新文件大小或 SHA256 不符')


def verify_payload_marker(root, *, expected_version=None):
    marker = Path(root) / PAYLOAD_MARKER
    if not marker.is_file():
        raise UpdateError('更新载荷缺少非独立启动标记')
    try:
        with marker.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('marker too large')
        document = json.loads(raw.decode('utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as error:
        raise UpdateError('更新载荷标记无效') from error
    if not isinstance(document, dict):
        raise UpdateError('更新载荷标记无效')
    version = document.get('version')
    try:
        version_key(version)
    except UpdateError as error:
        raise UpdateError('更新载荷版本标记无效') from error
    if (document.get('schema_version') != PAYLOAD_SCHEMA
            or document.get('standalone') is not False
            or document.get('requires_managed_root') is not True):
        raise UpdateError('更新载荷未声明为不可独立启动载荷')
    if expected_version is not None and version != expected_version:
        raise UpdateError('更新载荷版本与候选版本不符')
    return document


def extract_bundle(archive, destination, *, expected_version=None):
    paths = set()
    total = 0
    forbidden = '<>:"\\|?*'
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        for entry in entries:
            parts = entry.filename.removesuffix('/').split('/')
            if (any(not part or part in {'.', '..'} or part.endswith(('.', ' '))
                    or any(char in forbidden or ord(char) < 32 for char in part)
                    or ntpath.isreserved(part) for part in parts)
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFDIR, stat.S_IFREG}
                    or entry.orig_filename != entry.filename):
                raise UpdateError('更新 ZIP 包含不安全路径')
            key = entry.filename.rstrip('/').casefold()
            segments = key.split('/')
            if 'onlinetrader-defaults.json' in segments or any(part in _FORBIDDEN_PAYLOAD_MEMBERS for part in segments):
                raise UpdateError('更新载荷不能包含账号默认配置、业务参数或根目录状态文件')
            if segments[0] in _FORBIDDEN_PAYLOAD_TOP_DIRS:
                raise UpdateError('更新载荷不能包含运行配置、缓存或版本目录')
            total += entry.file_size
            if key in paths or len(paths) >= 12000 or total > 2 * MAX_BUNDLE:
                raise UpdateError('更新 ZIP 文件重复或超出限制')
            paths.add(key)
        for entry in entries:
            parts = entry.filename.removesuffix('/').split('/')
            target = destination.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
    if not all((destination / name).is_file() for name in ('OnlineTrader.exe', 'OnlineTraderCLI.exe')):
        raise UpdateError('更新 ZIP 缺少工作台或命令行程序')
    if not (destination / '_internal').is_dir():
        raise UpdateError('更新 ZIP 缺少运行环境')
    verify_payload_marker(destination, expected_version=expected_version)


def check_and_download(root, current=__version__, *, on_progress=None):
    from online_trader.update_install import active_version

    if active_version(root) != current:
        raise UpdateError('此工作台不是当前安装版本')
    source = load_update_source(root)
    client = GitLab(source['project_id'], host=source['host'], token=source['read_token'])
    if on_progress is not None:
        on_progress('CHECKING', None, 0, 0)
    candidate = client.latest(current, source['channel'])
    if candidate is None:
        return None
    if (root / 'updates' / f'{candidate.version}.failed').exists():
        raise UpdateError('此版本上次安装失败，等待下一版本')
    cache = root / 'updates'
    cache.mkdir(exist_ok=True)
    path = cache / f'{candidate.version}-{candidate.sha256}.zip'
    if on_progress is not None:
        client.download(candidate, path, on_progress=on_progress)
    else:
        client.download(candidate, path)
    if on_progress is not None:
        on_progress('VERIFYING', candidate.version, candidate.size, candidate.size)
    try:
        with tempfile.TemporaryDirectory(dir=cache, prefix='.verify-') as directory:
            extract_bundle(path, Path(directory), expected_version=candidate.version)
    except UpdateError:
        raise
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise UpdateError('下载文件不是有效的更新 ZIP') from error
    atomic_json(cache / 'pending.json', asdict(candidate))
    return candidate.version
