# Local-trust contract: desktop tunnel and runner controls

Status: CONFIRMED by the owner in chat on 2026-10-22, with Amendments A to E at the end of this file.
Amendment F, also at the end, was accepted by the owner in chat on 2026-10-23.
Amendment G, also at the end, was accepted by the owner in chat on 2026-10-26.
Amendment H, also at the end, was accepted by the owner in chat on 2026-10-26.
Amendment I, also at the end, is PROPOSED on 2026-10-26 and is not accepted. It does not apply until the owner accepts it in chat.
Written by the desktop maintainer at the owner's request (sprint item 84d8fd28 is not in the
repository).

## Rules

1. Start and stop are user actions only, except the launch-time starts in the Amendments: A and B,
   each off by default, and G, on by default from 2026-10-26. Nothing starts on a timer or from any
   request that arrives over the network.
2. Each action shows a confirmation dialog that names the exact command, the scope and the
   working folder. Cancel changes nothing.
3. The command is fixed: `python -m meridian.local_runner` with the subcommands `start`,
   `stop` and `status`, the `--scope` argument, and `--json` where it is used. It is built as an
   argument list. No shell is used, and no text is interpolated into a command. The scope comes
   from a fixed allowlist whose default is `tunnel`. Any other command or scope is refused.
4. Control is loopback-only. The desktop never accepts control requests from the network and
   never listens for them.
5. Timeouts: `start` is bounded at 30 seconds and `stop` at 15 seconds. On timeout the result is
   "not confirmed", and nothing is retried automatically.
6. The desktop logs each start and stop attempt to a local file in its settings folder: time,
   scope, outcome and the pid if reported. The log holds no secrets.
7. Status is read with `status`, without `--json`, because the core's `status` subcommand takes no
   such flag (rule 3 names `--json` only where it is used, and the core never uses it). A stopped,
   unknown or missing state is never displayed as running.
8. Tunnel tokens and credentials are never read, shown or logged by the desktop.
9. Restart is a stop followed by a start, and each is confirmed separately.
10. The controls stay off until the owner confirms this contract. The switch is the settings
    field `tunnel_control_enabled`. It defaulted to `false` until 2026-10-23, and it defaults to
    `true` from then, by owner decision (Amendment F).

## Owner confirmation

- [x] Read this contract and agree to it, with Amendments A to E.
- Confirmed by the owner in chat on: 2026-10-22
- Amendment F accepted by the owner in chat on: 2026-10-23
- Amendment G accepted by the owner in chat on: 2026-10-26
- Amendment H accepted by the owner in chat on: 2026-10-26
- [ ] Amendment I, proposed on 2026-10-26, accepted by the owner in chat on: not yet accepted

Once confirmed, the owner's sprint item 84d8fd28 is recorded done, and the start, stop and
restart controls may be built to this contract.

## Amendment (confirmed by the owner in chat on 2026-10-22)

A. Zotero at launch. The setting `zotero_autostart` (default false) turns this on. When the
   desktop launches with it on, it checks whether Zotero answers on 127.0.0.1:23119. If nothing
   answers, it starts Zotero once, as an argument list with no shell, from the zotero.exe the owner
   confirmed in Settings. The dialog looks for zotero.exe in the standard install folders only (the
   Program Files folders, and Programs\Zotero under %LOCALAPPDATA%). The exact path the dialog named is
   recorded in settings as `confirmed_zotero_exe`. With no confirmed path, the start is skipped and the
   status line says so. The start is logged. The desktop never restarts Zotero on its own.
B. Local server at launch. The setting `local_server_autostart` (default false) turns this on.
   When the desktop launches with it on, it runs the fixed `python -m meridian.local_runner start`
   command for scope `local`, under the same timeouts and log as rule 5 and rule 6. It uses the core
   Python and checkout the owner confirmed, recorded as `confirmed_core_python` and
   `confirmed_core_checkout`, and never the setup saved later. With neither confirmed, the start is
   skipped and the status line says so.
C. Enabling either setting in Settings shows the confirmation dialog from rule 2, naming the exact
   command and scope. Nothing starts until that dialog is confirmed. Confirming it records the exact
   values the dialog showed (the core Python and checkout, or the Zotero path). Saving a core checkout or
   Python path later turns both launch switches off, and the Settings status line says so. The owner
   must confirm them again against the new setup.
D. Tunnel start, stop and restart stay user actions only, and stay behind `tunnel_control_enabled`
   (default true from 2026-10-23, Amendment F), as rule 10 says.
E. Every start in A and B is logged to the same local file as rule 6, with the time, the scope or
   program, the outcome and the pid if reported. The log holds no secrets.

## Amendment F (accepted by the owner in chat on 2026-10-23)

F. Owner decision, 2026-10-23. `tunnel_control_enabled` now defaults to `true`, in place of the
   default `false` of rule 10. The Start, Stop and Restart buttons on the Tunnel tab are on while the
   switch is true and the Meridian core checkout and its Python are set. Each button still asks the
   owner first, every time, in the same dialog from rule 2, and Restart still asks before its start
   (rule 9). Cancel changes nothing. A saved `tunnel_control_enabled: false` turns the buttons off. No
   other part of the contract changes.

## Amendment G (accepted by the owner in chat on 2026-10-26)

G. The LaTeX (Overleaf) engine has its own launch-time start. It is a third launch-time start, separate
   from A and B, and rule 1 allows it on the terms below.
   1. `engine_auto_start` defaults to `true`, by owner decision. When the desktop launches with it on, the
      engine's port is not answering, and the owner has confirmed an engine folder under 3, the desktop
      runs that folder's `start_server_hidden.vbs` once. It runs as an argument list through
      `wscript.exe //B //nologo`, with no shell, and waits for the port for at most the 30 seconds that
      rule 5 allows. The desktop never stops or restarts the engine.
   2. With no confirmed folder, the launch start is skipped and the card says so. With the switch off,
      nothing starts at launch.
   3. Choosing the engine folder in the Overleaf card shows the confirmation dialog from rule 2, which
      names the exact `start_server_hidden.vbs` path and the command. Confirming saves the folder and
      records it as `confirmed_engine_folder`. Cancel saves nothing. The launch start uses only the
      confirmed folder. A folder that is saved without that confirmation, for example by editing the
      settings file by hand, is not used at launch, and the card says why.
   4. Turning `engine_auto_start` on, in Settings or on the card, shows the confirmation dialog from
      rule 2 first. If the owner cancels, the switch stays off. Turning it off needs no dialog.
   5. Every start from this card, manual or at launch, is logged to the same local file as rule 6, with
      the time, the program, the outcome and the pid if reported. The log holds no secrets. The desktop
      never reads, shows or logs the engine's session cookie.
   6. The launch-time session check (`overleaf_auto_check`, default true) reads the engine's session
      status once per launch, and only when the engine is listening. It starts and stops nothing and
      changes nothing, so rules 1 to 9 do not govern it. The owner was told that Overleaf's terms restrict
      automated access, and kept this check on by default.

## Amendment H (accepted by the owner in chat on 2026-10-26)

H. Chat requests and sub-tool runs. This amendment covers three things: a chat asking the desktop to
   start, the desktop running a Meridian sub-tool, and a chat's project binding. It records the owner's
   answers of 2026-10-26. Where those answers left a detail open, the detail is written as the default
   below, and the owner can change it by amendment.
   1. Starting the desktop. A chat may ask the desktop to start. The request does nothing until the owner
      answers the rule 2 dialog, which names "Start Meridian Desktop" and the program path. Cancel changes
      nothing. If the desktop is not running and the core runs on this computer, the core shows its own
      Yes/No dialog, default No, before it launches the desktop. If the core is hosted, the request waits
      until the desktop starts. Nothing starts from a network request on its own (rule 1 stands).
   2. Reading requests, rule 4. The desktop does not open a listener. While it runs, it reads pending chat
      requests from the core over the loopback connection it already uses. Reading starts nothing. A
      request that would act needs a yes in its own dialog. This amends rule 4 to that extent and no more.
   3. Sub-tool runs. The desktop may run any core sub-tool in its registry, through the core's tools/call
      over the loopback connection, with no shell. A read-only tool runs without a dialog. Any other tool
      shows a rule 2 dialog for each call, naming the tool, the project and a plain summary of the
      arguments. Cancel changes nothing. Argument values are never logged.
   4. Excluded tools. A tool that starts a process or an agent is not in the registry. This covers the
      core's enqueue_claude_task and any future tool that spawns a process. Adding one needs an amendment.
      The Zotero bridge update is in the registry as a write tool. It keeps its existing confirmations.
   5. Project binding. A chat has one current project, and the binding can be changed at any time,
      including moving between a project and its subprojects or switching back and forth. A change starts
      nothing, so it needs no dialog. Each change is logged to the rule 6 control log with the time, the
      chat's session id, and the old and new project ids. The desktop passes a project id explicitly on
      every call. It never falls back to a meridian.toml or CLAUDE.local.md file.
   6. Routing. Requests are routed by a fixed keyword table generated from the registry. No model chooses a
      tool. A request that matches no tool, or more than one, shows the matches and does nothing until the
      owner picks one. Every registry entry needs trigger words, and a test fails if one does not.
   7. Hosted tier. Under this amendment the desktop does not read or store an account token, and it makes
      no call to the hosted service. Hosted per-tool calls need a later amendment.
   8. Logging. Each sub-tool run and each start requested by a chat writes one line to the rule 6 control
      log: the time, the tool name or action, the project id, and the outcome. No arguments, tokens or
      tool output are written.
   9. Switch. Chat requests have their own switch, chat_requests_enabled, which defaults to off. Turning it
      on shows the rule 2 dialog first, as G.4 does. While it is off, chat requests are refused and nothing
      is read from the core.

## Amendment I (PROPOSED on 2026-10-26; not accepted by the owner)

Status: proposed, not accepted. This amendment does not apply until the owner accepts it in chat, and the acceptance line
under Owner confirmation is then filled in. Until then rules 1 to 10 and Amendments A to H stand as written, and the
launch-time check in G.6 is unchanged. The desktop carries the hourly check's switch, which is off by default, so nothing
is checked on a timer until the owner turns it on in the dialog.

I. Hourly Overleaf session check. Accepting this amendment means the owner agrees to these terms.
   1. What the check does. While the Home tab is showing, the desktop asks the LaTeX engine once an hour about the saved
      Overleaf session. That is the same one request as a press of the Overleaf card's Check button. The engine sends the
      owner's saved session to Overleaf once. The desktop never reads, shows or logs the session cookie.
   2. Off by default. The setting is overleaf_check_interval_minutes. Its default is 0, which is off. The only other value
      is 60, one hour, and a different interval needs another amendment. A file that holds any other value, or a value
      that is not a whole number, is read as 0, and the settings warning says so.
   3. Turning it on asks first. The hourly check box on the Overleaf card shows a confirmation dialog. The dialog says that
      each check sends the saved session to Overleaf once through the engine, that the check starts nothing, that nothing
      is sent when the engine is not running, and that Overleaf's terms restrict automated access. No, the default, leaves
      the check off and saves nothing. A yes saves 60.
   4. Turning it off needs no dialog. The timer stops at once.
   5. Only while Home is showing. The timer runs only while the Home tab is showing, and it stops when Home is hidden. A
      show does not run a check, and the first check comes an hour after the timer starts.
   6. Nothing else starts. The timer never starts, stops or restarts the engine, never sends a login request and never
      opens a window. Rule 1 still holds.
   7. Engine not running. If the engine is not answering, the check sends nothing to Overleaf, and the card says that the
      engine is not running.
   8. Logging. The hourly check starts nothing, so it writes no line to the control log, as the launch-time check in G.6
      does not.
   9. Unchanged. The Check button, the launch-time check in G.6 and the five-minute Home timer are unchanged. The
      five-minute timer still reads only the cards and the engine's port, and never asks Overleaf.
   10. Terms. The owner accepts that an hourly check is automated access to Overleaf, that Overleaf's terms restrict such
      access, and that turning the check on is the owner's own choice, made in the dialog in item 3.
