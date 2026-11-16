"""QA-only observation of two frozen source rejection branches and minute state.

Source sites are pinned by original_loop._load's code/archive hashes. Observe
executed line 8027 (after source geometry calculation) and 8062 (after source
minute consumption), never throttled text logs and never candidate decisions.
Other refusal reasons are deliberately outside this trace schema's coverage.
"""
from decimal import Decimal, InvalidOperation

SCHEMA = 'DSV_ENTRY_TRACE/1'
REASONS = frozenset({'GEOMETRY_BLOCK', 'NO_CHASE_AFTER_ACT'})
SOURCE_LINES = frozenset({8027, 8062})


class SourceEvidenceError(BaseException):
    """Escape the reference's broad Exception handlers instead of losing evidence."""


def bar_identity(value):
    if value is None:
        return None
    if type(value) is int or type(value) is str and value:
        return str(value)
    raise ValueError('Unsupported minute identity in parity evidence')


def _price(value):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError('Missing parity price')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Non-finite parity price')
    return number


def source_rejection(line, values):
    """Read actual frozen-loop locals; do not mutate state or reevaluate entry."""
    if line not in SOURCE_LINES:
        return None
    contract, state = values['cont_code'], values['st']
    if values['state'][contract] is not state:
        raise ValueError('Reference contract/state identity mismatch')
    if line == 8027:
        return None if values['_geo5'] else {'contract_id': contract, 'reason': 'GEOMETRY_BLOCK'}
    # This site is reached only after the source's no-chase branch consumed lb.
    bar = bar_identity(state['last_signal_bar'])
    if bar != bar_identity(values['lb']) or bar is None:
        raise ValueError('Source no-chase branch did not persist its minute')
    if values['_hit_long']:
        direction, trigger = 'LONG', values['lo_open']
        level = trigger + values['_act_off']
    elif values['_hit_short']:
        direction, trigger = 'SHORT', values['so_open']
        level = trigger - values['_act_off']
    else:
        raise ValueError('No executed source no-chase direction')
    # _trig/_lvl are set only when the 600-second text logger runs. Use the
    # actual source trigger and activation distance, in its float order, even
    # when text is throttled. These values do not decide whether to reject.
    _price(trigger); _price(level)
    return {'contract_id': contract, 'reason': 'NO_CHASE_AFTER_ACT', 'direction': direction,
            'trigger_price': str(trigger), 'activation_price': str(level), 'consume': bar}


class FrozenEntryObserver:
    def __init__(self, code, sdk):
        if not SOURCE_LINES <= {line for _, _, line in code.co_lines()}:
            raise ValueError('Frozen entry observation sites changed')
        self.code, self.sdk = code, sdk

    def __call__(self, frame, event, arg):
        if frame.f_code is not self.code:
            return None
        if event == 'line' and frame.f_lineno in SOURCE_LINES:
            try:
                row = source_rejection(frame.f_lineno, frame.f_locals)
                if row is not None:
                    if self.sdk.frame is None:
                        raise ValueError('Source rejection outside a recorded round')
                    self.sdk.entry_rejections.append(row)
            except Exception as error:
                raise SourceEvidenceError('Cannot capture frozen entry evidence') from error
        return self


def _signatures(rows):
    if type(rows) is not list:
        raise ValueError('Parity decisions must be ordered')
    signatures = []
    for row in rows:
        if type(row) is not dict or type(row.get('reason')) is not str:
            raise ValueError('Malformed parity decision')
        if row['reason'] not in REASONS:
            continue
        contract = row['contract_id']
        if type(contract) is not str or not contract:
            raise ValueError('Missing parity contract')
        signature = (contract, row['reason'])
        if row['reason'] == 'NO_CHASE_AFTER_ACT':
            if row['direction'] not in {'LONG', 'SHORT'} or row['consume'] is None:
                raise ValueError('Invalid directional minute consumption')
            signature += (row['direction'], _price(row['trigger_price']), _price(row['activation_price']),
                          bar_identity(row['consume']))
        signatures.append(signature)
    return signatures


def compare_entry_evidence(reference, current):
    """Missing old reference fields are evidence gaps, never an empty difference."""
    if (reference.get('entry_trace_schema') != SCHEMA or 'entry_rejections' not in reference
            or 'decisions' not in current):
        return ['entry_evidence_missing']
    source, candidate = reference['state'], current['symbols']
    if set(source) != set(candidate):
        return ['entry_evidence_scope']
    differences = []
    if any('last_signal_bar' not in row for row in [*source.values(), *candidate.values()]):
        differences.append('entry_evidence_missing')
    else:
        try:
            if any(bar_identity(row['last_signal_bar']) != bar_identity(candidate[code]['last_signal_bar'])
                   for code, row in source.items()):
                differences.append('last_signal_bar')
        except ValueError:
            differences.append('entry_evidence_invalid')
    try:
        if any(row.get('reason') not in REASONS for row in reference['entry_rejections']):
            raise ValueError('Unsupported source rejection in this trace schema')
        if _signatures(reference['entry_rejections']) != _signatures(current['decisions']):
            differences.append('entry_rejections')
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        if 'entry_evidence_invalid' not in differences:
            differences.append('entry_evidence_invalid')
    return differences
