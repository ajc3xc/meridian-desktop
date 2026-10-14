# Meridian Desktop — plan

Private repo. Local coordination only. The hosted site (usemeridian.us) already
provides the web UI, so this app does not reproduce it.

## Status (as of 2026-10-14)

| Item | State |
|---|---|
| Repo | Branch `master`, no commits. Untracked: `.gitignore`, `.venv`, and the Wave 1 files. |
| Skeleton build (workflow `wsil64j56`) | Stopped by the lead while still running. It had written a root-layout package, packaging scripts, and `LICENSES/`. Partial. See "Incidents". |
| Autodesk and auto-test research (workflow `wwcwaw6tn`) | Finished. See "Research findings". |
| Maintenance, adopters, LGPL-in-the-wild research (`wud5uwacx`) | Finished. See "Research findings". |
| Wave 1 foundation | Gates pass on this run: 56 tests, boundary ok, smoke ok, verifier exits 1 with no build (expected). Not committed. No real bundle has been built. |

## Scope

Desktop features are local coordination only:
Projects, Writing, Zotero, Tunnel, Services, Code, Logs, Settings.
Each tab is built in a numbered wave (see "Waves").

## Layout and interpreter

- Root package `meridian_desktop/` at the repo root, not `src/`.
  `packaging/verify_bundle.py` assumes this layout.
- Python 3.10.11 in `.venv` for now, because it is the only interpreter on this
  machine. PySide6 6.12.0 is installed there. The Meridian core needs Python 3.12,
  so `requires-python` must be raised before any core import. That needs an owner
  decision (see below).

## Licensing rules

- **PySide6 / Qt:** we elect LGPL-3.0-only. PySide6's own metadata reads
  `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` and points to a commercial Qt
  license file. We do not use the GPL or commercial options. The election is
  recorded in `packaging/lgpl_manifest.toml` and `NOTICE.md`.
  - Ship Qt as dynamically linked, separately replaceable DLLs.
  - Ship the LGPL text, notices, a written source offer, and an EULA that permits
    modification and reverse engineering for debugging.
  - Folder-mode PyInstaller only, not one-file.
- **meridian-docs** (`extensions/meridian-docs`): MIT. Bundling is allowed.
- **meridian-latex** (the meridian-latex directory in the Meridian repo, including its Overleaf Chrome
  extension and engine): **Meridian Source License 1.0 (MSL-1.0)**. This is not an
  OSI open-source license.
  - Section 3(a) bars Competing Use: offering it as a hosted or managed service.
  - Section 3(e) bars sublicensing or reselling it as a standalone commercial
    product for third parties.
  - **Rule:** the desktop never vendors, copies, or bundles any meridian-latex
    code. The desktop connects to a user-installed engine over loopback
    (`http://127.0.0.1:8471`). If no engine is running, the LaTeX section shows
    "not connected" and nothing else changes.
  - Whether a *connecting* desktop build raises a 3(e) issue for a commercial
    release is an owner decision. This is not legal advice. Get a read before any
    paid release.
- The repo's own license is `TBD-owner`.

Boundary enforcement: `tools/check_boundary.py` (Wave 1) fails when Qt is
imported outside `meridian_desktop/ui/` (with `tray.py` listed as a UI exception)
or when any file names the meridian-latex directory. The verifier's
`check_source_leak` scans the built bundle.

## Writing stack

### DOCX — meridian-docs (MIT)

- Package `meridian-docs-mcp` (stdlib-only OOXML parser). The MCP surface has
  about 65 tools. Examples: `audit_document`, `document_outline`,
  `check_render_capability`, `scan_citation_keys`, `search_document`,
  `write_section`, `insert_citation`, `insert_image`, `insert_equation`,
  `insert_figure_block`, `sync_bibliography`, `merge_docx_draft`.
- 29 journal style presets (28 publishers plus `default`) in
  `meridian_docs/journal_style_presets/`.
- **Connection (planned):**
  - Read-only calls (audit, outline, render capability, citation scan) run
    in-process. The package is stdlib-only, so there is no Qt conflict.
  - Writes go through the MCP server, never a direct file edit. The OOXML
    integrity checks and render gate then run on every write.
  - Figure and image inserts pass the rights policy first (`rights_policy.py`:
    STORE_IMAGE / STORE_UNMODIFIED / LINK_ONLY). In `commercialized` use mode, a
    gated image is refused, not inserted.
- Desktop shows: document list, audit findings, render-gate result, citation
  consistency, and a diff of the last write.

### LaTeX / Overleaf — meridian-latex (MSL-1.0, connect only)

- Engine HTTP server on `127.0.0.1:8471`. Routes:
  `GET /health`, `GET /extension-version`, `POST /outline`, `POST /claim`,
  `POST /lease`, `POST /release`, `GET|POST /provenance`,
  `GET /zotero-lookup`, `POST /open-overleaf`, `GET /claims`.
- MCP surface: about 23 tools, for example `outline_tex`, `lint_tex`,
  `lease_document`, `claim_node`, `record_provenance`, `overleaf_login_status`.
- Chrome MV3 extension for `https://www.overleaf.com/project/*` only. Host
  permissions are Overleaf and `127.0.0.1:8471`.
- **Desktop connection (planned):**
  - Probe `/health`, then `/extension-version`. Show "connected" only when both
    answer. Show the engine version and the extension version side by side.
  - Show live claims (`/claims`) and provenance (`/provenance`). Release a claim
    only after confirmation (`/release`).
  - "Open in Overleaf" calls `/open-overleaf` after confirmation.
  - Zotero lookup via `/zotero-lookup`, shared with the Zotero tab.
  - **Overleaf login is human-only.** The desktop shows login status from
    `overleaf_login_status` and tells the owner to run
    `node dist/overleaf-login.js login` in a terminal. The desktop never runs it,
    never reads a session cookie, and never stores one.
- Known risks from the engine README:
  - Its CORS once accepted any installed browser extension. It is now pinned to
    the extension id via the manifest `key`. The extension must be reloaded
    after that change. The desktop must not treat CORS as authentication.
  - The write-back spec (`docs/write-back-spec.md` in the meridian-latex directory) is
    marked "design, not yet implemented". Confirm which claim and lease routes
    are real before relying on them.
- The spec requires meridian-latex to stay self-contained. It must not depend on
  `meridian-docs` or the Meridian server, and the desktop must not change that.

### Google Docs — no extension exists yet

- **Finding:** the repo has no Google Docs extension or integration. Searching
  tracked files for Google Docs or gdocs returned nothing. "Google" appears only
  in comments and in the note that Chrome is required.
- **Google Drive connector:** a Google Drive connector is attached to this
  session (tools such as `search_files`, `read_file_content`, `update_file`,
  `share_file`). I have not called it. I have not confirmed whether it covers
  Google Docs editing.
- **Options (owner decision needed before Wave 5):**
  - **A. Chrome MV3 extension for `docs.google.com`,** mirroring the Overleaf
    one. Talks to the same loopback engine pattern. No OAuth. DOM-driven, so it
    breaks when Google changes its UI.
  - **B. Google Docs API with OAuth.** Needs a Google Cloud project and a
    consent screen created by the owner. Structured `batchUpdate` edits. Tokens
    kept in the OS keychain. This is the recommended write path.
  - **C. Drive connector for read and export only,** as a first step. Checks
    value before committing to A or B.
- Recommendation: test C first, then build B. Do not build A unless the owner
  wants the browser-driven path.

## Waves

Each wave ends with: tests green, `tools/check_boundary.py` clean, the verifier
run with a stated result, and a report of exactly what ran.

1. **Foundation (in progress).** Qt shell with the eight placeholder tabs,
   settings store (`local`/`hosted` mode), boundary checker, LGPL manifest, static
   bundle verifier, draft LICENSES/NOTICE/OFFER-SOURCE/EULA, smoke and unit tests.
   Placeholders make no network calls.
2. **Projects and settings.** Project list and detail, folders, locality, the
   commercialized use-mode toggle, Hosted/Local switch. Client setup for Claude
   Desktop and Codex (from `meridian/client_setup.py`, uncommitted on
   `mcp-client-setup`, so commit that first by path). Then Cursor, Gemini CLI,
   VS Code Copilot, Windsurf (after a path check), and Zed (UI only). Grok and
   ChatGPT get hosted-connector instructions only.
3. **Services, tunnel, logs.** Health for local MCP servers, server status, the
   six tunnel slots, served folders, logs, diagnostics. Tunnel start and stop stay
   locked until item `84d8fd28` (local-trust contract) is done.
4. **Zotero.** Status for the local API on `127.0.0.1:23119` (last check: not
   responding), launch with confirmation, library search, citation lookup.
   Autostart needs the owner's explicit `--yes`.
5. **Writing.** DOCX panel (reads in-process, writes via MCP, rights gate on
   images), LaTeX panel (engine probe, claims, provenance, Overleaf open, login
   status), Google Docs panel (per the owner's option choice).
6. **Research, outputs, code.** Research runs and findings (scheduler dry-run
   only; the simulator is blocked), outputs registry, provenance, staleness,
   BM25 code search (`meridian/code_search.py`, uncommitted on `mcp-client-setup`),
   codebase index status, Serena activation per project. `meridian-outputs` needs
   authorization through `/mcp` before it can be used.
7. **Release.** PyInstaller folder build. `verify_bundle.py --dist` against the
   real output. Confirm no meridian-latex or MSL file is present. Ship LICENSES,
   NOTICE, OFFER-SOURCE, EULA.
8. **Regression.** Per-tab offscreen smoke tests (pytest-qt), boundary check in
   CI, leak mutation tests (plant a forbidden import and confirm the check fails),
   a scan of the built folder for forbidden files.

## Research findings

From `wud5uwacx` (finished):
- **PySide6 cadence:** steady, tied to Qt 6.12 LTS. 6.12.0 shipped Oct 8, 2026.
- **wxPython:** 4.3.2 shipped Oct 13, 2026. Small core team, long gaps between
  releases, and it builds on the wxWidgets 3.3.x line that its own notes call
  API/ABI unstable. Higher stall risk than PySide6.
- **Commercial PySide6 adopters:** five confirmed or strongly indicated, all in
  VFX, 3D, and reverse-engineering tools. None found in medical, industrial HMI,
  CAD, or lab instruments.
- **Commercial wxPython adopters:** few, and mostly dated (Dropbox 2009 client,
  Transana, Vintech RCAM-Pro, Digsby). None confirmed for 2025–2026.
- **Gaps:** Qt LTS end dates unconfirmed (qt.io pages returned 404). PySide 6.12.0's
  bundled Qt version unconfirmed. wxPython licensing not checked.

From `wwcwaw6tn` (finished), checked against Autodesk and GitHub pages:
- **Maya 2022:** Autodesk published Qt 5.15.2 and PySide2 5.15.2 as forks under
  `github.com/autodesk-forks` (tag `Maya2022`). The release notes name no
  license. The fork repos contain LGPLv3, GPLv2, GPLv3, and a Qt commercial file.
  Which of those the tag carries is not verified.
- **Maya 2025 and 2026:** the DEVHELP pages list Qt 6.5.3 and PySide6 6.5.3 with no
  link, "available upon request", and no license named.
- **Autodesk's 2014 Inventor page** offered Qt 4.7.2 under LGPL-2.1, with a source
  offer valid three years from purchase. That is the only Autodesk page found
  that names an LGPL offer term. The three-year term is Autodesk's, not Qt's.
- **Maya DevKit** is distributed under the Autodesk License and Services
  Agreement, not as open source. Maya core is described as proprietary. The
  Autodesk EULA text itself was not retrieved.
- **What this means:** Autodesk publishes Qt/PySide source for some versions and
  offers it on request for others. No Autodesk statement of LGPL practice for
  current Maya was found. Do not cite Autodesk as a model without a stronger
  primary source.

Automated LGPL, license, and leak testing (from each tool's own page; from the
narrow research run):
- **License scanning:** the verifier reads licence metadata from the standard
  library (`importlib.metadata`). `pip-licenses` was dropped: the dependency guard
  blocked it, and the stdlib read covers the need. Other candidates, not adopted:
  `LicenseCheck` (MIT), `liccheck` (Apache-2.0), ScanCode toolkit (Apache-2.0 code,
  CC-BY-4.0 rules).
- **Linkage:** PyInstaller's `pyi-bindepend` lists DLL dependencies. Its output
  format is not yet confirmed. PyInstaller is GPL-2.0 with a bundling exception.
  The verifier reads PE import tables with `pefile` (already installed).
- **Leak and boundary:** `import-linter` (BSD-2-Clause) for forbidden-import
  contracts. `gitleaks` (MIT) for secrets. `check-wheel-contents` (MIT) for
  wheel-level rules. None of these detects proprietary source content in a public
  tree, so we need a custom CI check. `tools/check_boundary.py` and the verifier's
  source-leak scan cover part of it.
- **Maya test harnesses** (`mayatest`, AL_USDMaya googletest, maya-usd) are not
  relevant here. This app does not run inside Maya.
- **Gap:** no Qt-specific LGPL checker was found. The Qt LGPL obligations page
  gives no offer duration.

## Owner decisions needed

1. **Google Docs approach:** A, B, or C above. Recommended: test C, then build B.
2. **MSL-1.0 and a commercial desktop build:** confirm that connecting to a
   user-installed engine (never bundling it) is the rule you want. Get a read
   before any paid release.
3. **Python 3.12:** the core needs it. Approve installing 3.12 on this machine, or
   keep the desktop on 3.10 until the core import is needed.
4. **Repo license:** currently `TBD-owner`.
5. **Zotero autostart:** needs your explicit `--yes`.
6. **meridian-outputs:** needs `/mcp` authorization before the Outputs panel can
   use it.
7. **Overleaf login:** you run `node dist/overleaf-login.js login` yourself. The
   desktop never does.

## Incidents

- **Workflow `wsil64j56` stopped early (2026-10-14, 04:19).** The lead stopped it
  because its output file was 0 bytes and that looked like a dead run. It was
  still running. It had written a root-layout package, packaging scripts, and
  `LICENSES/`. Its partial work is kept and is being finished on that layout.
  Lesson: a 0-byte workflow output file does not mean the workflow has stopped.
- **Earlier wrong report.** The lead said the skeleton build had produced no
  files. That was wrong. Files existed when the check ran.
- **Wave 1 builder blocked.** The first brief said "no network downloads" and
  also allowed installing pytest-qt. That contradiction blocked the builder, which
  stopped with no deliverables. Corrected: installing declared dev dependencies
  is approved. No other downloads.
- **Duplicate research.** A narrow research run was relaunched while
  `wwcwaw6tn` was still running. Both finished. Their results are merged above.

## Not done / not verified (say so until it is)

- Wave 1: gates pass, not committed. The verifier's real checks have not run on a
  real bundle, because no PyInstaller build has been made.
- Wave 1 exe path: the embedded CArchive path is untested. Only PYZ files written by
  PyInstaller's own writer were tested.
- Google Docs: no extension exists. The Drive connector has not been tested.
- Autodesk: partly verified (see "Research findings"). No current Maya LGPL
  statement.
- Automated test tools: named from their own pages. Only `pytest-qt` is installed
  (Wave 1).
- Tunnel start and stop: locked by `84d8fd28`.
- Zotero local API: last check did not respond.
- Repo has no commits.
