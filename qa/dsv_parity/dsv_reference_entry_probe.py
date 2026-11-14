"""Extract and execute fixed DSV pure helper code on synthetic prices only."""
import argparse
import builtins
import hashlib
from io import BytesIO
import json
import marshal
from pathlib import Path
import sys
import types
import zipfile
from qa.dsv_parity.verify_reference import MANIFEST, archive_objects, code_objects, verify


HELPERS = ('opening_jump_block', 'hysteresis_ready', 'chase_within_cap', 'half_close_lots')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error('Use the independent Python 3.14 environment to inspect the archive')

    try:
        reference = json.loads(MANIFEST.read_text(encoding='utf-8'))['dsv_reference']
        archive = args.archive.read_bytes()
        verify('archive', archive, reference['archive_sha256'])
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            executable = bundle.read('trading/trading.exe')
        verify('exe', executable, reference['exe_sha256'])
        gui, _ = archive_objects(executable, reference['python_archive_version'])
        gui_sha = verify('gui', gui, reference['gui_marshal_sha256'])
        candidates = {code.co_qualname: code for code in code_objects(marshal.loads(gui)) if code.co_qualname in HELPERS}
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'Reference verification failed: {error}\n')

    allowed_globals = {name: getattr(builtins, name) for name in ('float', 'int', 'round', 'abs', 'min', 'max', 'Exception')}
    functions = {}
    code_hashes = {}
    for name in HELPERS:
        code = candidates[name]
        if code.co_freevars or not set(code.co_names) <= allowed_globals.keys():
            parser.exit(1, f'Reference verification failed: {name} has unexpected globals or free variables\n')
        code_hashes[name] = hashlib.sha256(code.co_code).hexdigest()
        manifest_helper = reference['gui_functions'].get(name)
        if manifest_helper is not None and (code_hashes[name] != manifest_helper['bytecode_sha256']
                                            or code.co_firstlineno != manifest_helper['line']):
            parser.exit(1, f'Reference verification failed: {name} code fingerprint mismatch\n')
        functions[name] = types.FunctionType(code, {'__builtins__': allowed_globals})

    def audit(event, args):
        if event.startswith(('socket.', 'subprocess.', 'os.spawn', 'os.exec')) or event == 'os.system':
            raise RuntimeError(f'Offline probe blocked {event}')

    sys.addaudithook(audit)
    samples = [
        ('opening_jump_block', [119, 100, 40, 0.5]),
        ('opening_jump_block', [120, 100, 40, 0.5]),
        ('opening_jump_block', [80, 100, 40, 0.5]),
        ('opening_jump_block', [0, 100, 40, 0.5]),
        ('opening_jump_block', [0.3, 0.2, 0.1, 1]),
        ('hysteresis_ready', ['long', 98, 100, 2, False]),
        ('hysteresis_ready', ['long', 99, 100, 2, False]),
        ('hysteresis_ready', ['short', 102, 100, 2, False]),
        ('hysteresis_ready', ['short', 100, 100, 2, True]),
        ('hysteresis_ready', ['long', 104, 100, 0, False]),
        ('chase_within_cap', ['long', 100, 102, 99, 102, 2]),
        ('chase_within_cap', ['long', 100, 105, 99, 100, 2]),
        ('chase_within_cap', ['long', 100, 105, 99, 102, 2]),
        ('chase_within_cap', ['short', 100, 101, 95, 100, 2]),
        ('chase_within_cap', ['short', 100, 101, 97, 99, 2]),
        ('chase_within_cap', ['short', 100, 101, 98, 98, 2]),
        *[('half_close_lots', [qty, 0.5]) for qty in (1, 2, 3, 5, 7, 9)],
    ]
    rows = [{'function': name, 'args': arguments, 'result': functions[name](*arguments)}
            for name, arguments in samples]
    print(json.dumps({'scope': 'extracted pure functions only', 'gui_sha256': gui_sha,
                      'code_sha256': code_hashes, 'rows': rows}, indent=2))


if __name__ == '__main__':
    main()
