"""Tests for packaging/make_release_key.py: the release signing seed is created once, outside the repository, and only the
public key is ever printed. Every path is under pytest's temp dir; the real %LOCALAPPDATA% is never touched.
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

from meridian_desktop import ed25519

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mk():
    spec = importlib.util.spec_from_file_location("make_release_key", REPO / "packaging" / "make_release_key.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(tmp_path: Path) -> dict[str, str]:
    return {"LOCALAPPDATA": str(tmp_path / "local")}


def test_the_key_file_is_under_local_app_data_and_never_in_the_repository(mk, tmp_path):
    path = mk.key_path(_env(tmp_path))
    assert path == tmp_path / "local" / "meridian-release-keys" / "release-ed25519.key"
    assert REPO not in path.parents


def test_a_missing_local_app_data_is_refused_with_plain_words(mk):
    with pytest.raises(mk.ReleaseKeyError, match="LOCALAPPDATA is not set"):
        mk.key_path({})


def test_the_first_run_creates_a_32_byte_seed_and_returns_its_public_key(mk, tmp_path):
    env = _env(tmp_path)
    text = mk.public_key_text(env)
    seed = base64.b64decode(mk.key_path(env).read_text(encoding="ascii").strip(), validate=True)
    assert len(seed) == 32
    assert base64.b64decode(text, validate=True) == ed25519.public_key(seed)


def test_a_second_run_prints_the_same_key_and_never_replaces_the_seed(mk, tmp_path):
    env = _env(tmp_path)
    first = mk.public_key_text(env)
    seed_before = mk.key_path(env).read_bytes()
    second = mk.public_key_text(env)
    assert second == first
    assert mk.key_path(env).read_bytes() == seed_before


def test_a_seed_file_that_is_not_32_bytes_is_refused_and_left_alone(mk, tmp_path):
    env = _env(tmp_path)
    path = mk.key_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(base64.b64encode(b"short").decode("ascii"), encoding="ascii")
    with pytest.raises(mk.ReleaseKeyError, match="32-byte seed"):
        mk.public_key_text(env)
    assert path.read_text(encoding="ascii") == base64.b64encode(b"short").decode("ascii")


def test_a_seed_file_that_is_not_base64_is_refused_and_left_alone(mk, tmp_path):
    env = _env(tmp_path)
    path = mk.key_path(env)
    path.parent.mkdir(parents=True)
    path.write_text("not base64 at all !!", encoding="ascii")
    with pytest.raises(mk.ReleaseKeyError, match="cannot be read"):
        mk.public_key_text(env)
    assert path.read_text(encoding="ascii") == "not base64 at all !!"


def test_main_prints_only_the_public_key_on_one_line(mk, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert mk.main() == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 1
    assert len(base64.b64decode(lines[0], validate=True)) == 32
    assert err == ""


def test_main_reports_a_refusal_on_stderr_and_returns_1(mk, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert mk.main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "LOCALAPPDATA is not set" in err
