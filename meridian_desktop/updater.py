"""Launch-version auto update: find the newest published desktop release, verify it, and stage it beside the app.

Pure Python, no Qt import. Nothing unverified is ever run:

1. fetch_latest reads the latest release from GitHub. It needs two assets, MeridianDesktop-<version>.zip and
   SHA256SUMS, and the version must be plain dotted integers.
2. The zip is downloaded into a scratch folder. Its SHA-256 must match its line in SHA256SUMS, and it must be
   under MAX_DOWNLOAD_BYTES. A failure installs nothing.
3. install_update extracts the zip into versions/<version>.partial. Entries that are absolute, carry a drive or
   a stream name, contain "..", or are symbolic links are refused. MeridianDesktop.exe must be in the result.
   The folder is then renamed to versions/<version>, and the shortcuts are pointed at its exe.

Every request goes through an injectable transport, and urllib never follows a redirect. Each hop is checked
against the https host allowlist before it is requested. Tests pass a fake transport, so they make no network call.

Layout under install_root:

    versions/<version>/MeridianDesktop.exe   installed versions. The current and the previous one are kept.
    versions/<version>.partial               an extraction in progress. Removed on any failure.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from meridian_desktop.version import APP_VERSION

REPO = "ajc3xc/meridian-desktop"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ALLOWED_HOSTS = frozenset(
    {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
EXE_NAME = "MeridianDesktop.exe"
VERSIONS_DIR = "versions"
SUMS_NAME = "SHA256SUMS"
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024  # the release zip
MAX_SMALL_BYTES = 1024 * 1024  # the release JSON and SHA256SUMS
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 30.0
DEV_RUN_REASON = "dev run: updates are not applied"
USER_AGENT = f"MeridianDesktop/{APP_VERSION} (updater)"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 64 * 1024
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_SUM_LINE = re.compile(r"([0-9a-fA-F]{64})[ \t]+\*?(\S.*)")


class UpdateError(Exception):
    """An update step failed. The message is plain text that can be shown to the user."""


class Refused(UpdateError):
    """The update is refused. The installed app is left as it was."""


class TooLarge(Refused):
    """A download is over its size cap."""


class Offline(UpdateError):
    """GitHub could not be reached, or it is failing for now. Try again later."""


Status = Literal["up_to_date", "update_staged", "refused", "offline"]


@dataclass(frozen=True)
class UpdateResult:
    status: Status
    version: str | None = None  # the release version, for up_to_date and update_staged
    reason: str = ""  # plain text, for refused and offline


@dataclass(frozen=True)
class Release:
    version: str
    zip_name: str
    zip_url: str
    zip_size: int | None
    sums_url: str


@dataclass(frozen=True)
class Reply:
    """One HTTP reply. The body is empty when a download went to a file, and for every redirect or error."""

    status: int
    headers: Mapping[str, str]  # names in lower case
    body: bytes


# A transport takes a URL, the most bytes it may return, and an optional file for a 2xx body to stream into.
# It returns the reply without following redirects. It raises TooLarge when the body is over the cap, and
# OSError, http.client.HTTPException or ValueError for a network fault.
Transport = Callable[[str, int, Path | None], Reply]
# A shortcut writer repoints one shortcut at the exe it is given. The caller binds the shortcut's own .lnk path.
ShortcutWriter = Callable[[Path], None]


class _RepointFailed(Exception):
    def __init__(self, undone: bool) -> None:
        super().__init__("a shortcut could not be written")
        self.undone = undone


# --- versions -------------------------------------------------------------------------------------


def parse_version(text: object) -> tuple[int, ...]:
    """Return the integer parts of a plain dotted version such as 0.2.0. Anything else is refused."""
    if not isinstance(text, str) or not _VERSION.fullmatch(text):
        raise Refused("A version must be plain dotted numbers such as 0.2.0.")
    return tuple(int(part) for part in text.split("."))


def is_newer(candidate: str, current: str) -> bool:
    """True when candidate is a higher version than current. Parts compare as integers, so 0.10.0 > 0.9.0."""
    a = parse_version(candidate)
    b = parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# --- URLs and the release ---------------------------------------------------------------------------


def require_allowed_url(url: str) -> None:
    """Refuse any URL that is not https on an allowed GitHub host, or that carries a user name or password."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("A download address is not valid.") from None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or host not in ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise Refused("A download address is not an allowed https GitHub address.")


def _fetch(transport: Transport, url: str, max_bytes: int, dest: Path | None = None) -> Reply:
    """Fetch url, following redirects one hop at a time. Each hop is checked before the transport is asked for it."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        require_allowed_url(current)
        try:
            reply = transport(current, max_bytes, dest)
        except (OSError, http.client.HTTPException, ValueError):
            # A fault from the network. The original exception is not attached, so no request detail leaks.
            raise Offline("GitHub could not be reached. Try again later.") from None
        if reply.status in REDIRECT_STATUSES:
            location = reply.headers.get("location", "")
            if not location:
                raise Refused("GitHub sent a redirect with no address.")
            current = urllib.parse.urljoin(current, location)
            continue
        return reply
    raise Refused("The download was redirected too many times.")


def _require_ok(reply: Reply, what: str) -> None:
    if 200 <= reply.status < 300:
        return
    if reply.status == 429 or reply.status >= 500:
        raise Offline("GitHub is not available right now. Try again later.")
    raise Refused(f"GitHub did not return {what} (HTTP {reply.status}).")


def fetch_latest(transport: Transport) -> Release:
    """Read the latest published release. Refuse it unless it has the zip and SHA256SUMS assets."""
    reply = _fetch(transport, LATEST_RELEASE_URL, MAX_SMALL_BYTES)
    _require_ok(reply, "the latest release")
    try:
        data = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("The latest release could not be read.") from None
    if not isinstance(data, dict) or data.get("draft") is True:
        raise Refused("The latest release is not a published release.")
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise Refused("The latest release has no version tag.")
    version = tag[1:] if tag.startswith("v") else tag
    parse_version(version)
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise Refused("The latest release has no asset list.")
    by_name: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            if asset["name"] in by_name:
                raise Refused("The latest release lists an asset twice.")
            by_name[asset["name"]] = asset
    zip_name = f"MeridianDesktop-{version}.zip"
    zip_asset = by_name.get(zip_name)
    sums_asset = by_name.get(SUMS_NAME)
    if zip_asset is None:
        raise Refused(f"The latest release has no {zip_name} asset.")
    if sums_asset is None:
        raise Refused("The latest release has no SHA256SUMS asset.")
    zip_url = zip_asset.get("browser_download_url")
    sums_url = sums_asset.get("browser_download_url")
    if not isinstance(zip_url, str) or not isinstance(sums_url, str):
        raise Refused("The latest release has an asset with no download address.")
    size = zip_asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        size = None
    elif size > MAX_DOWNLOAD_BYTES:
        raise TooLarge("The release zip is over the size limit.")
    return Release(version=version, zip_name=zip_name, zip_url=zip_url, zip_size=size, sums_url=sums_url)


# --- verification -----------------------------------------------------------------------------------


def expected_sha256(sums_text: str, file_name: str) -> str:
    """Return the SHA-256 that SHA256SUMS lists for file_name. The name must be listed exactly once."""
    found = []
    for raw in sums_text.splitlines():
        match = _SUM_LINE.fullmatch(raw.strip())
        if match is not None and match.group(2) == file_name:
            found.append(match.group(1).lower())
    if len(found) != 1:
        raise Refused("SHA256SUMS does not list the release zip exactly once.")
    return found[0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("SHA256SUMS could not be read.") from None


def _download_verified(release: Release, transport: Transport, scratch: Path) -> Path:
    """Download SHA256SUMS, then the zip into scratch. Refuse unless the zip's SHA-256 matches its line."""
    sums_reply = _fetch(transport, release.sums_url, MAX_SMALL_BYTES)
    _require_ok(sums_reply, SUMS_NAME)
    expected = expected_sha256(_text(sums_reply.body), release.zip_name)
    zip_path = scratch / release.zip_name
    zip_reply = _fetch(transport, release.zip_url, MAX_DOWNLOAD_BYTES, zip_path)
    _require_ok(zip_reply, "the release zip")
    if not zip_path.is_file():
        raise Refused("The release zip did not arrive.")
    if zip_path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise TooLarge("The release zip is over the size limit.")
    if _sha256_file(zip_path) != expected:
        raise Refused("The download does not match SHA256SUMS. Nothing was installed.")
    return zip_path


# --- extraction and install -------------------------------------------------------------------------

_UNSAFE_ENTRY = "The release zip has an entry with an unsafe path. Nothing was installed."


def _member_parts(name: str) -> tuple[str, ...]:
    """The relative path parts a zip entry may take, after backslashes become slashes. () means skip it.

    Refuses an absolute path, a drive or stream name (a colon), a NUL, and any ".." part.
    """
    text = name.replace("\\", "/")
    if text.startswith("/") or "\x00" in text:
        raise Refused(_UNSAFE_ENTRY)
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if any(part == ".." or ":" in part for part in parts):
        raise Refused(_UNSAFE_ENTRY)
    return parts


def _remove_tree(path: Path, *, strict: bool = False) -> None:
    """Delete a folder tree. With strict, a folder that cannot be removed refuses the update."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        if strict:
            raise Refused("An old copy of the update could not be removed. Close the app and try again.") from None


def _extract_verified(zip_path: Path, staging: Path) -> None:
    """Extract the zip into staging after checking every entry. The result must hold MeridianDesktop.exe."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise Refused("The release zip is not a valid zip file.") from None
    with archive:
        plan = []
        for info in archive.infolist():
            parts = _member_parts(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise Refused(_UNSAFE_ENTRY)
            if parts:
                plan.append((info, parts))
        staging.mkdir(parents=True)
        staging_root = staging.resolve()
        for info, parts in plan:
            target = staging.joinpath(*parts)
            if not target.resolve().is_relative_to(staging_root):
                raise Refused(_UNSAFE_ENTRY)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    if not (staging / EXE_NAME).is_file():
        raise Refused(f"The release zip does not contain {EXE_NAME}. Nothing was installed.")


def _point_shortcuts(writers: Sequence[ShortcutWriter], target: Path, fallback: Path) -> None:
    """Run every shortcut writer with target. On a failure, point the ones already done back at fallback.

    Raises _RepointFailed with undone=True when no shortcut is left pointing at target.
    """
    done: list[ShortcutWriter] = []
    try:
        for write in writers:
            write(target)
            done.append(write)
    except Exception:  # an injected writer may fail in any way; the failure is reported, never raised as is
        undone = True
        if done:
            if fallback.is_file():
                for write in done:
                    try:
                        write(fallback)
                    except Exception:
                        undone = False
            else:
                undone = False
        raise _RepointFailed(undone) from None


def _prune(versions: Path, keep: set[str]) -> None:
    """Delete every version folder not in keep. A folder that cannot be removed is left for the next update."""
    for entry in versions.iterdir():
        if entry.is_dir() and entry.name not in keep:
            _remove_tree(entry)


def install_update(
    release: Release,
    current_version: str,
    transport: Transport,
    install_root: Path,
    shortcut_writers: Sequence[ShortcutWriter],
) -> Path:
    """Download, verify, extract and install a release, then point the shortcuts at it. Return the version folder.

    Keeps the current and the new version folders and deletes the rest. Raises Refused, Offline or OSError.
    Does not check whether the app is frozen; check_and_stage does that.
    """
    install_root = Path(install_root)
    versions = install_root / VERSIONS_DIR
    final = versions / release.version
    staging = versions / f"{release.version}.partial"
    install_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root, prefix="update-") as scratch:
        zip_path = _download_verified(release, transport, Path(scratch))
        _remove_tree(staging, strict=True)
        try:
            _extract_verified(zip_path, staging)
            _remove_tree(final, strict=True)
            staging.rename(final)
        except BaseException:
            _remove_tree(staging)
            raise
    try:
        _point_shortcuts(shortcut_writers, final / EXE_NAME, versions / current_version / EXE_NAME)
    except _RepointFailed as failed:
        if failed.undone:
            _remove_tree(final)
            raise Refused("A shortcut could not be pointed at the new version. The installed app was not changed.") from None
        raise Refused(
            "A shortcut could not be pointed at the new version, and one may still point at it. Run the update again."
        ) from None
    _prune(versions, keep={release.version, current_version})
    return final


def rollback(current_version: str, install_root: Path, shortcut_writers: Sequence[ShortcutWriter]) -> str:
    """Point the shortcuts at the newest installed version below current_version, and return that version.

    Refuses when no earlier version folder with an exe is left.
    """
    install_root = Path(install_root)
    versions = install_root / VERSIONS_DIR
    current = parse_version(current_version)
    best: tuple[tuple[int, ...], Path] | None = None
    if versions.is_dir():
        for entry in versions.iterdir():
            if not entry.is_dir() or not (entry / EXE_NAME).is_file():
                continue
            try:
                parsed = parse_version(entry.name)
            except Refused:
                continue
            if parsed < current and (best is None or parsed > best[0]):
                best = (parsed, entry)
    if best is None:
        raise Refused("There is no earlier version to roll back to.")
    try:
        _point_shortcuts(shortcut_writers, best[1] / EXE_NAME, versions / current_version / EXE_NAME)
    except _RepointFailed:
        raise Refused("The shortcuts could not be pointed back at the earlier version.") from None
    return best[1].name


# --- top level ----------------------------------------------------------------------------------------


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def check_and_stage(
    current_version: str,
    transport: Transport,
    install_root: Path,
    shortcut_writers: Sequence[ShortcutWriter],
) -> UpdateResult:
    """Check GitHub for a newer release and stage it. Never raises for a network or download fault.

    A source run (not frozen) returns refused at once and makes no request. Otherwise the result is up_to_date,
    update_staged (the new version is installed and the shortcuts point at it), refused (with the plain reason),
    or offline.
    """
    if not _is_frozen():
        return UpdateResult("refused", reason=DEV_RUN_REASON)
    try:
        release = fetch_latest(transport)
        if not is_newer(release.version, current_version):
            return UpdateResult("up_to_date", version=release.version)
        install_update(release, current_version, transport, Path(install_root), shortcut_writers)
    except Offline as exc:
        return UpdateResult("offline", reason=str(exc))
    except Refused as exc:
        return UpdateResult("refused", reason=str(exc))
    except OSError:
        # A disk fault while staging. The message says nothing about the path, and the installed app is untouched.
        return UpdateResult("refused", reason="The update could not be written. The installed app was not changed.")
    return UpdateResult("update_staged", version=release.version)


# --- the real transport ---------------------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request, so urllib raises HTTPError for a 3xx reply and nothing is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _lower_headers(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in (headers or {}).items()}


def _copy_capped(source: BinaryIO, sink: BinaryIO, max_bytes: int) -> None:
    """Copy source to sink, and raise TooLarge before writing any byte past max_bytes."""
    total = 0
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise TooLarge("The download is over the size limit.")
        sink.write(chunk)


def urllib_transport(url: str, max_bytes: int, dest: Path | None) -> Reply:
    """The real transport: urllib, with redirects off and a size cap. A redirect or error status is returned, not raised.

    A 2xx body goes to dest when it is given, otherwise into Reply.body. A failed or oversized download removes dest.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/octet-stream"}
    )
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        exc.close()
        return Reply(exc.code, _lower_headers(exc.headers), b"")
    with response:
        status = response.status
        headers = _lower_headers(response.headers)
        if not 200 <= status < 300:
            return Reply(status, headers, b"")
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise TooLarge("The download is over the size limit.")
        if dest is None:
            buffer = io.BytesIO()
            _copy_capped(response, buffer, max_bytes)
            return Reply(status, headers, buffer.getvalue())
        try:
            with dest.open("wb") as sink:
                _copy_capped(response, sink, max_bytes)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return Reply(status, headers, b"")
