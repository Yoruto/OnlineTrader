"""Run a confirmed simulated strategy schedule without a Qt lifetime dependency."""

from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time

from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError
from online_trader.runtime.desktop_bootstrap import build_start_request, timed_start_trigger
from online_trader.runtime.operator_api import authenticate_operator
from online_trader.runtime.standalone import run_session
from online_trader.strategy.entry import CHINA_TIME


def _already_started(store, row, request_id):
    claim = store.directory / 'desktop-start-claims' / (request_id + '.json')
    if claim.exists() or (store.directory / 'desktop-starts' / request_id).exists():
        return True
    ledger = store.ledger_path(row)
    if not ledger.is_file():
        return False
    try:
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=.5)) as stream:
            scope = stream.execute('SELECT account_key, environment FROM ledger_scope').fetchall()
            if scope != [(row['account_key'], 'ONLINE_SIM')]:
                raise TraderError('E_SCOPE', 'Scheduled account differs from the execution ledger')
            return stream.execute('SELECT 1 FROM approval_manifests WHERE approval_record_id = ?',
                                  (request_id,)).fetchone() is not None
    except sqlite3.Error:
        raise TraderError('E_STORAGE', 'Cannot check the durable scheduled-start identity') from None


def schedule_session(store, row, profile, operator, token):
    settings = profile.to_record()['settings']
    if not settings['schedule']['enabled'] and not settings['trade']['auto_start_on_launch']:
        raise TraderError('E_RUNTIME_CONFIG', 'Enable a startup schedule in the confirmed strategy configuration')

    def authorize():
        actor, capabilities = authenticate_operator(store.directory, row['account_key'], operator, token)
        if 'SCHEDULE_STRATEGY' not in capabilities:
            raise TraderError('E_AUTH', 'The operator has no scheduled strategy capability')
        return actor

    authorize()
    first = True
    print(json.dumps({'status': 'WAITING_FOR_SCHEDULE', 'account_key': row['account_key']}), flush=True)
    try:
        while True:
            actor = authorize()
            now = datetime.now(timezone.utc)
            trigger = timed_start_trigger(profile, now)
            if trigger is None and first:
                trigger = timed_start_trigger(profile, now, on_launch=True)
            first = False
            if trigger is not None:
                local_day = now.astimezone(CHINA_TIME).date().isoformat()
                key = {'account_key': row['account_key'], 'day': local_day, 'trigger': trigger}
                request_id = 'desktop-runtime-schedule-' + content_hash(key)[:32]
                if not _already_started(store, row, request_id):
                    request = build_start_request(store, row['account_id'], profile, arm=True, now=now,
                                                  timed_trigger=trigger, scheduled_operator=actor)
                    request['request_id'] = request_id
                    run_session(store, row, request, use_profile_file=True, check_scheduled_authorization=authorize)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
