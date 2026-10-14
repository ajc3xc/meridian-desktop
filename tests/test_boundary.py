"""Tests for tools/check_boundary.py: a violation tree fails, a clean tree passes,
and the real repo is checked. The real-repo test is expected to fail until
PLAN.md stops naming the meridian-latex path (see the wave 1 report)."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("check_boundary", REPO / "tools" / "check_boundary.py")
check_boundary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_boundary)

LATEX_REF = check_boundary.FORBIDDEN_REFS[0]
LATEX_REF_ESCAPED = check_boundary.FORBIDDEN_REFS[2]  # doubled backslash, as in a Python string


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clean_tree(root: Path) -> None:
    _write(root, "meridian_desktop/__init__.py", '__version__ = "0.1.0"\n')
    _write(root, "meridian_desktop/settings_store.py", "import json\n")
    _write(root, "meridian_desktop/ui/__init__.py", "")
    _write(root, "meridian_desktop/ui/main_window.py", "from PySide6.QtWidgets import QLabel\n")
    _write(root, "meridian_desktop/tray.py", "from PySide6.QtGui import QIcon\n")
    _write(root, "README.md", "# clean\n")


def test_clean_tree_passes(tmp_path):
    _clean_tree(tmp_path)
    assert check_boundary.check(tmp_path) == []


def test_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "import PySide6.QtCore\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "logic.py" in problems[0]
    assert "PySide6.QtCore" in problems[0]


def test_from_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "from PySide6 import QtWidgets\n")
    problems = check_boundary.check(tmp_path)
    assert any("logic.py" in p and "PySide6" in p for p in problems)


def test_nested_pyqt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/services/status.py", "from PyQt5.QtCore import QTimer\n")
    problems = check_boundary.check(tmp_path)
    assert any("status.py" in p for p in problems)


def test_forbidden_reference_in_doc_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert any("notes.md" in p for p in problems)


def test_forbidden_reference_in_python_string_fails(tmp_path):
    _clean_tree(tmp_path)
    # Valid Python: the file holds the escaped form, the value has one backslash.
    _write(tmp_path, "meridian_desktop/paths.py", f'ENGINE = "{LATEX_REF_ESCAPED}"\n')
    problems = check_boundary.check(tmp_path)
    assert any("paths.py" in p for p in problems)


def test_forbidden_single_backslash_reference_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/paths.md", f"path: {check_boundary.FORBIDDEN_REFS[1]}\n")
    problems = check_boundary.check(tmp_path)
    assert any("paths.md" in p for p in problems)


def test_missing_package_fails_closed(tmp_path):
    problems = check_boundary.check(tmp_path)
    assert problems
    assert "fail closed" in problems[0]


def test_violation_tree_exits_1_and_lists_each_violation(tmp_path, capsys):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/a.py", "import PySide6\n")
    _write(tmp_path, "meridian_desktop/b.py", "from PySide6.QtCore import QObject\n")
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "a.py" in out
    assert "b.py" in out
    assert "boundary ok" not in out


def test_clean_tree_exits_0_with_ok_line(tmp_path, capsys):
    _clean_tree(tmp_path)
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "boundary ok" in out


def test_pixi_env_is_not_scanned(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    assert check_boundary.check(tmp_path) == []


def test_pixi_skip_does_not_hide_same_reference_elsewhere(tmp_path):
    # Control: the skip is limited to the .pixi directory.
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    _write(tmp_path, "envs/default/share/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "envs/default/share/notes.md" in problems[0]


def test_repo_files_prunes_pixi(tmp_path):
    _write(tmp_path, ".pixi/envs/default/x.txt", "x\n")
    _write(tmp_path, "README.md", "x\n")
    rels = [p.relative_to(tmp_path).as_posix() for p in check_boundary._repo_files(tmp_path)]
    assert rels == ["README.md"]


def test_real_repo_passes():
    problems = check_boundary.check(REPO)
    assert problems == [], "\n".join(problems)
