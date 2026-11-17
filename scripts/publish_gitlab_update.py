"""Verify an OnlineTrader Windows ZIP, or explicitly publish it to GitLab."""

import argparse
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from online_trader.updates import (Candidate, GitLab, MAX_BUNDLE, PACKAGE, UpdateError,
                                   archive_name, extract_bundle)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish(client, archive, expected, ref, token):
    tag = f'onlinetrader-{expected.version}'
    url = f'{client.api}/packages/generic/{PACKAGE}/{expected.version}/{archive.name}'
    headers = {'PRIVATE-TOKEN': token}
    authenticated = build_opener(NoRedirects()).open
    releases = [row for row in client.pages('releases') if row.get('tag_name') == tag]
    packages = [row for row in client.pages('packages', package_type='generic', package_name=PACKAGE)
                if row.get('name') == PACKAGE and row.get('version') == expected.version]
    if not packages and not releases:
        with archive.open('rb') as stream:
            request = Request(url, data=stream, headers=headers | {
                'Content-Type': 'application/octet-stream', 'Content-Length': str(expected.size),
            }, method='PUT')
            with authenticated(request, timeout=120) as reply:
                if reply.status != 201:
                    raise UpdateError('GitLab 上传未确认')
    if client.metadata(expected.version) != expected:
        raise UpdateError('GitLab 同版本制品与本地文件不符')
    with tempfile.TemporaryDirectory() as directory:
        client.download(expected, Path(directory) / archive.name)
    if releases:
        if len(releases) != 1:
            raise UpdateError('GitLab 版本标签重复')
        return
    body = json.dumps({
        'tag_name': tag, 'ref': ref, 'name': f'OnlineTrader {expected.version}',
        'assets': {'links': [{'name': archive.name, 'url': url, 'link_type': 'package'}]},
    }).encode('utf-8')
    request = Request(f'{client.api}/releases', data=body,
                      headers=headers | {'Content-Type': 'application/json'}, method='POST')
    with authenticated(request, timeout=30) as reply:
        if reply.status != 201:
            raise UpdateError('GitLab Release 创建未确认')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--project-id', type=int)
    parser.add_argument('--ref', help='GitLab 分发项目中用于创建标签的引用')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args(argv)
    try:
        size = args.archive.stat().st_size
        if not 0 < size <= MAX_BUNDLE or args.archive.name != archive_name(args.version):
            raise UpdateError('本地制品名称或大小无效')
        with args.archive.open('rb') as stream:
            expected = Candidate(args.version, size, hashlib.file_digest(stream, 'sha256').hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            extract_bundle(args.archive, Path(directory))
        if args.publish:
            token = os.environ.get('GITLAB_TOKEN')
            if not all((args.project_id, args.ref, token)):
                raise UpdateError('发布需要项目 ID、ref 和 GITLAB_TOKEN')
            publish(GitLab(args.project_id), args.archive, expected, args.ref, token)
        print(json.dumps({'version': expected.version, 'size': expected.size,
                          'sha256': expected.sha256, 'published': args.publish}))
        return 0
    except HTTPError as error:
        print(f'GitLab API HTTP {error.code}', file=sys.stderr)
    except (OSError, ValueError, UpdateError, HTTPException, BadZipFile) as error:
        print(error, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
