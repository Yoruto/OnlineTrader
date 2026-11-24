"""Offline V3.1 E2E testing framework for OnlineTrader.

This package lives strictly outside the production wheel and must never import
or activate real broker adapters (such as ``tqsdk``).
"""

from __future__ import annotations

import sys

_FORBIDDEN_ONLINE_MODULES = (
    "tqsdk",
    "online_trader.gateway.simnow",
)


def assert_offline_qa_environment(*, block_tqsdk: bool = True) -> None:
    """Verify no live broker SDK module is loaded and optionally block tqsdk imports."""
    for mod_name in _FORBIDDEN_ONLINE_MODULES:
        loaded = sys.modules.get(mod_name)
        if loaded is not None:
            raise RuntimeError(f"Offline QA environment must not load live module: {mod_name}")
    if block_tqsdk:
        for mod_name in _FORBIDDEN_ONLINE_MODULES:
            if mod_name not in sys.modules:
                sys.modules[mod_name] = None  # type: ignore[assignment]


__all__ = ["assert_offline_qa_environment"]
