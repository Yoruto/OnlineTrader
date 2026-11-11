"""Packaged, source-bound DSV reference values; no execution authority is granted."""

import copy
import hashlib
from importlib.resources import files

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


BASELINE_RESOURCE = "dsv-baseline-20261109.json"
BASELINE_SHA256 = "ab75379e5bc73fc767285202ea3c18d842f8477a3913078223c80c565c75a3ad"


def load_confirmed_baseline():
    """Return independent immutable values and a fresh reference approval manifest."""
    try:
        raw = files("online_trader").joinpath("resources", BASELINE_RESOURCE).read_bytes()
        if hashlib.sha256(raw).hexdigest() != BASELINE_SHA256:
            raise TraderError("E_CONFIG", "Bundled DSV reference integrity differs")
        material = strict_loads(raw)
        if set(material) != {"schema_version", "profile", "approval"} or material["schema_version"] != "DSV_BASELINE/1":
            raise TraderError("E_CONFIG", "Unsupported bundled DSV reference")
        profile = DsvProfile.from_record(material["profile"])
        approval = material["approval"]
        if (approval.get("schema_version") != "DSV_REFERENCE_APPROVAL/1"
                or approval.get("status") != "APPROVED"
                or approval.get("approval_kind") != "DSV_REFERENCE_AND_SAVED_CONFIGURATION"
                or approval.get("profile_hash") != profile.content_hash
                or approval.get("effective_scope") != {"purpose": "REFERENCE_ONLY", "runtime_activation": False,
                    "broker_connection_authorized": False, "account_operations_authorized": False}
                or [item.get("b_code") for item in approval.get("decisions", [])] != [f"B{i:02}" for i in range(1, 21)]
                or any(item.get("status") != "APPROVED" for item in approval["decisions"])):
            raise TraderError("E_CONFIG", "Bundled DSV reference approval differs")
        return profile, copy.deepcopy(approval)
    except TraderError:
        raise
    except (OSError, TypeError, KeyError, ValueError, AttributeError) as error:
        raise TraderError("E_CONFIG", "Cannot load the bundled DSV reference") from error
