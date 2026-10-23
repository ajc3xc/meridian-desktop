"""The update notice: one check for a newer release at startup, and a status-bar bar that says so.

The check runs once per window, on the thread pool, through updater.check_for_update. It makes one request,
for the latest release JSON on api.github.com, and downloads nothing. The bar shows only when the result is an
UpdateNotice. An UpToDate, an Offline, a Refused or a failed check shows nothing. The button is the only thing
that opens the release page, and it does so through the opener it was given. QDesktopServices.openUrl by default.

The transport and the opener are injected, so tests pass fakes and make no network call. The bar starts the check
only when the caller says it is enabled, and never when no transport is given.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from meridian_desktop import updater
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

LABEL_TEXT = "Update available: v{version}"
BUTTON_TEXT = "Open release page"

Opener = Callable[[str], object]


def notice_text(version: str) -> str:
    """The label text for a newer release. The version is plain dotted digits, checked by updater."""
    return LABEL_TEXT.format(version=version)


def open_release_page(url: str) -> bool:
    """Open a release page in the browser with QDesktopServices.openUrl. Only the notice's button calls this."""
    return bool(QDesktopServices.openUrl(QUrl(url)))


class _CheckJob(QRunnable):
    """Run one update check on the thread pool and deliver its result. A failure delivers None."""

    def __init__(
        self,
        current_version: str,
        transport: updater.Transport,
        deliver: Callable[[object], None],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._current = current_version
        self._transport = transport
        self._deliver = deliver

    def run(self) -> None:
        try:
            result: object = updater.check_for_update(self._current, self._transport)
        except Exception:  # noqa: BLE001 - a check must never raise into the pool; nothing is shown for it
            result = None
        try:
            self._deliver(result)
        except RuntimeError:
            pass  # the window was deleted while the check ran


class UpdateNoticeBar(QWidget):
    """The status-bar notice: a label and a button, hidden until a check finds a newer release.

    start(enabled) runs one check. A second call does nothing, and nothing runs when enabled is False or when
    no transport was given. Only an UpdateNotice result shows the bar. Every other result hides it again.
    The Home tab's Updates card reads the same result through ``result`` and hears ``resultChanged`` when it arrives.
    """

    # Emitted on the GUI thread when the check's result arrives, whatever it was.
    resultChanged = Signal()

    def __init__(
        self,
        current_version: str,
        transport: updater.Transport | None,
        opener: Opener = open_release_page,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("update-notice")
        self._current = current_version
        self._transport = transport
        self._opener = opener
        self._url = ""
        self._started = False
        self._finished = False
        self._result: object | None = None
        # Identifies this bar's check. A plain object, so the check job can hold it without holding the bar.
        self._owner = object()
        shared_delivery().delivered.connect(self._on_delivered)

        self._label = make_selectable(QLabel("", self))
        self._label.setObjectName("update-notice-label")
        self._label.setTextFormat(Qt.TextFormat.PlainText)

        self._button = QPushButton(BUTTON_TEXT, self)
        self._button.setObjectName("update-notice-open")
        self._button.clicked.connect(lambda _checked=False: self._open_release_page())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        layout.addWidget(self._button)
        self.hide()

    # ----------------------------------------------------------- state

    @property
    def check_finished(self) -> bool:
        """True once a check result has arrived, whatever it was."""
        return self._finished

    @property
    def result(self) -> object | None:
        """The check's result (UpdateNotice, UpToDate, Offline or Refused), or None before it arrives."""
        return self._result

    @property
    def enabled(self) -> bool:
        """True when the one check was started for this window. False when check_updates is off or no transport was given."""
        return self._started

    @property
    def checking(self) -> bool:
        """True while the one check is running."""
        return self._started and not self._finished

    @property
    def release_page_url(self) -> str:
        """The release page the notice would open. Empty until a newer release is found."""
        return self._url

    # ----------------------------------------------------------- check

    def start(self, enabled: bool) -> None:
        """Start the one check for this window, on the thread pool. Does nothing when disabled or already started."""
        if not enabled or self._started or self._transport is None:
            return
        self._started = True
        # The job holds the version, the transport, the owner token and the shared delivery. It never holds the bar.
        owner = self._owner
        delivery = shared_delivery()
        QThreadPool.globalInstance().start(
            _CheckJob(self._current, self._transport, lambda result: delivery.delivered.emit(Delivered(owner, result)))
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another bar is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        result = item.value
        self._finished = True
        self._result = result
        self._show_result(result)
        self.resultChanged.emit()

    def _show_result(self, result: object) -> None:
        if isinstance(result, updater.UpdateNotice):
            self._url = result.release_page_url
            self._label.setText(notice_text(result.version))
            self.show()
            return
        # UpToDate, Offline, Refused, or a failed check: nothing is shown.
        self._url = ""
        self.hide()

    # ----------------------------------------------------------- click

    def _open_release_page(self) -> None:
        """Open the release page. Nothing else in this class opens a page."""
        if self._url:
            self._opener(self._url)


__all__ = [
    "BUTTON_TEXT",
    "LABEL_TEXT",
    "UpdateNoticeBar",
    "notice_text",
    "open_release_page",
]
