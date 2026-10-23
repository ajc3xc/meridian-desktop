"""Create the Meridian release signing key once, and print its public key.

The private seed is 32 random bytes, stored as base64 text at

    %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key

outside the repository. It is created only when that file is absent. An existing seed is never replaced, so a
second run prints the same public key again. Only the public key is printed, on one line, and the seed is never
printed, logged or written anywhere else. Copy the printed key into meridian_desktop/release_key.py.

Run from the repository root:  python packaging/make_release_key.py
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KEY_DIR_NAME = "meridian-release-keys"
KEY_FILE_NAME = "release-ed25519.key"
SEED_BYTES = 32


class ReleaseKeyError(Exception):
    """The release key cannot be read or created. The message names the step and never holds the seed."""


def key_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the private seed lives. Only %LOCALAPPDATA% is used, so the seed is never inside the repository."""
    env = os.environ if env is None else env
    local_app_data = env.get("LOCALAPPDATA")
    if not local_app_data:
        raise ReleaseKeyError("LOCALAPPDATA is not set, so there is no place to keep the release key")
    return Path(local_app_data) / KEY_DIR_NAME / KEY_FILE_NAME


def _read_seed(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="ascii").strip()
        seed = base64.b64decode(text, validate=True)
    except (OSError, UnicodeDecodeError, binascii.Error, ValueError):
        raise ReleaseKeyError(f"the release key file at {path} cannot be read; it is not replaced") from None
    if len(seed) != SEED_BYTES:
        raise ReleaseKeyError(f"the release key file at {path} does not hold a 32-byte seed; it is not replaced")
    return seed


def _create_seed(path: Path) -> bytes:
    """Write a new random seed with exclusive create, so an existing file is never overwritten."""
    seed = os.urandom(SEED_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = base64.b64encode(seed).decode("ascii") + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_seed(path)  # created by another run between the check and this write
    except OSError:
        raise ReleaseKeyError(f"the release key file at {path} could not be created") from None
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(text)
    return seed


def load_or_create_seed(path: Path) -> bytes:
    """Return the seed at path, creating it first when the file is absent."""
    if path.exists():
        return _read_seed(path)
    return _create_seed(path)


def public_key_text(env: Mapping[str, str] | None = None) -> str:
    """The base64 public key for the release key, creating the seed first if it is absent."""
    from meridian_desktop import ed25519

    seed = load_or_create_seed(key_path(env))
    return base64.b64encode(ed25519.public_key(seed)).decode("ascii")


def main() -> int:
    try:
        print(public_key_text())
    except ReleaseKeyError as exc:
        print(f"make_release_key: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
