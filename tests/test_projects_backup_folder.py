"""Reading the backups folder: one unreadable entry is skipped, and the rest of the folder is still listed and pruned.

The stat call is made to raise for one entry, so the listing is tested where it can fail. Each test uses its own
LOCALAPPDATA folder, so no backup is left in the session's folder for another test to read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meridian_desktop import projects_backup

TAKEN = datetime(2026, 10, 24, 23, 54, 24, tzinfo=timezone.utc)
HOSTED_ROWS = [
    {"id": "h-1", "name": "Hosted one", "created_at": "2026-03-02T17:30:00"},
    {"id": "h-2", "name": "Hosted two", "created_at": "2026-04-01T09:00:00"},
]
FULL_GLOB = "projects-*.json"


def test_one_unreadable_entry_is_skipped_and_the_rest_of_the_folder_is_still_listed(tmp_path, monkeypatch):
    folder = tmp_path / "b"
    for day in range(3):
        projects_backup.write_backup(HOSTED_ROWS, folder=folder, taken_at=TAKEN + timedelta(days=day))
    names = sorted(path.name for path in folder.glob(FULL_GLOB))
    unreadable = names[0]
    real_is_file = type(folder).is_file

    def stat_refused(self):
        if self.name == unreadable:
            raise PermissionError("stat refused for this one entry")
        return real_is_file(self)

    monkeypatch.setattr(type(folder), "is_file", stat_refused)

    listed = projects_backup._backup_files(folder, projects_backup._NAME)

    assert sorted(path.name for path in listed) == names[1:]
