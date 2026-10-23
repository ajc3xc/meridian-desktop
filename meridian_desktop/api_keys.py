"""Named API keys for the desktop. Pure Python: no Qt import.

A key is one entry in the OS keychain under the desktop's service name, with one account per key name. RunPod keeps
the account the Meridian core already reads (``runpod-api-key``), so the RunPod entry and the core share one key. Any
other name gets the account ``key-<slug>``, where the slug is the name in lower case with each run of other characters
turned into one dash.

The keychain cannot be listed, so the saved custom names are kept in settings.json as ``key_names``. A name is not a
secret. A value is never written there. Nothing here returns a value: ``status`` tests only whether a value is set, and
a failed read reads as unreadable. Errors name the step and the class, never the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from meridian_desktop.secret_store import MAX_SECRET_LENGTH, RUNPOD_ACCOUNT, RUNPOD_SERVICE, SecretStore
from meridian_desktop.settings_store import SettingsStore

KEY_SERVICE = RUNPOD_SERVICE
RUNPOD_NAME = "RunPod"
BUILT_IN_NAMES: tuple[str, ...] = (RUNPOD_NAME,)
MAX_NAME_LENGTH = 40
MAX_CUSTOM_NAMES = 50
CUSTOM_ACCOUNT_PREFIX = "key-"

STATUS_SET = "set"
STATUS_NOT_SET = "not_set"
STATUS_UNREADABLE = "unreadable"

_NAME_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class KeyNameError(ValueError):
    """A key name is refused. The text says why, and it never repeats a value."""


@dataclass(frozen=True)
class KeyEntry:
    """One named key as the Keys section lists it: the name, whether it is built in, and its status. Never its value."""

    name: str
    status: str
    built_in: bool


def clean_name(text: object) -> str:
    """Collapse the spaces in a typed name and check it. Raise KeyNameError with plain words when it is refused."""
    if not isinstance(text, str):
        raise KeyNameError("The key name must be text.")
    collapsed = " ".join(text.split())
    if not collapsed:
        raise KeyNameError("Type a name for the key first, for example OpenAI.")
    if len(collapsed) > MAX_NAME_LENGTH:
        raise KeyNameError(f"The key name is longer than {MAX_NAME_LENGTH} characters.")
    if not _NAME_SHAPE.fullmatch(collapsed):
        raise KeyNameError("The key name may use letters, digits, spaces, dots, dashes and underscores.")
    return collapsed


def slug(name: str) -> str:
    """The name in lower case, with each run of other characters as one dash. Two names with one slug are one key."""
    return _NOT_SLUG.sub("-", name.lower()).strip("-")


def account_for(name: str) -> str:
    """The keychain account for a key name. RunPod keeps the account the Meridian core reads."""
    key = slug(clean_name(name))
    if key == slug(RUNPOD_NAME):
        return RUNPOD_ACCOUNT
    return f"{CUSTOM_ACCOUNT_PREFIX}{key}"


def _clean_value(value: object) -> str:
    """Strip the value and check it. The messages hold no part of the value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Type the key value first.")
    cleaned = value.strip()
    if len(cleaned) > MAX_SECRET_LENGTH:
        raise ValueError(f"The key value is longer than {MAX_SECRET_LENGTH} characters.")
    return cleaned


class ApiKeys:
    """The named keys: list them, read whether each one is set, save a value, replace it, or remove it."""

    def __init__(self, settings: SettingsStore, secrets: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets

    def names(self) -> list[str]:
        """The built-in names, then the saved custom names in the order they were added."""
        return [*BUILT_IN_NAMES, *self._custom_names()]

    def entries(self) -> list[KeyEntry]:
        """Every listed name with its status. No value is in the result."""
        return [KeyEntry(name, self.status(name), name in BUILT_IN_NAMES) for name in self.names()]

    def status(self, name: str) -> str:
        """Whether a value is set for this name. The value is read only to test it, and it is dropped here."""
        try:
            value = self._secrets.get_secret(KEY_SERVICE, account_for(name))
        except Exception:  # noqa: BLE001 - a failed read is shown as unreadable, and its text is never used
            return STATUS_UNREADABLE
        return STATUS_SET if value else STATUS_NOT_SET

    def save(self, name: str, value: str) -> str:
        """Save a value under a name. A new name is added to the list. Return the name as it is listed.

        An existing name (the same slug) keeps its listing and gets the new value. A new name writes the value first,
        then the list. When the list cannot be written, the value is removed again, so no orphan value is left.
        """
        new_name = clean_name(name)
        cleaned_value = _clean_value(value)
        listed = self._listed(new_name)
        if listed is not None:
            self._secrets.set_secret(KEY_SERVICE, account_for(listed), cleaned_value)
            return listed
        custom = self._custom_names()
        if len(custom) >= MAX_CUSTOM_NAMES:
            raise KeyNameError(f"The list holds at most {MAX_CUSTOM_NAMES} custom names. Remove one first.")
        self._secrets.set_secret(KEY_SERVICE, account_for(new_name), cleaned_value)
        try:
            self._write_custom([*custom, new_name])
        except (OSError, ValueError):
            self._drop_value(new_name)
            raise
        return new_name

    def remove(self, name: str) -> bool:
        """Remove the saved value. A custom name also leaves the list. Return True when a value was removed.

        A built-in name (RunPod) stays listed, so its card is always there. Raise KeyNameError when the name is not listed.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        removed = bool(self._secrets.delete_secret(KEY_SERVICE, account_for(listed)))
        if listed not in BUILT_IN_NAMES:
            self._write_custom([kept for kept in self._custom_names() if slug(kept) != slug(listed)])
        return removed

    def _listed(self, name: str) -> str | None:
        """The listed name with the same slug as ``name``, or None when there is none."""
        key = slug(name)
        for listed in self.names():
            if slug(listed) == key:
                return listed
        return None

    def _custom_names(self) -> list[str]:
        """The saved custom names, cleaned. A malformed, built-in or repeated name is left out."""
        seen = {slug(name) for name in BUILT_IN_NAMES}
        names: list[str] = []
        for raw in self._settings.load().settings.key_names:
            try:
                cleaned = clean_name(raw)
            except KeyNameError:
                continue
            if slug(cleaned) in seen:
                continue
            seen.add(slug(cleaned))
            names.append(cleaned)
        return names

    def _write_custom(self, names: list[str]) -> None:
        """Save the custom names. Every other field in the settings file is kept. Raises OSError when it cannot write."""
        current = self._settings.load().settings
        self._settings.save(replace(current, key_names=tuple(names)))

    def _drop_value(self, name: str) -> None:
        """Remove a value after a failed save. A failure here is not reported, because the save error is the one to show."""
        try:
            self._secrets.delete_secret(KEY_SERVICE, account_for(name))
        except Exception:  # noqa: BLE001 - nothing here may carry a value or a second error into the UI
            return


__all__ = [
    "BUILT_IN_NAMES",
    "KEY_SERVICE",
    "MAX_CUSTOM_NAMES",
    "MAX_NAME_LENGTH",
    "RUNPOD_NAME",
    "STATUS_NOT_SET",
    "STATUS_SET",
    "STATUS_UNREADABLE",
    "ApiKeys",
    "KeyEntry",
    "KeyNameError",
    "account_for",
    "clean_name",
    "slug",
]
