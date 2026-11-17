"""GitLab release discovery and immutable Windows update staging."""

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
from urllib.request import HTTPRedirectHandler, build_opener
import zipfile

from online_trader import __version__


PACKAGE = 'online-trader-windows-x64'
MAX_BUNDLE = 512 * 1024 * 1024
VERSION = re.compile(r'(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})(?:\.(dev|rc)(0|[1-9]\d{0,8}))?', re.ASCII)
SHA256 = re.compile(r'[0-9a-f]{64}')


class UpdateError(RuntimeError):
    pass


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
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Public client requests carry no credentials. API metadata stays on GitLab.
        target = urlsplit(newurl)
        storage = {'storage.googleapis.com', 'gitlab-com-packages.storage.googleapis.com'}
        if (target.scheme != 'https' or target.netloc not in storage | {'gitlab.com'}
                or ('/packages/generic/' not in req.full_url
                    and urlsplit(req.full_url).netloc not in storage)):
            raise UpdateError('GitLab 更新下载跳转被拒绝')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitLab:
    def __init__(self, project_id, *, opener=None):
        if type(project_id) is not int or not 0 < project_id < 2**63:
            raise UpdateError('GitLab 项目 ID 无效')
        self.api = f'https://gitlab.com/api/v4/projects/{project_id}'
        self.open = opener or build_opener(GitLabRedirects()).open

    def pages(self, path, **filters):
        for page in range(1, 21):
            query = urlencode(dict(filters, page=page, per_page=50))
            with self.open(f'{self.api}/{path}?{query}', timeout=10) as response:
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

    def download(self, candidate, path):
        from urllib.request import Request

        url = f'{self.api}/packages/generic/{PACKAGE}/{candidate.version}/{archive_name(candidate.version)}'
        digest = hashlib.sha256()
        count = path.stat().st_size if path.exists() else 0
        if count == candidate.size:
            with path.open('rb') as cached:
                if hashlib.file_digest(cached, 'sha256').hexdigest() == candidate.sha256:
                    return
        if count >= candidate.size:
            count = 0
        request = Request(url, headers={'Range': f'bytes={count}-'} if count else {})
        try:
            response = self.open(request, timeout=20)
        except HTTPError as error:
            if error.code != 416 or not count:
                raise
            error.close()
            count = 0
            response = self.open(Request(url), timeout=20)
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
        if count < candidate.size:
            raise UpdateError('GitLab 更新下载中断，可重试')
        if count != candidate.size or digest.hexdigest() != candidate.sha256:
            path.unlink(missing_ok=True)
            raise UpdateError('GitLab 更新文件大小或 SHA256 不符')


def extract_bundle(archive, destination):
    paths = set()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            parts = entry.filename.removesuffix('/').split('/')
            forbidden = '<>:"\\|?*'
            if (any(not part or part in {'.', '..'} or part.endswith(('.', ' '))
                    or any(char in forbidden or ord(char) < 32 for char in part)
                    or ntpath.isreserved(part) for part in parts)
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFDIR, stat.S_IFREG}
                    or entry.orig_filename != entry.filename):
                raise UpdateError('更新 ZIP 包含不安全路径')
            key = entry.filename.rstrip('/').casefold()
            total += entry.file_size
            if key in paths or len(paths) >= 12000 or total > 2 * MAX_BUNDLE:
                raise UpdateError('更新 ZIP 文件重复或超出限制')
            paths.add(key)
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


def check_and_download(root, current=__version__):
    from online_trader.update_install import active_version

    if active_version(root) != current:
        raise UpdateError('此工作台不是当前安装版本')
    settings = json.loads((root / 'update-source.json').read_text(encoding='utf-8-sig'))
    client = GitLab(settings['project_id'])
    candidate = client.latest(current, settings['channel'])
    if candidate is None:
        return None
    if (root / 'updates' / f'{candidate.version}.failed').exists():
        raise UpdateError('此版本上次安装失败，等待下一版本')
    cache = root / 'updates'
    cache.mkdir(exist_ok=True)
    path = cache / f'{candidate.version}-{candidate.sha256}.zip'
    client.download(candidate, path)
    try:
        with tempfile.TemporaryDirectory(dir=cache, prefix='.verify-') as directory:
            extract_bundle(path, Path(directory))
    except UpdateError:
        raise
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise UpdateError('下载文件不是有效的更新 ZIP') from error
    atomic_json(cache / 'pending.json', asdict(candidate))
    return candidate.version
