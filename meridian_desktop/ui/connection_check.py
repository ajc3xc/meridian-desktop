"""The connection check job for the mode bar. Qt code, so it sits in ui/.

The main window runs one connection check of the chosen mode on the thread pool each time the window opens, each time
the mode changes, and when Test connection is pressed (see ui/main_window.py). This job carries that check there and
hands its result back through the shared delivery object, so a worker never holds a window. Nothing here starts,
stops or restarts anything. The check itself is pure: meridian_desktop/connection_status.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRunnable

from meridian_desktop.connection_status import ConnectionResult


@dataclass(frozen=True)
class ConnectionDone:
    """One finished check, as the window receives it: the generation it was started at, its mode, and its result.

    ``result`` is None when the check raised, so the window shows its failed-check line and no raw exception.
    """

    generation: int
    mode: str
    result: ConnectionResult | None


class ConnectionCheckJob(QRunnable):
    """Run one connection check on the thread pool and deliver its result, or None when the check raised.

    The job holds only the work and the deliver callable its caller builds. The caller's deliver emits to the shared
    delivery object, never to a window, so the worker never holds one (see ui/main_window.py).
    """

    def __init__(
        self,
        work: Callable[[], ConnectionResult],
        deliver: Callable[[ConnectionResult | None], None],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        result: ConnectionResult | None = None
        try:
            result = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool; the failure text is not shown
            result = None
        try:
            self._deliver(result)
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


__all__ = ["ConnectionCheckJob", "ConnectionDone"]
