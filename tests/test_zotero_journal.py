"""The Zotero save journal: what it stores, how it survives a reload, how it refuses an unreadable file, and how it writes.

Each test uses its own journal in pytest's temp folder. No test touches the real desktop state folder.
"""

import json
import os
from pathlib import Path

import pytest

from meridian_desktop import zotero_journal
from meridian_desktop.settings_store import default_settings_path
from meridian_desktop.zotero_journal import (
    HELD_OUTCOMES,
    JOURNAL_NAME,
    LANDED,
    LINE_HELD_TEXT,
    LOCK_SUFFIX,
    LOCK_TIMEOUT_TEXT,
    REFUSED,
    UNKNOWN,
    WRITING,
    JournalError,
    SaveJournal,
    _lock_file,
    _unlock_file,
    default_path,
    journal_unreadable_text,
)

LINE = "Paper 1 | https://example.org/1"


def test_a_missing_journal_is_empty_and_is_written_on_the_first_change(tmp_path):
    path = tmp_path / "state" / JOURNAL_NAME
    journal = SaveJournal(path)
    assert journal.held() == []
    assert not path.exists()
    journal.begin(LINE)
    assert path.exists()


def test_a_change_survives_a_reload(tmp_path):
    path = tmp_path / JOURNAL_NAME
    first = SaveJournal(path)
    entry_id = first.begin(LINE)
    first.finish(entry_id, UNKNOWN)
    reloaded = SaveJournal(path)
    held = reloaded.held()
    assert [(entry.id, entry.line, entry.outcome) for entry in held] == [(entry_id, LINE, UNKNOWN)]


def test_a_write_still_in_progress_is_held_after_a_reload(tmp_path):
    """The app quit during the write. The outcome is not known, so the line is held rather than sent again."""
    path = tmp_path / JOURNAL_NAME
    SaveJournal(path).begin(LINE)
    held = SaveJournal(path).held()
    assert [entry.outcome for entry in held] == [WRITING]
    assert WRITING in HELD_OUTCOMES


def test_a_landed_or_refused_line_is_not_held(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    journal.finish(journal.begin(LINE), LANDED)
    journal.finish(journal.begin("Paper 2 | https://example.org/2"), REFUSED)
    assert SaveJournal(path).held() == []


def test_clear_removes_only_the_given_entries(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    first = journal.begin(LINE)
    second = journal.begin("Paper 2 | https://example.org/2")
    journal.finish(first, UNKNOWN)
    journal.finish(second, UNKNOWN)
    journal.clear([first])
    assert [entry.id for entry in SaveJournal(path).held()] == [second]


def test_only_the_line_outcome_id_and_time_are_stored(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    journal.finish(journal.begin(LINE), UNKNOWN)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"version", "entries"}
    for entry in stored["entries"]:
        assert set(entry) == {"id", "line", "outcome", "at"}
    assert "token" not in path.read_text(encoding="utf-8").lower()
    assert "grant" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"entries": "not a list"}',
        '{"entries": [42]}',
        '{"entries": [{"id": "a", "line": "x", "outcome": "bogus", "at": "t"}]}',
        '{"entries": [{"id": "a", "line": "x", "outcome": "unknown"}]}',
    ],
)
def test_an_unreadable_journal_raises_with_a_fixed_message(tmp_path, content):
    path = tmp_path / JOURNAL_NAME
    path.write_text(content, encoding="utf-8")
    with pytest.raises(JournalError) as info:
        SaveJournal(path)
    assert info.value.message == journal_unreadable_text(path)


def test_a_failed_write_leaves_the_previous_journal_whole_and_undoes_the_change(tmp_path, monkeypatch):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    kept = journal.begin(LINE)
    before = path.read_bytes()

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(JournalError):
        journal.begin("Paper 2 | https://example.org/2")
    assert path.read_bytes() == before
    assert [entry.id for entry in SaveJournal(path).held()] == [kept]
    assert [entry.id for entry in journal.held()] == [kept]
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]


def test_old_entries_are_pruned_but_held_entries_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(zotero_journal, "MAX_ENTRIES", 3)
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    held = journal.begin("Held | https://example.org/held")
    journal.finish(held, UNKNOWN)
    for number in range(5):
        journal.finish(journal.begin(f"Landed {number} | https://example.org/{number}"), LANDED)
    stored = SaveJournal(path)
    assert [entry.id for entry in stored.held()] == [held]
    assert len(json.loads(path.read_text(encoding="utf-8"))["entries"]) <= 3


def test_the_default_journal_sits_beside_the_settings_file():
    assert default_path().name == JOURNAL_NAME
    assert default_path().parent == default_settings_path().parent


def _raw_held(path: Path) -> dict[str, str]:
    """The held entries as the file on disk holds them, read without the journal's own code."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {entry["id"]: entry["outcome"] for entry in raw["entries"] if entry["outcome"] in HELD_OUTCOMES}


def test_two_instances_interleaved_never_drop_a_writing_or_unknown_entry(tmp_path):
    path = tmp_path / JOURNAL_NAME
    first = SaveJournal(path)
    second = SaveJournal(path)
    x = first.begin("X | https://example.org/x")
    assert _raw_held(path) == {x: WRITING}
    y = second.begin("Y | https://example.org/y")
    assert _raw_held(path) == {x: WRITING, y: WRITING}
    first.finish(x, UNKNOWN)
    assert _raw_held(path) == {x: UNKNOWN, y: WRITING}
    second.finish(y, LANDED)
    assert _raw_held(path) == {x: UNKNOWN}
    z = first.begin("Z | https://example.org/z")
    assert _raw_held(path) == {x: UNKNOWN, z: WRITING}
    assert second.clear([x]) == 1
    assert _raw_held(path) == {z: WRITING}
    assert first.clear([z]) == 0  # still writing: never cleared
    assert _raw_held(path) == {z: WRITING}
    second.finish(z, UNKNOWN)
    assert _raw_held(path) == {z: UNKNOWN}
    assert first.clear([z]) == 1
    assert _raw_held(path) == {}


def test_a_second_instance_refuses_a_line_the_first_is_writing_even_if_it_opened_first(tmp_path):
    path = tmp_path / JOURNAL_NAME
    first = SaveJournal(path)
    second = SaveJournal(path)  # opened before the first writes anything
    first.begin(LINE)
    with pytest.raises(JournalError) as info:
        second.begin(LINE)
    assert info.value.message == LINE_HELD_TEXT
    assert [entry.outcome for entry in second.held()] == [WRITING]


def test_clear_leaves_a_writing_entry_alone(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    writing = journal.begin(LINE)
    assert journal.clear([writing]) == 0
    assert [entry.id for entry in SaveJournal(path).held()] == [writing]


def test_a_deleted_journal_after_earlier_writes_is_unreadable_and_names_the_held_lines(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    journal.finish(journal.begin(LINE), UNKNOWN)
    path.unlink()
    with pytest.raises(JournalError) as info:
        SaveJournal(path)
    assert info.value.held_lines == (LINE,)
    assert LINE in info.value.message
    assert "Check Zotero before moving or fixing" in info.value.message


def test_a_truncated_journal_is_unreadable_and_names_the_held_lines(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path)
    journal.finish(journal.begin(LINE), UNKNOWN)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(JournalError) as info:
        SaveJournal(path)
    assert info.value.held_lines == (LINE,)
    assert LINE in info.value.message


def test_a_transient_permission_error_on_replace_is_retried(tmp_path, monkeypatch):
    path = tmp_path / JOURNAL_NAME
    real_replace = os.replace
    refusals: list[str] = []

    def flaky(source, target):
        if Path(target) == path and not refusals:
            refusals.append(str(target))
            raise PermissionError("sharing violation")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky)
    journal = SaveJournal(path)
    journal.begin(LINE)
    assert refusals  # the first replace was refused, and the write went through on the retry
    assert [entry.line for entry in SaveJournal(path).held()] == [LINE]


def test_a_lock_held_by_another_process_times_out_with_a_fixed_message(tmp_path):
    path = tmp_path / JOURNAL_NAME
    journal = SaveJournal(path, lock_timeout=0.2)
    holder = open(path.with_name(path.name + LOCK_SUFFIX), "a+b")
    _lock_file(holder)
    try:
        with pytest.raises(JournalError) as info:
            journal.held()
        assert info.value.message == LOCK_TIMEOUT_TEXT
    finally:
        _unlock_file(holder)
        holder.close()
