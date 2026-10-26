# Desktop router design

Status: proposal for owner review. Not committed. Dated 2026-10-26.

- Desktop: worktree `.claude/worktrees/desk-fix-integrate`, branch `run/desk-fix-integrate` (HEAD a095a05). Paths without a prefix are relative to it.
- Core: the Meridian repository, branch `dev` (cbd262d4). Core paths are read with `git show dev:<path>`. "Core dev" means that branch.

Citations are `path:line` as checked on 2026-10-26. Anything not checked is marked UNVERIFIED and listed in section 14.

## 1. Purpose and summary

A chat should be able to ask Meridian Desktop to start, to run a Meridian sub-tool, or to change the chat's project. Anything that changes state asks the owner first, in a dialog. The desktop opens no port. It reads pending chat requests from the core over the loopback connection it already uses. Routing uses a keyword table generated from a tool registry, so no model picks a tool. Edit protection uses symbol claims in the core, with file claims as the fallback.

Most of the new work is missing: no registry, router, request queue or re-bind action exists yet.

## 2. Decided

Owner decisions accepted 2026-10-26, recorded in Amendment H of `docs/local-trust-contract.md` (heading at line 107).

| # | Decision | Consequence | Source |
|---|---|---|---|
| D1 | "A chat may ask Meridian Desktop to start." A dialog comes first. If the desktop is not running and the core is local, the core shows its own Yes/No dialog before it launches the desktop. Its default answer is open (section 3, F). | Two dialogs. A hosted core waits until the desktop is started by hand. | H.1, line 113 |
| D2 | The desktop runs sub-tools, including write-capable ones. Each write asks, one dialog per call. The Zotero bridge update is included behind its dialog. `enqueue_claude_task` is excluded from the registry. Subagents stay in the chat (the author's reading; H.3 and H.4 do not say this). | Process-spawning tools are excluded from the registry. `enqueue_claude_task` exists on core dev (`meridian/enqueue.py:350`). The bridge update needs a core route that is not on dev (section 8, rows 18-19). | H.3, H.4, lines 121-127 |
| D3 | A chat's project can change at any time, including moves to subprojects and back. Each change is logged. | Each change is logged to the desktop control log, and the binding store is on the desktop (section 4). No core re-bind action exists on dev. Slice 5 (section 11) would add one as the author's design choice; H.5 does not require it. | H.5, line 128 |
| D4 | The desktop always passes an explicit `project_id`. It never falls back to the core checkout's `meridian.toml` or `CLAUDE.local.md`. | Core resolvers do fall back (section 7), so desktop calls never omit the id. | H.5, lines 131-132 |
| D6 | Routing is a fixed keyword table generated from the registry, with hard rules. No model chooses a tool. Zero or several matches show the options and wait for the owner. | Section 6. | H.6, line 133 |

Also in Amendment H: arguments are never logged (H.8, line 138). The desktop makes no hosted calls and reads or stores no account token (H.7, line 136). The switch `chat_requests_enabled` is in H.9 (line 141); its default is open (section 3, D).

## 3. Open decisions

These are not decided. Each has a recommendation only, or none where marked.

**A. How symbol claims are taken for edits.** The owner wants symbol locking in the same worktree as the objective, with file locking as the fallback. The owner rejected a warn-first recommendation. Neither statement has a citable source: `docs/local-trust-contract.md` has no symbol-claim or warn text.
- Option 1 (recommended): auto-claim the symbol at the start of each write and release it at the end. Claims auto-expire after a 2h TTL (AGENTS.md), which bounds a crash.
- Option 2: require a manual `claim_symbol` before each edit. Nothing on dev enforces this.
- Limit: the desktop gates only its own writes. A chat's direct Edit or Serena calls need a hook (section 8, row 6).

**B. Block or warn on a conflicting claim.**
- Block: `claim_symbol` already refuses an overlap with `symbol_conflict` and lists the free symbols (`meridian/db/locks.py`, about 1196-1216). Lost edits are prevented, and a second session waits or picks another symbol.
- Warn: `evaluate_claim_guard` has no warn mode; it returns allow or deny (`meridian/claim_guard.py:48-54`). The warn path on dev is the research-run guard, `guard_mode` "warn", which evaluates and always allows (`meridian/research_run.py:471`, `:516`; `meridian/db/research_runs.py:187`). A symbol-write warn path would have to be built. The edit would proceed, and a lost update is possible.
- Recommendation: block for symbol writes, and offer the free symbols in the dialog. The owner has not chosen. The warn-first rejection in A is not recorded, so it does not settle this.

**C. Whether a manual Start must also use a confirmed folder.** The manual Start uses the saved core folder, not the confirmed one (`meridian_desktop/ui/scope_controls.py:273-277`, `ui/runner_section.py:136`, `ui/runner_actions.py:193-199`, where `core()` returns the settings as saved now). The launch-time start uses confirmed values only (`ui/runner_actions.py:330-336`, contract G.3).
- Option 1: keep the manual Start. The yes in the dialog covers the folder shown.
- Option 2: require the confirmed folder for manual Start, as the launch start does.
- Recommendation: option 2, so both starts follow one rule.

**D. Default of `chat_requests_enabled`.**
- Off (current, H.9): chats are refused until the owner turns the switch on through a dialog.
- On: chats can ask at once. Each start still needs its own dialog.
- Recommendation: keep it off, so the first use comes from the owner.

**E. Per-tool token (D5).** Whether loopback calls need a token is open (section 10; section 14, item 5). Hosted calls stay off under H.7. No recommendation is made.

**F. Default answer of the core's Yes/No dialog (H.1, lines 115-116).** H.1 writes it as No, as the H preamble does for details the owner's answers left open (lines 109-112). It can be changed by amendment. No recommendation is made.

## 4. Architecture

| Piece | Repo / branch | Where | Status |
|---|---|---|---|
| Tool registry (generated) | desktop | new module in `meridian_desktop/` | missing |
| Keyword router | desktop | same | missing |
| Binding store (chat session to project) | desktop | settings folder | missing |
| Request reader | desktop | client on `meridian_desktop/local_client.py` (loopback check line 91) | missing |
| Dialogs | desktop | `ui/runner_actions.py` `ask_control` (line 120) | exists, reusable |
| Control log | desktop | `runner_controls.py` `default_log_path` (line 318) | exists |
| Request queue | core dev | new | missing |
| Re-bind action | core dev | new | missing |
| Symbol claims | core dev | `meridian/db/locks.py` `claim_symbol` (line 1062) | exists |
| Write gate | core dev | `meridian/claim_guard.py` (line 48), called only at `research_run.py:506` and `routes/tunnel.py:4859` | partial |
| Project checks | core dev | `meridian/routes/tasks.py:241` (HTTP); `stdio_handler.py:2458` has none | partial |

**Why the desktop still does not listen.** Contract rule 4 (line 21) bars the desktop from accepting control requests from the network or listening for them. H.2 (line 118) lets it read pending requests as a client over the connection it already uses, which needs no bound socket. No product code binds or listens; the matches for `QLocalServer`, `QTcpServer`, `bind` and `listen` are in tests only (`tests/conftest.py:33`). A listener would need a new amendment.

## 5. Tool registry

Fields: `id` (core tool name), `group`, `title`, `risk` (read or write), `project_required`, `trigger_words`, `excluded` (with a reason).

**Generation.** The desktop reads the core's tool list over loopback (H.3; handler at `meridian/mcp/handler.py:1602`) and keeps a snapshot for offline tests. The core's `readOnlyHint` and `destructiveHint` come from two sets (`meridian/mcp_tools.py:5889-5900`). They are a default only. Every write tool needs a reviewed entry.

**Count.** `_MCP_TOOLS_LIST` (`mcp_tools.py:307`) has 246 literal entries by AST on dev. The brief's 256 was not reproduced (UNVERIFIED).

**Exclusion.** `enqueue_claude_task` is defined at `meridian/enqueue.py:350`. Its stdio Tool registration, `meridian/mcp/stdio_handler.py:1720`, has no annotations. It is absent from `_MCP_TOOLS_LIST` (`mcp_tools.py:307`; 246 literal entries, none for this tool), so the `tools/list` read at `mcp/handler.py:1602` does not return it. The exclusion matters only for the stdio server listing. H.4 excludes it.

**Tests.** A test fails when any entry has empty `trigger_words` (H.6). A test fails when a core tool is in neither the registry nor the exclusion list. Unknown tools default to excluded.

## 6. Keyword router

- Lowercase the request, split it into tokens, and match trigger words as whole tokens or phrases. An exact tool id always matches. No model is called.
- **One match:** show the tool, project and a plain summary of arguments, then the dialog (section 9).
- **Zero matches:** say so. Nothing runs. Listing the groups is proposed; H.6 does not say it.
- **Several matches:** list them. Nothing runs until the owner picks one.

The owner's reason for a fixed table: models forgot Zotero and RunPod. A table keeps every trigger word. Broad words such as "project" will match often, so a test should list trigger words shared by more than one entry for owner review.

## 7. Project binding

**Data.** The binding store maps a chat session id to its current project id. Each change appends one line to the control log (H.5, H.8): time, chat session id, old and new project id. No arguments.

**Subprojects.** The core exposes `set_parent_project` (in `_MCP_TOOLS_LIST`). How the parent link is stored is UNVERIFIED.

**Explicit id (D4).** Core resolvers that fall back to files:
- `meridian/session_brief.py:367-377` `resolve_project_id`: `MERIDIAN_PROJECT_ID`, then `meridian.toml`, then `CLAUDE.local.md`.
- `meridian/toml_config.py:266` `get_default_project_id`: environment, then `meridian.toml` `[project]`.
- `meridian/mcp/handlers/project_tools.py:275-282` `start_session`: explicit id, then `project_name`, then the toml default.

AGENTS.md lists only the environment and toml. The brief's path `meridian/mcp/handlers/mcp/handler.py` does not exist on dev.

**Core facts.** `sessions.project_id` is set on insert (`meridian/db/__init__.py:4034`, `:4122`). `merge_project` is the only update path. It runs `UPDATE {table} SET project_id = ?` over `_MERGE_PROJECT_TABLES` (`:2076`, `:2152`), and that tuple includes `sessions` (`:2078`), so every session of the moved project changes. No per-session re-bind exists. Name searches for `rebind` and `set_session_project` find nothing on dev.

## 8. Overwrite protection and gaps

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Symbol claim refuses overlaps and lists free symbols | exists | core dev `meridian/db/locks.py:1062`; `symbol_conflict` about 1196-1216 |
| 2 | Whole-file lock blocks symbol claims | exists | core dev `locks.py:1107` |
| 3 | File-claim fallback for unparseable files | exists | core dev `locks.py:1128` (`unparseable`), `:358` (`claim_file`) |
| 4 | Auto-claim around every write | missing | no edit or write path auto-claims around each write. The sprint-item claim path claims files and symbols when an item is claimed: `db/sprint_items.py:8914`, `:8961`, `:8993` in `_claim_batch_resource` (`:8838`), and `:5607` in `_to_receiver` (`:5599`). No hook calls claim functions |
| 5 | Claim guard for Edit, Write, Serena | missing | `evaluate_claim_guard` called only at `research_run.py:506`, `routes/tunnel.py:4859` |
| 6 | Hook gate on Serena writes | missing | dev `.claude/settings.json` Edit/Write matchers (lines 26, 100, 110) name other hooks; `meridian_guard.ps1:208` matches find and search only |
| 7 | Symbol write tool pinned for sprint items | exists | core dev `meridian/tool_requirements.py:16`, `meridian/db/sprint_items.py:1040` |
| 8 | Claims checked against project | missing | `claim_file`, `release_file`, `claim_symbol` take no project (`locks.py:358`, `:663`, `:1062`) |
| 9 | Stdio `log_task` checks session project | missing | core dev `stdio_handler.py:2458-2466` calls the DB directly; HTTP checks at `routes/tasks.py:241` |
| 10 | cwd mismatch is stored | missing | `project_tools.py:650` returns `cwd_mismatch_warning` only; first cwd is stored (`:642`) |
| 11 | DEVLOG, DECISIONS, ROADMAP append-only | partial | `meridian/md_anchors.py:51-57` registry, `:311` `apply_append`; no hook blocks direct Write or Edit |
| 12 | ROADMAP version-history rewrite | gap | `meridian/server.py:3777` replaces the marker line; called from `routes/sprint.py:424-427`; no hosted check in the function or caller |
| 13 | CONTRIBUTORS.md and LICENSE | missing | not in `ANCHOR_REGISTRY`; no hook |
| 14 | Pinned decision edits | partial | `meridian/db/__init__.py:10332`; body history kept in `edit_log` (`:10375-10378`); title, category, priority overwritten with no history (`:10379-10384`); delete is hard (`:10468`) |
| 15 | Desktop settings writes | partial | `meridian_desktop/settings_store.py:455` save: temp file and replace, no lock (last writer wins); corrupt file loads as defaults (`:422-454`); `keep_unreadable_copy` (`:526`) runs before only three save sites (`core_locator.py:350`, `keys_section.py:724`, `runner_actions.py:303`); the others, such as `settings_tab.py:611`, do not |
| 16 | Keychain save over an existing name | missing | `meridian_desktop/api_keys.py:117-119` |
| 17 | Tray "Install plugin now" closes Zotero | not found | `meridian_desktop/tray.py:34-36` has Show and Quit only |
| 18 | Zotero bridge update dialog | exists; core route not on dev | `ui/zotero_panel.py:12-13`; route named at `zotero_bridge_client.py:3`; commits `667af0ae`, `0172bb7f` are on `run/` branches |
| 19 | Zotero `.xpi` overwrite | gap, bridge branch | `0172bb7f:meridian/zotero_plugin_install.py:515-556` replaces the file with no copy of the old one |
| 20 | Uninstall removes install folder | gap | `meridian_desktop/install.py:758-775`, no copy; settings sit outside it (`settings_store.py:3-4`) |

## 9. Desktop-function requests from a chat

1. The chat asks through its Meridian tools; the request goes to the core queue (new).
2. The desktop reads pending requests over loopback, only while `chat_requests_enabled` is on (H.9).
3. The desktop routes the request (section 6) and shows the H.3 dialog: tool, project, plain argument summary. Cancel changes nothing. Read-only tools skip the dialog.
4. On yes, the desktop calls `tools/call` over loopback, then writes one control-log line: time, tool, project id, outcome (H.8).

**Desktop not running.** Core on this computer: the core shows its own Yes/No (D1), and its default answer is open (section 3, F). Its dialog mechanism is UNVERIFIED. Hosted core: the request waits. Queue expiry is not yet designed.

## 10. Per-tool token and hosted tier

- **Open (D5):** whether loopback calls need a token. The core's loopback auth was not checked (UNVERIFIED). The Zotero bridge client sends no token to its loopback route (`zotero_bridge_client.py:19-20`), but that route is not on dev.
- **Off (H.7):** calls to usemeridian.us. The desktop does not read or store an account token under this amendment.
- A later amendment must define token storage. What the desktop's `token_store.py` and `secret_store.py` hold for this purpose is UNVERIFIED.

## 11. Slices in build order

Core work follows AGENTS.md: branch from `dev`, never push `main`, run `pixi run test` before and after, keep coverage at 85% or more. The desktop test command is UNVERIFIED.

| # | Slice | Repo / branch | Tests | Acceptance |
|---|---|---|---|---|
| 1 | Registry and router | desktop; no core or contract change | registry from a `tools/list` snapshot; trigger-word test; router one, zero, many; excluded-tool test | "zotero" shows only Zotero entries; zero and many run nothing |
| 2 | Binding store and change log | desktop | A to B to A writes two id-only lines; every request includes `project_id` | switch and back works with no dialog; both logged |
| 3 | Request queue | core dev | create, read, answer; project check on each | a pending request is visible to the reader |
| 4 | Request reader and dialogs | desktop | reader off when switch off; cancel changes nothing; no socket bound | one dialog per request; cancel leaves no change |
| 5 | Re-bind action | core dev | move to subproject and back; event written; `merge_project` unchanged | `sessions.project_id` changes and is logged |
| 6 | Write gate | desktop write path and core | conflict returns `symbol_conflict`; `unparseable` falls back to `claim_file`; claim released on error | two sessions cannot write one symbol in one worktree |
| 7 | Gap fixes | section 8 rows 8-20, one sub-slice each | one test per item, failing before and passing after | each row changes status |
| 8 | Hosted calls | after a new amendment | UNVERIFIED | UNVERIFIED |

## 12. Risks

| Risk | What makes it fail | Guard |
|---|---|---|
| Misrouting | a shared trigger word; a fast wrong click | several-match dialog; shared-word test |
| Registry drift | a new core tool with no entry | default excluded; drift test |
| Writes that bypass the desktop | a chat's own Edit or Serena calls | hook (row 6) |
| Stale claims | a crash before release | 2h TTL; release in `finally` |
| Settings race | two windows save at once | settings lock (slice 7) |
| Cross-project collision | claims keyed by path only | row 8 |
| Stdio write to another project | no project check | row 9 |
| Hosted start never comes | the request waits forever | queue expiry (owner to choose) |
| Dialog fatigue | every write asks; owner clicks yes | group reads and writes (not decided) |

## 13. Verification plan

**Automated, desktop:** registry snapshot, trigger-word, router, binding and log, explicit `project_id`, no-socket and settings keep-copy tests.

**Automated, core:** re-bind, request-queue, stdio project-check and claim project-check tests.

**Commands:** core `pixi run test` before and after each change. The desktop command is UNVERIFIED.

**Manual:** core dialog defaults to No when the desktop is not running; chat switch off refuses requests; start-dialog cancel changes nothing; a second session is blocked on one symbol and offered free symbols; uninstall keeps a copy once slice 7 lands.

## 14. Unverified items

1. The brief's 256 tool count. Dev has 246 literal entries.
2. The tray text "Install plugin now". Not found anywhere checked.
3. The brief's path `meridian/mcp/handlers/mcp/handler.py`. It does not exist on dev.
4. `meridian/tool_catalog.py` (lean profile). Not on dev; commit `176a8051` is not an ancestor of dev.
5. The core's loopback token requirement (D5).
6. How the core shows a Yes/No dialog (D1).
7. Whether a hook can call `claim_symbol` for Serena writes.
8. Storage of `set_parent_project` links.
9. A hosted-tier check for the ROADMAP rewrite (row 12), outside the function and its caller.
10. Bridge route and `.xpi` code: checked only on `667af0ae` and `0172bb7f`, not on other `run/` branches.
11. Existing core claim tests, and the desktop test command and coverage.
12. Keychain behavior on Windows.
13. The queue expiry time (section 9).
