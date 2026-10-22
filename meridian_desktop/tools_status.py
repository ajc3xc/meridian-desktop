"""Which Meridian tools each AI client has set up, and which are live right now. Pure Python: no Qt import.

The desktop runs no MCP server itself: the AI clients start them. So this module reads only what the
desktop can check, and changes nothing:

- each client's Meridian entries, from the Meridian core's client setup plan (ClientSetupRunner.check).
  The plan is the only command this module runs. A wrapper around the injected runner refuses any other
  argument list before it reaches the runner, so this read cannot apply or remove an entry.
- the local Meridian server: GET /health through local_client.probe_health, on a loopback address only.
- the tunnel: one read-only status call through tunnel_status.read_tunnel_status.

An AI client starts its own tools, so the desktop cannot see whether one of its entries is running. Those
rows say "Not checked" for live. Every row carries one plain sentence on what to do. No secret or token is
read or kept, and an exception's text is never shown, only its class name.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop import client_setup_runner, local_client, tunnel_status
from meridian_desktop.client_setup_runner import (
    CORE_MODULE,
    IN_PLACE,
    MISSING,
    NOT_READABLE,
    WOULD_CHANGE,
    CheckResult,
    ClientRow,
    ClientSetupRunner,
    CommandOutput,
    CommandRunner,
    unchecked_rows,
)
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.settings_store import SettingsStore

TIME_TEMPLATE = "Time checked: {}."
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The configured and live columns each take one of these. Live never takes NOT_SET.
YES = "yes"
NO = "no"
UNCHECKED = "unchecked"
NOT_SET = "not_set"
COLUMN_TEXT = {YES: "Yes", NO: "No", UNCHECKED: "Not checked", NOT_SET: "Not set"}

# The status word for a row, from its configured and live columns.
STATUS_LIVE = "Live"
STATUS_NOT_LIVE = "Configured but not live"
STATUS_LIVE_UNCHECKED = "Configured, live not checked"
STATUS_NOT_CONFIGURED = "Not configured"
STATUS_UNCHECKED = "Not checked"
STATUS_NOT_SET = "Not set"

ENTRIES_TOOL = "Meridian entries"
THIS_COMPUTER = "This computer"
SERVER_TOOL = "Local Meridian server"
TUNNEL_TOOL = "Tunnel"

LIVE_UNCHECKED_TEXT = "The AI client starts this tool itself, so the desktop cannot see whether it is running."
ENTRY_IN_PLACE_TEXT = "Its settings match the Meridian core's. " + LIVE_UNCHECKED_TEXT
ENTRY_MISSING_TEXT = (
    "Not in {label}'s settings yet. In Settings, use Client setup to apply Meridian's entries for {label}."
)
ENTRY_CHANGED_TEXT = (
    "Its settings differ from the Meridian core's. In Settings, use Client setup to apply Meridian's "
    "entries for {label}."
)
NOT_COVERED_TEXT = (
    "The Meridian core's client setup does not cover {label}, so this panel cannot read its Meridian "
    "entries. Check {label}'s own settings for Meridian tools."
)
NO_RESULT_TEXT = "The Meridian core did not report on {label}. Press Refresh to check again."
CHECK_FAILED_TEXT = "The Meridian setup check failed ({name}). Nothing was written."

SERVER_RUNNING_TEXT = "The local Meridian server answers on this computer."
SERVER_NOT_RUNNING_TEXT = (
    "The local Meridian server is not running. The desktop does not start it. Start it yourself, then press Refresh."
)
SERVER_NOT_MERIDIAN_TEXT = (
    "Something else answers at the local server address, not Meridian. Check the local server address in Settings."
)
SERVER_NO_ANSWER_TEXT = (
    "The check got no usable answer from the local server, so it is not shown as live. Press Refresh to check again."
)
SERVER_NOT_LOOPBACK_TEXT = (
    "{reason} The desktop checks the local server only at a loopback address. "
    "Change the local server address in Settings."
)
SERVER_CHECK_FAILED_TEXT = "The check failed unexpectedly ({name}), so the server is not shown as live."
TUNNEL_NOT_CHECKED_TEXT = "The desktop did not check the tunnel on this read."

_HEALTH_TEXT = {
    local_client.HEALTH_NOT_RUNNING: SERVER_NOT_RUNNING_TEXT,
    local_client.HEALTH_NOT_MERIDIAN: SERVER_NOT_MERIDIAN_TEXT,
}

PLAN_PREFIX = ("-m", CORE_MODULE, "plan")


def status_word(configured: str, live: str) -> str:
    """The status word for one row. Configured decides first, then live."""
    if configured == NOT_SET:
        return STATUS_NOT_SET
    if configured == UNCHECKED:
        return STATUS_UNCHECKED
    if configured == NO:
        return STATUS_NOT_CONFIGURED
    if live == YES:
        return STATUS_LIVE
    if live == NO:
        return STATUS_NOT_LIVE
    return STATUS_LIVE_UNCHECKED


@dataclass(frozen=True)
class ToolRow:
    """One tool's row: where it is, whether it is configured, whether it is live, and what to do."""

    client: str
    tool: str
    configured: str
    live: str
    detail: str

    @property
    def status(self) -> str:
        return status_word(self.configured, self.live)

    def cells(self) -> tuple[str, str, str, str, str, str]:
        """The table cells, in the panel's column order."""
        return (
            self.client,
            self.tool,
            self.status,
            COLUMN_TEXT[self.configured],
            COLUMN_TEXT[self.live],
            self.detail,
        )


@dataclass(frozen=True)
class ToolsStatus:
    """One read of every tool row. ``message`` is the plan's plain line. ``checked_at`` is None if no read finished."""

    rows: tuple[ToolRow, ...]
    message: str = ""
    checked_at: datetime | None = None


def status_line(status: ToolsStatus) -> str:
    """The panel's status line: the plan's message, then the time checked when a read finished."""
    if status.checked_at is None:
        return status.message
    stamp = TIME_TEMPLATE.format(status.checked_at.strftime(TIME_FORMAT))
    return f"{status.message} {stamp}".strip()


class PlanOnlyRefused(RuntimeError):
    """The runner was asked for something other than the core's plan. The call never reaches the runner."""


class PlanOnlyRunner:
    """The injected runner, limited to the core's plan. It keeps each plan's raw output so the entry names can be read.

    The runner's typed result carries only each client's state, not the entry names, so the names are read
    from the plan's own JSON here. Nothing else is kept.
    """

    def __init__(self, run: CommandRunner) -> None:
        self._run = run
        self.outputs: list[tuple[tuple[str, ...], CommandOutput]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: int) -> CommandOutput:
        args = tuple(argv)
        if args[1:4] != PLAN_PREFIX or "--yes" in args:
            raise PlanOnlyRefused("only the Meridian core's plan command is run here")
        output = self._run(list(args), cwd, timeout)
        self.outputs.append((args, output))
        return output


@dataclass(frozen=True)
class _Entries:
    """The Meridian entry names the plan reported for one client, by what the plan would do with them."""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added) | set(self.changed) | set(self.unchanged)))


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _plan_entries(outputs: Sequence[tuple[tuple[str, ...], CommandOutput]]) -> dict[str, _Entries]:
    """Read the entry names from each successful plan's JSON. A plan that does not read is skipped."""
    found: dict[str, _Entries] = {}
    for argv, output in outputs:
        if argv[1:4] != PLAN_PREFIX or output.returncode != 0:
            continue
        try:
            data = json.loads(output.stdout)
        except ValueError:
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("client"), str):
                continue
            changes = item.get("changes")
            changes = changes if isinstance(changes, dict) else {}
            found[item["client"]] = _Entries(
                added=_names(changes.get("added")),
                changed=_names(changes.get("changed")),
                unchanged=_names(changes.get("unchanged")),
            )
    return found


def _entry_row(label: str, name: str, entries: _Entries) -> ToolRow:
    if name in entries.unchanged:
        return ToolRow(label, name, YES, UNCHECKED, ENTRY_IN_PLACE_TEXT)
    if name in entries.changed:
        return ToolRow(label, name, NO, UNCHECKED, ENTRY_CHANGED_TEXT.format(label=label))
    return ToolRow(label, name, NO, UNCHECKED, ENTRY_MISSING_TEXT.format(label=label))


def _covered_rows(row: ClientRow, entries: _Entries | None) -> list[ToolRow]:
    """Rows for one client the core covers. A client the plan could not read says why, and is not configured."""
    label = row.label
    if row.state == NOT_READABLE:
        return [ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, row.detail or NO_RESULT_TEXT.format(label=label))]
    if row.state not in (IN_PLACE, WOULD_CHANGE, MISSING):
        return [ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, NO_RESULT_TEXT.format(label=label))]
    if entries is not None and entries.names:
        return [_entry_row(label, name, entries) for name in entries.names]
    if row.state == IN_PLACE:
        return [ToolRow(label, ENTRIES_TOOL, YES, UNCHECKED, ENTRY_IN_PLACE_TEXT)]
    return [ToolRow(label, ENTRIES_TOOL, NO, UNCHECKED, ENTRY_MISSING_TEXT.format(label=label))]


def _client_rows(check: CheckResult, entries: dict[str, _Entries]) -> list[ToolRow]:
    rows: list[ToolRow] = []
    for client_row in check.rows:
        label = client_row.label
        if not client_row.covered:
            rows.append(ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, NOT_COVERED_TEXT.format(label=label)))
        elif not check.configured:
            rows.append(ToolRow(label, ENTRIES_TOOL, NOT_SET, UNCHECKED, check.message))
        else:
            rows.extend(_covered_rows(client_row, entries.get(client_row.client)))
    return rows


def _local_server_row(settings_store: SettingsStore, probe: Callable[[str], str]) -> ToolRow:
    """The local server's health, checked only at a loopback address. The probe is never called otherwise."""
    url = settings_store.load().settings.local_url
    try:
        local_client.assert_loopback(url)
    except local_client.LocalClientError as exc:
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, NO, UNCHECKED, SERVER_NOT_LOOPBACK_TEXT.format(reason=str(exc)))
    try:
        health = probe(url)
    except Exception as exc:  # noqa: BLE001 - a failed probe is never live; only the class name is kept
        failure = SERVER_CHECK_FAILED_TEXT.format(name=exc.__class__.__name__)
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, NO, failure)
    if health == local_client.HEALTH_RUNNING:
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, YES, SERVER_RUNNING_TEXT)
    return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, NO, _HEALTH_TEXT.get(health, SERVER_NO_ANSWER_TEXT))


def _tunnel_row(status_call: Callable[[str], RunnerResult] | None, clock: Callable[[], datetime]) -> ToolRow:
    """The tunnel from one read-only status call. Only RUNNING is live; an unknown answer is never shown as live."""
    if status_call is None:
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, UNCHECKED, UNCHECKED, TUNNEL_NOT_CHECKED_TEXT)
    result = tunnel_status.read_tunnel_status(status_call, clock=clock)
    if result.state == tunnel_status.RUNNING:
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, YES, YES, result.reason)
    if result.state == tunnel_status.NOT_RUNNING:
        detail = " ".join((result.reason, tunnel_status.DEPENDENCY_TEXT, tunnel_status.BY_HAND_NOT_STARTED))
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, YES, NO, detail)
    detail = " ".join((result.reason, tunnel_status.BY_HAND_CHECK))
    return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, UNCHECKED, UNCHECKED, detail)


def read_tools_status(
    settings_store: SettingsStore,
    *,
    run: CommandRunner | None = None,
    probe: Callable[[str], str] = local_client.probe_health,
    tunnel_status_call: Callable[[str], RunnerResult] | None = None,
    clock: Callable[[], datetime] = datetime.now,
) -> ToolsStatus:
    """One read of every tool row. Never raises: a part that fails becomes a row that says so.

    ``run`` is the injected process runner (run_process by default). ``probe`` takes a base address and
    returns a health word (local_client.probe_health by default). ``tunnel_status_call`` is
    RunnerClient.status, or a fake. Nothing here starts, stops, installs or writes.
    """
    plan = PlanOnlyRunner(run if run is not None else client_setup_runner.run_process)
    try:
        check = ClientSetupRunner(settings_store, run=plan).check()
    except Exception as exc:  # noqa: BLE001 - the other rows must still be shown
        check = CheckResult(rows=unchecked_rows(), message=CHECK_FAILED_TEXT.format(name=exc.__class__.__name__))
    rows = _client_rows(check, _plan_entries(plan.outputs))
    rows.append(_local_server_row(settings_store, probe))
    rows.append(_tunnel_row(tunnel_status_call, clock))
    return ToolsStatus(rows=tuple(rows), message=check.message, checked_at=clock())


__all__ = [
    "COLUMN_TEXT",
    "ENTRIES_TOOL",
    "NO",
    "NOT_SET",
    "PlanOnlyRefused",
    "PlanOnlyRunner",
    "STATUS_LIVE",
    "STATUS_LIVE_UNCHECKED",
    "STATUS_NOT_CONFIGURED",
    "STATUS_NOT_LIVE",
    "STATUS_NOT_SET",
    "STATUS_UNCHECKED",
    "ToolRow",
    "ToolsStatus",
    "UNCHECKED",
    "YES",
    "read_tools_status",
    "status_line",
    "status_word",
]
