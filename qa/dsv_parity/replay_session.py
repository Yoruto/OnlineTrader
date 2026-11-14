"""Replay offline JSONL rounds and commands through the explicit DSV controller."""

import argparse
import json
from pathlib import Path
import sqlite3

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.errors import TraderError
from qa.recording_port import RecordingPort


def replay(source, database):
    with source.open(encoding='utf-8') as stream:
        header = strict_loads(next(stream))
        if header.pop('type') != 'session':
            raise ValueError('First record must bind the replay session')
        profile = DsvProfile.from_record(header.pop('profile'))
        port = RecordingPort()
        with DsvSession(database, profile=profile, port=port, **header) as session:
            for number, line in enumerate(stream, 2):
                try:
                    frame = strict_loads(line)
                    kind = frame.pop('type')
                    if kind == 'round':
                        result = session.apply_round(frame)
                    elif kind == 'command':
                        result = session.command(frame)
                    else:
                        raise ValueError('Unknown replay record type')
                    yield result
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f'Line {number}: {getattr(error, "code", "E_INPUT")} {error}') from error
            yield {'type': 'SNAPSHOT', 'state': session.snapshot(),
                   'orders': port.orders, 'cancellations': port.cancellations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        for record in replay(args.input, args.db):
            print(json.dumps(record, ensure_ascii=False), flush=True)
    except (OSError, sqlite3.Error, TraderError, StopIteration, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f'{getattr(error, "code", "E_INPUT")} {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
