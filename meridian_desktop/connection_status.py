"""Is the desktop connected to Meridian in the mode the owner chose? Pure Python: no Qt import.

The mode bar on the main window (ui/main_window.py) shows one line for the chosen mode, with the time it was checked:

- Local: the local Meridian server at the Local URL, through the probe the Projects tab uses (DataSources.probe, which
  is local_client.probe_health in the app). No token is read.
- Hosted: one GET to the hosted host's /health with no token, then one GET to /projects with the saved token. That
  second read is hosted_client.list_projects, the same read the Projects tab makes in hosted mode. With no token saved,
  no request is made at all.
- Tunnel: the tunnel's one read-only status call, the same read the top bar makes (tunnel_status.read_tunnel_status).
  Its line is built by tunnel_result from that read, so the tunnel is read once per check.

A check never starts, stops or restarts anything, and never writes. A line shows the host and port of an address only,
with no path, query, user name or password. It never shows a token, a response body or a raw exception.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from meridian_desktop import hosted_client, local_client
from meridian_desktop.tunnel_status import NOT_RUNNING as TUNNEL_NOT_RUNNING
from meridian_desktop.tunnel_status import RUNNING as TUNNEL_RUNNING
from meridian_desktop.tunnel_status import UNKNOWN as TUNNEL_UNKNOWN
from meridian_desktop.tunnel_status import TunnelStatus

LOCAL = "local"
HOSTED = "hosted"
TUNNEL = "tunnel"

# States of the local check. The probe's own words (local_client.HEALTH_*) are used for everything it reports. The one
# extra state is an address that is not on this computer, which is refused before any probe runs.
LOCAL_BAD_ADDRESS = "bad_address"

# States of the hosted check. The two reads are in order: /health, then /projects with the token.
HOSTED_CONNECTED = "connected"
HOSTED_NO_TOKEN = "no_token"
HOSTED_REFUSED = "refused"
HOSTED_UNREACHABLE = "unreachable"
HOSTED_BAD_TOKEN = "bad_token"
HOSTED_OTHER = "other"
HOSTED_BAD_ADDRESS = "bad_address"

# The fixed texts. A line is one of these, with the address or host filled in where it has one.
LOCAL_RUNNING_TEXT = "Local server at {address}: running."
LOCAL_NOT_RUNNING_TEXT = "Local server at {address}: not running. Start Meridian on this computer."
LOCAL_NO_ANSWER_TEXT = "Local server at {address}: did not answer in time."
LOCAL_NOT_MERIDIAN_TEXT = "Local server at {address}: answered, but it is not Meridian."
LOCAL_BAD_ADDRESS_TEXT = "Local server: the Local URL in Settings is not on this computer. Check it in Settings."

HOSTED_CONNECTED_TEXT = "Hosted at {host}: connected as the saved account."
HOSTED_NO_TOKEN_TEXT = "Hosted at {host}: no token is saved. Add it in Settings."
HOSTED_REFUSED_TEXT = "Hosted at {host}: the token was refused. Check it in Settings."
HOSTED_UNREACHABLE_TEXT = "Hosted at {host}: not reachable."
HOSTED_BAD_TOKEN_TEXT = (
    "Hosted at {host}: the saved token has characters the desktop cannot send. Paste it again in Settings."
)
HOSTED_OTHER_TEXT = "Hosted at {host}: answered, but the projects could not be read."
HOSTED_BAD_ADDRESS_TEXT = "Hosted: the hosted URL in Settings is not valid. Check it in Settings."

TUNNEL_RUNNING_TEXT = "Tunnel: running."
TUNNEL_NOT_RUNNING_TEXT = "Tunnel: not running."
TUNNEL_UNKNOWN_TEXT = "Tunnel: status unknown."

CHECKING_TEXT = "Checking the connection..."
CHECK_FAILED_TEXT = "The connection check did not finish. Press Test connection to check again."
CHECKED_TEMPLATE = "Checked at {}."
TIME_FORMAT = "%H:%M:%S"

_LOCAL_LINES = {
    local_client.HEALTH_RUNNING: LOCAL_RUNNING_TEXT,
    local_client.HEALTH_NOT_RUNNING: LOCAL_NOT_RUNNING_TEXT,
    local_client.HEALTH_ERROR: LOCAL_NO_ANSWER_TEXT,
    local_client.HEALTH_NOT_MERIDIAN: LOCAL_NOT_MERIDIAN_TEXT,
    LOCAL_BAD_ADDRESS: LOCAL_BAD_ADDRESS_TEXT,
}
_HOSTED_LINES = {
    HOSTED_CONNECTED: HOSTED_CONNECTED_TEXT,
    HOSTED_NO_TOKEN: HOSTED_NO_TOKEN_TEXT,
    HOSTED_REFUSED: HOSTED_REFUSED_TEXT,
    HOSTED_UNREACHABLE: HOSTED_UNREACHABLE_TEXT,
    HOSTED_BAD_TOKEN: HOSTED_BAD_TOKEN_TEXT,
    HOSTED_OTHER: HOSTED_OTHER_TEXT,
    HOSTED_BAD_ADDRESS: HOSTED_BAD_ADDRESS_TEXT,
}
_TUNNEL_LINES = {
    TUNNEL_RUNNING: TUNNEL_RUNNING_TEXT,
    TUNNEL_NOT_RUNNING: TUNNEL_NOT_RUNNING_TEXT,
    TUNNEL_UNKNOWN: TUNNEL_UNKNOWN_TEXT,
}


@dataclass(frozen=True)
class ConnectionResult:
    """One finished check. ``mode`` is the mode it was run for, ``state`` one of that mode's state words.

    ``address`` is the host and port the line shows: the Local URL or the hosted host. It is empty for Tunnel, and
    for an address that was refused. ``checked_at`` is when the check finished.
    """

    mode: str
    state: str
    address: str = ""
    checked_at: datetime | None = None


def shown_address(url: object) -> str:
    """The host, and the port when the URL names one: the only part of an address a status line shows.

    A path, a query, a fragment, a user name and a password are left out. An IPv6 host is in brackets. Returns ""
    when the URL is not text or has no host, so nothing that is not an address is ever shown.
    """
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    shown = f"[{host}]" if ":" in host else host
    return shown if port is None else f"{shown}:{port}"


def _has_text(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip())


def local_line(result: ConnectionResult) -> str:
    """The one line for a local result. Pure: it turns the probe's answer into fixed text."""
    return _LOCAL_LINES.get(result.state, CHECK_FAILED_TEXT).format(address=result.address)


def hosted_line(result: ConnectionResult) -> str:
    """The one line for a hosted result. Pure: it turns the two reads' answer into fixed text."""
    return _HOSTED_LINES.get(result.state, CHECK_FAILED_TEXT).format(host=result.address)


def tunnel_line(result: ConnectionResult) -> str:
    """The one line for a tunnel result. Pure: it turns the tunnel status into fixed text."""
    return _TUNNEL_LINES.get(result.state, TUNNEL_UNKNOWN_TEXT)


def status_line(result: ConnectionResult) -> str:
    """The one line for a result in its own mode, with no time. Any other mode gets the failed-check text."""
    if result.mode == LOCAL:
        return local_line(result)
    if result.mode == HOSTED:
        return hosted_line(result)
    if result.mode == TUNNEL:
        return tunnel_line(result)
    return CHECK_FAILED_TEXT


def status_text(result: ConnectionResult) -> str:
    """The line the mode bar shows: the status line, then the time the check finished when it is known."""
    line = status_line(result)
    if result.checked_at is None:
        return line
    return f"{line} {CHECKED_TEMPLATE.format(result.checked_at.strftime(TIME_FORMAT))}"


def probe_local(
    local_url: str,
    probe: Callable[[str], str],
    *,
    clock: Callable[[], datetime] = datetime.now,
) -> ConnectionResult:
    """Check the local Meridian server at ``local_url`` with ``probe``, which takes the URL and returns a HEALTH_* word.

    ``probe`` is the same callable the Projects tab uses for its one /health check (DataSources.probe). An address
    that is not on this computer is refused before the probe runs, so no request is made for it. A probe that raises
    did not answer, and is shown as one.
    """
    try:
        local_client.assert_loopback(local_url)
    except local_client.LocalClientError:
        return ConnectionResult(LOCAL, LOCAL_BAD_ADDRESS, "", clock())
    try:
        state = probe(local_url)
    except Exception:  # noqa: BLE001 - a probe that raises did not get an answer; its text is never shown
        state = local_client.HEALTH_ERROR
    return ConnectionResult(LOCAL, state, shown_address(local_url), clock())


def _hosted_reachable(base: str, transport, timeout: float) -> bool:
    """Step one: GET /health with no token. True when any reply came back, False on a timeout or a lost connection."""
    try:
        hosted_client.health_status(base, transport=transport, timeout=timeout)
    except Exception:  # noqa: BLE001 - a failed health read means no answer; its text is never shown
        return False
    return True


def _hosted_read_state(base: str, token: str, transport, timeout: float) -> str:
    """Step two: GET /projects with the saved token, and say what the reply means. Only the state is kept."""
    try:
        hosted_client.list_projects(base, token, transport=transport, timeout=timeout)
    except hosted_client.BadToken:
        return HOSTED_BAD_TOKEN
    except (hosted_client.NotSignedIn, hosted_client.Forbidden):
        return HOSTED_REFUSED
    except (hosted_client.HostedTimeout, hosted_client.HostedUnreachable):
        return HOSTED_UNREACHABLE
    except hosted_client.HostedError:
        return HOSTED_OTHER
    except Exception:  # noqa: BLE001 - a failure with no HTTP answer is shown as not reachable
        return HOSTED_UNREACHABLE
    return HOSTED_CONNECTED


def probe_hosted(
    hosted_url: str,
    token: str | None,
    *,
    transport: hosted_client.Transport | None = None,
    timeout: float = hosted_client.HOSTED_TIMEOUT,
    clock: Callable[[], datetime] = datetime.now,
) -> ConnectionResult:
    """Check the hosted account: one GET of /health with no token, then one GET of /projects with ``token``.

    Order of the checks, and when each one sends a request:
    1. A hosted URL that may not be used (see hosted_client.validate_base_url) is refused, and nothing is sent.
    2. No token saved: nothing is sent. The line says to add the token in Settings.
    3. /health with no token. A timeout or a lost connection is "not reachable", and the token is not sent.
    4. /projects with the bearer token, the same read as the Projects tab in hosted mode. Its reply sets the state:
       a 200 is connected, a 401 or 403 is a refused token, and any other failure says so without its text.

    ``transport`` is injectable, so tests send nothing. The token and the reply body never reach the result.
    """
    try:
        base = hosted_client.validate_base_url(hosted_url)
    except hosted_client.BadBaseUrl:
        return ConnectionResult(HOSTED, HOSTED_BAD_ADDRESS, "", clock())
    address = shown_address(base)
    if not _has_text(token):
        return ConnectionResult(HOSTED, HOSTED_NO_TOKEN, address, clock())
    if not _hosted_reachable(base, transport, timeout):
        return ConnectionResult(HOSTED, HOSTED_UNREACHABLE, address, clock())
    state = _hosted_read_state(base, token, transport, timeout)
    return ConnectionResult(HOSTED, state, address, clock())


def tunnel_result(status: TunnelStatus) -> ConnectionResult:
    """The result for one finished tunnel status read (tunnel_status.read_tunnel_status). Pure: no call is made."""
    return ConnectionResult(TUNNEL, status.state, "", status.checked_at)


__all__ = [
    "CHECKING_TEXT",
    "CHECK_FAILED_TEXT",
    "CHECKED_TEMPLATE",
    "HOSTED",
    "HOSTED_BAD_ADDRESS",
    "HOSTED_BAD_TOKEN",
    "HOSTED_CONNECTED",
    "HOSTED_NO_TOKEN",
    "HOSTED_OTHER",
    "HOSTED_REFUSED",
    "HOSTED_UNREACHABLE",
    "LOCAL",
    "LOCAL_BAD_ADDRESS",
    "TUNNEL",
    "ConnectionResult",
    "hosted_line",
    "local_line",
    "probe_hosted",
    "probe_local",
    "shown_address",
    "status_line",
    "status_text",
    "tunnel_line",
    "tunnel_result",
]
