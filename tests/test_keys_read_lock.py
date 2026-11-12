"""The keys backup file is read-locked for agents: the project settings deny a read of it, and AGENTS.md says never to read it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian_desktop.keys_export import EXPORT_NAME

REPO = Path(__file__).resolve().parents[1]
READ_RULE = f"Read(**/{EXPORT_NAME})"
ABSOLUTE_READ_RULE = f"Read(//**/{EXPORT_NAME})"
# The deny entries the project settings held before this rule. The desktop repo had none, so the list is empty. Any entry
# added later goes here, so this test fails if a later edit replaces the list instead of merging into it.
EXISTING_DENY_ENTRIES: tuple[str, ...] = ()
AGENTS_SENTENCE = f"The keys backup file, {EXPORT_NAME}, must never be read by an agent."


def test_the_read_lock_names_the_one_export_file():
    assert EXPORT_NAME == "meridian-keys-backup.json"


def test_agents_md_says_the_keys_backup_file_is_never_read_by_an_agent():
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")

    assert AGENTS_SENTENCE in text


def test_the_project_settings_deny_reads_of_the_keys_backup_and_keep_the_existing_deny_entries():
    path = REPO / ".claude" / "settings.json"
    if not path.exists():
        # .claude/ is in .gitignore as local Claude Code state, so a clean checkout has no project settings file to check.
        pytest.skip("no local .claude/settings.json in this checkout; the rule lives in the owner's local settings")
    deny = json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]

    assert isinstance(deny, list)
    assert READ_RULE in deny
    assert ABSOLUTE_READ_RULE in deny
    for entry in EXISTING_DENY_ENTRIES:
        assert entry in deny
