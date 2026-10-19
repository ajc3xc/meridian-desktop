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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The five states the core's get_tunnel_diagnostics documents, in display order.
SERVER_STATES = ("healthy", "stale", "degraded", "quarantined", "restart_required")

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


@dataclass(frozen=True)
class SlotRow:
    """One row of the Tunnel tab. ``state`` is one of SERVER_STATES, RETIRED_LABEL or NO_STATE."""

    slot: str
    state: str
    note: str = ""


def slots_of(snapshot: object) -> Mapping | None:
    """The ``slots`` dict of a snapshot, or None when there is no snapshot or no slots dict."""
    if not isinstance(snapshot, Mapping):
        return None
    slots = snapshot.get("slots")
    return slots if isinstance(slots, Mapping) else None


def _row_for(slot: str, entry: object) -> SlotRow:
    if slot == RETIRED_SLOT:
        return SlotRow(slot, RETIRED_LABEL, RETIRED_NOTE)
    state = entry.get("state") if isinstance(entry, Mapping) else None
    if state is None:
        return SlotRow(slot, NO_STATE, MISSING_NOTE)
    if isinstance(state, str) and state in SERVER_STATES:
        return SlotRow(slot, state)
    if state == DISABLED_STATE:
        return SlotRow(slot, NO_STATE, DISABLED_NOTE)
    return SlotRow(slot, NO_STATE, UNSHOWN_NOTE)


def slot_rows(snapshot: object) -> list[SlotRow]:
    """One row per key in the snapshot's slots dict, in the dict's order.

    A snapshot with no slots dict gives no rows. Each row's state is one of the five server
    states, RETIRED_LABEL for the word slot, or NO_STATE with a note.
    """
    slots = slots_of(snapshot)
    if slots is None:
        return []
    return [_row_for(str(slot), entry) for slot, entry in slots.items()]


__all__ = [
    "DISABLED_NOTE",
    "MISSING_NOTE",
    "NO_STATE",
    "RETIRED_LABEL",
    "RETIRED_NOTE",
    "RETIRED_SLOT",
    "SERVER_STATES",
    "UNSHOWN_NOTE",
    "SlotRow",
    "slot_rows",
    "slots_of",
]
