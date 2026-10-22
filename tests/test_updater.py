"""Tests for meridian_desktop/updater.py. Each test uses tmp_path and a fake transport: no network, no GitHub,
no real %LOCALAPPDATA%. The urllib transport test swaps the opener for a fake, so it makes no request either."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import stat
import struct
import urllib.error
import zipfile
from pathlib import Path

import pytest

import meridian_desktop
from meridian_desktop import updater as u
from meridian_desktop.version import APP_VERSION

ZIP_NAME = "MeridianDesktop-0.2.0.zip"
CDN_URL = "https://objects.githubusercontent.com/github-production-release-asset/1/MeridianDesktop-0.2.0.zip"


def asset_url(version: str, name: str) -> str:
    return f"https://github.com/{u.REPO}/releases/download/v{version}/{name}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


def truncated_member_zip(name: str, data: bytes, declared: int) -> bytes:
    """A stored zip whose one member declares `declared` bytes, more than the file holds after its data.

    zipfile refuses a member whose data runs into the central directory. A second record with the same header
    offset gets past that check (it only warns), so reading the member runs off the end of the file: EOFError.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(name, data)
    raw = buffer.getvalue()
    cd = raw.index(b"PK\x01\x02")  # the central directory record
    eocd = raw.index(b"PK\x05\x06")  # the end of central directory record
    bad = bytearray(raw[cd:eocd])
    struct.pack_into("<II", bad, 20, declared, declared)  # compressed size, then size: the first record only
    end = bytearray(raw[eocd:])
    struct.pack_into("<HHHHII", end, 4, 0, 0, 2, 2, 2 * len(bad), cd)  # two records, same start, same disk
    return raw[:cd] + bytes(bad) + raw[cd:eocd] + bytes(end)


def bundle_zip(version: str = "0.2.0") -> bytes:
    return make_zip(
        {u.EXE_NAME: f"MZ-{version}".encode(), "_internal/base.dll": b"dll", "install.cmd": b"@echo off\r\n"}
    )


def release_body(version="0.2.0", *, names=None, tag=None, draft=False, zip_size=1000) -> bytes:
    names = names if names is not None else [f"MeridianDesktop-{version}.zip", "SHA256SUMS"]
    assets = [
        {
            "name": name,
            "browser_download_url": asset_url(version, name),
            "size": zip_size if name.endswith(".zip") else 64,
        }
        for name in names
    ]
    return json.dumps({"tag_name": tag or f"v{version}", "draft": draft, "assets": assets}).encode()


class FakeGitHub:
    """A transport. A route maps a URL to a (status, headers, body) tuple, to an exception to raise, or to a
    function (max_bytes, dest) -> Reply. Every URL asked for is recorded. An unrouted URL fails the test."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls: list[str] = []
        self.requests: list[tuple[str, int, Path | None]] = []  # each URL with the size cap and file it was given

    def __call__(self, url, max_bytes, dest):
        self.calls.append(url)
        self.requests.append((url, max_bytes, dest))
        if url not in self.routes:
            raise AssertionError(f"unexpected request to {url}")
        route = self.routes[url]
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(max_bytes, dest)
        status, headers, body = route
        if not 200 <= status < 300:
            return u.Reply(status, headers, b"")
        if len(body) > max_bytes:
            raise u.TooLarge("The download is over the size limit.")
        if dest is None:
            return u.Reply(status, headers, body)
        dest.write_bytes(body)
        return u.Reply(status, headers, b"")


def github(version="0.2.0", *, zip_bytes=None, sums=None, **release_kwargs) -> FakeGitHub:
    zip_bytes = bundle_zip(version) if zip_bytes is None else zip_bytes
    name = f"MeridianDesktop-{version}.zip"
    sums_text = f"{sha256(zip_bytes)}  {name}\n" if sums is None else sums
    return FakeGitHub(
        {
            u.LATEST_RELEASE_URL: (200, {}, release_body(version, **release_kwargs)),
            asset_url(version, name): (302, {"location": CDN_URL}, b""),
            CDN_URL: (200, {}, zip_bytes),
            asset_url(version, "SHA256SUMS"): (200, {}, sums_text.encode()),
        }
    )


class Recorder:
    """A shortcut writer that records the exe it was pointed at."""

    def __init__(self):
        self.targets: list[Path] = []

    def __call__(self, exe: Path) -> None:
        self.targets.append(exe)


class Failing:
    """A shortcut writer that always fails."""

    def __call__(self, exe: Path) -> None:
        raise OSError("shortcut write failed")


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(u, "_is_frozen", lambda: True)


@pytest.fixture
def root(tmp_path):
    """An install root with 0.1.0 installed, as the running app would be."""
    install = tmp_path / "install"
    exe = install / "versions" / "0.1.0" / u.EXE_NAME
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ-0.1.0")
    return install


def version_names(install: Path) -> list[str]:
    return sorted(p.name for p in (install / "versions").iterdir())


def tree_state(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes (None for a folder), so a test can show that nothing changed."""
    paths = sorted(root.rglob("*"))
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None) for path in paths}


def release_0_2_0() -> u.Release:
    return u.Release(
        version="0.2.0",
        zip_name=ZIP_NAME,
        zip_url=asset_url("0.2.0", ZIP_NAME),
        zip_size=1000,
        sums_url=asset_url("0.2.0", "SHA256SUMS"),
    )


# --- versions and URLs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.10.0", "0.9.0", True),
        ("0.2", "0.2.0", False),
        ("1.0.0", "0.99.99", True),
    ],
)
def test_is_newer_compares_integer_parts(latest, current, expected):
    assert u.is_newer(latest, current) is expected


@pytest.mark.parametrize(
    "text", ["v0.2.0", "0.2.0-beta", "0.2.0 ", "", "1.x", "1..2", ".1", "0.2.0.rc1", None, "9" * 4301]
)
def test_versions_that_are_not_plain_dotted_integers_are_refused(text):
    with pytest.raises(u.Refused):
        u.parse_version(text)


def test_app_version_is_a_plain_version():
    assert u.parse_version(APP_VERSION) == (0, 1, 0)


def test_the_package_version_is_the_app_version():
    assert meridian_desktop.__version__ == APP_VERSION


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/ajc3xc/meridian-desktop/x.zip",
        "https://evil.example/x.zip",
        "https://github.com.evil.example/x.zip",
        "https://user:pw@github.com/x.zip",
        "https://github.com:8443/x.zip",
        "ftp://github.com/x.zip",
    ],
)
def test_urls_that_are_not_allowed_https_github_addresses_are_refused(url):
    with pytest.raises(u.Refused):
        u.require_allowed_url(url)


def test_every_listed_host_is_allowed():
    for host in sorted(u.ALLOWED_HOSTS):
        u.require_allowed_url(f"https://{host}/x")


# --- release and checksum ------------------------------------------------------------------------


def test_fetch_latest_reads_the_zip_and_sums_assets():
    release = u.fetch_latest(github())
    assert release == u.Release(
        version="0.2.0",
        zip_name=ZIP_NAME,
        zip_url=asset_url("0.2.0", ZIP_NAME),
        zip_size=1000,
        sums_url=asset_url("0.2.0", "SHA256SUMS"),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"names": ["SHA256SUMS"]},  # the zip is missing
        {"names": [ZIP_NAME]},  # SHA256SUMS is missing
        {"tag": "v0.2.0-beta"},  # not plain dotted integers
        {"draft": True},
    ],
)
def test_a_release_missing_an_asset_or_with_a_bad_version_is_refused(kwargs):
    with pytest.raises(u.Refused):
        u.fetch_latest(github(**kwargs))


def test_a_zip_asset_over_the_cap_is_refused_before_download(monkeypatch):
    gh = github(zip_size=u.MAX_DOWNLOAD_BYTES + 1)
    with pytest.raises(u.TooLarge):
        u.fetch_latest(gh)
    assert CDN_URL not in gh.calls


def test_expected_sha256_accepts_both_common_line_forms():
    assert u.expected_sha256(f"{'AB' * 32}  {ZIP_NAME}\n", ZIP_NAME) == "ab" * 32
    assert u.expected_sha256(f"{'cd' * 32} *{ZIP_NAME}\n", ZIP_NAME) == "cd" * 32


def test_expected_sha256_refuses_a_missing_or_duplicate_zip_line():
    line = f"{'ab' * 32}  {ZIP_NAME}\n"
    with pytest.raises(u.Refused):
        u.expected_sha256("", ZIP_NAME)
    with pytest.raises(u.Refused):
        u.expected_sha256(line * 2, ZIP_NAME)
    with pytest.raises(u.Refused):
        u.expected_sha256(f"{'ab' * 32}  other.zip\n", ZIP_NAME)


def _with_every_member_marked_encrypted(data: bytes) -> bytes:
    """Set general purpose bit 0 (encrypted) on each local and central header, as a password-protected zip has."""
    raw = bytearray(data)
    for signature, flags_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = raw.find(signature)
        while position != -1:
            raw[position + flags_offset] |= 0x01
            position = raw.find(signature, position + 4)
    return bytes(raw)


class TestInstallWhenEnabled:
    """The install path, with INSTALL_ENABLED on. The autouse fixture below turns it on for these tests only.

    Every other test runs with the value the build ships with, which is False.
    """

    @pytest.fixture(autouse=True)
    def install_enabled(self, monkeypatch):
        monkeypatch.setattr(u, "INSTALL_ENABLED", True)

    # --- check_and_stage: the install path -------------------------------------------------------------


    def test_successful_update_stages_repoints_both_shortcuts_and_prunes_older(self, root, frozen):
        old = root / "versions" / "0.0.9"
        old.mkdir()
        (old / u.EXE_NAME).write_bytes(b"MZ-0.0.9")
        start, menu = Recorder(), Recorder()

        result = u.check_and_stage("0.1.0", github(), root, [start, menu])

        new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
        assert result == u.UpdateResult("update_staged", version="0.2.0")
        assert new_exe.read_bytes() == b"MZ-0.2.0"
        assert (root / "versions" / "0.2.0" / "_internal" / "base.dll").read_bytes() == b"dll"
        assert start.targets == [new_exe]
        assert menu.targets == [new_exe]
        assert version_names(root) == ["0.1.0", "0.2.0"]
        assert not list(root.glob("update-*"))


    def test_checksum_mismatch_installs_nothing(self, root, frozen):
        rec = Recorder()
        result = u.check_and_stage("0.1.0", github(sums=f"{'0' * 64}  {ZIP_NAME}\n"), root, [rec])
        assert result.status == "refused"
        assert "does not match SHA256SUMS" in result.reason
        assert rec.targets == []
        assert version_names(root) == ["0.1.0"]
        assert (root / "versions" / "0.1.0" / u.EXE_NAME).read_bytes() == b"MZ-0.1.0"


    def test_missing_sums_line_stops_before_the_zip_is_downloaded(self, root, frozen):
        gh = github(sums="")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "refused"
        assert CDN_URL not in gh.calls
        assert version_names(root) == ["0.1.0"]


    def test_download_over_the_size_cap_installs_nothing(self, root, frozen, monkeypatch):
        monkeypatch.setattr(u, "MAX_DOWNLOAD_BYTES", 64)
        result = u.check_and_stage("0.1.0", github(zip_size=10), root, [])
        assert result.status == "refused"
        assert "size limit" in result.reason
        assert version_names(root) == ["0.1.0"]
        assert not list(root.glob("update-*"))


    def test_zip_without_the_exe_is_refused_and_leaves_nothing_behind(self, root, frozen):
        zip_bytes = make_zip({"_internal/base.dll": b"dll"})
        result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
        assert result.status == "refused"
        assert u.EXE_NAME in result.reason
        assert version_names(root) == ["0.1.0"]


    def test_a_file_that_is_not_a_zip_is_refused(self, root, frozen):
        result = u.check_and_stage("0.1.0", github(zip_bytes=b"not a zip"), root, [])
        assert result.status == "refused"
        assert version_names(root) == ["0.1.0"]


    @pytest.mark.parametrize(
        "entry",
        [
            "../evil.txt",
            "/abs.txt",
            "C:/evil.txt",
            "C:evil.txt",
            "..\\evil.txt",
            "a/../../evil.txt",
            "\\\\server\\share\\evil.txt",
            "a/b:stream.txt",
        ],
    )
    def test_zip_slip_entries_are_refused_and_write_nothing(self, root, frozen, entry, tmp_path):
        zip_bytes = make_zip({u.EXE_NAME: b"MZ", entry: b"evil"})
        result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
        assert result.status == "refused"
        assert not list(tmp_path.rglob("evil.txt"))
        assert version_names(root) == ["0.1.0"]


    def test_a_symbolic_link_entry_is_refused(self, root, frozen):
        zip_bytes = make_zip({u.EXE_NAME: b"MZ"}, symlinks={"link": "/etc/passwd"})
        result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
        assert result.status == "refused"
        assert version_names(root) == ["0.1.0"]


    @pytest.mark.parametrize(("name", "attributes"), [("linked.txt", 0x0400), ("junction/", 0x0410)])
    def test_a_windows_reparse_point_entry_is_refused_and_writes_nothing(self, root, frozen, name, attributes):
        # The Windows attributes sit in the low 16 bits of external_attr. No Unix mode is set, so only the
        # reparse-point bit can refuse this entry.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(u.EXE_NAME, b"MZ")
            info = zipfile.ZipInfo(name)
            info.external_attr = attributes
            archive.writestr(info, b"C:/Windows")
        result = u.check_and_stage("0.1.0", github(zip_bytes=buffer.getvalue()), root, [])
        assert result.status == "refused"
        assert version_names(root) == ["0.1.0"]
        assert not list(root.glob("update-*"))


    def test_a_member_with_a_bad_crc_is_refused_and_installs_nothing(self, root, frozen):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(u.EXE_NAME, b"MZ-0.2.0-payload")
        raw = bytearray(buffer.getvalue())
        raw[raw.index(b"MZ-0.2.0-payload")] ^= 0xFF  # the stored bytes no longer match the CRC in the archive
        result = u.check_and_stage("0.1.0", github(zip_bytes=bytes(raw)), root, [])
        assert result.status == "refused"
        assert "could not be extracted" in result.reason
        assert version_names(root) == ["0.1.0"]
        assert (root / "versions" / "0.1.0" / u.EXE_NAME).read_bytes() == b"MZ-0.1.0"
        assert not list(root.glob("update-*"))


    def test_an_encrypted_member_is_refused_and_installs_nothing(self, root, frozen):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(u.EXE_NAME, b"MZ-0.2.0")
        zip_bytes = _with_every_member_marked_encrypted(buffer.getvalue())
        result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
        assert result.status == "refused"
        assert "could not be extracted" in result.reason
        assert version_names(root) == ["0.1.0"]
        assert not list(root.glob("update-*"))


    @pytest.mark.filterwarnings("ignore:Overlapped entries")
    def test_a_truncated_member_is_refused_and_leaves_the_install_unchanged(self, root, frozen):
        zip_bytes = truncated_member_zip(u.EXE_NAME, b"MZ-0.2.0-payload", declared=4096 + 17)
        # Precondition: plain zipfile reads this member to the end of the file and raises EOFError. Without that the
        # test would prove nothing about the EOFError path.
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            with pytest.warns(UserWarning, match="Overlapped entries"):
                member = archive.open(archive.infolist()[0])
            with member, pytest.raises(EOFError):
                member.read()
        before = tree_state(root)
        result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
        assert result.status == "refused"
        assert "could not be extracted" in result.reason
        assert tree_state(root) == before
        assert not list(root.glob("update-*"))

    def test_an_oversized_version_tag_is_refused_and_never_raises(self, root, frozen):
        gh = github(tag="v" + "9" * 4301)
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "refused"
        assert gh.calls == [u.LATEST_RELEASE_URL]
        assert version_names(root) == ["0.1.0"]


    def test_a_failing_shortcut_undoes_the_ones_already_done(self, root, frozen):
        rec = Recorder()
        result = u.check_and_stage("0.1.0", github(), root, [rec, Failing()])
        new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
        previous = root / "versions" / "0.1.0" / u.EXE_NAME
        assert result.status == "refused"
        assert "not changed" in result.reason
        assert rec.targets == [new_exe, previous]
        assert version_names(root) == ["0.1.0"]


    def test_a_failing_first_shortcut_removes_the_new_version(self, root, frozen):
        result = u.check_and_stage("0.1.0", github(), root, [Failing()])
        assert result.status == "refused"
        assert version_names(root) == ["0.1.0"]


    def test_a_failed_undo_keeps_the_new_version_and_says_so(self, tmp_path, frozen):
        install = tmp_path / "install"  # no previous version, so the undo has nowhere to point
        rec = Recorder()
        result = u.check_and_stage("0.1.0", github(), install, [rec, Failing()])
        assert result.status == "refused"
        assert "may still point" in result.reason
        assert (install / "versions" / "0.2.0" / u.EXE_NAME).is_file()


    # --- check_and_stage: states and network faults ----------------------------------------------------


    def test_a_source_run_returns_refused_and_makes_no_request(self, root, monkeypatch):
        monkeypatch.setattr(u, "_is_frozen", lambda: False)
        gh = github()
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result == u.UpdateResult("refused", reason=u.DEV_RUN_REASON)
        assert gh.calls == []
        assert version_names(root) == ["0.1.0"]


    def test_the_same_version_is_up_to_date(self, root, frozen):
        gh = github("0.1.0")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result == u.UpdateResult("up_to_date", version="0.1.0")
        assert gh.calls == [u.LATEST_RELEASE_URL]


    def test_github_unreachable_is_offline_and_never_raises(self, root, frozen):
        gh = github()
        gh.routes[u.LATEST_RELEASE_URL] = OSError("no route")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "offline"


    def test_a_timeout_during_download_is_offline_and_leaves_no_partial_files(self, root, frozen):
        gh = github()
        gh.routes[CDN_URL] = TimeoutError("slow")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "offline"
        assert version_names(root) == ["0.1.0"]
        assert not list(root.glob("update-*"))


    @pytest.mark.parametrize(
        ("status", "expected"), [(404, "refused"), (403, "refused"), (429, "offline"), (503, "offline")]
    )
    def test_github_error_statuses_map_to_refused_or_offline(self, root, frozen, status, expected):
        gh = github()
        gh.routes[u.LATEST_RELEASE_URL] = (status, {}, b"")
        assert u.check_and_stage("0.1.0", gh, root, []).status == expected


    @pytest.mark.parametrize(
        "location",
        [
            "https://evil.example/MeridianDesktop-0.2.0.zip",
            "http://objects.githubusercontent.com/MeridianDesktop-0.2.0.zip",
            "https://objects.githubusercontent.com.evil.example/x.zip",
        ],
    )
    def test_a_redirect_to_a_disallowed_address_is_refused_before_it_is_requested(self, root, frozen, location):
        gh = github()
        gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": location}, b"")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "refused"
        assert location not in gh.calls
        assert version_names(root) == ["0.1.0"]


    def test_a_redirect_loop_is_refused(self, root, frozen):
        gh = github()
        gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": asset_url("0.2.0", ZIP_NAME)}, b"")
        result = u.check_and_stage("0.1.0", gh, root, [])
        assert result.status == "refused"
        assert "too many times" in result.reason


    # --- rollback ------------------------------------------------------------------------------------


    def test_rollback_points_back_at_the_previous_version(self, root, frozen):
        assert u.check_and_stage("0.1.0", github(), root, []).status == "update_staged"
        rec = Recorder()
        assert u.rollback("0.2.0", root, [rec]) == "0.1.0"
        assert rec.targets == [root / "versions" / "0.1.0" / u.EXE_NAME]


    def test_rollback_picks_the_newest_version_below_current(self, root):
        for version in ("0.1.5", "0.0.9"):
            exe = root / "versions" / version / u.EXE_NAME
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
        rec = Recorder()
        assert u.rollback("0.2.0", root, [rec]) == "0.1.5"
        assert rec.targets == [root / "versions" / "0.1.5" / u.EXE_NAME]


    def test_rollback_refuses_when_no_earlier_version_is_left(self, root):
        rec = Recorder()
        with pytest.raises(u.Refused, match="no earlier version"):
            u.rollback("0.1.0", root, [rec])
        assert rec.targets == []


    def test_a_rollback_holds_the_rolled_back_release_on_every_later_check(self, root, frozen):
        shortcut = Recorder()
        assert u.check_and_stage("0.1.0", github(), root, [shortcut]).status == "update_staged"
        assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"
        targets_after_rollback = list(shortcut.targets)
        assert (root / "versions" / u.SKIP_MARKER).is_file()
        for _ in range(2):
            gh = github()
            result = u.check_and_stage("0.1.0", gh, root, [shortcut])
            assert result.status == "refused"
            assert result.version == "0.2.0"
            assert "held after rollback" in result.reason
            assert gh.calls == [u.LATEST_RELEASE_URL]  # the release is held before any download
            assert shortcut.targets == targets_after_rollback  # the shortcut stays on 0.1.0
        assert shortcut.targets[-1] == root / "versions" / "0.1.0" / u.EXE_NAME


    @pytest.mark.parametrize("latest", ["0.2.0", "0.3.0"])
    def test_a_release_not_newer_than_the_rolled_back_version_is_held(self, root, frozen, latest):
        shortcut = Recorder()
        for version in ("0.2.0", "0.3.0"):
            exe = root / "versions" / version / u.EXE_NAME
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
        assert u.rollback("0.3.0", root, [shortcut]) == "0.2.0"
        assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"  # the marker keeps 0.3.0, not 0.2.0
        gh = github(latest)
        result = u.check_and_stage("0.1.0", gh, root, [shortcut])
        assert result.status == "refused"
        assert "held after rollback" in result.reason
        assert gh.calls == [u.LATEST_RELEASE_URL]


    def test_a_newer_release_after_a_rollback_stages_and_clears_the_marker(self, root, frozen):
        shortcut = Recorder()
        assert u.check_and_stage("0.1.0", github(), root, [shortcut]).status == "update_staged"
        assert u.rollback("0.2.0", root, [shortcut]) == "0.1.0"
        result = u.check_and_stage("0.1.0", github("0.3.0"), root, [shortcut])
        assert result == u.UpdateResult("update_staged", version="0.3.0")
        assert not (root / "versions" / u.SKIP_MARKER).exists()
        assert shortcut.targets[-1] == root / "versions" / "0.3.0" / u.EXE_NAME
        assert version_names(root) == ["0.1.0", "0.3.0"]


    def test_an_unreadable_marker_is_ignored_and_holds_nothing(self, root, frozen):
        (root / "versions" / u.SKIP_MARKER).write_text("{not json", encoding="utf-8")
        result = u.check_and_stage("0.1.0", github(), root, [])
        assert result == u.UpdateResult("update_staged", version="0.2.0")


    def test_a_rollback_whose_shortcuts_all_fail_leaves_no_marker(self, root):
        with pytest.raises(u.Refused, match="could not be pointed back"):
            u.rollback("0.2.0", root, [Failing()])
        assert not (root / "versions" / u.SKIP_MARKER).exists()


# --- installing is off: the value this build ships with ---------------------------------------------


def test_installing_is_off_in_the_shipped_build():
    assert u.INSTALL_ENABLED is False


@pytest.mark.parametrize("frozen_build", [True, False])
def test_check_and_stage_refuses_while_installing_is_off_and_makes_no_request(root, monkeypatch, frozen_build):
    monkeypatch.setattr(u, "_is_frozen", lambda: frozen_build)
    rec = Recorder()
    gh = github()
    result = u.check_and_stage("0.1.0", gh, root, [rec])
    assert result == u.UpdateResult("refused", reason="Installing is off until release signing exists.")
    assert gh.requests == []
    assert rec.targets == []
    assert version_names(root) == ["0.1.0"]


def test_install_update_raises_install_disabled_and_changes_nothing(root):
    before = tree_state(root)
    rec = Recorder()
    gh = github()
    with pytest.raises(u.InstallDisabled, match="Installing is off until release signing exists."):
        u.install_update(release_0_2_0(), "0.1.0", gh, root, [rec])
    assert gh.calls == []
    assert rec.targets == []
    assert tree_state(root) == before


def test_install_update_while_installing_is_off_does_not_create_the_install_folder(tmp_path):
    install = tmp_path / "never-created"
    with pytest.raises(u.InstallDisabled):
        u.install_update(release_0_2_0(), "0.1.0", github(), install, [])
    assert not install.exists()


# --- the copy cap and the urllib transport ---------------------------------------------------------


def test_copy_capped_allows_exactly_the_cap_and_refuses_one_byte_more():
    out = io.BytesIO()
    u._copy_capped(io.BytesIO(b"x" * 10), out, 10)
    assert out.getvalue() == b"x" * 10
    with pytest.raises(u.TooLarge):
        u._copy_capped(io.BytesIO(b"x" * 11), io.BytesIO(), 10)


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None, status: int = 200):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class _FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome

    def open(self, request, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _patch_opener(monkeypatch, outcome) -> None:
    monkeypatch.setattr(u.urllib.request, "build_opener", lambda *handlers: _FakeOpener(outcome))


def test_urllib_transport_streams_a_2xx_body_to_the_file(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"zipbytes", {"Content-Type": "application/zip"}))
    dest = tmp_path / "x.zip"
    reply = u.urllib_transport("https://github.com/x", 100, dest)
    assert reply.status == 200
    assert reply.body == b""
    assert dest.read_bytes() == b"zipbytes"
    assert reply.headers["content-type"] == "application/zip"


def test_urllib_transport_returns_a_small_body_in_memory(monkeypatch):
    _patch_opener(monkeypatch, _FakeResponse(b"{}"))
    assert u.urllib_transport("https://api.github.com/x", 100, None).body == b"{}"


def test_urllib_transport_removes_a_file_that_goes_over_the_cap(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"x" * 11))
    dest = tmp_path / "x.zip"
    with pytest.raises(u.TooLarge):
        u.urllib_transport("https://github.com/x", 10, dest)
    assert not dest.exists()


def test_urllib_transport_refuses_a_declared_size_over_the_cap_before_reading(monkeypatch, tmp_path):
    _patch_opener(monkeypatch, _FakeResponse(b"x", {"Content-Length": "999"}))
    dest = tmp_path / "x.zip"
    with pytest.raises(u.TooLarge):
        u.urllib_transport("https://github.com/x", 10, dest)
    assert not dest.exists()


def test_urllib_transport_returns_a_redirect_without_following_it(monkeypatch, tmp_path):
    location = "https://objects.githubusercontent.com/x"
    headers = http.client.HTTPMessage()
    headers["Location"] = location
    _patch_opener(monkeypatch, urllib.error.HTTPError("https://github.com/x", 302, "Found", headers, io.BytesIO()))
    dest = tmp_path / "x.zip"
    reply = u.urllib_transport("https://github.com/x", 100, dest)
    assert reply.status == 302
    assert reply.headers["location"] == location
    assert not dest.exists()


# --- check_for_update: a notice only, never a download ----------------------------------------------


def page(version: str) -> str:
    return f"https://github.com/ajc3xc/meridian-desktop/releases/tag/v{version}"


def test_a_newer_release_gives_a_notice_and_only_the_api_is_requested():
    gh = github("0.2.0")
    result = u.check_for_update("0.1.0", gh)
    assert result == u.UpdateNotice(
        version="0.2.0", release_page_url="https://github.com/ajc3xc/meridian-desktop/releases/tag/v0.2.0"
    )
    assert gh.requests == [(u.LATEST_RELEASE_URL, u.MAX_SMALL_BYTES, None)]


@pytest.mark.parametrize(("latest", "current"), [("0.1.0", "0.1.0"), ("0.0.9", "0.1.0"), ("0.9.0", "0.10.0")])
def test_the_same_or_an_older_release_is_up_to_date(latest, current):
    gh = github(latest)
    assert u.check_for_update(current, gh) == u.UpToDate()
    assert gh.calls == [u.LATEST_RELEASE_URL]


def test_a_newer_release_compares_its_parts_as_integers():
    assert u.check_for_update("0.9.0", github("0.10.0")) == u.UpdateNotice("0.10.0", page("0.10.0"))


@pytest.mark.parametrize("failure", [OSError("no route"), TimeoutError("slow"), http.client.HTTPException("reset")])
def test_an_offline_transport_gives_offline(failure):
    def offline(url, max_bytes, dest):
        raise failure

    result = u.check_for_update("0.1.0", offline)
    assert isinstance(result, u.Offline)
    assert result.reason


@pytest.mark.parametrize(("status", "kind"), [(429, u.Offline), (503, u.Offline), (404, u.Refused), (403, u.Offline)])
def test_github_error_statuses_on_the_check_are_offline_or_refused(status, kind):
    gh = github()
    gh.routes[u.LATEST_RELEASE_URL] = (status, {}, b"")
    assert isinstance(u.check_for_update("0.1.0", gh), kind)


@pytest.mark.parametrize("status", [403, 429])
def test_a_rate_limited_check_is_offline_and_says_to_try_again_later(status):
    # GitHub answers 403 for an unauthenticated rate limit as well as for a refusal, so the user is told to retry.
    gh = github()
    gh.routes[u.LATEST_RELEASE_URL] = (status, {}, b"")
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Offline)
    assert result.reason.endswith("Try again later.")


@pytest.mark.parametrize("tag", ["v0.2.0-beta", "0.2.0", "V0.2.0", "vv0.2.0", "v", "v" + "9" * 4301, "latest"])
def test_a_bad_tag_gives_refused(tag):
    gh = github(tag=tag)
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert result.reason
    assert gh.calls == [u.LATEST_RELEASE_URL]


def test_a_release_with_no_tag_gives_refused():
    gh = FakeGitHub({u.LATEST_RELEASE_URL: (200, {}, json.dumps({"draft": False, "assets": []}).encode())})
    assert isinstance(u.check_for_update("0.1.0", gh), u.Refused)


def test_a_reply_that_is_not_json_gives_refused():
    gh = FakeGitHub({u.LATEST_RELEASE_URL: (200, {}, b"<html>not json</html>")})
    assert isinstance(u.check_for_update("0.1.0", gh), u.Refused)


def test_a_reply_nested_too_deeply_to_parse_gives_refused_and_never_raises():
    # 400 KB of open brackets is under the small cap, and far past the parser's recursion limit.
    deep = b"[" * 200_000 + b"]" * 200_000
    gh = FakeGitHub({u.LATEST_RELEASE_URL: (200, {}, deep)})
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert result.reason == "The latest release could not be read."


def test_a_draft_release_gives_refused():
    assert isinstance(u.check_for_update("0.1.0", github("0.2.0", draft=True)), u.Refused)


def test_a_reply_over_the_small_cap_gives_refused():
    def too_large(url, max_bytes, dest):
        raise u.TooLarge("The download is over the size limit.")

    assert isinstance(u.check_for_update("0.1.0", too_large), u.Refused)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/releases/latest",
        "http://api.github.com/repos/ajc3xc/meridian-desktop/releases/latest",
        "https://api.github.com.evil.example/releases/latest",
    ],
)
def test_a_redirect_to_an_off_list_host_gives_refused(location):
    gh = github()
    gh.routes[u.LATEST_RELEASE_URL] = (302, {"location": location}, b"")
    result = u.check_for_update("0.1.0", gh)
    assert isinstance(result, u.Refused)
    assert gh.calls == [u.LATEST_RELEASE_URL]  # the off-list address is never requested


@pytest.mark.parametrize("current", ["0.1.0-beta", "v0.1.0", "", "1..2"])
def test_a_current_version_that_is_not_plain_is_refused_before_any_request(current):
    gh = github("0.2.0")
    assert isinstance(u.check_for_update(current, gh), u.Refused)
    assert gh.calls == []


@pytest.mark.parametrize("version", ["0.2.0", "10.0.1", "1.2"])
def test_the_release_page_is_built_from_the_validated_version(version):
    assert u.release_page_url_for(version) == page(version)


@pytest.mark.parametrize("version", ["v0.2.0", "0.2.0/../x", "0.2.0?x=1", "0.2.0-beta"])
def test_a_release_page_is_never_built_from_an_unvalidated_version(version):
    with pytest.raises(u.Refused):
        u.release_page_url_for(version)


def test_check_for_update_cannot_reach_the_install_path(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the install path was reached from check_for_update")

    for name in (
        "check_and_stage",
        "install_update",
        "rollback",
        "_download_verified",
        "_extract_verified",
        "_point_shortcuts",
    ):
        monkeypatch.setattr(u, name, forbidden)
    assert isinstance(u.check_for_update("0.1.0", github("0.2.0")), u.UpdateNotice)
    assert isinstance(u.check_for_update("0.2.0", github("0.2.0")), u.UpToDate)
    assert isinstance(u.check_for_update("0.1.0", github("0.1.0")), u.UpToDate)
