"""Immutable DSV reference approval and local parameter previews."""

import hashlib
from importlib.resources import files

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


BASELINE_RESOURCE = "dsv-baseline-20261109.json"
BASELINE_SHA256 = "ab75379e5bc73fc767285202ea3c18d842f8477a3913078223c80c565c75a3ad"
APPROVAL_RESOURCE = "dsv-reference-approval-20261109.json"
APPROVAL_SHA256 = "2fff611c7c6f97d30898e46fc07347ca0049e28e05f5c8fcb668378d61633336"


def _resource(name):
    return files("online_trader").joinpath("resources", name)


def _read_reference(name, expected_hash):
    try:
        raw = _resource(name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise TraderError("E_CONFIG", "Bundled DSV reference integrity differs")
        return strict_loads(raw)
    except TraderError:
        raise
    except (OSError, ValueError) as error:
        raise TraderError("E_CONFIG", "Cannot load the bundled DSV reference") from error


def load_reference_approval():
    """Read the approval used to verify historical reference facts."""
    approval = _read_reference(APPROVAL_RESOURCE, APPROVAL_SHA256)
    if (approval.get("schema_version") != "DSV_REFERENCE_APPROVAL/1"
            or approval.get("status") != "APPROVED"
            or approval.get("approval_kind") != "DSV_REFERENCE_AND_SAVED_CONFIGURATION"
            or approval.get("effective_scope") != {"purpose": "REFERENCE_ONLY", "runtime_activation": False,
                "broker_connection_authorized": False, "account_operations_authorized": False}
            or [item.get("b_code") for item in approval.get("decisions", [])] != [f"B{i:02}" for i in range(1, 21)]
            or any(item.get("status") != "APPROVED" for item in approval["decisions"])):
        raise TraderError("E_CONFIG", "Bundled DSV reference approval differs")
    return approval


def check_reference_profile(profile, approval):
    if not isinstance(profile, DsvProfile) or profile.content_hash != approval["profile_hash"]:
        raise TraderError("E_CONFIG", "Profile differs from the confirmed DSV reference")


def legacy_baseline_available():
    return _resource(BASELINE_RESOURCE).is_file()


def load_confirmed_baseline():
    """Load the original saved profile from the source reference collection."""
    if not legacy_baseline_available():
        raise TraderError("E_CONFIG_FILE", "Supply the confirmed original DSV profile explicitly")
    material = _read_reference(BASELINE_RESOURCE, BASELINE_SHA256)
    if set(material) != {"schema_version", "profile", "approval"} or material["schema_version"] != "DSV_BASELINE/1":
        raise TraderError("E_CONFIG", "Unsupported bundled DSV reference")
    approval = load_reference_approval()
    if material["approval"] != approval:
        raise TraderError("E_CONFIG", "Bundled DSV reference approval differs")
    profile = DsvProfile.from_record(material["profile"])
    check_reference_profile(profile, approval)
    return profile, approval


def default_preview_profile():
    return DsvProfile.from_dsv_json(b'{"trade":{"auto_start_on_launch":false}}')


def load_parameter_reference():
    if legacy_baseline_available():
        return load_confirmed_baseline()
    return default_preview_profile(), load_reference_approval()
