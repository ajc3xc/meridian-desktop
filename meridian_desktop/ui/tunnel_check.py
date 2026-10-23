"""The tunnel check job for the top bar. Qt code, so it sits in ui/.

The main window runs one read-only tunnel status call on the thread pool each time Tunnel mode is chosen, and
this job carries it there. The Home tunnel card runs its own read through the same status call. Nothing here
starts, stops or restarts the tunnel.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRunnable

from meridian_desktop.tunnel_status import TunnelStatus


class TunnelCheckJob(QRunnable):
    """Run one tunnel check on the thread pool and deliver (status, failure_name).

    The job holds only the work and the deliver callable its caller builds. The caller's deliver emits to the shared
    delivery object, never to a window, so the worker never holds one (see ui/main_window.py). A check that raises is
    reduced to its class name here, so a raw exception never reaches the UI.
    """

    def __init__(self, work: Callable[[], TunnelStatus], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        status: TunnelStatus | None = None
        failure: str | None = None
        try:
            status = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((status, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown
