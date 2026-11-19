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


def _announce(previous, status, **details):
    value = (status, tuple(sorted(details.items())))
    if value != previous:
        print(json.dumps({'status': status, **details}, ensure_ascii=False), flush=True)
    return value


def schedule_session(store, row, operator, token):
    """Wait for configured start points and reload local controls before every trigger."""
    def authorize():
        actor, capabilities, _ = authenticate_operator(store.directory, row['account_key'], operator, token)
        if 'SCHEDULE_STRATEGY' not in capabilities:
            raise TraderError('E_AUTH', 'The operator has no scheduled strategy capability')
        return actor

    authorize()
    first = True
    announced = None
    try:
        while True:
            actor = authorize()
            try:
                control = store.runtime_control()
                profile = store.profile()
            except TraderError as error:
                announced = _announce(announced, 'WAITING_FOR_SCHEDULE_CONFIG', account_key=row['account_key'],
                                      error_code=error.code)
                first = False
                time.sleep(1)
                continue

            settings = profile.to_record()['settings']
            runtime_enabled = control['scheduled_start']['enabled']
            profile_enabled = settings['schedule']['enabled'] or settings['trade']['auto_start_on_launch']
            if not runtime_enabled:
                announced = _announce(announced, 'SCHEDULE_DISABLED', account_key=row['account_key'],
                                      profile_hash=profile.content_hash)
                first = False
                time.sleep(1)
                continue
            if not profile_enabled:
                announced = _announce(announced, 'WAITING_FOR_PROFILE_SCHEDULE', account_key=row['account_key'],
                                      profile_hash=profile.content_hash)
                first = False
                time.sleep(1)
                continue

            announced = _announce(announced, 'WAITING_FOR_SCHEDULE', account_key=row['account_key'],
                                  profile_hash=profile.content_hash)
            now = datetime.now(timezone.utc)
            trigger = timed_start_trigger(profile, now)
            if trigger is None and first:
                trigger = timed_start_trigger(profile, now, on_launch=True)
            first = False
            if trigger is not None:
                try:
                    control = store.runtime_control()
                    latest = store.profile()
                except TraderError as error:
                    announced = _announce(announced, 'WAITING_FOR_SCHEDULE_CONFIG',
                                          account_key=row['account_key'], error_code=error.code)
                    time.sleep(1)
                    continue
                if not control['scheduled_start']['enabled']:
                    announced = _announce(announced, 'SCHEDULE_DISABLED', account_key=row['account_key'],
                                          profile_hash=latest.content_hash)
                    time.sleep(1)
                    continue
                profile = latest
                if timed_start_trigger(profile, now) != trigger and not (
                        trigger == 'AUTO_START_ON_LAUNCH' and timed_start_trigger(profile, now, on_launch=True) == trigger):
                    announced = _announce(announced, 'SCHEDULE_CHANGED_BEFORE_TRIGGER', account_key=row['account_key'],
                                          profile_hash=profile.content_hash)
                    time.sleep(1)
                    continue
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
