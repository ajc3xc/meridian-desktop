# Local-trust contract: desktop tunnel and runner controls

Status: PENDING OWNER CONFIRMATION. Written by the desktop maintainer at the owner's request
(sprint item 84d8fd28 is not in the repository). Until the owner confirms this contract in
chat, no start, stop or restart control may be built or shown in the desktop.

## Rules

1. Start and stop are user actions only. Nothing starts at app launch, on a timer, or from any
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
7. Status comes from `status --json`. A stopped, unknown or missing state is never displayed as
   running.
8. Tunnel tokens and credentials are never read, shown or logged by the desktop.
9. Restart is a stop followed by a start, and each is confirmed separately.
10. The controls stay off until the owner confirms this contract. The switch is the settings
    field `tunnel_control_enabled`, which defaults to `false`.

## Owner confirmation

- [ ] Read this contract and agree to it.
- Confirmed by the owner in chat on: ____________

Once confirmed, the owner's sprint item 84d8fd28 is recorded done, and the start, stop and
restart controls may be built to this contract.
