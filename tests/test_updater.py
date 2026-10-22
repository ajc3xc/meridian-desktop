"""Tests for meridian_desktop/updater.py. Each test uses tmp_path and a fake transport: no network, no GitHub,
no real %LOCALAPPDATA%. The urllib transport test swaps the opener for a fake, so it makes no request either."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import stat
import urllib.error
import zipfile
from pathlib import Path

import pytest

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

    def __call__(self, url, max_bytes, dest):
        self.calls.append(url)
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


@pytest.mark.parametrize("text", ["v0.2.0", "0.2.0-beta", "0.2.0 ", "", "1.x", "1..2", ".1", "0.2.0.rc1", None])
def test_versions_that_are_not_plain_dotted_integers_are_refused(text):
    with pytest.raises(u.Refused):
        u.parse_version(text)


def test_app_version_is_a_plain_version():
    assert u.parse_version(APP_VERSION) == (0, 1, 0)


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


# --- check_and_stage: the install path -------------------------------------------------------------


def test_successful_update_stages_repoints_both_shortcuts_and_prunes_older(root, frozen):
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


def test_checksum_mismatch_installs_nothing(root, frozen):
    rec = Recorder()
    result = u.check_and_stage("0.1.0", github(sums=f"{'0' * 64}  {ZIP_NAME}\n"), root, [rec])
    assert result.status == "refused"
    assert "does not match SHA256SUMS" in result.reason
    assert rec.targets == []
    assert version_names(root) == ["0.1.0"]
    assert (root / "versions" / "0.1.0" / u.EXE_NAME).read_bytes() == b"MZ-0.1.0"


def test_missing_sums_line_stops_before_the_zip_is_downloaded(root, frozen):
    gh = github(sums="")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result.status == "refused"
    assert CDN_URL not in gh.calls
    assert version_names(root) == ["0.1.0"]


def test_download_over_the_size_cap_installs_nothing(root, frozen, monkeypatch):
    monkeypatch.setattr(u, "MAX_DOWNLOAD_BYTES", 64)
    result = u.check_and_stage("0.1.0", github(zip_size=10), root, [])
    assert result.status == "refused"
    assert "size limit" in result.reason
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


def test_zip_without_the_exe_is_refused_and_leaves_nothing_behind(root, frozen):
    zip_bytes = make_zip({"_internal/base.dll": b"dll"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
    assert result.status == "refused"
    assert u.EXE_NAME in result.reason
    assert version_names(root) == ["0.1.0"]


def test_a_file_that_is_not_a_zip_is_refused(root, frozen):
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
def test_zip_slip_entries_are_refused_and_write_nothing(root, frozen, entry, tmp_path):
    zip_bytes = make_zip({u.EXE_NAME: b"MZ", entry: b"evil"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
    assert result.status == "refused"
    assert not list(tmp_path.rglob("evil.txt"))
    assert version_names(root) == ["0.1.0"]


def test_a_symbolic_link_entry_is_refused(root, frozen):
    zip_bytes = make_zip({u.EXE_NAME: b"MZ"}, symlinks={"link": "/etc/passwd"})
    result = u.check_and_stage("0.1.0", github(zip_bytes=zip_bytes), root, [])
    assert result.status == "refused"
    assert version_names(root) == ["0.1.0"]


def test_a_failing_shortcut_undoes_the_ones_already_done(root, frozen):
    rec = Recorder()
    result = u.check_and_stage("0.1.0", github(), root, [rec, Failing()])
    new_exe = root / "versions" / "0.2.0" / u.EXE_NAME
    previous = root / "versions" / "0.1.0" / u.EXE_NAME
    assert result.status == "refused"
    assert "not changed" in result.reason
    assert rec.targets == [new_exe, previous]
    assert version_names(root) == ["0.1.0"]


def test_a_failing_first_shortcut_removes_the_new_version(root, frozen):
    result = u.check_and_stage("0.1.0", github(), root, [Failing()])
    assert result.status == "refused"
    assert version_names(root) == ["0.1.0"]


def test_a_failed_undo_keeps_the_new_version_and_says_so(tmp_path, frozen):
    install = tmp_path / "install"  # no previous version, so the undo has nowhere to point
    rec = Recorder()
    result = u.check_and_stage("0.1.0", github(), install, [rec, Failing()])
    assert result.status == "refused"
    assert "may still point" in result.reason
    assert (install / "versions" / "0.2.0" / u.EXE_NAME).is_file()


# --- check_and_stage: states and network faults ----------------------------------------------------


def test_a_source_run_returns_refused_and_makes_no_request(root, monkeypatch):
    monkeypatch.setattr(u, "_is_frozen", lambda: False)
    gh = github()
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result == u.UpdateResult("refused", reason=u.DEV_RUN_REASON)
    assert gh.calls == []
    assert version_names(root) == ["0.1.0"]


def test_the_same_version_is_up_to_date(root, frozen):
    gh = github("0.1.0")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result == u.UpdateResult("up_to_date", version="0.1.0")
    assert gh.calls == [u.LATEST_RELEASE_URL]


def test_github_unreachable_is_offline_and_never_raises(root, frozen):
    gh = github()
    gh.routes[u.LATEST_RELEASE_URL] = OSError("no route")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result.status == "offline"


def test_a_timeout_during_download_is_offline_and_leaves_no_partial_files(root, frozen):
    gh = github()
    gh.routes[CDN_URL] = TimeoutError("slow")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result.status == "offline"
    assert version_names(root) == ["0.1.0"]
    assert not list(root.glob("update-*"))


@pytest.mark.parametrize(("status", "expected"), [(404, "refused"), (403, "refused"), (429, "offline"), (503, "offline")])
def test_github_error_statuses_map_to_refused_or_offline(root, frozen, status, expected):
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
def test_a_redirect_to_a_disallowed_address_is_refused_before_it_is_requested(root, frozen, location):
    gh = github()
    gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": location}, b"")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result.status == "refused"
    assert location not in gh.calls
    assert version_names(root) == ["0.1.0"]


def test_a_redirect_loop_is_refused(root, frozen):
    gh = github()
    gh.routes[asset_url("0.2.0", ZIP_NAME)] = (302, {"location": asset_url("0.2.0", ZIP_NAME)}, b"")
    result = u.check_and_stage("0.1.0", gh, root, [])
    assert result.status == "refused"
    assert "too many times" in result.reason


# --- rollback ------------------------------------------------------------------------------------


def test_rollback_points_back_at_the_previous_version(root, frozen):
    assert u.check_and_stage("0.1.0", github(), root, []).status == "update_staged"
    rec = Recorder()
    assert u.rollback("0.2.0", root, [rec]) == "0.1.0"
    assert rec.targets == [root / "versions" / "0.1.0" / u.EXE_NAME]


def test_rollback_picks_the_newest_version_below_current(root):
    for version in ("0.1.5", "0.0.9"):
        exe = root / "versions" / version / u.EXE_NAME
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    rec = Recorder()
    assert u.rollback("0.2.0", root, [rec]) == "0.1.5"
    assert rec.targets == [root / "versions" / "0.1.5" / u.EXE_NAME]


def test_rollback_refuses_when_no_earlier_version_is_left(root):
    rec = Recorder()
    with pytest.raises(u.Refused, match="no earlier version"):
        u.rollback("0.1.0", root, [rec])
    assert rec.targets == []


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
