"""The Search tab's three kinds (code, outputs and research), each read from the local Meridian server and nowhere else.

Pure Python: no Qt import. The base address is the Local URL the window passes in, and every request goes through
the code and outputs clients, which refuse any address that is not on this computer.

- code: code_client.search, one GET of the code search route.
- outputs: outputs_client.search_outputs, one GET of the outputs search route.
- research: outputs_client.list_research_runs, one GET of the research runs route. The server has no research
  search route, so the runs it lists are kept when the id, state or provider contains the query.

A kind whose route the server does not have is shown as "Not connected: the local server has no <kind> search route
yet". Nothing is invented: a kind with no answer shows its state, never a made-up row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from meridian_desktop import code_client, outputs_client
from meridian_desktop.code_client import MAX_QUERY_CHARS
from meridian_desktop.local_client import LocalClientError

KIND_CODE = "code"
KIND_OUTPUTS = "outputs"
KIND_RESEARCH = "research"
KINDS = (KIND_CODE, KIND_OUTPUTS, KIND_RESEARCH)
KIND_LABELS = {KIND_CODE: "Code", KIND_OUTPUTS: "Outputs", KIND_RESEARCH: "Research"}

# Each state a kind's outcome can take.
STATE_RESULTS = "results"
STATE_EMPTY = "empty"
STATE_NOT_CONNECTED = "not_connected"
STATE_NOT_INDEXED = "not_indexed"
STATE_INVALID = "invalid"
STATE_ERROR = "error"

NOT_CONNECTED_ROUTE = "Not connected: the local server has no {} search route yet"
NOT_CONNECTED_STOPPED = (
    "Not connected: the local Meridian server is not running at {}. Start Meridian on this computer, then search again."
)
BLANK_TEXT = "Type a search term first. Nothing was sent to the local Meridian server."
TOO_LONG_TEXT = f"The search text is longer than {MAX_QUERY_CHARS} characters. Shorten it and search again. Nothing was sent."
NOT_INDEXED_TEXT = "Code: the code index has not been built, so there is nothing to search yet."
NO_MATCH_TEXT = '{}: nothing matches "{}".'
MATCH_TEXT = '{}: {} match(es) for "{}".'
RESEARCH_SCOPE_TEXT = " Research searches the {} run(s) the server lists."
ERROR_TEXT = "{}: the search could not be completed. {}"
NO_DETAIL = "Try again."
UNREADABLE_TEXT = "The local server sent an answer the desktop could not read."

# The rows one search shows at most, so a broad query cannot fill the window. The code and outputs clients cap their
# own answers too. Research lists the newest runs the server keeps.
RESULT_LIMIT = 25
RESEARCH_LIMIT = 50

Transport = outputs_client.Transport


@dataclass(frozen=True)
class Row:
    """One result line: its kind, where it is, and what the server said about it. All plain text."""

    kind: str
    where: str
    details: str


@dataclass(frozen=True)
class KindOutcome:
    """One kind's answer: its state, the one line that says it, and its rows (empty unless the state is results)."""

    kind: str
    state: str
    line: str
    rows: tuple[Row, ...] = ()


@dataclass(frozen=True)
class SearchOutcome:
    """One search over the chosen kinds, in the order they were asked for."""

    query: str
    outcomes: tuple[KindOutcome, ...]

    @property
    def rows(self) -> tuple[Row, ...]:
        return tuple(row for outcome in self.outcomes for row in outcome.rows)


def _label(kind: str) -> str:
    return KIND_LABELS[kind]


def _join(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _blank_outcome(kind: str) -> KindOutcome:
    return KindOutcome(kind, STATE_INVALID, BLANK_TEXT)


def _no_route(kind: str) -> KindOutcome:
    """The server answers, but it has no search route for this kind yet."""
    return KindOutcome(kind, STATE_NOT_CONNECTED, NOT_CONNECTED_ROUTE.format(kind))


def _not_running(kind: str, base_url: str) -> KindOutcome:
    """Nothing listens at the Local URL, so nothing is connected."""
    return KindOutcome(kind, STATE_NOT_CONNECTED, NOT_CONNECTED_STOPPED.format(base_url))


def _code(base_url: str, query: str, transport: Transport | None) -> KindOutcome:
    result = code_client.search(base_url, query, limit=RESULT_LIMIT, transport=transport)
    kind = KIND_CODE
    if result.state == code_client.STATE_OK:
        rows = tuple(
            Row(_label(kind), f"{hit.path}:{hit.line}" if hit.line else hit.path, " ".join(hit.snippet.split()))
            for hit in result.hits
        )
        if rows:
            return KindOutcome(kind, STATE_RESULTS, MATCH_TEXT.format(_label(kind), len(rows), query), rows)
        return KindOutcome(kind, STATE_EMPTY, NO_MATCH_TEXT.format(_label(kind), query))
    if result.state == code_client.STATE_ROUTE_MISSING:
        return _no_route(kind)
    if result.state == code_client.STATE_NOT_RUNNING:
        return _not_running(kind, base_url)
    if result.state == code_client.STATE_NOT_INDEXED:
        return KindOutcome(kind, STATE_NOT_INDEXED, NOT_INDEXED_TEXT)
    if result.state == code_client.STATE_BLANK_QUERY:
        return _blank_outcome(kind)
    return KindOutcome(kind, STATE_ERROR, ERROR_TEXT.format(_label(kind), result.detail or NO_DETAIL))


def _outputs(base_url: str, query: str, transport: Transport | None) -> KindOutcome:
    result = outputs_client.search_outputs(base_url, query, limit=RESULT_LIMIT, transport=transport)
    kind = KIND_OUTPUTS
    if result.state == outputs_client.RESULTS:
        rows = tuple(
            Row(
                _label(kind),
                hit.path,
                _join(hit.kind, f"generated by {hit.generating_script}" if hit.generating_script else ""),
            )
            for hit in result.hits
        )
        return KindOutcome(kind, STATE_RESULTS, MATCH_TEXT.format(_label(kind), len(rows), query), rows)
    if result.state == outputs_client.EMPTY:
        return KindOutcome(kind, STATE_EMPTY, NO_MATCH_TEXT.format(_label(kind), query))
    if result.state == outputs_client.MISSING_ROUTE:
        return _no_route(kind)
    if result.state == outputs_client.NOT_RUNNING:
        return _not_running(kind, base_url)
    if result.state == outputs_client.INVALID:
        return KindOutcome(kind, STATE_INVALID, result.message or BLANK_TEXT)
    return KindOutcome(kind, STATE_ERROR, ERROR_TEXT.format(_label(kind), result.message or NO_DETAIL))


def _research(base_url: str, query: str, transport: Transport | None) -> KindOutcome:
    result = outputs_client.list_research_runs(base_url, limit=RESEARCH_LIMIT, transport=transport)
    kind = KIND_RESEARCH
    if result.state == outputs_client.RESULTS:
        needle = query.strip().lower()
        rows = tuple(
            Row(
                _label(kind),
                run.run_id,
                _join(run.state, run.provider, f"started {run.started_at}" if run.started_at else ""),
            )
            for run in result.runs
            if needle in f"{run.run_id} {run.state} {run.provider}".lower()
        )
        scope = RESEARCH_SCOPE_TEXT.format(len(result.runs))
        if rows:
            line = MATCH_TEXT.format(_label(kind), len(rows), query) + scope
            return KindOutcome(kind, STATE_RESULTS, line, rows)
        return KindOutcome(kind, STATE_EMPTY, NO_MATCH_TEXT.format(_label(kind), query) + scope)
    if result.state == outputs_client.EMPTY:
        return KindOutcome(kind, STATE_EMPTY, NO_MATCH_TEXT.format(_label(kind), query))
    if result.state == outputs_client.MISSING_ROUTE:
        return _no_route(kind)
    if result.state == outputs_client.NOT_RUNNING:
        return _not_running(kind, base_url)
    return KindOutcome(kind, STATE_ERROR, ERROR_TEXT.format(_label(kind), result.message or NO_DETAIL))


_READERS = {KIND_CODE: _code, KIND_OUTPUTS: _outputs, KIND_RESEARCH: _research}


def search_kind(
    kind: str,
    base_url: str,
    query: str,
    *,
    transport: Transport | None = None,
) -> KindOutcome:
    """One kind's search. Never raises: a read that fails becomes an outcome that says so."""
    text = " ".join(query.split())
    if not text:
        return _blank_outcome(kind)
    if len(text) > MAX_QUERY_CHARS:
        return KindOutcome(kind, STATE_INVALID, TOO_LONG_TEXT)
    try:
        return _READERS[kind](base_url, text, transport)
    except LocalClientError as exc:
        return KindOutcome(kind, STATE_ERROR, ERROR_TEXT.format(_label(kind), exc.args[0] if exc.args else NO_DETAIL))


def run_search(
    base_url: str,
    query: str,
    kinds: Sequence[str] = KINDS,
    *,
    transports: Mapping[str, Transport | None] | None = None,
) -> SearchOutcome:
    """Search each chosen kind, in order. Every kind gets its own outcome, even when another kind fails."""
    text = " ".join(query.split())
    chosen = tuple(kind for kind in kinds if kind in KIND_LABELS)
    lookup = dict(transports or {})
    outcomes = tuple(search_kind(kind, base_url, text, transport=lookup.get(kind)) for kind in chosen)
    return SearchOutcome(query=text, outcomes=outcomes)


__all__ = [
    "KINDS",
    "KIND_CODE",
    "KIND_LABELS",
    "KIND_OUTPUTS",
    "KIND_RESEARCH",
    "KindOutcome",
    "Row",
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_INVALID",
    "STATE_NOT_CONNECTED",
    "STATE_NOT_INDEXED",
    "STATE_RESULTS",
    "SearchOutcome",
    "run_search",
    "search_kind",
]
