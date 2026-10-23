# Local-trust contract: desktop tunnel and runner controls

Status: CONFIRMED by the owner in chat on 2026-10-22, with the Amendment at the end of this file.
Written by the desktop maintainer at the owner's request (sprint item 84d8fd28 is not in the
repository).

## Rules

1. Start and stop are user actions only, except the two launch-time starts in the Amendment,
   each off by default. Nothing starts on a timer or from any request that arrives over the network.
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
7. Status comes from `status --json`. A stopped, unknown or missing state is never displayed as
   running.
8. Tunnel tokens and credentials are never read, shown or logged by the desktop.
9. Restart is a stop followed by a start, and each is confirmed separately.
10. The controls stay off until the owner confirms this contract. The switch is the settings
    field `tunnel_control_enabled`, which defaults to `false`.

## Owner confirmation

- [x] Read this contract and agree to it, with the Amendment.
- Confirmed by the owner in chat on: 2026-10-22

Once confirmed, the owner's sprint item 84d8fd28 is recorded done, and the start, stop and
restart controls may be built to this contract.

## Amendment (confirmed by the owner in chat on 2026-10-22)

A. Zotero at launch. The setting `zotero_autostart` (default false) turns this on. When the
   desktop launches with it on, it checks whether Zotero answers on 127.0.0.1:23119. If nothing
   answers, it starts Zotero once, as an argument list with no shell, from the path in settings
   or the standard install folder. The start is logged. The desktop never restarts Zotero on its own.
B. Local server at launch. The setting `local_server_autostart` (default false) turns this on.
   When the desktop launches with it on, it runs the fixed `python -m meridian.local_runner start`
   command for scope `local`, under the same timeouts and log as rule 5 and rule 6.
C. Enabling either setting in Settings shows the confirmation dialog from rule 2, naming the exact
   command and scope. Nothing starts until that dialog is confirmed.
D. Tunnel start, stop and restart stay user actions only, and stay behind `tunnel_control_enabled`
   (default false), as rule 10 says.
E. Every start in A and B is logged to the same local file as rule 6, with the time, the scope or
   program, the outcome and the pid if reported. The log holds no secrets.
