DRAFT - NOT FOR RELEASE

# Written offer for source code (draft)

## Covered components

- PySide6 6.12.0, shiboken6 6.12.0 (Qt for Python), under LGPL-3.0-only.
- Qt 6.12.0 libraries shipped in the application folder. The expected set is
  Qt6Core, Qt6Gui and Qt6Widgets, plus the platforms and styles plugins. The
  manifest in packaging/lgpl_manifest.toml is the authority for this list.

## Offer

For at least three years after you receive the application, we will give any
third party who asks the complete corresponding source code for the components
above, at no charge beyond the cost of a physical copy. This is a placeholder
term. The Qt LGPL obligations page gives no offer duration (see PLAN.md), so
counsel must confirm the term before release.

Where to get the source: https://TBD-owner-source-host (owner to fill in. This
URL is not verified.)

Upstream: PySide6 and Qt source are published by their upstream projects. The
exact upstream URLs are not verified in this draft.

## Replacing the Qt libraries

Qt is shipped as separate DLL files, not linked into the executable. You may
replace them with a compatible build of the same Qt version, as LGPL-3.0
permits.

## Status

Draft. Owner and counsel review are open.
