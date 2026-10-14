"""Entry point: ``python -m meridian_desktop``.

``--smoke`` builds the window and tray, prints "smoke ok" and exits 0 without
starting the event loop. The Qt code lives in meridian_desktop/ui/. This module
re-exports the names that callers and tests use.
"""

from __future__ import annotations

from meridian_desktop.ui.app import main
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow

__all__ = ["MainWindow", "TAB_NAMES", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
