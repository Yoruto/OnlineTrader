"""Compatibility launcher for the Runtime-backed dev workbench.

There is intentionally no second Runtime-specific widget tree here. Runtime
attachment changes transport and authority only; every visible control comes
from the established WorkbenchWindow/WorkbenchLayout used by dev users.
"""

from online_trader.desktop.workbench import WorkbenchWindow, run


class RuntimeWorkbenchWindow(WorkbenchWindow):
    """The existing dev window attached to the independent Runtime view."""

    def __init__(self, pipe):
        super().__init__(pipe, runtime_view=True)


def run_runtime(pipe):
    return run(pipe, runtime_view=True)
