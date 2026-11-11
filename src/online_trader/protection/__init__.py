"""Finite, deterministic protection plans; this package never submits an order."""

from .reducer import (
    ProtectionDecision,
    ProtectionState,
    available_exit_quantity,
    initial_state,
    reduce,
    reduce_event,
    revise_plan,
)

__all__ = [
    "ProtectionDecision", "ProtectionState", "initial_state", "reduce_event", "reduce",
    "available_exit_quantity", "revise_plan",
]
