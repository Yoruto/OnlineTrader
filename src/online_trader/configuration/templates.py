"""DSV trade-form templates in an immutable, revision-checked local journal.

The 33 saved keys follow App.on_save_template, gui.py 15126-15162.
Templates are values only; applying one returns a new preview profile.
"""

from dataclasses import dataclass
from pathlib import Path
import unicodedata

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.errors import TraderError


TEMPLATE_KEYS = frozenset("""max_volume max_risk_ratio max_daily_orders max_net_loss_stops
close_before_seconds trailing_activate_ratio trailing_pullback_ratio stop_day_on_trailing
manual_close_day_stop stale_close_day_stop aggressive_mode passive_entry_first auto_start_on_launch
risk_per_trade_ratio stale_force_close_seconds risk_reference_equity night_session_close
entry_slip_ratio chase_window_minutes chase_slip_ratio chase_base_now min_one_lot_fallback
stop_cooldown_enabled stop_cooldown_minutes require_trigger_update max_symbols_enabled
rollover_stop_enabled no_open_after_open no_open_after_open_minutes max_symbols_on total_margin_cap
no_open_before_close_minutes position_sync_seconds""".split())
MAX_REVISIONS = 1024
MAX_TEMPLATES = 64
MAX_REVISION_BYTES = 32_768
EMPTY_HASH = "0" * 64
SCHEMA = "DSV_TRADE_TEMPLATES/1"


def _error(message="Invalid local template journal", code="E_TEMPLATE"):
    raise TraderError(code, message)


def _name(name):
    if (type(name) is not str or not 1 <= len(name) <= 64 or name != name.strip()
            or name != unicodedata.normalize("NFC", name)
            or any(unicodedata.category(c).startswith("C") or c in '/\\' for c in name)
            or name in {".", ".."}):
        _error("Expected a nonempty template name without control characters or path separators")
    return name


def _trade(values):
    if type(values) is not dict or values.keys() != TEMPLATE_KEYS:
        _error("Template fields must match the DSV saved trade form")
    record = load_confirmed_baseline()[0].to_record()
    record["settings"]["trade"].update(values)
    try:
        validated = DsvProfile.from_record(record).to_record()["settings"]["trade"]
    except TraderError:
        _error("Invalid template trade values")
    return {key: validated[key] for key in sorted(TEMPLATE_KEYS)}


@dataclass(frozen=True, slots=True)
class TemplateSnapshot:
    revision: int
    journal_hash: str
    _templates: bytes

    @property
    def templates(self):
        return strict_loads(self._templates)

    def apply(self, name, profile):
        """Overlay saved trade fields only; symbols and other parameters survive."""
        _name(name)
        if not isinstance(profile, DsvProfile):
            _error("Expected a validated preview profile")
        templates = self.templates
        if name not in templates:
            _error("Template does not exist", "E_TEMPLATE_MISSING")
        record = profile.to_record()
        record["settings"]["trade"].update(templates[name])
        return DsvProfile.from_record(record)

    def summary(self):
        return {"revision": self.revision, "journal_hash": self.journal_hash,
                "names": sorted(self.templates), "runtime_activation": False,
                "scope": "DSV_TRADE_FORM_33"}


class TemplateStore:
    """Append only. Each successful hard-link claims exactly one revision.

    No mutable head or replace-in-place file exists. A racing writer loses the
    exclusive publication and must reload. Earlier revisions remain recoverable.
    Reads neither create a directory nor acquire a kernel/ledger lock.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self):
        templates, previous, revision = {}, EMPTY_HASH, 0
        try:
            if self.directory.exists() and not self.directory.is_dir():
                _error()
            paths = sorted(self.directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].json"))
            if len(paths) > MAX_REVISIONS:
                _error("Template journal capacity exceeded")
            for revision, path in enumerate(paths, 1):
                if path.name != f"{revision:08}.json" or path.stat().st_size > MAX_REVISION_BYTES:
                    _error("Template revision gap or size limit")
                with path.open("rb") as stream:
                    raw = stream.read(MAX_REVISION_BYTES + 1)
                if len(raw) > MAX_REVISION_BYTES:
                    _error()
                record = strict_loads(raw)
                if (type(record) is not dict or set(record) != {"schema_version", "revision", "previous_hash", "action", "name", "trade"}
                        or record["schema_version"] != SCHEMA or type(record["revision"]) is not int
                        or record["revision"] != revision or record["previous_hash"] != previous
                        or canonical_bytes(record) != raw):
                    _error()
                name, action = _name(record["name"]), record["action"]
                if type(action) is not str:
                    _error("Invalid template transition")
                if action in {"CREATE", "OVERWRITE"}:
                    if (action == "CREATE") == (name in templates):
                        _error("Invalid template transition")
                    templates[name] = _trade(record["trade"])
                    if len(templates) > MAX_TEMPLATES:
                        _error("Template count limit exceeded")
                elif action == "DELETE" and name in templates and record["trade"] is None:
                    del templates[name]
                else:
                    _error("Invalid template transition")
                previous = content_hash(record)
        except OSError as error:
            raise TraderError("E_TEMPLATE_IO", "Cannot read the local template journal") from error
        except TraderError as error:
            if error.code.startswith("E_TEMPLATE"):
                raise
            raise TraderError("E_TEMPLATE", "Template journal validation failed") from error
        return TemplateSnapshot(revision, previous, canonical_bytes(templates))

    def save(self, name, profile, *, expected_revision, overwrite=False):
        if not isinstance(profile, DsvProfile) or type(overwrite) is not bool:
            _error("Expected validated template values")
        values = {k: profile.to_record()["settings"]["trade"][k] for k in sorted(TEMPLATE_KEYS)}
        return self._append(name, "OVERWRITE" if overwrite else "CREATE", _trade(values), expected_revision)

    def delete(self, name, *, expected_revision):
        return self._append(name, "DELETE", None, expected_revision)

    def _append(self, name, action, trade, expected_revision):
        _name(name)
        if type(expected_revision) is not int or not 0 <= expected_revision < MAX_REVISIONS:
            _error("Invalid expected template revision")
        snapshot = self.read()
        if snapshot.revision != expected_revision:
            _error("Template journal changed; reload before editing", "E_TEMPLATE_CONFLICT")
        exists = name in snapshot.templates
        if action == "CREATE" and exists:
            _error("Template already exists; explicit overwrite required", "E_DUPLICATE_CONFLICT")
        if action in {"OVERWRITE", "DELETE"} and not exists:
            _error("Template does not exist", "E_TEMPLATE_MISSING")
        if action == "CREATE" and len(snapshot.templates) >= MAX_TEMPLATES:
            _error("Template count limit exceeded")
        record = {"schema_version": SCHEMA, "revision": snapshot.revision + 1,
                  "previous_hash": snapshot.journal_hash, "action": action, "name": name, "trade": trade}
        try:
            publish_new_bytes(canonical_bytes(record), self.directory / f"{record['revision']:08}.json")
        except TraderError as error:
            if error.code == "E_DUPLICATE_CONFLICT":
                raise TraderError("E_TEMPLATE_CONFLICT", "Another writer claimed this revision; reload") from error
            raise TraderError("E_TEMPLATE_IO", "Cannot publish the local template revision") from error
        # Return the exact committed revision, even if another writer appends next.
        values = snapshot.templates
        if action == "DELETE": del values[name]
        else: values[name] = trade
        return TemplateSnapshot(record["revision"], content_hash(record), canonical_bytes(values))
