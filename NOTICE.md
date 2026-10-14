# Third-party notices

Meridian Desktop, wave 1 (0.1.0). Draft. This file lists third-party software
that the desktop app uses or may bundle. The bundle check
(`packaging/verify_bundle.py`) looks for some of the names below.

## PySide6 and shiboken6 (Qt for Python), elected LGPL-3.0-only

- Packages: PySide6 6.12.0, PySide6_Essentials 6.12.0, PySide6_Addons 6.12.0,
  shiboken6 6.12.0.
- Qt runtime: Qt 6.12.0 (`PySide6.QtCore.qVersion()` in the pixi dev environment).
- Upstream licence metadata, verbatim: `License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`
  and `License-File: LicenseRef-Qt-Commercial.txt`.
- Election: this project uses PySide6 and Qt under LGPL-3.0-only. It does not
  elect GPL-2.0-only, GPL-3.0-only, or the Qt commercial licence.
- Licence text: `LICENSES/LGPL-3.0.txt`. Its provenance against the FSF copy is
  not yet verified.
- Linking: Qt is shipped as separate, dynamically linked DLL files in the
  ONEDIR bundle, so a user can replace them. The app is folder-mode only.
- Source offer: see `OFFER-SOURCE.md`.
- Dev environment only: PySide6_WebEngine and PySide6_Pdf 6.12.0.140 are installed as
  PySide6 requirements. Their modules are excluded from the bundle. They carry
  the same licence metadata.

## meridian-docs (MIT): not used in wave 1

If a later wave bundles meridian-docs, its MIT notice and copyright line must be
added here.

## meridian-latex: excluded, not bundled

The desktop does not include, copy, vendor or bundle any meridian-latex code. In
a later wave the desktop may connect to a user-installed engine over loopback.
That component is covered by its own licence, which is not reproduced here.

## Open items

- `LICENSES/LGPL-3.0.txt`: kept as is. Its provenance is not verified against
  the FSF copy.
- Python runtime and other dependencies: not yet inventoried. The verifier's
  licence inventory (standard library, importlib.metadata) runs at build time.
- Upstream source URLs: see `OFFER-SOURCE.md`.
