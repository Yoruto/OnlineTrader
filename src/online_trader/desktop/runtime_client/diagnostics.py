"""Stable diagnostic catalog and Chinese presentation for the Runtime client."""

from dataclasses import dataclass

from online_trader.diagnostics.catalog import lookup_diagnostic


@dataclass(frozen=True)
class PresentedDiagnostic:
    title: str
    explanation: str
    action: str


def present(diagnostic):
    code = diagnostic.get('code', 'OT-SYS-9999')
    entry = lookup_diagnostic(code)
    return PresentedDiagnostic(entry.title, entry.explanation, entry.action)


class NotificationCenter:
    def __init__(self):
        self._items = {}

    def update(self, diagnostics, *, account_key, boot_id):
        active = set()
        for row in diagnostics:
            key = (row.get('code'), account_key, row.get('impact_scope'), boot_id,
                   row.get('command_id'), row.get('trace_id'))
            active.add(key)
            prior = self._items.get(key)
            # A Runtime snapshot repeats active facts.  Refreshing the same
            # stable identity is not a new notification occurrence.
            self._items[key] = {'diagnostic': dict(row), 'count': 1 if prior is None else prior['count'],
                                'active': row.get('recovery_state') not in {'RECOVERED', 'RESOLVED'}}
        for key, value in self._items.items():
            # The publication is the complete active diagnostic set.  Items
            # absent from the next snapshot have recovered, regardless of
            # whether the original source was Runtime or this client.
            if key not in active:
                value['active'] = False
        return tuple(value for value in self._items.values() if value['active'])
