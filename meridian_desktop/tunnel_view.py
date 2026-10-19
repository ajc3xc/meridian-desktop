"""Tunnel tab view model: one state row per slot, from a get_tunnel_diagnostics snapshot. Pure Python: no Qt import.

The snapshot is the dict the core's get_tunnel_diagnostics tool returns. Its ``slots`` dict has
one entry per slot, keyed by the slot name, and each entry carries a ``state`` string. The
caller hands the snapshot in. Nothing here makes a request.

The state column shows only the five server states, in SERVER_STATES: healthy, stale,
degraded, quarantined and restart_required. The word slot is retired in the core, so its row
reads RETIRED_LABEL whatever the server says about it. The core's code also labels a slot that
is turned off in the dashboard as "disabled". The tool description lists five states and does
not name that one, so this view does not show it as a state. Its cell holds NO_STATE and its
note says why. Any other value gets the same treatment.

Two more rules keep a saved setting from reading as a running slot:

- A slot with no process running (process_active is not True) never shows "healthy". This
  covers a slot the dashboard has on (dashboard_configured.enabled true) but that is not
  running, so the saved setting cannot make it look active. Its state cell holds NO_STATE and
  its note says no process is running. A missing process_active counts as not running. The
  other server states stay as the server sent them, because each already says something other
  than "running".
- A healthy row carries its health_basis and health_age_seconds beside it, so the page shows
  how the healthy flag was derived and how old it is. Other rows carry no health text.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

# The five states the core's get_tunnel_diagnostics documents, in display order.
SERVER_STATES = ("healthy", "stale", "degraded", "quarantined", "restart_required")
HEALTHY_STATE = "healthy"

# The slot the core retired. Its row reads RETIRED_LABEL whatever state the server reports.
RETIRED_SLOT = "word"
RETIRED_LABEL = "retired"

# The state cell for a row that has no state to show.
NO_STATE = "-"

DISABLED_STATE = "disabled"
RETIRED_NOTE = "Retired. This slot is no longer used."
DISABLED_NOTE = "Turned off in the dashboard."
UNSHOWN_NOTE = "The server reported a state this view does not show."
MISSING_NOTE = "The server sent no state for this slot."
NOT_RUNNING_NOTE = "No process is running for this slot, so it is not shown as healthy."

# The health cell text when the snapshot gives no value for it.
NOT_REPORTED = "not reported"


@dataclass(frozen=True)
class SlotRow:
    """One row of the Tunnel tab. ``state`` is one of SERVER_STATES, RETIRED_LABEL or NO_STATE."""

    slot: str
    state: str
    note: str = ""


@dataclass(frozen=True)
class SlotDetail:
    """A row plus the health text shown beside it. The health text is empty unless the row is healthy."""

    row: SlotRow
    health_basis: str = ""
    health_age: str = ""


def slots_of(snapshot: object) -> Mapping | None:
    """The ``slots`` dict of a snapshot, or None when there is no snapshot or no slots dict."""
    if not isinstance(snapshot, Mapping):
        return None
    slots = snapshot.get("slots")
    return slots if isinstance(slots, Mapping) else None


def _process_running(entry: Mapping) -> bool:
    """True only when the snapshot says a process is running. A missing value counts as not running."""
    return entry.get("process_active") is True


def _row_for(slot: str, entry: object) -> SlotRow:
    if slot == RETIRED_SLOT:
        return SlotRow(slot, RETIRED_LABEL, RETIRED_NOTE)
    state = entry.get("state") if isinstance(entry, Mapping) else None
    if state is None:
        return SlotRow(slot, NO_STATE, MISSING_NOTE)
    if isinstance(state, str) and state in SERVER_STATES:
        if state == HEALTHY_STATE and not _process_running(entry):
            return SlotRow(slot, NO_STATE, NOT_RUNNING_NOTE)
        return SlotRow(slot, state)
    if state == DISABLED_STATE:
        return SlotRow(slot, NO_STATE, DISABLED_NOTE)
    return SlotRow(slot, NO_STATE, UNSHOWN_NOTE)


def _basis_text(value: object) -> str:
    """The health_basis label as sent, or NOT_REPORTED when it is missing, blank or not text."""
    return value if isinstance(value, str) and value.strip() else NOT_REPORTED


def _age_text(value: object) -> str:
    """Whole seconds for a finite number, or NOT_REPORTED. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return NOT_REPORTED
    try:
        seconds = float(value)
    except OverflowError:
        return NOT_REPORTED
    if not math.isfinite(seconds):
        return NOT_REPORTED
    return f"{seconds:.0f}"


def _detail_for(slot: str, entry: object) -> SlotDetail:
    row = _row_for(slot, entry)
    # Only a healthy row shows its health facts. A row that was turned from healthy into
    # NO_STATE for a slot that is not running shows none either.
    if row.state != HEALTHY_STATE or not isinstance(entry, Mapping):
        return SlotDetail(row)
    return SlotDetail(
        row,
        _basis_text(entry.get("health_basis")),
        _age_text(entry.get("health_age_seconds")),
    )


def slot_details(snapshot: object) -> list[SlotDetail]:
    """One detail per key in the snapshot's slots dict, in the dict's order.

    A snapshot with no slots dict gives no details. Each row's state is one of the five server
    states, RETIRED_LABEL for the word slot, or NO_STATE with a note.
    """
    slots = slots_of(snapshot)
    if slots is None:
        return []
    return [_detail_for(str(slot), entry) for slot, entry in slots.items()]


def slot_rows(snapshot: object) -> list[SlotRow]:
    """One row per key in the snapshot's slots dict, in the dict's order.

    A snapshot with no slots dict gives no rows. Each row's state is one of the five server
    states, RETIRED_LABEL for the word slot, or NO_STATE with a note.
    """
    return [detail.row for detail in slot_details(snapshot)]


__all__ = [
    "DISABLED_NOTE",
    "MISSING_NOTE",
    "NOT_REPORTED",
    "NOT_RUNNING_NOTE",
    "NO_STATE",
    "RETIRED_LABEL",
    "RETIRED_NOTE",
    "RETIRED_SLOT",
    "SERVER_STATES",
    "UNSHOWN_NOTE",
    "SlotDetail",
    "SlotRow",
    "slot_details",
    "slot_rows",
    "slots_of",
]
